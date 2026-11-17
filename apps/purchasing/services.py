"""Luồng A: PR -> PO -> Chuyến Inbound (mốc thủ công + ảnh) -> Lịch hạ tải -> GRN -> Tồn kho."""
import math
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.services import BusinessError, audit, next_doc_no, param, raise_alert, resolve_alerts
from apps.inventory.services import default_storage_location, post_stock_move
from apps.sales.services import check_image
from apps.masterdata.models import CarrierRate, Dock, Location, SupplierPrice

from .models import (Claim, GoodsReceipt, GoodsReceiptLine, InboundTrip, InboundTripLine, PurchaseOrder, PurchaseRequisition,
                     PurchaseRequisitionLine, PurchaseOrderLine, TripEtaLog, TripIncident, TripMilestone, UnloadingSlot)

D0 = Decimal(0)
SLOT_STEP = 15  # phút


# ------------------------------------------------------------------ PR -> PO
def latest_price(supplier, product):
    today = timezone.localdate()
    # chỉ giá đã được Giám đốc duyệt mới dùng cho PO (giá Thu mua vừa nhập ở trạng thái Chờ duyệt)
    sp = SupplierPrice.objects.filter(supplier=supplier, product=product, valid_from__lte=today, status="APPROVED") \
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gte=today)).order_by("-valid_from", "-id").first()
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
            for l in pr_lines:  # mọi dòng PR đã gộp đều ghi lại PO nhận -> hủy PO trả được đúng các PR (STT 47)
                l.merged_into = pol
                l.save(update_fields=["merged_into"])
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
    po.status, po.approved_at, po.auto_approved = "APPROVED", timezone.now(), True
    po.approved_by = None
    po.save()
    audit("APPROVE", po, {"auto": f"Tự động – dưới ngưỡng duyệt {param('PO_APPROVAL_THRESHOLD'):,.0f} ₫"})
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
    released = release_prs_of_po(po)
    if released:
        audit("UPDATE", po, {"PR trả về kế hoạch": ", ".join(pr.code for pr in released)})
    _po_trigger(po, "PO_CANCELLED", user)
    return released


def release_prs_of_po(po):
    """STT 47: hủy PO -> các PR đã gộp vào PO trả về "Đã gửi" (chờ lập PO), lượng cần mua quay lại kế hoạch bổ sung
    (pipeline) và gộp được vào PO khác. PR còn dòng nằm ở PO khác đang hiệu lực thì giữ "Đã lập PO"."""
    pol_ids = list(po.lines.values_list("pk", flat=True))
    lines = list(PurchaseRequisitionLine.objects.filter(Q(po_line_id__in=pol_ids) | Q(merged_into_id__in=pol_ids))
                 .select_related("pr"))
    prs = {l.pr_id: l.pr for l in lines}
    for l in lines:
        l.po_line, l.merged_into = None, None
        l.save(update_fields=["po_line", "merged_into"])
    released = []
    for pr in prs.values():
        still = pr.lines.filter(Q(po_line__isnull=False) | Q(merged_into__isnull=False)) \
            .exclude(merged_into__po__status="CANCELLED").exists()
        if pr.status == "CONVERTED" and not still:
            pr.status = "SUBMITTED"
            pr.note = (f"PO {po.code} đã hủy – PR trả về chờ lập PO. " + (pr.note or ""))[:255]
            pr.save(update_fields=["status", "note", "updated_at"])
            audit("UPDATE", pr, {"Trạng thái": "Đã gửi (PO bị hủy)", "PO": po.code})
            released.append(pr)
    return released


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
    for seq, code, name, photo, offset in standard_milestones(route):
        TripMilestone.objects.create(trip=trip, seq=seq, code=code, name=name, photo_required=photo,
                                     planned_at=planned_departure + timedelta(minutes=offset))
    TripEtaLog.objects.create(trip=trip, new_eta=trip.eta, reason="Lập chuyến")
    return trip


def standard_milestones(route):
    """STT 73 (nhóm đã chốt): mọi chuyến chỉ còn 2 mốc tài xế bấm + sự cố (báo khi có):
      (1) Rời nhà máy – bắt buộc ảnh phiếu cân / niêm phong, nhập SL thực bốc;
      (2) Còn khoảng 2 giờ tới kho – kho xếp bến khi nhận mốc này (STT 74).
    ETA tính từ mốc (1) và (2), cộng trễ của sự cố đang mở. Mốc tuyến cũ trong danh mục không còn dùng."""
    total = int(float(route.std_hours) * 60)
    return [(1, "DEPART", "Rời nhà máy (ảnh phiếu cân / niêm phong)", True, 0),
            (2, "NEAR", "Còn khoảng 2 giờ tới kho", False, max(0, total - NEAR_MINUTES))]


NEAR_MINUTES = 120
MILESTONE_STATUS = {"FACTORY": "AT_FACTORY", "LOADED": "LOADED", "CHECKPOINT": "IN_TRANSIT", "GATE": "ARRIVED",
                    "DEPART": "IN_TRANSIT", "NEAR": "IN_TRANSIT"}


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
    # STT 74: không còn tự đề xuất trước 4 giờ / tự dời lịch theo ETA – bến chỉ xếp khi xe báo "còn 2 giờ"


def hasattr_slot(trip):
    return UnloadingSlot.objects.filter(trip=trip).exclude(status="CANCELLED").exists()


def recalc_eta(trip, reason="Cập nhật mốc"):
    """ETA = thời điểm mốc thực tế gần nhất + phần thời gian chuẩn còn lại + trễ do sự cố chưa xử lý.

    STT 48: thời gian trễ của sự cố chỉ cộng cho đến mốc KẾ TIẾP – sự cố ghi trước mốc thực tế gần nhất đã nằm sẵn
    trong giờ thực tế của mốc đó, nên không cộng thêm lần nữa (trước đây ETA bị +90' hai lần)."""
    last = trip.milestones.exclude(actual_at=None).order_by("-seq").first()
    total_min = float(trip.route.std_hours) * 60
    if last:
        offset = (last.planned_at - trip.planned_departure).total_seconds() / 60
        base = last.actual_at + timedelta(minutes=max(0, total_min - offset))
    else:
        base = max(trip.planned_departure, timezone.now()) + timedelta(minutes=total_min)
    pending = trip.incidents.filter(is_resolved=False)
    if last:
        pending = pending.filter(reported_at__gt=last.actual_at)
    extra = sum(i.delay_minutes for i in pending)
    set_eta(trip, base + timedelta(minutes=extra), reason)


def _after_milestone(trip_id, milestone_id):
    """Tính ETA / đề xuất lịch hạ tải SAU khi mốc đã lưu (STT 32). Lỗi ở đây không được làm hỏng request: mốc đã lưu,
    lần quét Inbound kế tiếp (maybe_scan_inbound / tác vụ đêm) sẽ tính lại."""
    import logging
    try:
        trip = InboundTrip.objects.select_related("route").get(pk=trip_id)
        milestone = TripMilestone.objects.get(pk=milestone_id)
        if milestone.code != "GATE":
            recalc_eta(trip, f"Mốc {milestone.name}")
        else:
            set_eta(trip, milestone.actual_at, "Xe tới cổng")
        if milestone.code in ("NEAR", "GATE") and not hasattr_slot(trip):
            propose_slot(trip)  # STT 74: xếp bến khi xe báo "còn 2 giờ" (bến trống sớm nhất từ ETA)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("Tính ETA sau mốc lỗi: trip=%s milestone=%s", trip_id, milestone_id)


def record_milestone(trip, milestone, user, photo=None, note="", loaded_qty=None):
    """Ghi mốc hành trình. Bấm gửi 2 lần / gửi lại sau lỗi mạng không tạo mốc trùng: mốc đã có thực tế thì trả về
    luôn (không báo lỗi). Ảnh được upload TRƯỚC giao dịch DB; ETA / lịch hạ tải tính sau commit."""
    if milestone.photo_required and not photo and not milestone.actual_at:
        raise BusinessError(f"Mốc '{milestone.name}' bắt buộc chụp ảnh xác thực.")
    check_image(photo, "Ảnh xác thực")
    stored = None
    if photo and not milestone.actual_at:
        from django.core.files.storage import default_storage
        stored = default_storage.save(milestone.photo.field.generate_filename(milestone, photo.name), photo)
    with transaction.atomic():
        trip = InboundTrip.objects.select_for_update().get(pk=trip.pk)
        milestone = TripMilestone.objects.select_for_update().get(pk=milestone.pk)
        if milestone.actual_at:  # đã lưu ở lần gửi trước (vd. máy chủ trả 503 sau khi lưu) -> không ghi lại
            if stored:
                transaction.on_commit(lambda: _drop_file(stored))
            return milestone, False
        if trip.status in ("RECEIVED", "CANCELLED"):
            raise BusinessError("Chuyến đã kết thúc.")
        nxt = trip.next_milestone
        if not nxt or nxt.pk != milestone.pk:
            raise BusinessError("Phải cập nhật mốc theo đúng thứ tự.")
        if milestone.code in ("LOADED", "DEPART"):  # SL thực bốc nhập ở mốc Rời nhà máy
            for line in trip.lines.all():
                q = (loaded_qty or {}).get(line.pk)
                line.qty_loaded = Decimal(q) if q not in (None, "") else line.qty_planned
                line.save(update_fields=["qty_loaded"])
        now = timezone.now()
        milestone.actual_at, milestone.by_user, milestone.note = now, user, note
        if stored:
            milestone.photo.name = stored
        milestone.save()
        replan_after_early(trip, milestone)
        trip.status = MILESTONE_STATUS.get(milestone.code, trip.status)
        if milestone.code == "GATE":
            trip.arrived_at = now
        trip.is_late = False
        trip.save()
        resolve_alerts("MILESTONE_LATE", "inbound_trip", trip.pk)
        tid, mid = trip.pk, milestone.pk
        transaction.on_commit(lambda: _after_milestone(tid, mid))
    return milestone, True


def _drop_file(name):
    from django.core.files.storage import default_storage
    try:
        default_storage.delete(name)
    except Exception:  # noqa: BLE001
        pass


@transaction.atomic
def force_close_trip(trip, user, reason):
    """STT 51: Giám đốc đóng chuyến kẹt (vd. "Đang hạ tải" từ tháng trước): giải phóng tài xế / bến.
    Đã có phiếu nhập ghi sổ -> "Đã nhập kho"; chưa có -> "Đã hủy" (hủy phiếu nhập nháp, trả SL chưa nhận về PO)."""
    reason = (reason or "").strip()
    if not reason:
        raise BusinessError("Nhập lý do đóng chuyến.")
    trip = InboundTrip.objects.select_for_update().get(pk=trip.pk)
    if trip.status in ("RECEIVED", "CANCELLED", "PLANNED", "AT_FACTORY"):
        raise BusinessError("Chuyến không ở trạng thái cần đóng thủ công.")
    now = timezone.now()
    posted = trip.receipts.filter(status="POSTED").exists()
    for gr in trip.receipts.filter(status__in=["DRAFT", "PENDING_APPROVAL"]):
        gr.status, gr.cancel_reason = "CANCELLED", f"Đóng chuyến thủ công: {reason}"[:255]
        gr.save(update_fields=["status", "cancel_reason", "updated_at"])
        resolve_alerts("GRN_VARIANCE", "grn", gr.pk)
    if not posted:
        for l in trip.lines.select_related("po_line"):
            l.po_line.qty_on_trips = max(D0, l.po_line.qty_on_trips - l.qty_planned)
            l.po_line.save(update_fields=["qty_on_trips"])
    UnloadingSlot.objects.filter(trip=trip).exclude(status__in=["DONE", "CANCELLED"]) \
        .update(status="DONE" if posted else "CANCELLED", done_at=now)
    old = trip.get_status_display()
    trip.status = "RECEIVED" if posted else "CANCELLED"
    trip.cancel_reason = f"Đóng thủ công ({old}): {reason}"[:255]
    trip.is_late = False
    trip.save()
    for t in ("MILESTONE_LATE", "STALE_INBOUND"):
        resolve_alerts(t, "inbound_trip", trip.pk)
    audit("CANCEL" if not posted else "UPDATE", trip, {"Đóng chuyến thủ công": reason, "Trạng thái cũ": old})
    return trip


def replan_after_early(trip, milestone):
    """Mốc thực tế SỚM hơn dự kiến quá ngưỡng (mặc định 30') -> tính lại giờ dự kiến các mốc sau theo mốc thực tế:
    dự kiến mới = thực tế mốc này + (kế hoạch gốc mốc sau − kế hoạch gốc mốc này). Kế hoạch gốc giữ nguyên."""
    t = milestone.timing()
    if not t or t[0] != "EARLY":
        return 0
    n = 0
    for m in trip.milestones.filter(seq__gt=milestone.seq, actual_at__isnull=True):
        m.replanned_at = milestone.actual_at + (m.planned_at - milestone.planned_at)
        m.save(update_fields=["replanned_at"])
        n += 1
    return n


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
        if nxt and now > nxt.expected_at + timedelta(minutes=late_min):
            if not trip.is_late:
                trip.is_late = True
                trip.save(update_fields=["is_late"])
            raise_alert("MILESTONE_LATE", f"Chuyến {trip.code} trễ mốc '{nxt.name}'",
                        f"Dự kiến {timezone.localtime(nxt.expected_at):%H:%M}, chưa cập nhật.", ref_type="inbound_trip",
                        ref_id=trip.pk, url=f"/app/inbound/trips/{trip.pk}", target_role="PURCHASER")
            # chưa có mốc -> đẩy ETA theo thời gian trễ thực tế
            offset_left = float(trip.route.std_hours) * 60 - (nxt.planned_at - trip.planned_departure).total_seconds() / 60
            # chỉ cập nhật khi ETA lệch đáng kể (mặc định ≥ 15') – tránh ghi log mỗi lần quét 5'
            set_eta(trip, now + timedelta(minutes=max(0, offset_left)), AUTO_ETA_REASON, param("ETA_LOG_MIN"))
            n += 1
    # STT 74: bỏ tự đề xuất lịch trước 4 giờ và tự dời lịch (normalize_slots) – xếp bến theo mốc "còn 2 giờ"
    return n


# ------------------------------------------------------------------ Lịch hạ tải (bến × 15’, khung 06:00–21:00)
def _parse_hm(raw, default):
    """'06:00' / '6' / '6.5' -> time; giá trị lỗi -> default."""
    try:
        raw = str(raw).strip()
        if ":" in raw:
            h, m = raw.split(":")[:2]
            return time(int(h), int(m))
        f = float(raw)
        return time(int(f), int(round((f % 1) * 60)))
    except (TypeError, ValueError):
        return default


def slot_hours():
    """Khung giờ hạ tải (giờ mở, giờ đóng) – tham số SLOT_DAY_START / SLOT_DAY_END, mặc định 06:00–21:00.
    Dùng chung cho tự xếp lịch, kéo–thả và lưới hiển thị."""
    start = _parse_hm(param("SLOT_DAY_START", str) or "06:00", time(6))
    end = _parse_hm(param("SLOT_DAY_END", str) or "21:00", time(21))
    if end <= start:
        return time(6), time(21)
    return start, end


def slot_window(day):
    """(đầu khung, cuối khung) của 1 ngày, có múi giờ."""
    h0, h1 = slot_hours()
    tz = timezone.get_current_timezone()
    return timezone.make_aware(datetime.combine(day, h0), tz), timezone.make_aware(datetime.combine(day, h1), tz)


def fit_window(start, dur):
    """Đưa giờ bắt đầu vào khung: trước giờ mở -> giờ mở cùng ngày; bắt đầu + thời lượng vượt giờ đóng -> giờ mở hôm sau."""
    start = timezone.localtime(start)
    ws, we = slot_window(start.date())
    if start < ws:
        return ws
    if start + dur > we:
        return slot_window(start.date() + timedelta(days=1))[0]
    return start


def in_window(start, end):
    ws, we = slot_window(timezone.localtime(start).date())
    return ws <= start and end <= we


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


def find_free_slot(docks, start_from, dur, exclude_pk=None, days=14):
    """Khung trống sớm nhất ≥ start_from và NẰM TRONG khung giờ hạ tải. Trả về (dock, start) hoặc (None, None)."""
    start = fit_window(_ceil_step(start_from), dur)
    limit = start + timedelta(days=days)
    while start < limit:
        for dock in docks:
            if not _overlaps(dock, start, start + dur, exclude_pk):
                return dock, start
        start = fit_window(start + timedelta(minutes=SLOT_STEP), dur)
    return None, None


@transaction.atomic
def propose_slot(trip):
    """Tìm bến trống sớm nhất từ ETA trong khung giờ hạ tải (mặc định 06:00–21:00), chống trùng bến.
    Hết khung trong ngày -> giờ mở hôm sau (trùng thì lấy khung trống kế tiếp)."""
    docks = list(Dock.objects.filter(warehouse=trip.po.warehouse, is_active=True))
    if not docks:
        return None
    dur = slot_duration(trip)
    existing = UnloadingSlot.objects.filter(trip=trip).first()
    dock, start = find_free_slot(docks, max(trip.eta or timezone.now(), timezone.now()), dur,
                                 existing.pk if existing else None)
    if dock is None:
        return None
    if existing:
        existing.dock, existing.start_at, existing.end_at = dock, start, start + dur
        existing.status = "PROPOSED" if existing.status in ("PROPOSED", "CANCELLED") else existing.status
        existing.save()
        return existing
    return UnloadingSlot.objects.create(trip=trip, dock=dock, start_at=start, end_at=start + dur)


def normalize_slots():
    """Dời các lịch chưa check-in đang nằm ngoài khung giờ hạ tải về khung trống hợp lệ sớm nhất.
    Lịch đã xác nhận bị dời -> quay về "Đề xuất" để Thủ kho xác nhận lại giờ mới."""
    moved = 0
    now = timezone.now()
    for slot in UnloadingSlot.objects.filter(status__in=["PROPOSED", "CONFIRMED"]).select_related("dock"):
        if in_window(slot.start_at, slot.end_at):
            continue
        others = Dock.objects.filter(warehouse_id=slot.dock.warehouse_id, is_active=True).exclude(pk=slot.dock_id)
        docks = [slot.dock] + list(others)
        dur = slot.end_at - slot.start_at
        with transaction.atomic():
            dock, start = find_free_slot(docks, max(slot.start_at, now), dur, slot.pk)
            if dock is None:
                continue
            slot.dock, slot.start_at, slot.end_at, slot.status = dock, start, start + dur, "PROPOSED"
            slot.save()
            audit("UPDATE", slot.trip, {"Lý do dời lịch hạ tải": "Lịch cũ ngoài khung giờ hạ tải – tự dời về khung hợp lệ"})
        moved += 1
    return moved


def move_slot(slot, dock, start):
    if slot.status in ("CHECKED_IN", "DONE"):
        raise BusinessError("Xe đã check-in, không đổi lịch được.")
    start = _ceil_step(start)
    end = start + (slot.end_at - slot.start_at)
    if not in_window(start, end):
        h0, h1 = slot_hours()
        raise BusinessError(f"Chỉ được xếp lịch hạ tải trong khung {h0:%H:%M}–{h1:%H:%M} "
                            f"(lịch này {timezone.localtime(start):%H:%M}–{timezone.localtime(end):%H:%M}).")
    if _overlaps(dock, start, end, slot.pk):
        raise BusinessError(f"Bến {dock.code} đã có lịch trong khung {timezone.localtime(start):%H:%M}.")
    slot.dock, slot.start_at, slot.end_at = dock, start, end
    slot.save()


def confirm_slot(slot):
    if slot.status != "PROPOSED":
        raise BusinessError("Lịch không ở trạng thái đề xuất.")
    slot.status = "CONFIRMED"
    slot.save(update_fields=["status"])


def _dur(minutes):
    from apps.core.templatetags.tracon import dur
    return dur(minutes)


def _busy_now(dock, start, end, exclude_pk=None):
    """Bến có lịch chiếm khoảng [start, end) không – bỏ qua lịch đã hủy / đã hạ xong (DONE)."""
    qs = UnloadingSlot.objects.filter(dock=dock, start_at__lt=end, end_at__gt=start) \
        .exclude(status__in=["CANCELLED", "DONE"])
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


def gate_dock(trip, slot=None, now=None):
    """STT 74: xe tới cổng -> bến trống ĐẦU TIÊN ngay bây giờ (ưu tiên bến đang xếp cho xe). Trả về
    (bến trống ngay | None, (bến, giờ) trống sớm nhất nếu đang bận hết, thời lượng hạ tải)."""
    now = (now or timezone.now()).replace(second=0, microsecond=0)
    dur = (slot.end_at - slot.start_at) if slot else slot_duration(trip)
    docks = list(Dock.objects.filter(warehouse=trip.po.warehouse, is_active=True).order_by("code"))
    if slot:
        docks.sort(key=lambda d: d.pk != slot.dock_id)
    for d in docks:
        if not _busy_now(d, now, now + dur, slot.pk if slot else None):
            return d, None, dur
    return None, find_free_slot(docks, now, dur, slot.pk if slot else None, days=2), dur


def checkin_state(slot, now=None):
    """Tình trạng check-in (màn Lịch hạ tải): late_min = muộn hơn giờ kết thúc slot; need_reason khi muộn quá
    SLOT_CHECKIN_TOLERANCE_MIN; free_now = bến trống đầu tiên ngay lúc này (xe vào luôn, không chờ tới giờ slot);
    next_free = (bến, giờ) trống sớm nhất khi mọi bến đang bận. Xe đã bấm "Xe tới cổng" (đang chờ bến) -> sớm/muộn
    tính theo giờ tới cổng, không theo lúc bến trống (STT 89)."""
    now = now or timezone.now()
    arr = slot.trip.arrived_at or now
    tol = int(param("SLOT_CHECKIN_TOLERANCE_MIN") or 60)
    early = max(0, int((slot.start_at - arr).total_seconds() // 60))
    late = max(0, int((arr - slot.end_at).total_seconds() // 60))
    free, nxt, _ = gate_dock(slot.trip, slot, now)
    return {"early_min": early, "late_min": late, "need_reason": late > tol, "tolerance": tol,
            "free_now": free, "next_free": nxt}


class GateBusy(BusinessError):
    """Mọi bến đang bận: giờ tới cổng ĐÃ được ghi, xe "Chờ ngoài cổng" (không phải lỗi thao tác)."""


def gate_arrive(trip):
    """STT 89: bấm "Xe tới cổng" ghi arrived_at NGAY (kể cả khi mọi bến bận), xe chuyển sang "Chờ ngoài cổng".
    Bấm lại khi có bến trống không ghi đè giờ tới cổng -> KPI-4 thời gian chờ = checked_in_at − arrived_at.
    Giao dịch riêng: lỗi ở bước check-in sau đó (bến bận, thiếu lý do) không làm mất giờ tới cổng."""
    with transaction.atomic():
        t = InboundTrip.objects.select_for_update().get(pk=trip.pk)
        if t.status not in ("ARRIVED", "IN_TRANSIT", "LOADED"):
            raise BusinessError("Xe chưa xuất phát / đã nhập kho – không check-in được.")
        if not t.arrived_at:
            t.arrived_at, t.status, t.is_late = timezone.now(), "ARRIVED", False
            t.save()
            resolve_alerts("MILESTONE_LATE", "inbound_trip", t.pk)
    trip.arrived_at, trip.status = t.arrived_at, t.status
    return t


def gate_checkin(trip, user, reason=""):
    """STT 74 / 49 / 89: xe tới cổng -> ghi giờ tới cổng, rồi vào bến trống đầu tiên và check-in ngay (dời lịch đã
    xếp nếu cần, tạo lịch nếu chưa có). Mọi bến đang bận -> GateBusy (giờ tới cổng vẫn lưu, xe chờ ngoài cổng).
    Tới muộn quá ngưỡng (tính theo giờ tới cổng) -> bắt nhập lý do."""
    gate_arrive(trip)
    return _gate_checkin(trip, user, reason)


@transaction.atomic
def _gate_checkin(trip, user, reason=""):
    trip = InboundTrip.objects.select_for_update().select_related("po__warehouse").get(pk=trip.pk)
    if trip.status not in ("ARRIVED", "IN_TRANSIT", "LOADED"):
        raise BusinessError("Xe chưa xuất phát / đã nhập kho – không check-in được.")
    slot = UnloadingSlot.objects.filter(trip=trip).exclude(status="CANCELLED").first()
    if slot and slot.status not in ("PROPOSED", "CONFIRMED"):
        raise BusinessError("Xe đã check-in.")
    now = timezone.now().replace(second=0, microsecond=0)
    arr = trip.arrived_at or now
    reason = (reason or "").strip()
    tol = int(param("SLOT_CHECKIN_TOLERANCE_MIN") or 60)
    if slot and (arr - slot.end_at) > timedelta(minutes=tol) and not reason:
        raise BusinessError(f"Xe tới muộn {_dur(int((arr - slot.end_at).total_seconds() // 60))} so với lịch "
                            f"{timezone.localtime(slot.start_at):%H:%M}–{timezone.localtime(slot.end_at):%H:%M}. "
                            "Vui lòng nhập lý do.")
    dock, nxt, dur = gate_dock(trip, slot, now)
    if dock is None:
        when = f" – bến trống sớm nhất {nxt[0].code} lúc {timezone.localtime(nxt[1]):%H:%M}" if nxt and nxt[0] else ""
        raise GateBusy(f"Tất cả bến đang bận{when}. Đã ghi giờ tới cổng {timezone.localtime(arr):%H:%M} – "
                       "xe chờ ngoài cổng, bấm lại khi có bến trống.")
    note = ""
    if slot is None:
        slot = UnloadingSlot.objects.create(trip=trip, dock=dock, start_at=now, end_at=now + dur, status="CONFIRMED")
        note = f"Xe tới cổng chưa có lịch – vào {dock.code} {timezone.localtime(now):%H:%M}"
    elif slot.dock_id != dock.pk or slot.start_at != now:
        old = f"{slot.dock.code} {timezone.localtime(slot.start_at):%H:%M}"
        slot.dock, slot.start_at, slot.end_at = dock, now, now + dur
        slot.save(update_fields=["dock", "start_at", "end_at"])
        note = f"Xe tới cổng – vào bến trống {dock.code} {timezone.localtime(now):%H:%M} (lịch cũ {old})"
    if note:
        audit("UPDATE", trip, {"Lịch hạ tải": note})
    return check_in(slot, user, reason=reason)


@transaction.atomic
def check_in(slot, user, reason=""):
    """Check-in xe vào bến của lịch -> mở phiếu nhập GRN nháp từ hàng đã bốc.
    Thao tác của Thủ kho đi qua gate_checkin (chọn bến trống đầu tiên, lý do khi tới muộn)."""
    if slot.status not in ("PROPOSED", "CONFIRMED"):
        raise BusinessError("Lịch đã check-in hoặc bị hủy.")
    trip = slot.trip
    if trip.status not in ("ARRIVED", "IN_TRANSIT", "LOADED"):
        raise BusinessError("Xe chưa tới cổng.")
    now = timezone.now()
    reason = (reason or "").strip()
    if not trip.arrived_at:  # check-in không qua "Xe tới cổng": coi như tới cổng lúc này (chờ 0 phút)
        trip.arrived_at = now
    slot.status, slot.checked_in_at = "CHECKED_IN", now  # chỉ ghi giờ check-in, không ghi đè giờ tới cổng (STT 89)
    if reason:
        slot.checkin_reason = reason[:255]
    slot.save()
    if reason:
        audit("UPDATE", trip, {"Lý do check-in lệch slot": reason[:255]})
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
    """STT 75: xác nhận phiếu nhập là GHI SỔ NGAY theo số thực nhận (hàng hỏng vào cách ly) – hàng đã nằm trong kho,
    không treo chờ Giám đốc. Phần thiếu / hỏng tự lập khiếu nại; Giám đốc duyệt khiếu nại (đòi ai, bao nhiêu)."""
    # bấm 2 lần / 2 request song song: khóa dòng, đọc trạng thái thật -> chỉ ghi sổ 1 lần
    gr.status = GoodsReceipt.objects.select_for_update().values_list("status", flat=True).get(pk=gr.pk)
    if gr.status != "DRAFT":
        raise BusinessError("Phiếu nhập không ở trạng thái nháp.")
    post_grn(gr, user, "CLOSE")
    return True


SHORTAGE_ACTIONS = {"CLOSE": "Khiếu nại & đóng phần thiếu trên PO", "BACKFILL": "NCC giao bù (giữ PO mở)"}


@transaction.atomic
def approve_grn(gr, user, shortage_action="CLOSE"):
    """Giám đốc duyệt chênh lệch: chọn NCC giao bù (PO còn "đang về" phần thiếu, không khiếu nại) hay khiếu nại và
    đóng phần thiếu trên dòng PO (STT 61)."""
    gr.status = GoodsReceipt.objects.select_for_update().values_list("status", flat=True).get(pk=gr.pk)
    if gr.status != "PENDING_APPROVAL":
        raise BusinessError("Phiếu không chờ duyệt.")
    if shortage_action not in SHORTAGE_ACTIONS:
        raise BusinessError("Chọn cách xử lý phần thiếu.")
    gr.approved_by = user
    post_grn(gr, user, shortage_action)
    resolve_alerts("GRN_VARIANCE", "grn", gr.pk)
    audit("APPROVE", gr, {"Xử lý phần thiếu": SHORTAGE_ACTIONS[shortage_action]})


def post_grn(gr, user, shortage_action="CLOSE"):
    """Nhập kho POSTED: cộng tồn thực, tăng SL đã nhận của PO (incoming giảm đúng bằng lượng nhập), giữ hàng cho
    backorder theo thứ tự ưu tiên, rồi sau commit gọi check_replenishment(GRN_POSTED). Hàng vào vị trí cách ly không
    tính vào tồn thực nhưng vẫn tính là đã nhận của PO (T15).

    Khiếu nại (STT 58): thiếu = dự kiến − thực nhận (đòi nhà xe); hỏng = SL hỏng (đòi NCC) – không cộng hỏng vào thiếu.
    shortage_action="CLOSE": phần thiếu + hỏng đã khiếu nại được ĐÓNG trên dòng PO (không còn "Đang về");
    "BACKFILL": NCC giao bù -> không lập khiếu nại, PO vẫn chờ nhận phần còn lại."""
    from apps.inventory.replenishment import Trigger, fire_checks
    from apps.sales.services import retry_backorders

    quarantine = Location.objects.filter(warehouse=gr.warehouse, loc_type="QUARANTINE", is_active=True).first()
    now = timezone.now()
    claims = []  # (loại, đối tượng, sản phẩm, SL, đơn giá)
    for line in gr.lines.select_related("po_line"):  # G4: chặn nhập vượt SL còn lại của PO
        pol = PurchaseOrderLine.objects.select_for_update().get(pk=line.po_line_id)
        if line.qty_good > pol.qty_open:
            raise BusinessError(f"{line.product.code}: nhập {line.qty_good:g} vượt SL còn lại của PO "
                                f"({pol.qty_open:g}). Thu mua cần sửa PO trước.")
    for line in gr.lines.select_related("po_line", "product", "location"):
        if line.qty_good > 0:
            post_stock_move(line.product, line.location, line.qty_good, "IN_PURCHASE", "GRN", gr.pk,
                            unit_cost=line.unit_cost, note=gr.code, at=now)
        if line.qty_damaged > 0 and quarantine:
            post_stock_move(line.product, quarantine, line.qty_damaged, "IN_PURCHASE", "GRN", gr.pk,
                            unit_cost=line.unit_cost, note=f"{gr.code} – hàng lỗi cách ly", at=now)
        short = max(D0, line.qty_expected - line.qty_received)  # thực nhận đã gồm cả hàng hỏng
        pol = PurchaseOrderLine.objects.get(pk=line.po_line_id)
        pol.qty_received += line.qty_good
        if shortage_action == "CLOSE":
            if short > 0:
                claims.append(("SHORTAGE", "CARRIER", line.product, short, line.unit_cost))
            if line.qty_damaged > 0:
                claims.append(("DAMAGE", "SUPPLIER", line.product, line.qty_damaged, line.unit_cost))
            close = min(short + line.qty_damaged, max(D0, pol.qty - pol.qty_received - pol.qty_closed))
            pol.qty_closed += close
        pol.save(update_fields=["qty_received", "qty_closed"])

    po = gr.po
    po.status = "RECEIVED" if all(l.qty_received + l.qty_closed >= l.qty for l in po.lines.all()) else "PARTIAL"
    po.save(update_fields=["status", "updated_at"])

    start = po.sent_at or po.approved_at or po.created_at
    gr.lead_time_days = Decimal(f"{(now - start).total_seconds() / 86400:.2f}")
    # lead time ngắn bất thường (vd. chuyến test bấm liền các mốc: 0,01 ngày) -> tự loại khỏi thống kê SS/ROP
    lt_min = Decimal(str(param("LEADTIME_MIN_DAYS") or 0))
    if gr.lead_time_days < lt_min and not gr.exclude_from_stats:
        gr.exclude_from_stats = True
        gr.exclude_reason = f"Tự động: lead time {gr.lead_time_days:g} ngày < tối thiểu {lt_min:g} ngày"
    gr.status, gr.posted_at = "POSTED", now
    gr.save()

    if gr.trip_id:
        trip = gr.trip
        trip.status = "RECEIVED"
        trip.save(update_fields=["status", "updated_at"])
        UnloadingSlot.objects.filter(trip=trip).update(status="DONE", done_at=now)
    for ctype, against, prod, q, cost in claims:
        Claim.objects.create(code=next_doc_no("CL"), gr=gr, claim_type=ctype, against=against, product=prod, qty=q,
                             amount=(q * cost).quantize(Decimal(1)),
                             description=(f"Thiếu {q:g} {prod.base_uom.code} {prod.code} khi kiểm nhận (dự kiến − thực nhận)"
                                          if ctype == "SHORTAGE" else
                                          f"Hư hỏng {q:g} {prod.base_uom.code} {prod.code} – đưa vào khu cách ly"))
    if claims:
        raise_alert("CLAIM_OPEN", f"{gr.code}: {len(claims)} khiếu nại chờ Giám đốc duyệt", ref_type="grn", ref_id=gr.pk,
                    url="/app/warehouse/claims?status=OPEN", target_role="DIRECTOR")
    audit("POST", gr)
    trig = Trigger("GRN_POSTED", "GRN", gr.code, user)
    pids = set(gr.lines.values_list("product_id", flat=True))
    for pid in sorted(pids):
        retry_backorders(pid, trig)
    fire_checks(pids, trig)


# ------------------------------------------------------------------ Khiếu nại (STT 59)
CLAIM_FLOW = {"approve": ("OPEN",), "backfill": ("OPEN",), "send": ("APPROVED",),
              "compensate": ("APPROVED", "SENT"), "close": ("APPROVED", "SENT", "COMPENSATED"),
              "cancel": ("OPEN", "APPROVED", "SENT")}
DIRECTOR_OPS = ("approve", "backfill")


@transaction.atomic
def claim_action(claim, user, op, amount=None, note="", against=None):
    """Chờ duyệt -> (Giám đốc) Đã duyệt -> Đã gửi -> Đã bồi thường -> Đóng.
    Giám đốc duyệt: chốt đòi ai (NCC / nhà xe) và bao nhiêu; hoặc "NCC giao bù": hủy khiếu nại, mở lại phần thiếu
    trên PO (lại thành "Đang về"). Bồi thường: lập chứng từ giảm trừ công nợ phải trả của NCC / nhà xe."""
    from apps.accounting.models import Bill

    claim = Claim.objects.select_for_update().select_related("gr__po__supplier", "gr__trip__carrier").get(pk=claim.pk)
    if op not in CLAIM_FLOW:
        raise BusinessError("Thao tác không hợp lệ.")
    if op in DIRECTOR_OPS and user.role not in ("DIRECTOR", "ADMIN"):
        raise BusinessError("Giám đốc duyệt khiếu nại.")
    if claim.status not in CLAIM_FLOW[op]:
        raise BusinessError(f"Khiếu nại đang ở trạng thái {claim.get_status_display()} – không thực hiện được.")
    now = timezone.now()
    note = (note or "").strip()
    if note:
        claim.note = (claim.note + "\n" if claim.note else "") + f"{timezone.localtime(now):%d/%m %H:%M} {user}: {note}"
    if op == "approve":
        if against in ("SUPPLIER", "CARRIER"):
            claim.against = against
        if amount not in (None, ""):
            amt = Decimal(amount)
            if amt <= 0:
                raise BusinessError("Số tiền khiếu nại phải lớn hơn 0.")
            claim.amount = amt
        claim.status = "APPROVED"
    elif op == "backfill":  # NCC giao bù: không khiếu nại, PO lại chờ nhận phần này
        gl = claim.gr.lines.filter(product=claim.product).select_related("po_line").first() if claim.product_id else None
        if gl and claim.qty:
            pol = PurchaseOrderLine.objects.select_for_update().get(pk=gl.po_line_id)
            pol.qty_closed = max(D0, pol.qty_closed - claim.qty)
            pol.save(update_fields=["qty_closed"])
            po = pol.po
            if po.status == "RECEIVED" and any(l.qty_received + l.qty_closed < l.qty for l in po.lines.all()):
                po.status = "PARTIAL"
                po.save(update_fields=["status", "updated_at"])
            from apps.inventory.replenishment import Trigger, fire_checks
            fire_checks([pol.product_id], Trigger("PO_APPROVED", "PO", po.code, user))
        claim.status, claim.closed_at = "CANCELLED", now
        claim.note = (claim.note + "\n" if claim.note else "") + "NCC giao bù – mở lại phần thiếu trên PO."
    elif op == "send":
        claim.status, claim.sent_at = "SENT", now
    elif op == "compensate":
        amt = Decimal(amount if amount not in (None, "") else claim.amount)
        if amt <= 0:
            raise BusinessError("Số tiền bồi thường phải lớn hơn 0.")
        partner = claim.partner
        if partner is None:
            raise BusinessError("Chuyến dùng xe nội bộ – không có nhà xe để trừ công nợ. Đóng khiếu nại kèm ghi chú.")
        today = timezone.localdate()
        kind = partner._meta.model_name
        bill = Bill.objects.create(code=next_doc_no("BILL"), bill_type="CLAIM_CREDIT", **{kind: partner},
                                   vendor_invoice_no=claim.code, bill_date=today, due_date=today, status="POSTED",
                                   subtotal=-amt, vat_amount=0, total=-amt)
        claim.status, claim.compensated_amount, claim.compensated_at, claim.credit_bill = "COMPENSATED", amt, now, bill
        audit("CREATE", bill, {"Giảm trừ công nợ theo khiếu nại": claim.code, "amount": str(amt)})
    elif op == "close":
        claim.status, claim.closed_at = "CLOSED", now
    elif op == "cancel":
        if not note:
            raise BusinessError("Nhập lý do hủy khiếu nại.")
        claim.status, claim.closed_at = "CANCELLED", now
    claim.save()
    audit("APPROVE" if op == "approve" else "UPDATE", claim, {"Thao tác": op, "Trạng thái": claim.get_status_display(),
                                                             "Đòi": claim.get_against_display(), "Số tiền": str(claim.amount)})
    if not Claim.objects.filter(gr=claim.gr, status="OPEN").exists():
        resolve_alerts("CLAIM_OPEN", "grn", claim.gr_id)
    return claim
