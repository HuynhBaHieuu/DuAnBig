"""Móc xích QT2 (xử lý đơn) → QT1 (bổ sung tồn) – đặc tả test/logic.docx.

MỌI logic bổ sung tồn nằm ở đây, không nhân bản ở view / signal / tác vụ đêm / template:
  - stock_metrics()        : on_hand, reserved, incoming, demand_unreserved, projected, pipeline, position (mục 2)
  - effective_params()     : SS, ROP, Q, truck_qty, LT đang hiệu lực
  - calc_order_qty()       : SL PR = max(Q; ROP + Q − position), làm tròn theo xe (dùng cho cả "Lượng PR gợi ý")
  - check_replenishment()  : hàm lõi (mục 3) – cảnh báo theo projected, quyết định tạo PR theo position
  - fire_checks()          : gọi hàm lõi SAU KHI giao dịch nghiệp vụ commit, đồng bộ trong cùng request (mục 4)
  - allocate_backorders()  : hàng về / nhả hàng -> giữ cho các dòng đang thiếu theo thứ tự ưu tiên (mục 5)

Ánh xạ mã trạng thái đặc tả -> code (giả định G6):
  Đơn bán  CONFIRMED / PARTIALLY_DELIVERED  -> CONFIRMED / PARTIAL
  PO       WAITING_APPROVAL                 -> PENDING_APPROVAL
           APPROVED, SENT, CONFIRMED        -> APPROVED, SENT (code không có bước "NCC xác nhận")
           PARTIALLY_RECEIVED               -> PARTIAL
  PR       DRAFT, SUBMITTED, CONVERTED, REJECTED, CANCELLED (giống đặc tả)
  Đơn bán: backorder_qty là thuộc tính tính từ qty − reserved_qty − delivered_qty (không lưu cột riêng).
"""
import logging
import math
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

D0 = Decimal(0)
log = logging.getLogger(__name__)

EVENT_LABELS = {
    "SO_CONFIRMED": "Xác nhận đơn bán", "SO_APPROVED": "Giám đốc duyệt đơn", "SO_EDITED": "Sửa đơn đã xác nhận",
    "SO_CANCELLED": "Hủy đơn bán", "ALLOCATION_APPROVED": "Duyệt phân bổ", "ISSUE_POSTED": "Xuất kho",
    "STOCK_ADJUSTED": "Điều chỉnh tồn", "COUNT_APPROVED": "Duyệt kiểm kê", "PO_APPROVED": "Duyệt PO",
    "PO_CANCELLED": "Hủy PO", "PO_CLOSED": "Đóng PO", "PO_RETURNED": "Trả lại PO", "GRN_POSTED": "Nhập kho",
    "PARAM_SAVED": "Lưu tham số SS/ROP", "NIGHTLY_JOB": "Tác vụ đêm",
}
# Loại chứng từ -> đường dẫn mở chứng từ gây ra PR
REF_URLS = {"SO": ("sales.SalesOrder", "/app/sales/orders/{}"), "PO": ("purchasing.PurchaseOrder", "/app/purchase/orders/{}"),
            "GRN": ("purchasing.GoodsReceipt", "/app/warehouse/receipts/{}"), "GIN": ("sales.GoodsIssue", None),
            "IC": ("inventory.InventoryCount", "/app/inventory/counts/{}"), "AL": ("sales.Allocation", "/app/sales/allocations/{}")}

SO_OPEN = ("CONFIRMED", "PARTIAL")
PO_INCOMING = ("APPROVED", "SENT", "PARTIAL")        # đã duyệt: nằm trong "Đang về"
PO_PIPELINE = ("DRAFT", "PENDING_APPROVAL")         # đã đề xuất nhưng chưa duyệt: nằm trong pipeline
PR_PIPELINE = ("DRAFT", "SUBMITTED")
REJECT_WINDOW = timedelta(hours=24)


@dataclass
class Trigger:
    event: str
    ref_type: str = ""
    ref_code: str = "–"
    user: Any = None

    @property
    def label(self):
        return EVENT_LABELS.get(self.event, self.event)

    @property
    def user_obj(self):
        u = self.user
        return u if (u is not None and getattr(u, "is_authenticated", False)) else None


def event_label(code):
    return EVENT_LABELS.get(code, code or "")


def ref_url(ref_type, code):
    """Đường dẫn tới chứng từ gây ra PR (để bấm vào mở)."""
    from django.apps import apps
    if ref_type not in REF_URLS or not code or code == "–":
        return None
    label, url = REF_URLS[ref_type]
    if not url:
        return None
    obj = apps.get_model(label).objects.filter(code=code).only("pk").first()
    return url.format(obj.pk) if obj else None


# ------------------------------------------------------------------ Tham số hiệu lực
@dataclass
class EffectiveParams:
    SS: Decimal
    ROP: Decimal
    Q: Decimal
    truck_qty: Decimal
    round_to_truck: bool
    LT: Decimal


def get_param(product):
    from apps.core.services import param
    from .models import ReplenishmentParam
    rp, _ = ReplenishmentParam.objects.get_or_create(product=product, defaults={"z": param("SERVICE_Z")})
    return rp


def effective_params(product, rp=None):
    """SS, ROP = số khóa tay nếu bật Khóa thủ công, ngược lại số tự tính gần nhất.
    Q = Q nhập tay, ngược lại max(MOQ; D̄ × chu kỳ). LT = lead time đang lưu (calc_param chỉ dùng thực tế khi ≥ 3 GRN)."""
    rp = rp or get_param(product)
    return EffectiveParams(SS=Decimal(rp.eff_ss), ROP=Decimal(rp.eff_rop), Q=Decimal(rp.order_q),
                           truck_qty=Decimal(rp.truck_qty or 0), round_to_truck=bool(rp.round_by_truck),
                           LT=Decimal(rp.lead_time_days or 0))


def calc_order_qty(p, pos):
    """(raw, qty): raw = max(Q; ROP + Q − position); qty làm tròn lên theo xe nếu bật. Dùng chung PR tự động & gợi ý."""
    pos = Decimal(pos)
    raw = max(p.Q, p.ROP + p.Q - pos, D0)
    qty = raw
    if p.round_to_truck and p.truck_qty > 0 and raw > 0:
        qty = Decimal(math.ceil(raw / p.truck_qty)) * p.truck_qty
    return raw.quantize(Decimal("0.001")), Decimal(qty).quantize(Decimal("0.001"))


# ------------------------------------------------------------------ Số liệu (mục 2) – một công thức cho mọi nơi
def stock_metrics(products, today=None):
    """{product_id: dict} với on_hand, quarantine, reserved, available, incoming, demand_unreserved, projected,
    pipeline, position.

    projected = on_hand + incoming − reserved − demand_unreserved ;  position = projected + pipeline."""
    from apps.purchasing.models import PurchaseOrderLine, PurchaseRequisitionLine
    from apps.sales.models import SalesOrderLine, StockReservation

    from .models import ReplenishmentParam, StockQuant

    today = today or timezone.localdate()
    products = list(products)
    ids = [p.pk for p in products]

    def agg(qs, expr):
        return {k: v or D0 for k, v in qs.values_list("product_id").annotate(s=expr)}

    on_hand = agg(StockQuant.objects.filter(product_id__in=ids).exclude(location__loc_type="QUARANTINE"), Sum("on_hand"))
    quarantine = agg(StockQuant.objects.filter(product_id__in=ids, location__loc_type="QUARANTINE"), Sum("on_hand"))
    reserved = agg(StockReservation.objects.filter(product_id__in=ids, status="ACTIVE",
                                                   so_line__so__status__in=SO_OPEN), Sum("qty"))
    incoming = agg(PurchaseOrderLine.objects.filter(product_id__in=ids, po__status__in=PO_INCOMING,
                                                    qty__gt=F("qty_received")), Sum(F("qty") - F("qty_received")))
    # một dòng PR đã gộp vào PO (po_line khác rỗng) chỉ được tính ở PO – không đếm hai lần
    pr_pipe = agg(PurchaseRequisitionLine.objects.filter(product_id__in=ids, pr__status__in=PR_PIPELINE,
                                                         po_line__isnull=True), Sum("qty"))
    po_pipe = agg(PurchaseOrderLine.objects.filter(product_id__in=ids, po__status__in=PO_PIPELINE), Sum("qty"))
    lt = {r.product_id: r.lead_time_days for r in ReplenishmentParam.objects.filter(product_id__in=ids)}
    unres = {}
    for l in SalesOrderLine.objects.filter(product_id__in=ids, so__status__in=SO_OPEN) \
            .values("product_id", "qty", "reserved_qty", "delivered_qty", "so__requested_date"):
        horizon = today + timedelta(days=math.ceil(float(lt.get(l["product_id"]) or 0)))
        if l["so__requested_date"] <= horizon:  # backorder cần sau hôm nay + LT chưa tính vào dự báo (G7)
            bo = max(l["qty"] - l["reserved_qty"] - l["delivered_qty"], D0)
            unres[l["product_id"]] = unres.get(l["product_id"], D0) + bo
    out = {}
    for p in products:
        oh, rs, inc = on_hand.get(p.pk, D0), reserved.get(p.pk, D0), incoming.get(p.pk, D0)
        du = unres.get(p.pk, D0)
        pipe = pr_pipe.get(p.pk, D0) + po_pipe.get(p.pk, D0)
        proj = oh + inc - rs - du
        out[p.pk] = {"on_hand": oh, "quarantine": quarantine.get(p.pk, D0), "reserved": rs, "available": oh - rs,
                     "incoming": inc, "demand_unreserved": du, "projected": proj, "pipeline": pipe,
                     "position": proj + pipe}
    return out


# ------------------------------------------------------------------ Cảnh báo HIT_ROP & thông báo
def open_alert_once(product, trigger, projected):
    from apps.core.services import raise_alert
    from .models import ReplenishmentAlert
    if not ReplenishmentAlert.objects.filter(product=product, alert_type="HIT_ROP", closed_at__isnull=True).exists():
        ReplenishmentAlert.objects.create(product=product, alert_type="HIT_ROP", opened_at=timezone.now(),
                                          opened_event=trigger.event, opened_ref=trigger.ref_code or "",
                                          opened_projected=projected)
    raise_alert("HIT_ROP", f"{product.code} chạm điểm đặt hàng lại", f"Tồn dự báo {projected:g} – do {trigger.label} "
                f"{trigger.ref_code}", ref_type="product", ref_id=product.pk,
                url=f"/app/inventory/replenishment?product={product.pk}", target_role="PURCHASER")


def close_alert(product, trigger):
    from apps.core.services import resolve_alerts
    from .models import ReplenishmentAlert
    ReplenishmentAlert.objects.filter(product=product, alert_type="HIT_ROP", closed_at__isnull=True).update(
        closed_at=timezone.now(), closed_event=trigger.event, closed_ref=trigger.ref_code or "")
    resolve_alerts("HIT_ROP", "product", product.pk)
    resolve_alerts("BELOW_ROP", "product", product.pk)  # cảnh báo kiểu cũ (trước khi có HIT_ROP)


def notify(role, title, message="", url="", ref_type="", ref_id=None, level="INFO"):
    """Thông báo cho một vai trò (bảng Alert dùng chung). Không bao giờ chứa giá."""
    from apps.core.models import Alert
    Alert.objects.create(alert_type="NOTIFY", level=level, title=title[:200], message=message, url=url,
                         ref_type=ref_type, ref_id=ref_id, target_role=role)


def _log(product, trigger, result, proj=None, pos=None, rop=None, pr=None, note=""):
    from .models import ReplenishmentCheckLog
    return ReplenishmentCheckLog.objects.create(product=product, event=trigger.event, ref=trigger.ref_code or "",
                                                user=trigger.user_obj, projected=proj, position=pos, rop=rop,
                                                result=result, pr=pr, note=note)


def _f(v):
    return float(Decimal(v).quantize(Decimal("0.001")))


# ------------------------------------------------------------------ Hàm lõi (mục 3)
def check_replenishment(product_id, trigger):
    """Kiểm tra 1 vật tư sau một sự kiện. Trả về PR vừa tạo hoặc None.

    Chạy với quyền hệ thống (không kiểm quyền người gọi – quyền đã kiểm ở view nghiệp vụ).
    Idempotent: gọi lại với cùng dữ liệu không tạo thêm PR vì lần sau pipeline đã chứa PR vừa tạo."""
    from apps.core.services import audit, next_doc_no
    from apps.masterdata.models import Product
    from apps.purchasing.models import PurchaseRequisition, PurchaseRequisitionLine

    with transaction.atomic():
        product = Product.objects.select_for_update().get(pk=product_id)  # khóa: 2 request không tạo 2 PR
        if not product.track_rop or not product.is_active:
            return None
        p = effective_params(product)
        m = stock_metrics([product])[product.pk]
        proj, pos = m["projected"], m["position"]

        # 1. Cảnh báo theo tồn dự báo
        if proj > p.ROP:
            close_alert(product, trigger)
            _log(product, trigger, "CHUA_CHAM", proj, pos, p.ROP)
            return None
        open_alert_once(product, trigger, proj)

        # 2. Chống trùng theo position (không dùng kiểu "có PR / PO mở là chặn")
        if pos > p.ROP:
            _log(product, trigger, "DA_DU_DE_XUAT", proj, pos, p.ROP)
            return None

        # 2b. Thu mua vừa từ chối PR tự động: trong 24h chỉ tạo lại khi có nhu cầu mới (position giảm thêm)
        rej = PurchaseRequisition.objects.filter(
            source="AUTO", status="REJECTED", lines__product=product,
            rejected_at__gte=timezone.now() - REJECT_WINDOW).order_by("-rejected_at").first()
        if rej and rej.rejected_position is not None and pos >= rej.rejected_position:
            _log(product, trigger, "DA_TU_CHOI_GAN_DAY", proj, pos, p.ROP, note=f"Từ chối {rej.code}")
            return None

        # 3. SL cho phần còn thiếu – luôn tính từ position nên PR bổ sung chỉ bù phần thiếu
        raw, qty = calc_order_qty(p, pos)
        if qty <= 0:
            _log(product, trigger, "KHONG_CAN", proj, pos, p.ROP)
            return None
        supplier = product.default_supplier
        now = timezone.now()
        pr = PurchaseRequisition.objects.create(
            code=next_doc_no("PR"), source="AUTO", status="SUBMITTED",
            urgency="URGENT" if m["available"] < p.SS else "NORMAL",
            need_date=timezone.localdate() + timedelta(days=math.ceil(float(p.LT))),
            requested_by=None, missing_supplier=supplier is None,
            trigger_event=trigger.event, trigger_ref_type=trigger.ref_type, trigger_ref_code=trigger.ref_code or "",
            trigger_user=trigger.user_obj, trigger_at=now,
            note=f"Tự động (ROP) – do {trigger.label} {trigger.ref_code}"[:255],
            snapshot={"on_hand": _f(m["on_hand"]), "reserved": _f(m["reserved"]), "incoming": _f(m["incoming"]),
                      "demand_unreserved": _f(m["demand_unreserved"]), "pipeline": _f(m["pipeline"]),
                      "available": _f(m["available"]), "projected": _f(proj), "position": _f(pos),
                      "SS": _f(p.SS), "ROP": _f(p.ROP), "Q": _f(p.Q), "truck_qty": _f(p.truck_qty),
                      "round_to_truck": p.round_to_truck, "raw": _f(raw), "qty": _f(qty)})
        PurchaseRequisitionLine.objects.create(pr=pr, product=product, qty=qty, suggested_supplier=supplier)
        _log(product, trigger, "TAO_PR", proj, pos, p.ROP, pr=pr)
        audit("CREATE", pr, {"source": "AUTO", "event": trigger.event, "ref": trigger.ref_code, "qty": str(qty)},
              user=trigger.user_obj)
        uom = product.base_uom.code
        notify("PURCHASER", f"{pr.code}: {product.code} {qty:g} {uom} – do {trigger.label} {trigger.ref_code}",
               f"Tồn dự báo {proj:g} ≤ ROP {p.ROP:g}. Đề xuất mua tự động cần lập PO.",
               url=f"/app/purchase/requisitions/{pr.pk}", ref_type="pr", ref_id=pr.pk, level="WARN")
        if supplier is None:
            notify("PURCHASER", f"{pr.code}: {product.code} chưa có NCC mặc định",
                   "PR tự động đã tạo nhưng thiếu NCC – chọn NCC trước khi lập PO.",
                   url=f"/app/purchase/requisitions/{pr.pk}", ref_type="pr", ref_id=pr.pk, level="WARN")
        return pr


def safe_check(product_id, trigger):
    """Lỗi của hàm lõi không được làm hỏng nghiệp vụ đã commit: ghi log LỖI, báo quản trị; tác vụ đêm sẽ quét bù."""
    try:
        return check_replenishment(product_id, trigger)
    except Exception as e:  # noqa: BLE001 – cố ý nuốt lỗi sau commit
        log.exception("check_replenishment lỗi: product=%s trigger=%s", product_id, trigger)
        try:
            from apps.masterdata.models import Product
            product = Product.objects.get(pk=product_id)
            _log(product, trigger, "LOI", note=f"{type(e).__name__}: {e}"[:2000])
            notify("ADMIN", f"Lỗi kiểm tra bổ sung tồn {product.code}", f"{trigger.label} {trigger.ref_code}: {e}"[:500],
                   level="CRIT")
        except Exception:  # noqa: BLE001
            log.exception("Không ghi được log lỗi check_replenishment")
        return None


def fire_checks(product_ids, trigger):
    """Gọi ở mọi điểm kích hoạt (mục 4): chạy hàm lõi cho từng vật tư SAU KHI giao dịch commit, đồng bộ trong request.

    Theo product_id tăng dần, mỗi vật tư một giao dịch riêng -> 2 request đồng thời không khóa chéo nhau.
    Giao dịch nghiệp vụ bị rollback thì Django bỏ các callback này (không PR, không cảnh báo, không log)."""
    for pid in sorted(set(product_ids)):
        transaction.on_commit(lambda pid=pid: safe_check(pid, trigger), robust=True)


# ------------------------------------------------------------------ QT1 -> QT2: hàng về / nhả hàng (mục 5)
def allocate_backorders(product, trigger):
    """Giữ hàng khả dụng cho các dòng đơn đang thiếu: ngày cần sớm trước -> ưu tiên công trình cao trước
    (Site.priority: 1 = cao nhất) -> xác nhận sớm trước. Dòng được giữ thêm -> báo Điều phối. Chạy trong giao dịch
    của phiếu nhập / hủy đơn."""
    from apps.masterdata.models import Product
    from apps.sales.models import SalesOrderLine
    from apps.sales.services import _reserve, available_qty, so_log

    Product.objects.select_for_update().get(pk=product.pk)
    lines = list(SalesOrderLine.objects.filter(product=product, so__status__in=SO_OPEN,
                                               atp_status__in=["BACKORDER", "SHORT"])
                 .select_related("so__site").order_by("so__requested_date", "so__site__priority",
                                                     F("so__confirmed_at").asc(nulls_last=True), "so_id", "id"))
    avail = max(available_qty(product), D0)
    done = []
    for line in lines:
        need = line.qty - line.reserved_qty - line.delivered_qty
        if avail <= 0:
            break
        if need <= 0:
            continue
        take = min(need, avail)
        _reserve(line, take)
        avail -= take
        line.atp_status = "OK" if take >= need else "BACKORDER"
        line.save(update_fields=["atp_status"])
        so_log(line.so, "RESERVED", trigger.user_obj, {"product": product.code, "qty": float(take),
                                                       "event": trigger.event, "ref": trigger.ref_code})
        done.append((line, take))
        tail = "đủ hàng" if line.atp_status == "OK" else f"giữ thêm {take:g}, còn thiếu {need - take:g}"
        notify("DISPATCHER", f"{line.so.code} đã có hàng", f"{product.code}: {tail} {product.base_uom.code} "
               f"(do {trigger.label} {trigger.ref_code}).", url=f"/app/sales/orders/{line.so_id}", ref_type="so",
               ref_id=line.so_id)
    return done
