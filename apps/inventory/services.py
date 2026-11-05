"""Dịch vụ tồn kho. MỌI thay đổi tồn kho phải đi qua post_stock_move()."""
import math
import statistics
from collections import defaultdict
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.core.middleware import get_current_user
from apps.core.services import BusinessError, next_doc_no, param, raise_alert, resolve_alerts
from apps.masterdata.models import Location, Product

from .models import ReplenishmentParam, ReplenishmentSnapshot, StockAlert, StockMove, StockQuant

D0 = Decimal(0)
IN_TYPES = ("IN_PURCHASE", "IN_RETURN")
OUT_DEMAND_TYPES = ("OUT_SALE", "OUT_INTERNAL")


@transaction.atomic
def post_stock_move(product, location, qty, move_type, ref_type, ref_id, unit_cost=None, note="", at=None):
    """Ghi 1 bút toán sổ kho + cập nhật stock_quant trong transaction, khóa dòng SELECT ... FOR UPDATE.

    - Chặn âm kho (đồng thời có CHECK on_hand >= 0 ở DB).
    - IN_PURCHASE cập nhật giá vốn Moving Average của sản phẩm.
    """
    qty = Decimal(qty)
    if qty == 0:
        raise BusinessError("Số lượng bút toán kho phải khác 0.")
    at = at or timezone.now()

    # khóa sản phẩm (để tính moving average an toàn) và quant
    product = Product.objects.select_for_update().get(pk=product.pk)
    quant, _ = StockQuant.objects.get_or_create(product=product, location=location)
    quant = StockQuant.objects.select_for_update().get(pk=quant.pk)

    new_balance = quant.on_hand + qty
    if new_balance < 0:
        raise BusinessError(
            f"Không đủ tồn: {product.code} tại {location.code} còn {quant.on_hand}, cần xuất {abs(qty)}.")

    if unit_cost is None:
        unit_cost = product.avg_cost
    unit_cost = Decimal(unit_cost)

    if move_type == "IN_PURCHASE" and qty > 0:
        total_before = StockQuant.objects.filter(product=product).aggregate(s=Sum("on_hand"))["s"] or D0
        if total_before + qty > 0:
            product.avg_cost = ((total_before * product.avg_cost + qty * unit_cost) / (total_before + qty)) \
                .quantize(Decimal(1), rounding=ROUND_HALF_UP)
            product.save(update_fields=["avg_cost", "updated_at"])

    quant.on_hand = new_balance
    if qty > 0 and (quant.in_date_first is None or quant.on_hand == qty):
        quant.in_date_first = at
    if quant.on_hand == 0:
        quant.in_date_first = None
    quant.save(update_fields=["on_hand", "in_date_first"])

    user = get_current_user()
    return StockMove.objects.create(
        at=at, product=product, location=location, qty=qty, unit_cost=unit_cost, move_type=move_type,
        ref_type=ref_type, ref_id=ref_id, balance_after=new_balance, note=note,
        user=user if (user and user.is_authenticated) else None,
    )


def transfer(product, from_loc, to_loc, qty, note=""):
    """Chuyển vị trí = 2 bút toán TRF trong cùng transaction."""
    with transaction.atomic():
        ref = int(timezone.now().timestamp())
        post_stock_move(product, from_loc, -Decimal(qty), "TRF", "TRF", ref, note=note)
        post_stock_move(product, to_loc, Decimal(qty), "TRF", "TRF", ref, note=note)


# ----------------------------------------------------------------------------------------------
# Chỉ số tồn: on_hand, reserved, available, incoming, projected, days_cover
# ----------------------------------------------------------------------------------------------
def stock_summary(products=None):
    from apps.purchasing.models import PurchaseOrderLine
    from apps.sales.models import StockReservation

    qs = products if products is not None else Product.objects.filter(is_active=True)
    ids = [p.pk for p in qs]
    on_hand = dict(StockQuant.objects.filter(product_id__in=ids, location__loc_type__in=["STORAGE", "STAGING_OUT"])
                   .values_list("product_id").annotate(s=Sum("on_hand")))
    quarantine = dict(StockQuant.objects.filter(product_id__in=ids, location__loc_type="QUARANTINE")
                      .values_list("product_id").annotate(s=Sum("on_hand")))
    reserved = dict(StockReservation.objects.filter(product_id__in=ids, status="ACTIVE")
                    .values_list("product_id").annotate(s=Sum("qty")))
    incoming = dict(PurchaseOrderLine.objects
                    .filter(product_id__in=ids, po__status__in=["APPROVED", "SENT", "PARTIAL"], qty__gt=F("qty_received"))
                    .values_list("product_id").annotate(s=Sum(F("qty") - F("qty_received"))))
    params = {p.product_id: p for p in ReplenishmentParam.objects.filter(product_id__in=ids)}
    out = {}
    for p in qs:
        oh = on_hand.get(p.pk) or D0
        rs = reserved.get(p.pk) or D0
        inc = incoming.get(p.pk) or D0
        rp = params.get(p.pk)
        av = oh - rs
        proj = av + inc
        d_avg = rp.d_avg if rp else D0
        ss = rp.eff_ss if rp else D0
        rop = rp.eff_rop if rp else D0
        out[p.pk] = {
            "product": p, "on_hand": oh, "quarantine": quarantine.get(p.pk) or D0, "reserved": rs, "available": av,
            "incoming": inc, "projected": proj, "d_avg": d_avg, "ss": ss, "rop": rop,
            "days_cover": (av / d_avg) if d_avg else None, "locked": bool(rp and rp.manual_lock),
            "suggested_q": rp.suggested_q if rp else D0,
            "level": "STOCKOUT" if av <= 0 else ("BELOW_SS" if proj <= ss else ("BELOW_ROP" if proj <= rop else "OK")),
        }
    return out


def fifo_quants(product, qty_needed, loc_types=("STORAGE",)):
    """Trả về [(location, qty)] theo FIFO (in_date_first sớm nhất trước)."""
    res, remain = [], Decimal(qty_needed)
    quants = StockQuant.objects.filter(product=product, on_hand__gt=0, location__loc_type__in=loc_types,
                                       location__is_active=True) \
        .select_related("location").order_by(F("in_date_first").asc(nulls_last=True), "location__code")
    for q in quants:
        if remain <= 0:
            break
        take = min(q.on_hand, remain)
        res.append((q.location, take))
        remain -= take
    return res, remain


def default_storage_location(product, warehouse):
    q = StockQuant.objects.filter(product=product, location__warehouse=warehouse, location__loc_type="STORAGE",
                                  location__is_active=True).order_by("-on_hand").first()
    if q:
        return q.location
    return Location.objects.filter(warehouse=warehouse, loc_type="STORAGE", is_active=True).first()


# ----------------------------------------------------------------------------------------------
# SS / ROP (Celery 01:00 hằng đêm)
#   Davg, σD từ lịch sử xuất; LT, σLT từ lead time thực tế các GRN
#   FULL: SS = Z * sqrt(LT * σD² + Davg² * σLT²)
#   ROP = Davg * LT + SS ;  Q = max(MOQ; Davg * chu kỳ đặt) ; PR = max(Q; ROP + Q - projected)
#   Khóa thủ công: vẫn tính SS/ROP tự động để tham khảo nhưng ngưỡng áp dụng là số nhập tay.
# ----------------------------------------------------------------------------------------------
def outlier_cut(values):
    """Ngưỡng phiếu xuất đột biến (modified z-score > 3,5): trung vị + 3,5 × 1,4826 × MAD, tối thiểu 3 × trung vị.

    Dùng trung vị / MAD vì không bị chính các phiếu đột biến kéo lệch như trung bình hay IQR. Dưới 4 phiếu thì không loại."""
    if len(values) < 4:
        return None
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    return max(med + 3.5 * 1.4826 * mad, 3 * med)


def demand_series(rp, today=None):
    """Nhu cầu xuất theo ngày trong history_days ngày trước hôm nay.

    Trả về (ngày, nhu cầu gốc, nhu cầu dùng tính, các phiếu bị loại, ngưỡng loại)."""
    today = today or timezone.localdate()
    start = today - timedelta(days=rp.history_days)
    types = ("OUT_SALE",) if rp.demand_source == "SALE" else OUT_DEMAND_TYPES
    moves = list(StockMove.objects.filter(product_id=rp.product_id, move_type__in=types, at__date__gte=start,
                                          at__date__lt=today).values_list("at", "qty", "note"))
    cut = outlier_cut([float(-q) for _, q, _ in moves]) if rp.exclude_outliers else None
    raw, used, dropped = defaultdict(float), defaultdict(float), []
    for at, q, note in moves:
        d = timezone.localtime(at).date()
        raw[d] += float(-q)
        if cut is not None and float(-q) > cut:
            dropped.append((d, float(-q), note))
        else:
            used[d] += float(-q)
    days = [start + timedelta(days=i) for i in range(rp.history_days)]
    return days, [raw[d] for d in days], [used[d] for d in days], dropped, cut


def calc_param(product, today=None):
    from apps.purchasing.models import GoodsReceiptLine

    rp, _ = ReplenishmentParam.objects.get_or_create(product=product, defaults={"z": param("SERVICE_Z")})
    _, _, series, _, _ = demand_series(rp, today)
    d_avg = statistics.fmean(series) if series else 0.0
    d_std = statistics.stdev(series) if len(series) > 1 else 0.0  # mẫu – khớp STDEV.S của Excel

    lts = [float(x) for x in GoodsReceiptLine.objects.filter(product=product, gr__status="POSTED",
                                                              gr__lead_time_days__isnull=False)
           .order_by("-gr__posted_at").values_list("gr__lead_time_days", flat=True)[:20]]
    lt = statistics.fmean(lts) if lts else float(rp.lead_time_days or 3)
    lt_std = statistics.pstdev(lts) if len(lts) > 1 else 0.0

    z = float(rp.z)
    if rp.method == "FULL":
        ss = z * math.sqrt(lt * d_std ** 2 + d_avg ** 2 * lt_std ** 2)
    elif rp.method == "DEMAND":
        ss = z * d_std * math.sqrt(lt)
    else:
        ss = float(rp.manual_ss or 0)
    rop = d_avg * lt + ss

    rp.d_avg, rp.d_std = Decimal(f"{d_avg:.3f}"), Decimal(f"{d_std:.3f}")
    rp.lead_time_days, rp.lt_std_days = Decimal(f"{lt:.2f}"), Decimal(f"{lt_std:.2f}")
    rp.ss, rp.rop = Decimal(f"{ss:.3f}"), Decimal(f"{rop:.3f}")
    rp.calculated_at = timezone.now()
    rp.save()
    return rp, series


@transaction.atomic
def run_replenishment(today=None):
    """Tính lại SS/ROP, chụp snapshot, cảnh báo và tự sinh PR (SUBMITTED) khi projected <= ROP."""
    from apps.purchasing.models import PurchaseRequisition, PurchaseRequisitionLine

    today = today or timezone.localdate()
    products = list(Product.objects.filter(is_active=True, track_rop=True))
    for p in products:
        calc_param(p, today)
    summary = stock_summary(products)

    pending = set(PurchaseRequisitionLine.objects.filter(pr__status__in=["DRAFT", "SUBMITTED"])
                  .values_list("product_id", flat=True))
    to_order = []
    for p in products:
        s = summary[p.pk]
        rp = p.rop_param
        rp.suggested_q = rp.pr_qty(s["projected"])
        rp.save(update_fields=["suggested_q"])
        y_demand = -(StockMove.objects.filter(product=p, move_type__in=OUT_DEMAND_TYPES,
                                               at__date=today - timedelta(days=1)).aggregate(s=Sum("qty"))["s"] or D0)
        ReplenishmentSnapshot.objects.update_or_create(
            product=p, date=today,
            defaults=dict(on_hand=s["on_hand"], reserved=s["reserved"], incoming=s["incoming"],
                          projected=s["projected"], demand=y_demand, d_avg=s["d_avg"], ss=s["ss"], rop=s["rop"]))

        if s["level"] != "OK":
            StockAlert.objects.filter(product=p, is_resolved=False).exclude(alert_type=s["level"]).update(is_resolved=True)
            if not StockAlert.objects.filter(product=p, alert_type=s["level"], is_resolved=False).exists():
                StockAlert.objects.create(product=p, alert_type=s["level"], projected=s["projected"],
                                          threshold=s["rop"] if s["level"] == "BELOW_ROP" else s["ss"])
            raise_alert("BELOW_ROP", f"{p.code} dưới điểm đặt hàng",
                        f"Dự báo {s['projected']:.0f} ≤ ROP {s['rop']:.0f}", ref_type="product", ref_id=p.pk,
                        url="/app/inventory/replenishment?product=%d" % p.pk, target_role="PURCHASER")
            if p.pk not in pending and rp.suggested_q > 0:
                to_order.append((p, rp.suggested_q))
        else:
            StockAlert.objects.filter(product=p, is_resolved=False).update(is_resolved=True)
            resolve_alerts("BELOW_ROP", "product", p.pk)

    pr = None
    if to_order:
        pr = PurchaseRequisition.objects.create(code=next_doc_no("PR"), status="SUBMITTED", source="AUTO",
                                                need_date=today + timedelta(days=3),
                                                note="Sinh tự động khi tồn dự báo ≤ ROP")
        for p, q in to_order:
            PurchaseRequisitionLine.objects.create(pr=pr, product=p, qty=q, suggested_supplier=p.default_supplier)
    return {"products": len(products), "auto_pr": pr.code if pr else None, "lines": len(to_order)}
