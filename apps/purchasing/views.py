import json
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.permissions import role_required
from apps.core.services import BusinessError, next_doc_no
from apps.core.utils import business_action, paginate, pdf_response
from apps.masterdata.models import Carrier, Dock, Driver, Location, Product, Route, Supplier, Vehicle, Warehouse

from . import services as svc
from .models import (GoodsReceipt, InboundTrip, PurchaseOrder, PurchaseOrderLine, PurchaseRequisition,
                     PurchaseRequisitionLine, TripIncident, UnloadingSlot)

BUYERS = ("DIRECTOR", "PURCHASER")


def _dec(v, default="0"):
    try:
        return Decimal(str(v).replace(",", "") or default)
    except Exception:
        return Decimal(default)


# ------------------------------------------------------------------ PR
@role_required(*BUYERS)
def pr_list(request):
    qs = PurchaseRequisition.objects.select_related("requested_by").annotate(n=Count("lines")).order_by("-id")
    if request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"])
    page, qs_str = paginate(request, qs)
    return render(request, "purchasing/pr_list.html", {
        "title": "Đề xuất mua (PR)", "page_obj": page, "qs": qs_str, "statuses": PurchaseRequisition.STATUS,
        "warehouses": Warehouse.objects.filter(is_active=True)})


@role_required(*BUYERS)
@business_action
def pr_new(request):
    if request.method == "POST":
        with transaction.atomic():
            pr = PurchaseRequisition.objects.create(code=next_doc_no("PR"), status="SUBMITTED", source="MANUAL",
                                                    need_date=request.POST.get("need_date") or None,
                                                    note=request.POST.get("note", ""), requested_by=request.user)
            n = 0
            for pid, q in zip(request.POST.getlist("product"), request.POST.getlist("qty")):
                if pid and _dec(q) > 0:
                    p = Product.objects.get(pk=pid)
                    PurchaseRequisitionLine.objects.create(pr=pr, product=p, qty=_dec(q), suggested_supplier=p.default_supplier)
                    n += 1
            if not n:
                raise BusinessError("Chưa nhập dòng hàng nào.")
        messages.success(request, f"Đã tạo đề xuất {pr.code}.")
        return redirect("/app/purchase/requisitions")
    return render(request, "purchasing/pr_form.html", {
        "title": "Tạo đề xuất mua", "products": Product.objects.filter(is_active=True).select_related("base_uom")})


@role_required(*BUYERS)
@require_POST
@business_action
def pr_convert(request):
    ids = request.POST.getlist("pr")
    if not ids:
        raise BusinessError("Chọn ít nhất 1 PR.")
    wh = get_object_or_404(Warehouse, pk=request.POST["warehouse"])
    pos = svc.convert_prs_to_po(list(PurchaseRequisition.objects.filter(pk__in=ids)), wh, request.user)
    messages.success(request, "Đã lập " + ", ".join(p.code for p in pos))
    return redirect(f"/app/purchase/orders/{pos[0].pk}" if len(pos) == 1 else "/app/purchase/orders")


@role_required(*BUYERS)
@business_action
def pr_detail(request, pk):
    pr = get_object_or_404(PurchaseRequisition, pk=pk)
    if request.method == "POST" and request.POST.get("op") == "cancel":
        if pr.status == "CONVERTED":
            raise BusinessError("PR đã lập PO.")
        svc.cancel_doc(pr, request.POST.get("reason"))
        return redirect(request.path)
    return render(request, "purchasing/pr_detail.html", {"title": pr.code, "pr": pr,
                                                         "lines": pr.lines.select_related("product__base_uom", "suggested_supplier", "po_line__po")})


# ------------------------------------------------------------------ PO (MH-22)
@role_required("DIRECTOR", "PURCHASER", "ACCOUNTANT")
def po_list(request):
    qs = PurchaseOrder.objects.select_related("supplier").prefetch_related("lines")
    st = request.GET.get("status")
    if st:
        qs = qs.filter(status=st)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(code__icontains=q) | Q(supplier__name__icontains=q) | Q(supplier__code__icontains=q))
    counts = dict(PurchaseOrder.objects.values_list("status").annotate(n=Count("id")))
    page, qs_str = paginate(request, qs)
    return render(request, "purchasing/po_list.html", {
        "title": "Đơn mua hàng (PO)", "page_obj": page, "qs": qs_str, "counts": counts,
        "statuses": PurchaseOrder.STATUS, "total": sum(counts.values())})


def _supplier_prices():
    from apps.masterdata.models import SupplierPrice
    data = {}
    for sp in SupplierPrice.objects.order_by("valid_from"):
        data.setdefault(str(sp.supplier_id), {})[str(sp.product_id)] = float(sp.price)
    return data


@role_required(*BUYERS)
@business_action
def po_form(request, pk=None):
    po = get_object_or_404(PurchaseOrder, pk=pk) if pk else None
    if po and po.status != "DRAFT":
        raise BusinessError("Chỉ sửa được PO nháp.")
    if request.method == "POST":
        with transaction.atomic():
            sup = get_object_or_404(Supplier, pk=request.POST["supplier"])
            if not po:
                po = PurchaseOrder(code=next_doc_no("PO"), created_by=request.user)
            po.supplier = sup
            po.warehouse_id = request.POST["warehouse"]
            po.route_id = request.POST.get("route") or sup.default_route_id
            po.order_date = request.POST.get("order_date") or timezone.localdate()
            po.pickup_date = request.POST.get("pickup_date") or None
            po.note = request.POST.get("note", "")
            po.save()
            po.lines.all().delete()
            for pid, q, pr in zip(request.POST.getlist("product"), request.POST.getlist("qty"), request.POST.getlist("price")):
                if pid and _dec(q) > 0:
                    PurchaseOrderLine.objects.create(po=po, product_id=pid, qty=_dec(q), unit_price=_dec(pr))
            if not po.lines.exists():
                raise BusinessError("PO cần ít nhất 1 dòng hàng.")
            po.recalc_total()
            po.save()
        messages.success(request, f"Đã lưu {po.code}.")
        return redirect(f"/app/purchase/orders/{po.pk}")
    lines = [{"product": str(l.product_id), "qty": float(l.qty), "price": float(l.unit_price)} for l in po.lines.all()] if po else []
    return render(request, "purchasing/po_form.html", {
        "title": f"Sửa {po.code}" if po else "Tạo đơn mua", "po": po, "lines_json": lines,
        "suppliers": Supplier.objects.filter(is_active=True), "warehouses": Warehouse.objects.filter(is_active=True),
        "routes": Route.objects.filter(is_active=True),
        "products": Product.objects.filter(is_active=True).select_related("base_uom"),
        "weights": {str(p.pk): [float(p.unit_weight_kg), float(p.truck or 30)] for p in
                    Product.objects.filter(is_active=True).annotate(truck=F("rop_param__truck_ton"))},
        "prices": _supplier_prices(), "threshold": svc.param("PO_APPROVAL_THRESHOLD")})


@role_required("DIRECTOR", "PURCHASER", "ACCOUNTANT")
@business_action
def po_detail(request, pk):
    po = get_object_or_404(PurchaseOrder.objects.select_related("supplier", "route", "warehouse"), pk=pk)
    if request.method == "POST":
        op = request.POST.get("op")
        if request.user.role == "ACCOUNTANT":
            raise BusinessError("Kế toán chỉ được xem PO.")
        if op == "submit":
            svc.submit_po(po, request.user)
            messages.success(request, "Đã gửi duyệt." if po.status == "PENDING_APPROVAL" else "PO dưới ngưỡng – tự động duyệt.")
        elif op in ("approve", "reject"):
            if request.user.role not in ("DIRECTOR", "ADMIN"):
                raise BusinessError("Chỉ Director được duyệt PO.")
            svc.approve_po(po, request.user, op == "approve", request.POST.get("reason", ""))
            messages.success(request, "Đã duyệt PO." if op == "approve" else "Đã từ chối PO.")
        elif op == "send":
            svc.send_po(po, request.POST.get("pickup_date") or po.pickup_date)
            messages.success(request, "Đã ghi nhận gửi NCC.")
        elif op == "cancel":
            if po.status in ("PARTIAL", "RECEIVED", "CLOSED") or po.trips.exclude(status="CANCELLED").exists():
                raise BusinessError("PO đã có chuyến / đã nhận hàng – không hủy được.")
            svc.cancel_doc(po, request.POST.get("reason"))
        elif op == "delete" and po.status == "DRAFT":
            po.delete()
            messages.success(request, "Đã xóa PO nháp.")
            return redirect("/app/purchase/orders")
        return redirect(request.path)
    if request.GET.get("pdf"):
        return pdf_response(request, "docs/po.html", {"po": po}, po.code)
    return render(request, "purchasing/po_detail.html", {
        "title": po.code, "po": po, "lines": po.lines.select_related("product__base_uom"),
        "trips": po.trips.select_related("vehicle", "driver"), "receipts": po.receipts.all(),
        "est": svc.estimate_trips(po.lines.select_related("product")),
        "threshold": svc.param("PO_APPROVAL_THRESHOLD")})


# ------------------------------------------------------------------ Inbound (MH-24)
@role_required("DIRECTOR", "PURCHASER", "WAREHOUSE")
def inbound_board(request):
    svc.maybe_scan_inbound()
    view = request.GET.get("view", "kanban")
    trips = InboundTrip.objects.exclude(status="CANCELLED").select_related("po__supplier", "vehicle", "driver", "route") \
        .prefetch_related("milestones")
    recent = timezone.now() - timedelta(days=2)
    trips = trips.filter(Q(~Q(status="RECEIVED")) | Q(updated_at__gte=recent))
    q = request.GET.get("q", "").strip()
    if q:
        trips = trips.filter(Q(code__icontains=q) | Q(vehicle__plate__icontains=q) | Q(po__supplier__name__icontains=q))
    trips = list(trips.order_by("eta"))
    for t in trips:
        ms = list(t.milestones.all())
        t.ms_done = sum(1 for m in ms if m.actual_at)
        t.ms_total = len(ms)
        t.ms_pct = int(t.ms_done / t.ms_total * 100) if ms else 0
    colors = ["var(--purple)", "var(--primary)", "var(--info)", "var(--warn)", "var(--success)"]
    cols = [{"title": title, "trips": [t for t in trips if t.status in sts], "color": colors[i]}
            for i, (title, sts) in enumerate(InboundTrip.KANBAN)]
    now = timezone.now()
    timeline = None
    if view == "timeline":
        start = now - timedelta(hours=2)
        end = start + timedelta(hours=24)
        timeline = {"hours": [timezone.localtime(start + timedelta(hours=h)).strftime("%H:00") for h in range(24)],
                    "rows": []}
        for t in trips:
            if t.eta and start <= t.eta <= end and t.status != "RECEIVED":
                timeline["rows"].append({"t": t, "left": (t.eta - start).total_seconds() / 86400 * 100})
        timeline["now"] = (now - start).total_seconds() / 86400 * 100
    ctx = {"title": "Bảng điều phối Inbound", "cols": cols, "trips": trips, "view": view, "timeline": timeline,
           "now": now, "late": sum(1 for t in trips if t.late_active)}
    if request.headers.get("HX-Request"):
        return render(request, "purchasing/_board_body.html", ctx)
    return render(request, "purchasing/inbound_board.html", ctx)


@role_required(*BUYERS)
@business_action
def trip_new(request):
    po = get_object_or_404(PurchaseOrder, pk=request.GET.get("po") or request.POST.get("po"))
    if request.method == "POST":
        qty = {l: request.POST.get(f"q_{l.pk}") for l in po.lines.all()}
        dep = timezone.make_aware(datetime.fromisoformat(request.POST["planned_departure"]))
        trip = svc.create_inbound_trip(
            po, get_object_or_404(Route, pk=request.POST["route"]), get_object_or_404(Vehicle, pk=request.POST["vehicle"]),
            get_object_or_404(Driver, pk=request.POST["driver"]), dep, qty,
            carrier=Carrier.objects.filter(pk=request.POST.get("carrier") or 0).first())
        messages.success(request, f"Đã lập chuyến {trip.code}.")
        return redirect(f"/app/inbound/trips/{trip.pk}")
    vehicles = Vehicle.objects.filter(is_active=True).select_related("carrier")
    drivers = Driver.objects.filter(is_active=True).select_related("carrier")
    return render(request, "purchasing/trip_form.html", {
        "title": f"Lập chuyến Inbound cho {po.code}", "po": po, "lines": po.lines.select_related("product__base_uom"),
        "routes": Route.objects.filter(is_active=True), "vehicles": vehicles, "drivers": drivers,
        "carriers": Carrier.objects.filter(is_active=True), "today": timezone.localdate()})


@role_required("DIRECTOR", "PURCHASER", "WAREHOUSE")
@business_action
def trip_detail(request, pk):
    trip = get_object_or_404(InboundTrip.objects.select_related("po__supplier", "vehicle", "driver", "route", "carrier"), pk=pk)
    if request.method == "POST":
        op = request.POST.get("op")
        if op == "milestone":  # điều phối viên cập nhật thay tài xế (ví dụ tài xế gọi điện báo)
            ms = get_object_or_404(trip.milestones, pk=request.POST["milestone"])
            svc.record_milestone(trip, ms, request.user, photo=request.FILES.get("photo"),
                                 note=request.POST.get("note", "") or "Cập nhật bởi điều phối")
            messages.success(request, f"Đã cập nhật mốc {ms.name}.")
        elif op == "incident":
            svc.report_incident(trip, request.user, request.POST["incident_type"], request.POST.get("description", ""),
                                int(request.POST.get("delay_minutes") or 0), request.FILES.get("photo"))
            messages.success(request, "Đã ghi nhận sự cố, ETA được tính lại.")
        elif op == "resolve":
            TripIncident.objects.filter(pk=request.POST["incident"], trip=trip).update(is_resolved=True)
            svc.recalc_eta(trip, "Sự cố đã xử lý")
        elif op == "propose_slot":
            if not svc.propose_slot(trip):
                raise BusinessError("Không tìm được bến trống trong 24h tới.")
            messages.success(request, "Đã đề xuất lịch hạ tải.")
        elif op == "cancel":
            if trip.status not in ("PLANNED", "AT_FACTORY"):
                raise BusinessError("Chỉ hủy được chuyến chưa bốc hàng.")
            with transaction.atomic():
                for l in trip.lines.select_related("po_line"):
                    l.po_line.qty_on_trips -= l.qty_planned
                    l.po_line.save(update_fields=["qty_on_trips"])
                UnloadingSlot.objects.filter(trip=trip).update(status="CANCELLED")
                svc.cancel_doc(trip, request.POST.get("reason"))
        return redirect(request.path)
    milestones, overdue_min = _plan_vs_actual(trip)
    done = [m for m in milestones if m.actual_at]
    late_ms = [m for m in done if m.delay_min > 0]
    return render(request, "purchasing/trip_detail.html", {
        "title": trip.code, "trip": trip, "milestones": milestones, "next_ms": trip.next_milestone,
        "lines": trip.lines.select_related("po_line__product__base_uom"), "incidents": trip.incidents.all(),
        "eta_logs": _group_eta_logs(trip.eta_logs.all()[:200])[:10],
        "slot": UnloadingSlot.objects.filter(trip=trip).select_related("dock").first(),
        "incident_types": TripIncident.TYPES, "receipts": trip.receipts.all(), "overdue_min": overdue_min,
        "load": _load_rating(trip),
        "sla": {"done": len(done), "late": len(late_ms), "max_delay": max((m.delay_min for m in late_ms), default=0)}})


def _plan_vs_actual(trip):
    """Gắn cho mỗi mốc chưa làm: overdue_min (quá hạn bao lâu so với kế hoạch) và resched_at (dự kiến lại).

    Độ trễ hiện tại = max(trễ của mốc thực tế gần nhất, thời gian quá hạn của mốc kế tiếp); các mốc còn lại
    trượt theo đúng độ trễ đó. Kế hoạch gốc giữ nguyên để làm báo cáo SLA."""
    now = timezone.now()
    ms = list(trip.milestones.all())
    active = trip.status in InboundTrip.ACTIVE
    last_done = next((m for m in reversed(ms) if m.actual_at), None)
    delay = max(0, last_done.delay_min if last_done else 0)
    pending = [m for m in ms if not m.actual_at]
    overdue = 0
    if active and pending and now > pending[0].planned_at:
        overdue = int((now - pending[0].planned_at).total_seconds() // 60)
        delay = max(delay, overdue)
    for m in pending:
        m.overdue_min = int((now - m.planned_at).total_seconds() // 60) if active and now > m.planned_at else 0
        m.resched_at = m.planned_at + timedelta(minutes=delay) if active and delay else None
    return ms, overdue


def _group_eta_logs(logs):
    """Gom các dòng log ETA liên tiếp cùng lý do (dữ liệu cũ do quét 5' sinh ra) thành 1 dòng kèm số lần."""
    out = []
    for e in logs:
        if out and out[-1]["reason"] == e.reason and e.reason == svc.AUTO_ETA_REASON:
            out[-1]["n"] += 1
            out[-1]["first_at"] = e.at
            continue
        out.append({"reason": e.reason, "new_eta": e.new_eta, "at": e.at, "first_at": e.at, "n": 1})
    return out


def _load_rating(trip):
    pct = trip.load_pct
    cap = trip.vehicle.capacity_ton
    if pct >= 90:
        label, tone = "Tối ưu tải trọng", "success"
    elif pct >= 70:
        label, tone = "Khá – còn có thể ghép thêm hàng", "warn"
    else:
        label, tone = "Chở dưới tải – hiệu quả khai thác thấp", "danger"
    return {"pct": pct, "label": label, "tone": tone, "free": max(cap - trip.total_ton, 0), "cap": cap}


# ------------------------------------------------------------------ Lịch hạ tải (MH-26)
@role_required("DIRECTOR", "PURCHASER", "WAREHOUSE")
@business_action
def slots(request):
    day = request.GET.get("date")
    day = datetime.strptime(day, "%Y-%m-%d").date() if day else timezone.localdate()
    wh = Warehouse.objects.filter(pk=request.GET.get("wh")).first() or Warehouse.objects.filter(is_active=True).first()
    docks = list(Dock.objects.filter(warehouse=wh, is_active=True)) if wh else []
    h0, h1 = 6, 22  # khung 06:00–22:00
    tz = timezone.get_current_timezone()
    day_start = timezone.make_aware(datetime.combine(day, time(h0)), tz)
    day_end = timezone.make_aware(datetime.combine(day, time(h1)), tz)
    qs = UnloadingSlot.objects.filter(dock__in=docks, start_at__lt=day_end, end_at__gt=day_start) \
        .exclude(status="CANCELLED").select_related("trip__vehicle", "trip__po__supplier", "dock")
    rows = (h1 - h0) * 4
    items = []
    for s in qs:
        top = max(0, (s.start_at - day_start).total_seconds() / 900)
        h = (min(s.end_at, day_end) - max(s.start_at, day_start)).total_seconds() / 900
        items.append({"s": s, "col": docks.index(s.dock), "row": top, "h": h})
    now = timezone.now()
    now_row = (now - day_start).total_seconds() / 900 if day_start <= now <= day_end else None
    waiting = InboundTrip.objects.filter(status__in=["IN_TRANSIT", "LOADED", "ARRIVED"], po__warehouse=wh) \
        .exclude(slot__status__in=["PROPOSED", "CONFIRMED", "CHECKED_IN", "DONE"]).select_related("vehicle")
    times = [f"{h0 + i // 4:02d}:{(i % 4) * 15:02d}" for i in range(rows)]
    items_json = json.dumps([{
        "id": it["s"].pk, "trip_id": it["s"].trip_id, "code": it["s"].trip.code, "plate": it["s"].trip.vehicle.plate,
        "status": it["s"].status, "col": it["col"], "row": it["row"], "h": it["h"],
        "start": timezone.localtime(it["s"].start_at).strftime("%H:%M"),
        "end": timezone.localtime(it["s"].end_at).strftime("%H:%M")} for it in items])
    return render(request, "purchasing/slots.html", {
        "items_json": items_json,
        "title": "Lịch hạ tải", "day": day, "prev": day - timedelta(days=1), "next": day + timedelta(days=1),
        "wh": wh, "warehouses": Warehouse.objects.filter(is_active=True), "docks": docks, "items": items,
        "times": times, "rows": rows, "now_row": now_row, "waiting": waiting, "h0": h0,
        "slot_list": sorted(qs, key=lambda s: s.start_at)})


@role_required("DIRECTOR", "PURCHASER", "WAREHOUSE")
@require_POST
@business_action
def slot_action(request, pk):
    slot = get_object_or_404(UnloadingSlot, pk=pk)
    op = request.POST.get("op")
    if request.content_type == "application/json":
        data = json.loads(request.body)
        dock = get_object_or_404(Dock, pk=data["dock"])
        start = timezone.make_aware(datetime.fromisoformat(data["start"]))
        svc.move_slot(slot, dock, start)
        return JsonResponse({"ok": True})
    if op == "confirm":
        svc.confirm_slot(slot)
        messages.success(request, f"Đã xác nhận lịch hạ tải {slot.trip.code}.")
    elif op == "checkin":
        gr = svc.check_in(slot, request.user)
        messages.success(request, f"Đã check-in xe {slot.trip.vehicle.plate}. Mở phiếu nhập {gr.code}.")
        return redirect(f"/app/warehouse/receipts/{gr.pk}")
    return redirect(request.META.get("HTTP_REFERER") or "/app/warehouse/slots")


# ------------------------------------------------------------------ GRN (MH-27)
@role_required("DIRECTOR", "PURCHASER", "WAREHOUSE")
def receipts(request):
    qs = GoodsReceipt.objects.select_related("po__supplier", "trip__vehicle")
    if request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"])
    page, qs_str = paginate(request, qs)
    return render(request, "purchasing/receipts.html", {"title": "Phiếu nhập kho (GRN)", "page_obj": page, "qs": qs_str,
                                                        "statuses": GoodsReceipt.STATUS})


@role_required("DIRECTOR", "PURCHASER", "WAREHOUSE")
@business_action
def receipt_detail(request, pk):
    gr = get_object_or_404(GoodsReceipt.objects.select_related("po__supplier", "trip__vehicle", "warehouse"), pk=pk)
    lines = gr.lines.select_related("product__base_uom", "location", "po_line")
    if request.method == "POST":
        op = request.POST.get("op")
        if op in ("save", "submit") and gr.status == "DRAFT":
            with transaction.atomic():
                for l in lines:
                    l.qty_received = _dec(request.POST.get(f"r_{l.pk}"), l.qty_received)
                    l.qty_damaged = _dec(request.POST.get(f"d_{l.pk}"), l.qty_damaged)
                    if l.qty_damaged > l.qty_received:
                        raise BusinessError(f"{l.product.code}: SL hỏng lớn hơn SL thực nhận.")
                    if request.POST.get(f"loc_{l.pk}"):
                        l.location_id = request.POST[f"loc_{l.pk}"]
                    l.save()
                gr.note = request.POST.get("note", gr.note)
                gr.save()
            if op == "submit":
                if svc.submit_grn(gr, request.user):
                    messages.success(request, f"Đã nhập kho {gr.code}. Tồn kho, giá vốn & lead time đã cập nhật.")
                else:
                    messages.warning(request, f"Lệch > {svc.param('GRN_VARIANCE_PCT'):g}% – phiếu chuyển Director duyệt.")
            else:
                messages.success(request, "Đã lưu nháp.")
        elif op == "approve":
            if request.user.role not in ("DIRECTOR", "ADMIN"):
                raise BusinessError("Chỉ Director duyệt chênh lệch GRN.")
            svc.approve_grn(gr, request.user)
            messages.success(request, "Đã duyệt chênh lệch và nhập kho.")
        elif op == "cancel":
            if gr.status == "POSTED":
                raise BusinessError("Phiếu đã nhập kho – không hủy được (lập phiếu điều chỉnh).")
            svc.cancel_doc(gr, request.POST.get("reason"))
        return redirect(request.path)
    if request.GET.get("pdf"):
        return pdf_response(request, "docs/grn.html", {"gr": gr, "lines": lines}, gr.code)
    return render(request, "purchasing/receipt_detail.html", {
        "title": gr.code, "gr": gr, "lines": lines, "limit": svc.param("GRN_VARIANCE_PCT"),
        "locations": Location.objects.filter(warehouse=gr.warehouse, is_active=True).exclude(loc_type="STAGING_OUT"),
        "claims": gr.claims.all()})
