"""Luồng A: PR -> PO -> Chuyến Inbound (mốc thủ công + ảnh) -> Lịch hạ tải -> GRN -> Tồn kho."""
import math
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.services import BusinessError, audit, next_doc_no, param, raise_alert, resolve_alerts
from apps.inventory.services import default_storage_location, post_stock_move
from apps.sales.services import check_image
from apps.masterdata.models import CarrierRate, Dock, Location, SupplierPrice

from .models import (Claim, GoodsReceipt, GoodsReceiptLine, InboundTrip, InboundTripLine, PurchaseOrder, PurchaseRequisition,
                     PurchaseOrderLine, TripEtaLog, TripIncident, TripMilestone, UnloadingSlot)

D0 = Decimal(0)
SLOT_STEP = 15  # phút


# ------------------------------------------------------------------ PR -> PO
def latest_price(supplier, product):
    today = timezone.localdate()
    sp = SupplierPrice.objects.filter(supplier=supplier, product=product, valid_from__lte=today) \
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=today)).order_by("-valid_from").first()
    return sp.price if sp else product.standard_cost


def estimate_trips(lines):
    """Số chuyến ước tính cho PO: tổng tấn / tải trọng xe chuẩn (lấy theo tham số SS/ROP của vật tư, mặc định 30 t)."""
    from apps.inventory.models import ReplenishmentParam
    lines = list(lines)
    ton = sum((l.qty * l.product.unit_weight_kg for l in lines), D0) / 1000
    trucks = {rp.product_id: rp.truck_ton for rp in ReplenishmentParam.objects.filter(product__in=[l.product for l in lines])}
    truck = min([trucks.get(l.product_id) or Decimal(30) for l in lines] or [Decimal(30)])
    return {"ton": ton, "truck": truck, "trips": math.ceil(ton / truck) if ton and truck else 0}


@transaction.atomic
def convert_prs_to_po(prs, warehouse, user):
    """Gộp các dòng PR theo NCC gợi ý -> mỗi NCC một PO nháp."""
    groups = defaultdict(list)
    for pr in prs:
        if pr.status not in ("SUBMITTED", "DRAFT"):
            raise BusinessError(f"{pr.code} không ở trạng thái có thể lập PO.")
        for line in pr.lines.filter(po_line__isnull=True):
            supplier = line.suggested_supplier or line.product.default_supplier
            if not supplier:
                raise BusinessError(f"{line.product.code} chưa có NCC mặc định.")
            groups[supplier].append(line)
    pos = []
    for supplier, lines in groups.items():
        po = PurchaseOrder.objects.create(code=next_doc_no("PO"), supplier=supplier, order_date=timezone.localdate(),
                                          route=supplier.default_route, warehouse=warehouse, created_by=user)
        merged = defaultdict(list)
        for l in lines:
            merged[l.product].append(l)
        for product, pr_lines in merged.items():
            pol = PurchaseOrderLine.objects.create(po=po, product=product, qty=sum(l.qty for l in pr_lines),
                                                   unit_price=latest_price(supplier, product))
            pr_lines[0].po_line = pol  # quan hệ 1-1: dòng PR đầu tiên được gắn với dòng PO đã gộp
            pr_lines[0].save(update_fields=["po_line"])
        po.recalc_total()
        po.save()
        pos.append(po)
    for pr in prs:
        pr.status = "CONVERTED"
        pr.save(update_fields=["status", "updated_at"])
    return pos


def _po_trigger(po, event, user):
    from apps.inventory.replenishment import Trigger, fire_checks
    fire_checks(po.lines.values_list("product_id", flat=True), Trigger(event, "PO", po.code, user))


@transaction.atomic
def reject_pr(pr, user, reason=""):
    """Thu mua từ chối PR: lưu rejected_position = position của vật tư ngay sau khi bỏ PR này khỏi pipeline.
    Trong 24 giờ, hàm lõi chỉ tạo lại PR tự động khi position mới < rejected_position (có nhu cầu mới)."""
    from apps.inventory.replenishment import stock_metrics

    pr.status = PurchaseRequisition.objects.select_for_update().values_list("status", flat=True).get(pk=pr.pk)
    if pr.status not in ("DRAFT", "SUBMITTED"):
        raise BusinessError("Chỉ từ chối được PR chưa lập PO.")
    pr.status, pr.rejected_at, pr.rejected_by, pr.reject_reason = "REJECTED", timezone.now(), user, reason or ""
    pr.save()
    products = [l.product for l in pr.lines.select_related("product")]
    if products:
        m = stock_metrics(products)
        pr.rejected_position = min(m[p.pk]["position"] for p in products)
        pr.save(update_fields=["rejected_position"])
    audit("REJECT", pr, {"reason": reason, "position": str(pr.rejected_position)})
    return pr


@transaction.atomic
def submit_po(po, user):
    """Gửi duyệt: tổng PO GỒM VAT ≥ ngưỡng -> chờ Giám đốc duyệt (vẫn ở pipeline, chưa vào "Đang về").
    Dưới ngưỡng -> tự duyệt (giả định G1) và chuyển sang "Đang về" như khi Giám đốc duyệt."""
    if po.status != "DRAFT":
        raise BusinessError("Chỉ PO nháp mới gửi duyệt được.")
    if not po.lines.exists():
        raise BusinessError("PO chưa có dòng hàng.")
    po.recalc_total()
    if po.total_with_vat >= param("PO_APPROVAL_THRESHOLD"):
        po.status = "PENDING_APPROVAL"
        po.save()
        raise_alert("PO_APPROVAL", f"PO {po.code} chờ duyệt", f"Giá trị {po.total_with_vat:,.0f} ₫ (gồm VAT)",
                    ref_type="po", ref_id=po.pk, url=f"/app/purchase/orders/{po.pk}", target_role="DIRECTOR")
        audit("SUBMIT", po)
        return
    po.status = "APPROVED"
    po.approved_at = timezone.now()
    po.save()
    audit("APPROVE", po, {"auto": "dưới ngưỡng duyệt"})
    _po_trigger(po, "PO_APPROVED", user)


@transaction.atomic
def approve_po(po, user, approve=True, reason=""):
    """Giám đốc duyệt -> SL PO chuyển từ pipeline sang "Đang về" (gọi check_replenishment PO_APPROVED).
    Trả lại -> PO về Nháp, vẫn nằm trong pipeline (giả định G5)."""
    # khóa dòng & đọc trạng thái thật trong DB, vẫn cập nhật đúng object của nơi gọi
    po.status = PurchaseOrder.objects.select_for_update().values_list("status", flat=True).get(pk=po.pk)
    if po.status != "PENDING_APPROVAL":
        raise BusinessError("PO không ở trạng thái chờ duyệt.")
    if approve:
        po.status, po.approved_by, po.approved_at = "APPROVED", user, timezone.now()
    else:
        po.status, po.cancel_reason = "DRAFT", ""
        po.note = (f"Giám đốc trả lại: {reason}" if reason else "Giám đốc trả lại")[:255]
    po.save()
    resolve_alerts("PO_APPROVAL", "po", po.pk)
    audit("APPROVE" if approve else "RETURN", po, {"reason": reason} if reason else None)
    _po_trigger(po, "PO_APPROVED" if approve else "PO_RETURNED", user)


@transaction.atomic
def cancel_po(po, reason, user=None):
    """Hủy PO: phần chưa nhận rời khỏi "Đang về" (hoặc pipeline nếu chưa duyệt) -> có thể phải tạo PR mới (T12)."""
    if po.status in ("PARTIAL", "RECEIVED", "CLOSED", "CANCELLED") or po.trips.exclude(status="CANCELLED").exists():
        raise BusinessError("PO đã có chuyến / đã nhận hàng – không hủy được.")
    cancel_doc(po, reason)
    _po_trigger(po, "PO_CANCELLED", user)


def send_po(po, pickup_date):
    if po.status != "APPROVED":
        raise BusinessError("PO phải được duyệt trước khi gửi NCC.")
    po.status, po.sent_at, po.pickup_date = "SENT", timezone.now(), pickup_date
    po.save()


def cancel_doc(doc, reason):
    if not reason:
        raise BusinessError("Phải nhập lý do hủy.")
    doc.status, doc.cancel_reason = "CANCELLED", reason
    doc.save()
    audit("CANCEL", doc, {"reason": reason})


# ------------------------------------------------------------------ Chuyến Inbound
@transaction.atomic
def create_inbound_trip(po, route, vehicle, driver, planned_departure, qty_by_line, carrier=None):
    if po.status not in ("APPROVED", "SENT", "PARTIAL"):
        raise BusinessError("PO chưa được duyệt / gửi NCC.")
    if not driver.license_valid:
        raise BusinessError(f"Bằng lái của tài xế {driver.name} đã hết hạn hoặc chưa khai báo.")
    total_kg = D0
    lines = []
    for pol, q in qty_by_line.items():
        q = Decimal(q or 0)
        if q <= 0:
            continue
        remain = pol.qty - pol.qty_on_trips
        if q > remain:
            raise BusinessError(f"{pol.product.code}: chỉ còn {remain} chưa xếp chuyến.")
        total_kg += q * pol.product.unit_weight_kg
        lines.append((pol, q))
    if not lines:
        raise BusinessError("Chưa chọn hàng cho chuyến.")
    total_ton = total_kg / 1000
    if total_ton > vehicle.capacity_ton:
        raise BusinessError(f"Tổng {total_ton:.2f} tấn vượt tải trọng xe {vehicle.plate} ({vehicle.capacity_ton} tấn).")

    carrier = carrier or vehicle.carrier
    freight = D0
    if carrier:
        rate = CarrierRate.objects.filter(contract__carrier=carrier, contract__is_active=True, route=route).first()
        if rate:
            freight = rate.rate_per_trip + rate.rate_per_ton * total_ton
    trip = InboundTrip.objects.create(
        code=next_doc_no("IB"), po=po, route=route, carrier=carrier, vehicle=vehicle, driver=driver,
        planned_departure=planned_departure, total_ton=total_ton, freight_amount=freight.quantize(Decimal(1)),
        eta=planned_departure + timedelta(hours=float(route.std_hours)))
    for pol, q in lines:
        InboundTripLine.objects.create(trip=trip, po_line=pol, qty_planned=q)
        pol.qty_on_trips += q
        pol.save(update_fields=["qty_on_trips"])
    for rm in route.milestones.all():
        TripMilestone.objects.create(trip=trip, seq=rm.seq, code=rm.code, name=rm.name,
                                     photo_required=rm.photo_required,
                                     planned_at=planned_departure + timedelta(minutes=rm.planned_offset_min))
    TripEtaLog.objects.create(trip=trip, new_eta=trip.eta, reason="Lập chuyến")
    return trip


MILESTONE_STATUS = {"FACTORY": "AT_FACTORY", "LOADED": "LOADED", "CHECKPOINT": "IN_TRANSIT", "GATE": "ARRIVED"}


AUTO_ETA_REASON = "Trễ mốc – ước tính lại"


def set_eta(trip, new_eta, reason, min_change_min=1):
    """Đổi ETA + ghi lịch sử. Lần ước tính lại tự động liên tiếp được gom vào 1 dòng log thay vì sinh dòng mới."""
    if trip.eta and abs((trip.eta - new_eta).total_seconds()) < min_change_min * 60:
        return
    last = trip.eta_logs.order_by("-at", "-id").first()
    if reason == AUTO_ETA_REASON and last and last.reason == reason:
        last.new_eta, last.at = new_eta, timezone.now()
        last.save(update_fields=["new_eta", "at"])
    else:
        TripEtaLog.objects.create(trip=trip, old_eta=trip.eta, new_eta=new_eta, reason=reason)
    trip.eta = new_eta
    trip.save(update_fields=["eta", "updated_at"])
    if trip.status not in ("ARRIVED", "UNLOADING", "RECEIVED", "CANCELLED"):
        if new_eta - timezone.now() <= timedelta(hours=param("SLOT_PROPOSE_HOURS")) and not hasattr_slot(trip):
            propose_slot(trip)
        elif hasattr_slot(trip) and trip.slot.status == "PROPOSED":
            propose_slot(trip)  # ETA đổi -> đề xuất lại lịch chưa xác nhận


def hasattr_slot(trip):
    return UnloadingSlot.objects.filter(trip=trip).exclude(status="CANCELLED").exists()


def recalc_eta(trip, reason="Cập nhật mốc"):
    """ETA = thời điểm mốc thực tế gần nhất + phần thời gian chuẩn còn lại + trễ do sự cố chưa xử lý."""
    last = trip.milestones.exclude(actual_at=None).order_by("-seq").first()
    total_min = float(trip.route.std_hours) * 60
    if last:
        offset = (last.planned_at - trip.planned_departure).total_seconds() / 60
        base = last.actual_at + timedelta(minutes=max(0, total_min - offset))
    else:
        base = max(trip.planned_departure, timezone.now()) + timedelta(minutes=total_min)
    extra = sum(i.delay_minutes for i in trip.incidents.filter(is_resolved=False))
    set_eta(trip, base + timedelta(minutes=extra), reason)


@transaction.atomic
def record_milestone(trip, milestone, user, photo=None, note="", loaded_qty=None):
    if trip.status in ("RECEIVED", "CANCELLED"):
        raise BusinessError("Chuyến đã kết thúc.")
    nxt = trip.next_milestone
    if not nxt or nxt.pk != milestone.pk:
        raise BusinessError("Phải cập nhật mốc theo đúng thứ tự.")
    if milestone.photo_required and not photo:
        raise BusinessError(f"Mốc '{milestone.name}' bắt buộc chụp ảnh xác thực.")
    check_image(photo, "Ảnh xác thực")
    if milestone.code == "LOADED":
        for line in trip.lines.all():
            q = (loaded_qty or {}).get(line.pk)
            line.qty_loaded = Decimal(q) if q not in (None, "") else line.qty_planned
            line.save(update_fields=["qty_loaded"])
    now = timezone.now()
    milestone.actual_at, milestone.by_user, milestone.note = now, user, note
    if photo:
        milestone.photo = photo
    milestone.save()
    trip.status = MILESTONE_STATUS.get(milestone.code, trip.status)
    if milestone.code == "GATE":
        trip.arrived_at = now
    trip.is_late = False
    trip.save()
    resolve_alerts("MILESTONE_LATE", "inbound_trip", trip.pk)
    if milestone.code != "GATE":
        recalc_eta(trip, f"Mốc {milestone.name}")
    else:
        set_eta(trip, now, "Xe tới cổng")
        if not hasattr_slot(trip):
            propose_slot(trip)


def report_incident(trip, user, incident_type, description, delay_minutes=0, photo=None):
    check_image(photo, "Ảnh sự cố")
    inc = TripIncident.objects.create(trip=trip, incident_type=incident_type, description=description,
                                      delay_minutes=delay_minutes or 0, photo=photo, reported_by=user)
    raise_alert("TRIP_INCIDENT", f"Sự cố chuyến {trip.code}: {inc.get_incident_type_display()}", description,
                level="CRIT" if incident_type == "ACCIDENT" else "WARN", ref_type="trip_incident", ref_id=inc.pk,
                url=f"/app/inbound/trips/{trip.pk}", target_role="PURCHASER")
    from apps.core.templatetags.tracon import dur
    recalc_eta(trip, f"Sự cố: {inc.get_incident_type_display()} (+{dur(delay_minutes or 0)})")
    return inc


def maybe_scan_inbound(minutes=5):
    """Khi không có Celery (Vercel): quét khi người dùng mở trang, tối đa mỗi `minutes` phút một lần."""
    from django.core.cache import cache
    if cache.add("scan_inbound_throttle", 1, minutes * 60):
        try:
            scan_inbound()
        except Exception:  # không để lỗi quét làm hỏng trang
            cache.delete("scan_inbound_throttle")


def scan_inbound():
    """Tác vụ nền: cảnh báo MILESTONE_LATE (quá giờ dự kiến + 60’), tính lại ETA, đề xuất lịch hạ tải."""
    now = timezone.now()
    late_min = param("MILESTONE_LATE_MIN")
    n = 0
    for trip in InboundTrip.objects.exclude(status__in=["ARRIVED", "UNLOADING", "RECEIVED", "CANCELLED"]):
        nxt = trip.next_milestone
        if nxt and now > nxt.planned_at + timedelta(minutes=late_min):
            if not trip.is_late:
                trip.is_late = True
                trip.save(update_fields=["is_late"])
            raise_alert("MILESTONE_LATE", f"Chuyến {trip.code} trễ mốc '{nxt.name}'",
                        f"Dự kiến {timezone.localtime(nxt.planned_at):%H:%M}, chưa cập nhật.", ref_type="inbound_trip",
                        ref_id=trip.pk, url=f"/app/inbound/trips/{trip.pk}", target_role="PURCHASER")
            # chưa có mốc -> đẩy ETA theo thời gian trễ thực tế
            offset_left = float(trip.route.std_hours) * 60 - (nxt.planned_at - trip.planned_departure).total_seconds() / 60
            # chỉ cập nhật khi ETA lệch đáng kể (mặc định ≥ 15') – tránh ghi log mỗi lần quét 5'
            set_eta(trip, now + timedelta(minutes=max(0, offset_left)), AUTO_ETA_REASON, param("ETA_LOG_MIN"))
            n += 1
        elif trip.eta and trip.eta - now <= timedelta(hours=param("SLOT_PROPOSE_HOURS")) and not hasattr_slot(trip):
            propose_slot(trip)
    return n


# ------------------------------------------------------------------ Lịch hạ tải (bến × 15’)
def _ceil_step(dt):
    dt = timezone.localtime(dt).replace(second=0, microsecond=0)
    extra = (-dt.minute) % SLOT_STEP
    return dt + timedelta(minutes=extra)


def slot_duration(trip):
    """30’ cơ bản + 15’ mỗi 10 tấn, làm tròn bước 15’."""
    blocks = 2 + int(Decimal(trip.total_ton) // 10)
    return timedelta(minutes=blocks * SLOT_STEP)


def _overlaps(dock, start, end, exclude_pk=None):
    qs = UnloadingSlot.objects.filter(dock=dock, start_at__lt=end, end_at__gt=start).exclude(status__in=["CANCELLED"])
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


@transaction.atomic
def propose_slot(trip):
    """Tìm bến trống sớm nhất từ ETA, chống trùng bến."""
    docks = list(Dock.objects.filter(warehouse=trip.po.warehouse, is_active=True))
    if not docks:
        return None
    dur = slot_duration(trip)
    start = _ceil_step(max(trip.eta or timezone.now(), timezone.now()))
    existing = UnloadingSlot.objects.filter(trip=trip).first()
    for _ in range(96):  # thử trong 24h
        for dock in docks:
            if not _overlaps(dock, start, start + dur, existing.pk if existing else None):
                if existing:
                    existing.dock, existing.start_at, existing.end_at = dock, start, start + dur
                    existing.status = "PROPOSED" if existing.status in ("PROPOSED", "CANCELLED") else existing.status
                    existing.save()
                    return existing
                return UnloadingSlot.objects.create(trip=trip, dock=dock, start_at=start, end_at=start + dur)
        start += timedelta(minutes=SLOT_STEP)
    return None


def move_slot(slot, dock, start):
    if slot.status in ("CHECKED_IN", "DONE"):
        raise BusinessError("Xe đã check-in, không đổi lịch được.")
    start = _ceil_step(start)
    end = start + (slot.end_at - slot.start_at)
    if _overlaps(dock, start, end, slot.pk):
        raise BusinessError(f"Bến {dock.code} đã có lịch trong khung {timezone.localtime(start):%H:%M}.")
    slot.dock, slot.start_at, slot.end_at = dock, start, end
    slot.save()


def confirm_slot(slot):
    if slot.status != "PROPOSED":
        raise BusinessError("Lịch không ở trạng thái đề xuất.")
    slot.status = "CONFIRMED"
    slot.save(update_fields=["status"])


@transaction.atomic
def check_in(slot, user):
    """Thủ kho check-in xe -> mở phiếu nhập GRN nháp từ hàng đã bốc."""
    if slot.status not in ("PROPOSED", "CONFIRMED"):
        raise BusinessError("Lịch đã check-in hoặc bị hủy.")
    trip = slot.trip
    if trip.status not in ("ARRIVED", "IN_TRANSIT", "LOADED"):
        raise BusinessError("Xe chưa tới cổng.")
    now = timezone.now()
    if not trip.arrived_at:
        trip.arrived_at = now
    slot.status, slot.checked_in_at = "CHECKED_IN", now
    slot.save()
    trip.status, trip.is_late = "UNLOADING", False  # đã tới kho: hết trạng thái "trễ mốc" (độ trễ vẫn lưu ở từng mốc)
    trip.save()
    resolve_alerts("MILESTONE_LATE", "inbound_trip", trip.pk)
    gr = GoodsReceipt.objects.create(code=next_doc_no("GRN"), trip=trip, po=trip.po, warehouse=trip.po.warehouse,
                                     received_at=now, created_by=user)
    for tl in trip.lines.select_related("po_line__product"):
        q = tl.qty_loaded if tl.qty_loaded is not None else tl.qty_planned
        GoodsReceiptLine.objects.create(
            gr=gr, po_line=tl.po_line, product=tl.po_line.product,
            location=default_storage_location(tl.po_line.product, gr.warehouse),
            qty_expected=q, qty_received=q, unit_cost=tl.po_line.unit_price)
    return gr


# ------------------------------------------------------------------ GRN
@transaction.atomic
def submit_grn(gr, user):
    """Lệch > 2% -> chờ Director duyệt; ngược lại ghi sổ ngay."""
    # bấm 2 lần / 2 request song song: khóa dòng, đọc trạng thái thật -> chỉ ghi sổ 1 lần
    gr.status = GoodsReceipt.objects.select_for_update().values_list("status", flat=True).get(pk=gr.pk)
    if gr.status != "DRAFT":
        raise BusinessError("Phiếu nhập không ở trạng thái nháp.")
    limit = param("GRN_VARIANCE_PCT")
    if gr.max_variance_pct > limit:
        gr.status = "PENDING_APPROVAL"
        gr.save()
        raise_alert("GRN_VARIANCE", f"GRN {gr.code} lệch > {limit:g}%", ref_type="grn", ref_id=gr.pk,
                    url=f"/app/warehouse/receipts/{gr.pk}", target_role="DIRECTOR")
        return False
    post_grn(gr, user)
    return True


@transaction.atomic
def approve_grn(gr, user):
    gr.status = GoodsReceipt.objects.select_for_update().values_list("status", flat=True).get(pk=gr.pk)
    if gr.status != "PENDING_APPROVAL":
        raise BusinessError("Phiếu không chờ duyệt.")
    gr.approved_by = user
    post_grn(gr, user)
    resolve_alerts("GRN_VARIANCE", "grn", gr.pk)
    audit("APPROVE", gr)


def post_grn(gr, user):
    """Nhập kho POSTED: cộng tồn thực, tăng SL đã nhận của PO (incoming giảm đúng bằng lượng nhập), giữ hàng cho
    backorder theo thứ tự ưu tiên, rồi sau commit gọi check_replenishment(GRN_POSTED). Hàng vào vị trí cách ly không
    tính vào tồn thực nhưng vẫn tính là đã nhận của PO (T15)."""
    from apps.inventory.replenishment import Trigger, fire_checks
    from apps.sales.services import retry_backorders

    quarantine = Location.objects.filter(warehouse=gr.warehouse, loc_type="QUARANTINE", is_active=True).first()
    now = timezone.now()
    shortage_amt = damage_amt = D0
    for line in gr.lines.select_related("po_line"):  # G4: chặn nhập vượt SL còn lại của PO
        pol = PurchaseOrderLine.objects.select_for_update().get(pk=line.po_line_id)
        if line.qty_good > pol.qty - pol.qty_received:
            raise BusinessError(f"{line.product.code}: nhập {line.qty_good:g} vượt SL còn lại của PO "
                                f"({pol.qty - pol.qty_received:g}). Thu mua cần sửa PO trước.")
    for line in gr.lines.select_related("po_line", "product", "location"):
        if line.qty_good > 0:
            post_stock_move(line.product, line.location, line.qty_good, "IN_PURCHASE", "GRN", gr.pk,
                            unit_cost=line.unit_cost, note=gr.code, at=now)
        if line.qty_damaged > 0 and quarantine:
            post_stock_move(line.product, quarantine, line.qty_damaged, "IN_PURCHASE", "GRN", gr.pk,
                            unit_cost=line.unit_cost, note=f"{gr.code} – hàng lỗi cách ly", at=now)
            damage_amt += line.qty_damaged * line.unit_cost
        if line.variance < 0:
            shortage_amt += -line.variance * line.unit_cost
        pol = line.po_line
        pol.qty_received += line.qty_good
        pol.save(update_fields=["qty_received"])

    po = gr.po
    po.status = "RECEIVED" if all(l.qty_received >= l.qty for l in po.lines.all()) else "PARTIAL"
    po.save(update_fields=["status", "updated_at"])

    start = po.sent_at or po.approved_at or po.created_at
    gr.lead_time_days = Decimal(f"{(now - start).total_seconds() / 86400:.2f}")
    gr.status, gr.posted_at = "POSTED", now
    gr.save()

    if gr.trip_id:
        trip = gr.trip
        trip.status = "RECEIVED"
        trip.save(update_fields=["status", "updated_at"])
        UnloadingSlot.objects.filter(trip=trip).update(status="DONE", done_at=now)
    if shortage_amt > 0:
        Claim.objects.create(code=next_doc_no("CL"), gr=gr, claim_type="SHORTAGE", against="CARRIER",
                             amount=shortage_amt, description="Thiếu hàng khi kiểm nhận")
    if damage_amt > 0:
        Claim.objects.create(code=next_doc_no("CL"), gr=gr, claim_type="DAMAGE", against="SUPPLIER",
                             amount=damage_amt, description="Hàng hư hỏng – đưa vào khu cách ly")
    audit("POST", gr)
    trig = Trigger("GRN_POSTED", "GRN", gr.code, user)
    pids = set(gr.lines.values_list("product_id", flat=True))
    for pid in sorted(pids):
        retry_backorders(pid, trig)
    fire_checks(pids, trig)
