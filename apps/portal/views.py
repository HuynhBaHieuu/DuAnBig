"""/portal/ – cổng khách hàng. Không hiển thị tồn kho, giá vốn."""
from datetime import datetime
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.accounting.models import Invoice
from apps.core.services import BusinessError, next_doc_no, raise_alert, resolve_alerts
from apps.core.utils import business_action, paginate, pdf_response
from apps.masterdata.models import PriceListLine, Product, Site
from apps.sales.models import DeliveryStop, SalesOrder
from apps.sales.services import customer_debt
from apps.sales.views import save_so_lines


def _ctx(request):
    u = request.user
    if u.role != "CUSTOMER" or not u.customer_id:
        raise PermissionDenied("Tài khoản chưa gắn với khách hàng.")
    sites = Site.objects.filter(customer=u.customer, is_active=True)
    granted = sites.filter(user_access__user=u)
    return u.customer, (granted if granted.exists() else sites)


@login_required
def home(request):
    cust, sites = _ctx(request)
    orders = SalesOrder.objects.filter(customer=cust, site__in=sites)
    open_orders = orders.exclude(status__in=["DELIVERED", "CLOSED", "CANCELLED"])
    upcoming = DeliveryStop.objects.filter(so__in=orders, status="PENDING").select_related("trip__vehicle", "so", "site") \
        .order_by("trip__planned_date")[:5]
    debt, overdue = customer_debt(cust)
    return render(request, "portal/home.html", {
        "title": "Tổng quan", "cust": cust, "sites": sites, "open_count": open_orders.count(),
        "recent": orders.select_related("site").prefetch_related("stops__trip")[:6], "upcoming": upcoming, "debt": debt, "overdue": overdue,
        "delivered_month": orders.filter(status="DELIVERED", updated_at__date__gte=timezone.localdate().replace(day=1)).count()})


def _parse_date(raw, today):
    """Ngày cần giao: bắt buộc, không được trước hôm nay (STT 55 – kiểm tra cả ở máy chủ)."""
    try:
        d = datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise BusinessError("Ngày cần giao không hợp lệ.")
    if d < today:
        raise BusinessError(f"Ngày cần giao {d:%d/%m/%Y} đã qua – chọn từ hôm nay ({today:%d/%m/%Y}) trở đi.")
    return d


def _products(cust):
    if cust.price_list_id:
        prices = {l.product_id: l.price for l in PriceListLine.objects.filter(price_list_id=cust.price_list_id)}
        products = [p for p in Product.objects.filter(is_active=True, pk__in=prices).select_related("base_uom")]
        for p in products:
            p.sell_price = prices[p.pk]
        return products
    return list(Product.objects.filter(is_active=True).select_related("base_uom"))


def _form_rows(request, today):
    """Các dòng của form đặt hàng: [(ngày cần giao, sản phẩm, SL)] – mỗi dòng chọn được ngày giao riêng (STT 69)."""
    rows = []
    products, qtys, dates = request.POST.getlist("product"), request.POST.getlist("qty"), request.POST.getlist("date")
    if len(dates) == 1 and len(products) > 1:  # form sửa đơn: 1 ngày giao chung cho mọi dòng
        dates = dates * len(products)
    for pid, q, d in zip(products, qtys, dates):
        if not pid or not q:
            continue
        try:
            if Decimal(q) <= 0:
                continue
        except Exception:
            raise BusinessError(f"Số lượng '{q}' không hợp lệ.")
        rows.append((_parse_date(d, today), pid, q))
    if not rows:
        raise BusinessError("Chưa nhập vật tư / số lượng.")
    return rows


@login_required
@business_action
def order_new(request):
    """Đặt hàng (STT 69–72): bỏ "Kế hoạch tuần" – một form, mỗi dòng chọn ngày giao (nhiều ngày giao -> mỗi ngày 1
    đơn). Bấm gửi là đơn ở trạng thái "Đã gửi" với khách (không có Nháp phía khách). Tự điền công trình đặt gần nhất
    và ngày giao sớm nhất hợp lệ; ô ngày không cho chọn trước hôm nay."""
    cust, sites = _ctx(request)
    today = timezone.localdate()
    if request.method == "POST":
        site = get_object_or_404(sites, pk=request.POST.get("site"))
        rows = _form_rows(request, today)
        by_date = {}
        for d, pid, q in rows:
            by_date.setdefault(d, []).append((pid, q))
        created = []
        with transaction.atomic():
            for d in sorted(by_date):
                so = SalesOrder.objects.create(
                    code=next_doc_no("SO"), customer=cust, site=site, source="PORTAL",
                    order_type="INTERNAL" if cust.customer_type == "INTERNAL" else "B2B",
                    order_date=today, requested_date=d, note=request.POST.get("note", ""), created_by=request.user)
                pids, qtys = [p for p, _ in by_date[d]], [q for _, q in by_date[d]]
                # khách không tự đặt giá: lấy theo bảng giá của khách
                save_so_lines(so, pids, qtys, [None] * len(pids), user_can_price=False)
                created.append(so)
        from apps.sales.services import auto_submit_portal
        result = {}
        for so in created:  # STT 77: kiểm tra ngay – đơn hợp lệ, đủ hàng tự xác nhận
            result[so.code] = auto_submit_portal(so, request.user)
        ok = [c for c, r in result.items() if r == "AUTO"]
        wait = [c for c, r in result.items() if r != "AUTO"]
        msg = []
        if ok:
            msg.append(f"Đã xác nhận đơn {', '.join(ok)} – hàng đã được giữ.")
        if wait:
            msg.append(f"Đã gửi đơn {', '.join(wait)} – TRACON 80 sẽ kiểm tra và xác nhận sớm.")
        messages.success(request, " ".join(msg))
        return redirect(f"/portal/orders/{created[0].pk}" if len(created) == 1 else "/portal/orders")
    last = SalesOrder.objects.filter(customer=cust, site__in=sites, created_by=request.user).order_by("-id").first() \
        or SalesOrder.objects.filter(customer=cust, site__in=sites).order_by("-id").first()
    return render(request, "portal/order_new.html", {
        "title": "Đặt hàng", "sites": sites, "products": _products(cust), "show_price": cust.customer_type == "B2B",
        "default_site": last.site_id if last else (sites[0].pk if sites else None), "today": today,
        "rows_json": [{"p": "", "q": "", "d": today.isoformat()}]})


@login_required
@business_action
def order_edit(request, pk):
    """STT 57: khách sửa đơn khi TRACON chưa xác nhận (sau khi xác nhận thì khóa)."""
    cust, sites = _ctx(request)
    today = timezone.localdate()
    so = get_object_or_404(SalesOrder, pk=pk, customer=cust, site__in=sites)
    if not so.customer_editable:
        raise BusinessError("Đơn đã được TRACON 80 xác nhận – không sửa được. Vui lòng liên hệ điều phối.")
    if request.method == "POST":
        site = get_object_or_404(sites, pk=request.POST.get("site"))
        rows = _form_rows(request, today)
        dates = {d for d, _, _ in rows}
        if len(dates) > 1:
            raise BusinessError("Khi sửa, mọi dòng của đơn dùng chung một ngày giao. Muốn giao nhiều ngày – đặt đơn mới.")
        with transaction.atomic():
            was_pending = so.status == "PENDING_APPROVAL"
            so.site, so.requested_date, so.note = site, dates.pop(), request.POST.get("note", "")
            so.status = "DRAFT"  # sửa lúc đang chờ Giám đốc duyệt -> về chờ điều phối xác nhận lại
            so.save()
            save_so_lines(so, [p for _, p, _ in rows], [q for _, _, q in rows], [None] * len(rows), user_can_price=False)
            if was_pending:
                resolve_alerts("SO_APPROVAL", "so", so.pk)
            from apps.sales.services import so_log
            so_log(so, "EDITED", request.user, {"by": "Khách hàng (Cổng KH)"})
        from apps.sales.services import auto_submit_portal
        r = auto_submit_portal(so, request.user)  # kiểm tra lại như đơn mới (STT 77)
        messages.success(request, f"Đã cập nhật đơn {so.code}" + (" – đơn đã được xác nhận, hàng đã giữ." if r == "AUTO"
                                                                  else " – TRACON 80 sẽ kiểm tra và xác nhận sớm."))
        return redirect(f"/portal/orders/{so.pk}")
    rows = [{"p": str(l.product_id), "q": str(l.qty.normalize()), "d": so.requested_date.isoformat()}
            for l in so.lines.all()]
    return render(request, "portal/order_new.html", {
        "title": f"Sửa đơn {so.code}", "so": so, "sites": sites, "products": _products(cust),
        "show_price": cust.customer_type == "B2B", "default_site": so.site_id, "today": today, "rows_json": rows,
        "note": so.note})


@login_required
def orders(request):
    cust, sites = _ctx(request)
    qs = SalesOrder.objects.filter(customer=cust, site__in=sites).select_related("site").prefetch_related("stops__trip")
    if request.GET.get("site"):
        qs = qs.filter(site_id=request.GET["site"])
    page, qs_str = paginate(request, qs, 50)  # Gà STT 103: chia trang 50 dòng
    return render(request, "portal/orders.html", {"title": "Theo dõi đơn hàng", "orders": page, "page_obj": page,
                                                  "qs": qs_str, "sites": sites})


def _order_sig(so):
    """Chữ ký trạng thái đơn: đổi khi đơn được xếp chuyến / xe xuất kho / giao xong -> Portal tự tải lại."""
    code, _ = so.delivery_status
    parts = [so.status, code] + [f"{s.pk}:{s.status}:{s.trip.status}" for s in
                                 so.stops.select_related("trip").order_by("pk")]
    return "|".join(parts)


@login_required
def order_status(request, pk):
    """API nhẹ cho Portal hỏi trạng thái mỗi 20 giây (thay cho websocket – chạy được trên Vercel serverless)."""
    from django.http import JsonResponse
    cust, sites = _ctx(request)
    so = get_object_or_404(SalesOrder, pk=pk, customer=cust, site__in=sites)
    return JsonResponse({"sig": _order_sig(so)})


@login_required
@business_action
def order_detail(request, pk):
    cust, sites = _ctx(request)
    so = get_object_or_404(SalesOrder.objects.select_related("site"), pk=pk, customer=cust, site__in=sites)
    if request.method == "POST" and request.POST.get("op") == "cancel":  # STT 57: khách hủy đơn chưa xác nhận
        if not so.customer_editable:
            raise BusinessError("Đơn đã được TRACON 80 xác nhận – không hủy được trên cổng. Vui lòng liên hệ điều phối.")
        from apps.sales.services import cancel_so
        cancel_so(so, f"Khách hủy trên Cổng KH: {request.POST.get('reason', '').strip()}"[:255], request.user)
        resolve_alerts("SO_APPROVAL", "so", so.pk)
        raise_alert("PORTAL_ORDER", f"Khách hủy đơn {so.code}", f"{cust.name} – {so.site.name}", level="INFO",
                    ref_type="so", ref_id=so.pk, url=f"/app/sales/orders/{so.pk}", target_role="DISPATCHER")
        messages.success(request, f"Đã hủy đơn {so.code}.")
        return redirect(request.path)
    stops = so.stops.select_related("trip__vehicle", "trip__driver").prefetch_related("lines__so_line__product")         .exclude(trip__status="CANCELLED")
    code, label, reached = so.customer_status
    steps = [(s, i < reached) for i, s in enumerate(SalesOrder.CUSTOMER_STEPS)]
    return render(request, "portal/order_detail.html", {"title": so.code, "so": so, "stops": stops, "steps": steps,
                                                        "ds_code": code, "ds_label": label, "sig": _order_sig(so),
                                                        "live": so.status not in ("DELIVERED", "CLOSED", "CANCELLED"),
                                                        "now": timezone.now(),
                                                        "lines": so.lines.select_related("product__base_uom"),
                                                        "show_price": cust.customer_type == "B2B"})


@login_required
def documents(request):
    cust, sites = _ctx(request)
    invoices = Invoice.objects.filter(customer=cust).exclude(status__in=["DRAFT", "CANCELLED"])
    pods = DeliveryStop.objects.filter(so__customer=cust, so__site__in=sites).exclude(delivered_at=None) \
        .select_related("so", "site", "trip").order_by("-delivered_at")
    # Gà STT 103: 2 danh sách, mỗi danh sách chia trang riêng (tham số ip / pp)
    inv_page, inv_qs = paginate(request, invoices.order_by("-invoice_date", "-pk"), 20, "ip")
    pod_page, pod_qs = paginate(request, pods, 20, "pp")
    return render(request, "portal/documents.html", {"title": "Chứng từ", "invoices": inv_page, "pods": pod_page,
                                                     "inv_qs": inv_qs, "pod_qs": pod_qs})


@login_required
def invoice_pdf(request, pk):
    cust, _ = _ctx(request)
    inv = get_object_or_404(Invoice, pk=pk, customer=cust)
    if inv.status in ("DRAFT", "CANCELLED"):
        raise PermissionDenied
    return pdf_response(request, "docs/invoice.html", {"inv": inv}, inv.code)


@login_required
def debts(request):
    cust, _ = _ctx(request)
    debt, overdue = customer_debt(cust)
    today = timezone.localdate()
    invs = Invoice.objects.filter(customer=cust, status__in=["POSTED", "PARTIAL"]).order_by("due_date")
    for i in invs:
        i.days_over = (today - i.due_date).days
    return render(request, "portal/debts.html", {"title": "Công nợ", "cust": cust, "debt": debt, "overdue": overdue,
                                                 "invoices": invs, "payments": cust.payments.all()[:20]})
