import json
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.permissions import role_required
from apps.core.services import BusinessError, next_doc_no
from apps.core.utils import business_action, excel_response, paginate
from apps.inventory.services import stock_summary
from apps.masterdata.models import Customer, Driver, PriceListLine, Product, Site, Vehicle

from . import services as svc
from .models import (Allocation, DeliveryStop, DeliveryTrip, GoodsIssue, Picking, SalesOrder, SalesOrderLine,
                     SalesReturn)

SALES = ("DIRECTOR", "DISPATCHER", "ACCOUNTANT")
WAREHOUSE_OPS = ("WAREHOUSE", "ADMIN")  # điều phối chỉ phát lệnh soạn; soạn & xuất kho do Thủ kho xác nhận


def _dec(v, default="0"):
    try:
        return Decimal(str(v).replace(",", "") or default)
    except Exception:
        return Decimal(default)


def save_so_lines(so, products, qtys, prices, user_can_price=True):
    so.lines.all().delete()
    for pid, q, pr in zip(products, qtys, prices):
        if pid and _dec(q) > 0:
            product = Product.objects.get(pk=pid)
            price = _dec(pr) if (user_can_price and pr not in (None, "")) else svc.default_price(so.customer, product)
            if so.order_type == "INTERNAL":
                price = svc.default_price(so.customer, product)  # nội bộ: luôn giá vốn
            SalesOrderLine.objects.create(so=so, product=product, qty=_dec(q), unit_price=price)
    if not so.lines.exists():
        raise BusinessError("Đơn cần ít nhất 1 dòng hàng.")
    so.recalc_total()
    so.save()


def _atp_msg(so):
    """Thông báo sau khi chạy ATP: chỉ báo phần thiếu = SL đặt − lượng đã giữ."""
    short = [f"{l.product.code} giữ {l.reserved_qty.normalize():f}, thiếu {l.backorder_qty.normalize():f}"
             for l in so.lines.select_related("product") if l.backorder_qty > 0]
    return "Đã xác nhận đơn & giữ hàng." + (" Thiếu hàng: " + "; ".join(short) + "." if short else "")


# ------------------------------------------------------------------ SO (MH-31)
@role_required(*SALES)
def so_list(request):
    qs = SalesOrder.objects.select_related("customer", "site").prefetch_related("lines")
    st = request.GET.get("status")
    if st:
        qs = qs.filter(status=st)
    if request.GET.get("stock") == "short":
        qs = qs.filter(lines__atp_status__in=["SHORT", "BACKORDER"], status__in=["CONFIRMED", "PARTIAL"]).distinct()
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(code__icontains=q) | Q(customer__name__icontains=q) | Q(customer__code__icontains=q)
                       | Q(site__name__icontains=q))
    if request.GET.get("export") == "xlsx":
        stock_lbl = {"OK": "Đủ", "SHORT": "Thiếu", "BACKORDER": "Backorder"}
        head = ["Mã SO", "Ngày đặt", "Ngày cần giao", "Khách hàng", "Công trình", "Loại", "Nguồn", "Mã SP", "Tên SP",
                "ĐVT", "SL đặt", "Đã giữ", "Thiếu", "Đã giao", "Đơn giá", "Thành tiền", "Tình trạng hàng",
                "Trạng thái đơn"]
        rows = []
        for so in qs.prefetch_related("lines__product__base_uom")[:5000]:
            for l in so.lines.all():
                short = l.backorder_qty if so.status not in ("DRAFT", "PENDING_APPROVAL", "CANCELLED") else None
                rows.append([so.code, so.order_date, so.requested_date, so.customer.name, so.site.name,
                             so.get_order_type_display(), so.get_source_display(), l.product.code, l.product.name,
                             l.product.base_uom.code, l.qty, l.reserved_qty, short, l.delivered_qty, l.unit_price,
                             l.amount, stock_lbl.get(l.atp_status, ""), so.get_status_display()])
        return excel_response(f"don_ban_{timezone.localdate():%Y%m%d}", head, rows, "Đơn bán")
    counts = dict(SalesOrder.objects.values_list("status").annotate(n=Count("id")))
    page, qs_str = paginate(request, qs)
    return render(request, "sales/so_list.html", {"title": "Đơn bán hàng (SO)", "page_obj": page, "qs": qs_str,
                                                  "counts": counts, "statuses": SalesOrder.STATUS,
                                                  "total": sum(counts.values())})


def _price_map():
    out = {}
    for c in Customer.objects.filter(is_active=True).select_related("price_list"):
        if c.customer_type == "INTERNAL":
            out[str(c.pk)] = {str(p.pk): {"price": float(p.avg_cost or p.standard_cost), "floor": 0}
                              for p in Product.objects.filter(is_active=True)}
        elif c.price_list_id:
            out[str(c.pk)] = {str(l.product_id): {"price": float(l.price), "floor": float(l.floor_price)}
                              for l in PriceListLine.objects.filter(price_list_id=c.price_list_id)}
    return out


@role_required("DIRECTOR", "DISPATCHER")
@business_action
def so_form(request, pk=None):
    so = get_object_or_404(SalesOrder, pk=pk) if pk else None
    if so and so.status != "DRAFT":
        raise BusinessError("Chỉ sửa được đơn nháp.")
    if request.method == "POST":
        with transaction.atomic():
            cust = get_object_or_404(Customer, pk=request.POST["customer"])
            site = get_object_or_404(Site, pk=request.POST["site"], customer=cust)
            if not so:
                so = SalesOrder(code=next_doc_no("SO"), created_by=request.user)
            so.customer, so.site = cust, site
            so.order_type = "INTERNAL" if cust.customer_type == "INTERNAL" else "B2B"
            so.order_date = timezone.localdate()
            so.requested_date = request.POST.get("requested_date") or timezone.localdate()
            so.note = request.POST.get("note", "")
            is_new = so.pk is None
            so.save()
            save_so_lines(so, request.POST.getlist("product"), request.POST.getlist("qty"), request.POST.getlist("price"))
            svc.so_log(so, "CREATED" if is_new else "EDITED", request.user,
                       {"lines": [{"product": l.product.code, "qty": float(l.qty)} for l in so.lines.select_related("product")]})
        if request.POST.get("submit"):
            ok = svc.submit_so(so, request.user)
            messages.success(request, _atp_msg(so) if ok else "Đơn cần Director duyệt: " + "; ".join(so.approval_reasons))
        else:
            messages.success(request, f"Đã lưu nháp {so.code}.")
        return redirect(f"/app/sales/orders/{so.pk}")
    lines = [{"product": str(l.product_id), "qty": float(l.qty), "price": float(l.unit_price)} for l in so.lines.all()] if so else []
    sites = {str(c.pk): [{"id": s.pk, "name": s.name} for s in c.sites.filter(is_active=True)]
             for c in Customer.objects.filter(is_active=True).prefetch_related("sites")}
    summ = stock_summary()
    avail = {str(pid): float(s["available"]) for pid, s in summ.items()}
    return render(request, "sales/so_form.html", {
        "title": f"Sửa {so.code}" if so else "Tạo đơn bán", "so": so, "lines_json": lines,
        "customers": Customer.objects.filter(is_active=True),
        "products": Product.objects.filter(is_active=True).select_related("base_uom"),
        "prices": _price_map(), "sites": sites, "avail": avail})


@role_required(*SALES)
@business_action
def so_detail(request, pk):
    so = get_object_or_404(SalesOrder.objects.select_related("customer", "site"), pk=pk)
    if request.method == "POST":
        op = request.POST.get("op")
        if request.user.role == "ACCOUNTANT":
            raise BusinessError("Kế toán chỉ được xem đơn bán.")
        if op == "submit":
            ok = svc.submit_so(so, request.user)
            messages.success(request, _atp_msg(so) if ok else "Đơn chuyển Director duyệt.")
        elif op in ("approve", "reject"):
            if request.user.role not in ("DIRECTOR", "ADMIN"):
                raise BusinessError("Chỉ Director duyệt đơn.")
            svc.approve_so(so, request.user, op == "approve", request.POST.get("reason", ""))
            messages.success(request, "Đã duyệt." if op == "approve" else "Đã từ chối.")
        elif op == "reserve":
            for l in so.lines.filter(atp_status__in=["SHORT", "BACKORDER"]):
                svc.reserve_line(l)
            messages.info(request, "Đã chạy lại ATP.")
        elif op == "cancel":
            svc.cancel_so(so, request.POST.get("reason"), request.user)
            messages.success(request, "Đã hủy đơn, nhả hàng giữ.")
        elif op == "delete" and so.status == "DRAFT":
            so.delete()
            return redirect("/app/sales/orders")
        return redirect(request.path)
    from apps.sales.services import customer_debt
    debt, overdue = customer_debt(so.customer)
    lines = list(so.lines.select_related("product__base_uom"))
    preview = so.status in ("DRAFT", "PENDING_APPROVAL")
    if preview:
        svc.atp_preview(lines)  # đơn chưa xác nhận: tính trước Thiếu = SL đặt − Khả dụng
    else:
        for l in lines:
            l.short_qty = l.backorder_qty
    from apps.inventory.replenishment import event_label
    from apps.purchasing.models import PurchaseRequisition
    history = list(so.history.select_related("user"))
    trig_prs = list(PurchaseRequisition.objects.filter(trigger_ref_type="SO", trigger_ref_code=so.code)
                    .prefetch_related("lines__product__base_uom"))
    for pr in trig_prs:
        pr.event_label = event_label(pr.trigger_event)
    return render(request, "sales/so_detail.html", {
        "title": so.code, "so": so, "lines": lines, "preview": preview, "history": history, "trig_prs": trig_prs,
        "stops": so.stops.select_related("trip__vehicle").prefetch_related("lines"), "debt": debt, "overdue": overdue,
        "allocs": Allocation.objects.filter(lines__so_line__so=so).distinct()})


# ------------------------------------------------------------------ Phân bổ thiếu hàng
@role_required("DIRECTOR", "DISPATCHER")
def alloc_list(request):
    qs = Allocation.objects.select_related("product").annotate(n=Count("lines")).order_by("-id")
    page, qs_str = paginate(request, qs)
    return render(request, "sales/alloc_list.html", {"title": "Phân bổ khi thiếu hàng", "page_obj": page, "qs": qs_str})


@role_required("DIRECTOR", "DISPATCHER")
@business_action
def alloc_detail(request, pk):
    al = get_object_or_404(Allocation.objects.select_related("product__base_uom"), pk=pk)
    if request.method == "POST":
        op = request.POST.get("op")
        if op == "rerun":
            if al.status != "PROPOSED":
                raise BusinessError("Phân bổ đã được xử lý.")
            new = svc.propose_allocation(al.product, request.POST.get("rule", al.priority_rule))
            if new:
                messages.success(request, f"Đã sắp xếp & phân bổ lại theo quy tắc: {new.get_priority_rule_display()}.")
            return redirect(f"/app/sales/allocations/{new.pk}" if new else "/app/sales/allocations")
        if op == "set_penalty":
            if al.status != "PROPOSED":
                raise BusinessError("Phân bổ đã được xử lý.")
            line = get_object_or_404(al.lines.select_related("so_line__so__site"), pk=request.POST.get("line"))
            site = line.so_line.so.site
            site.penalty_date = request.POST.get("penalty_date") or None
            site.save(update_fields=["penalty_date"])
            new = svc.propose_allocation(al.product, al.priority_rule)
            messages.success(request, f"Đã cập nhật mốc phạt công trình {site.name} và phân bổ lại.")
            return redirect(f"/app/sales/allocations/{new.pk}" if new else "/app/sales/allocations")
        if request.user.role not in ("DIRECTOR", "ADMIN"):
            raise BusinessError("Chỉ Director duyệt phân bổ.")
        if op == "approve":
            svc.approve_allocation(al, request.user, {l.pk: request.POST.get(f"a_{l.pk}") for l in al.lines.all()})
            messages.success(request, "Đã duyệt phân bổ. Phần thiếu chuyển backorder.")
        return redirect(request.path)
    lines = list(al.lines.select_related("so_line__so__customer", "so_line__so__site"))
    for l in lines:
        l.penalty = svc.penalty_date(l.so_line.so)
        l.penalty_from_so = not l.so_line.so.site.penalty_date
    return render(request, "sales/alloc_detail.html", {"title": al.code, "al": al, "lines": lines, "rules": Allocation.RULES})


# ------------------------------------------------------------------ Kế hoạch giao (MH-34)
@role_required("DIRECTOR", "DISPATCHER")
@business_action
def planning(request):
    if request.method == "POST":
        if request.content_type == "application/json":
            data = json.loads(request.body)
            op = data.get("op")
            if op == "add":
                svc.add_order_to_trip(get_object_or_404(DeliveryTrip, pk=data["trip"]), get_object_or_404(SalesOrder, pk=data["so"]))
            elif op == "remove":
                svc.remove_stop(get_object_or_404(DeliveryStop, pk=data["stop"]))
            elif op == "reorder":
                for i, sid in enumerate(data["stops"], 1):
                    DeliveryStop.objects.filter(pk=sid, trip_id=data["trip"]).update(seq=i)
            return JsonResponse({"ok": True})
        op = request.POST.get("op")
        if op == "new_trip":
            t = svc.create_trip(get_object_or_404(Vehicle, pk=request.POST["vehicle"]),
                                get_object_or_404(Driver, pk=request.POST["driver"]), request.POST["planned_date"])
            messages.success(request, f"Đã tạo chuyến {t.code}.")
        elif op == "release":
            pk_ = svc.release_picking(get_object_or_404(DeliveryTrip, pk=request.POST["trip"]), request.user)
            messages.success(request, f"Đã phát lệnh soạn hàng {pk_.code} (theo vị trí FIFO).")
        elif op == "cancel_trip":
            t = get_object_or_404(DeliveryTrip, pk=request.POST["trip"], status="PLANNED")
            for s in list(t.stops.all()):
                svc.remove_stop(s)
            t.status, t.cancel_reason = "CANCELLED", "Hủy khi lập kế hoạch"
            t.save()
        return redirect(request.get_full_path())
    date = request.GET.get("date") or timezone.localdate().isoformat()
    orders = svc.orders_waiting_delivery()
    for o in orders:
        ls = [l for l in o.lines.select_related("product") if l.qty_to_plan > 0]
        o.plan_ton = sum((l.qty_to_plan * l.product.unit_weight_kg for l in ls), Decimal(0)) / 1000
        o.plan_lines = ls
    trips = DeliveryTrip.objects.filter(planned_date=date).exclude(status="CANCELLED") \
        .select_related("vehicle", "driver").prefetch_related("stops__so__customer", "stops__site", "stops__lines__so_line__product")
    return render(request, "sales/planning.html", {
        "title": "Kế hoạch giao hàng", "orders": orders, "trips": trips, "date": date,
        "today_date": timezone.localdate(),
        "vehicles": Vehicle.objects.filter(is_active=True, carrier__isnull=True),
        "drivers": svc.driver_availability(Driver.objects.filter(is_active=True, carrier__isnull=True), _parse_day(date))})


def _parse_day(s):
    from datetime import date as _date
    try:
        return _date.fromisoformat(str(s))
    except ValueError:
        return timezone.localdate()


# ------------------------------------------------------------------ Soạn hàng & xuất kho (MH-35)
@role_required("DIRECTOR", "DISPATCHER", "WAREHOUSE")
def picking_list(request):
    trips = DeliveryTrip.objects.filter(status__in=["PICKING", "READY", "IN_TRANSIT", "COMPLETED"]) \
        .select_related("vehicle", "driver", "picking", "goods_issue").order_by("-planned_date", "-id")
    page, qs_str = paginate(request, trips)
    return render(request, "sales/picking_list.html", {"title": "Soạn hàng & xuất kho", "page_obj": page, "qs": qs_str})


@role_required("DIRECTOR", "DISPATCHER", "WAREHOUSE")
@business_action
def picking_detail(request, pk):
    pk_obj = get_object_or_404(Picking.objects.select_related("trip__vehicle", "trip__driver"), pk=pk)
    trip = pk_obj.trip
    gi = GoodsIssue.objects.filter(trip=trip).first()
    if request.method == "POST":
        op = request.POST.get("op")
        if request.user.role not in WAREHOUSE_OPS:
            raise BusinessError("Chỉ Thủ kho được xác nhận soạn hàng và xuất kho.")
        if op == "start" and pk_obj.status == "DRAFT":
            pk_obj.status, pk_obj.picker = "IN_PROGRESS", request.user
            pk_obj.save()
        elif op == "confirm":
            gi = svc.confirm_picking(pk_obj, request.user, {l.pk: request.POST.get(f"p_{l.pk}") for l in pk_obj.lines.all()})
            messages.success(request, f"Đã xác nhận soạn. Phiếu xuất {gi.code} chờ xác nhận xuất kho.")
        elif op == "post_gi" and gi:
            svc.post_goods_issue(gi, request.user)
            messages.success(request, f"Đã xuất kho {gi.code}. Chuyến chuyển sang Đang giao.")
        return redirect(request.path)
    lines = pk_obj.lines.select_related("product__base_uom", "location", "so_line__so")
    return render(request, "sales/picking_detail.html", {
        "title": pk_obj.code, "pk": pk_obj, "trip": trip, "lines": lines, "gi": gi,
        "gi_lines": gi.lines.select_related("product", "location") if gi else [],
        "stops": trip.stops.select_related("so__customer", "site"), "can_operate": request.user.role in WAREHOUSE_OPS})


# ------------------------------------------------------------------ Trả hàng (RT)
@role_required("DIRECTOR", "DISPATCHER", "WAREHOUSE")
@business_action
def returns(request):
    if request.method == "POST":
        ret = get_object_or_404(SalesReturn, pk=request.POST["ret"])
        if request.POST.get("op") == "post":
            svc.post_sales_return(ret, request.user)
            messages.success(request, f"Đã nhập lại kho {ret.code}.")
        return redirect("/app/fulfillment/returns")
    qs = SalesReturn.objects.select_related("so__customer", "stop__trip").prefetch_related("lines__product", "lines__location")
    page, qs_str = paginate(request, qs)
    return render(request, "sales/returns.html", {"title": "Trả hàng (RT)", "page_obj": page, "qs": qs_str})
