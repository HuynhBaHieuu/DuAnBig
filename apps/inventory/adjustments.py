"""Điều chỉnh tồn (phiếu ADJ) – thêm / sửa / xoá tồn thực tại vị trí.

Test Gà STT 34: Thủ kho cộng / trừ tồn ghi sổ ngay, bỏ qua được cả quy trình kiểm kê. Nay:
  - Lý do bắt buộc, tối thiểu ADJ_REASON_MIN_LEN ký tự (không chấp nhận "x").
  - Giá trị phiếu (Σ |SL lệch| × đơn giá vốn) ≥ ADJ_APPROVAL_THRESHOLD và người lập không phải Giám đốc / Admin
    -> phiếu ở trạng thái "Chờ duyệt", CHƯA ghi sổ; chỉ ghi sổ khi Giám đốc duyệt.
Test Gà STT 60: điều chỉnh / kiểm kê làm tồn thực < đã giữ -> tự bỏ giữ phần thiếu (đơn ưu tiên thấp nhất trước) và
đưa sang phân bổ thiếu hàng, để Khả dụng không âm.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from apps.core.services import BusinessError, audit, next_doc_no, param, raise_alert, resolve_alerts

from .models import StockAdjustment, StockAdjustmentLine, StockQuant
from .services import post_stock_move

D0 = Decimal(0)
APPROVERS = ("DIRECTOR", "ADMIN")


def check_reason(reason):
    reason = (reason or "").strip()
    n = int(param("ADJ_REASON_MIN_LEN") or 10)
    if len(reason) < n:
        raise BusinessError(f"Lý do điều chỉnh tồn phải ghi rõ, tối thiểu {n} ký tự "
                            "(vd: \"Kiểm đếm cuối ca phát hiện thừa 2 bao\").")
    return reason


def _sellable(product):
    return StockQuant.objects.filter(product=product).exclude(location__loc_type="QUARANTINE") \
        .aggregate(s=Sum("on_hand"))["s"] or D0


def _held(product):
    from apps.sales.models import StockReservation
    return StockReservation.objects.filter(product=product, status="ACTIVE",
                                           so_line__so__status__in=["CONFIRMED", "PARTIAL"]) \
        .aggregate(s=Sum("qty"))["s"] or D0


@transaction.atomic
def create_adjustment(user, reason, lines, label="Điều chỉnh tồn"):
    """lines: [(product, location, qty_diff, unit_cost)]. Trả về phiếu ADJ (POSTED hoặc PENDING_APPROVAL)."""
    reason = check_reason(reason)
    lines = [(p, loc, Decimal(d), Decimal(c if c is not None else (p.avg_cost or p.standard_cost or 0)))
             for p, loc, d, c in lines if Decimal(d) != 0]
    if not lines:
        raise BusinessError("Không có chênh lệch để điều chỉnh.")
    for p, loc, d, _ in lines:
        if d < 0:
            on_hand = StockQuant.objects.filter(product=p, location=loc).values_list("on_hand", flat=True).first() or D0
            if on_hand + d < 0:
                raise BusinessError(f"Không đủ tồn: {p.code} tại {loc.code} còn {on_hand:g}.")
    value = sum((abs(d) * c for _, _, d, c in lines), D0)
    need_approval = user.role not in APPROVERS and value >= Decimal(param("ADJ_APPROVAL_THRESHOLD") or 0)
    adj = StockAdjustment.objects.create(code=next_doc_no("ADJ"), reason=f"{label}: {reason}"[:255],
                                         requested_by=user, status="PENDING_APPROVAL" if need_approval else "DRAFT")
    for p, loc, d, c in lines:
        StockAdjustmentLine.objects.create(adjustment=adj, product=p, location=loc, qty_diff=d, unit_cost=c)
    audit("CREATE", adj, {"label": label, "reason": reason, "value": str(value),
                          "lines": [f"{p.code}@{loc.code} {d:+g}" for p, loc, d, _ in lines]})
    if need_approval:
        raise_alert("ADJ_APPROVAL", f"Điều chỉnh tồn {adj.code} chờ duyệt",
                    f"{label} – giá trị {value:,.0f} ₫ – {user}: {reason}"[:250], ref_type="stock_adjustment",
                    ref_id=adj.pk, url=f"/app/inventory/adjustments/{adj.pk}", target_role="DIRECTOR")
        return adj
    post_adjustment(adj, user)
    return adj


@transaction.atomic
def post_adjustment(adj, user):
    """Ghi sổ phiếu ADJ (tự động khi dưới ngưỡng, hoặc khi Giám đốc duyệt)."""
    from apps.masterdata.models import Product
    from apps.sales.services import retry_backorders

    from .replenishment import Trigger, fire_checks

    adj = StockAdjustment.objects.select_for_update().get(pk=adj.pk)
    if adj.status not in ("DRAFT", "PENDING_APPROVAL"):
        raise BusinessError("Phiếu điều chỉnh đã được xử lý.")
    lines = list(adj.lines.select_related("product__base_uom", "location"))
    for l in lines:  # giảm tồn khu bán được không được thấp hơn lượng đang giữ cho đơn bán
        if l.qty_diff < 0 and l.location.loc_type != "QUARANTINE":
            sellable, held = _sellable(l.product), _held(l.product)
            if sellable + l.qty_diff < held:
                raise BusinessError(f"Không giảm được: {l.product.code} đang giữ {held:g} {l.product.base_uom.code} "
                                    f"cho đơn bán, sau điều chỉnh tồn thực chỉ còn {sellable + l.qty_diff:g}. "
                                    "Hủy / giảm đơn trước, hoặc dùng kiểm kê.")
    for l in lines:
        if l.qty_diff > 0:  # giá vốn bình quân gia quyền như nhập mua
            prod = Product.objects.select_for_update().get(pk=l.product_id)
            total = StockQuant.objects.filter(product=prod).aggregate(s=Sum("on_hand"))["s"] or D0
            if total + l.qty_diff > 0 and l.unit_cost != prod.avg_cost:
                prod.avg_cost = ((total * prod.avg_cost + l.qty_diff * l.unit_cost) / (total + l.qty_diff)) \
                    .quantize(Decimal(1))
                prod.save(update_fields=["avg_cost", "updated_at"])
        post_stock_move(l.product, l.location, l.qty_diff, "ADJ", "ADJ", adj.pk, unit_cost=l.unit_cost,
                        note=f"{adj.code} – {adj.reason}"[:255])
    adj.status, adj.approved_by, adj.approved_at = "POSTED", user, timezone.now()
    adj.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    resolve_alerts("ADJ_APPROVAL", "stock_adjustment", adj.pk)
    if adj.requested_by_id and adj.requested_by_id != getattr(user, "pk", None):
        audit("APPROVE", adj)
    trig = Trigger("STOCK_ADJUSTED", "ADJ", adj.code, user)
    for pid in {l.product_id for l in lines if l.qty_diff > 0}:
        retry_backorders(pid, trig)
    fire_checks([l.product_id for l in lines], trig)
    return adj


@transaction.atomic
def reject_adjustment(adj, user, reason):
    adj = StockAdjustment.objects.select_for_update().get(pk=adj.pk)
    if adj.status != "PENDING_APPROVAL":
        raise BusinessError("Phiếu không ở trạng thái chờ duyệt.")
    reason = (reason or "").strip()
    if not reason:
        raise BusinessError("Nhập lý do từ chối.")
    adj.status, adj.approved_by, adj.approved_at, adj.reject_reason = "CANCELLED", user, timezone.now(), reason[:255]
    adj.save(update_fields=["status", "approved_by", "approved_at", "reject_reason", "updated_at"])
    resolve_alerts("ADJ_APPROVAL", "stock_adjustment", adj.pk)
    audit("REJECT", adj, {"reason": reason})
    return adj


# ------------------------------------------------------------------ STT 60: tồn thực < đã giữ
def _release_order(product):
    """Dòng đơn đang giữ hàng, thứ tự BỎ GIỮ: công trình ưu tiên thấp nhất (số lớn) trước -> cần giao muộn hơn trước
    -> xác nhận muộn hơn trước."""
    from apps.sales.models import SalesOrderLine
    return list(SalesOrderLine.objects.filter(product=product, so__status__in=["CONFIRMED", "PARTIAL"],
                                              reservations__status="ACTIVE").distinct()
                .select_related("so__site")
                .order_by(F("so__site__priority").desc(), F("so__requested_date").desc(),
                          F("so__confirmed_at").desc(nulls_last=True), "-so_id", "-id"))


@transaction.atomic
def release_excess_reservations(product, trigger=None):
    """Tồn thực (ngoài khu cách ly) < Đã giữ -> nhả phần chênh lệch khỏi các dòng ưu tiên thấp, chuyển các dòng đó
    sang "Thiếu" và lập đề xuất phân bổ thiếu hàng (AL) cho Giám đốc. Trả về [(dòng, SL nhả)]."""
    from apps.masterdata.models import Product
    from apps.sales.services import propose_allocation, so_log

    Product.objects.select_for_update().get(pk=product.pk)
    excess = _held(product) - _sellable(product)
    if excess <= 0:
        return []
    released = []
    for line in _release_order(product):
        if excess <= 0:
            break
        # phần đã xếp chuyến mà chưa xuất thì giữ lại (đang soạn / chờ xuất), chỉ nhả phần chưa xếp chuyến trước
        active = line.reservations.filter(status="ACTIVE").aggregate(s=Sum("qty"))["s"] or D0
        consumed = line.reservations.filter(status="CONSUMED").aggregate(s=Sum("qty"))["s"] or D0
        locked = max(D0, line.planned_qty - consumed)
        movable = max(D0, active - locked)
        take = min(movable, excess)
        if take <= 0:
            continue
        left = take
        for r in line.reservations.filter(status="ACTIVE").order_by("-created_at", "-id"):
            if left <= 0:
                break
            q = min(r.qty, left)
            if q == r.qty:
                r.status = "RELEASED"
                r.save(update_fields=["status"])
            else:
                r.qty -= q
                r.save(update_fields=["qty"])
            left -= q
        line.reserved_qty -= take
        line.atp_status = "SHORT"
        line.save(update_fields=["reserved_qty", "atp_status"])
        so_log(line.so, "RELEASED", getattr(trigger, "user_obj", None),
               {"product": product.code, "qty": float(take), "reason": "Tồn thực giảm dưới lượng đã giữ",
                "ref": getattr(trigger, "ref_code", "")})
        released.append((line, take))
        excess -= take
    if released:
        propose_allocation(product)
        from .replenishment import notify
        codes = ", ".join(f"{l.so.code} ({q:g})" for l, q in released)
        notify("DISPATCHER", f"{product.code}: bỏ giữ do tồn giảm",
               f"Tồn thực {product.code} thấp hơn lượng đã giữ – đã bỏ giữ {codes}, chờ Giám đốc phân bổ lại.",
               url="/app/sales/allocations", ref_type="product", ref_id=product.pk)
    return released
