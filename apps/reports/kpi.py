"""Một chỗ duy nhất tính 6 KPI dự án – Dashboard và trang Báo cáo KPI cùng gọi (test Gà STT 79, 81, 88).

Kỳ tính (period) chọn bằng một bộ chọn duy nhất (?period=… hoặc ?from=…&to=…), mặc định "Tháng này" cho cả hai
trang, và luôn ghi rõ trên thẻ (vd. "01/11–16/11"). Mỗi thẻ có: giá trị kỳ chọn, mục tiêu, đạt / chưa đạt, so với kỳ
trước cùng độ dài (mũi tên + tốt / xấu), chuỗi 8 tuần gần nhất cho sparkline.
"""
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import F, Sum
from django.utils import timezone

D0 = Decimal(0)

# ------------------------------------------------------------------ kỳ tính dùng chung
PERIODS = [("month", "Tháng này"), ("prev_month", "Tháng trước"), ("30d", "30 ngày qua"), ("quarter", "Quý này"),
           ("year", "Năm nay")]


def resolve_period(request):
    """(d_from, d_to, key) từ ?period= hoặc ?from=&to= (tùy chọn). Mặc định: tháng này."""
    today = timezone.localdate()
    f, t = request.GET.get("from"), request.GET.get("to")
    if f and t:
        try:
            d_from, d_to = datetime.strptime(f, "%Y-%m-%d").date(), datetime.strptime(t, "%Y-%m-%d").date()
            if d_from > d_to:
                d_from, d_to = d_to, d_from
            return d_from, min(d_to, today), "custom"
        except ValueError:
            pass
    key = request.GET.get("period") or "month"
    ms = today.replace(day=1)
    if key == "prev_month":
        last = ms - timedelta(days=1)
        return last.replace(day=1), last, key
    if key == "30d":
        return today - timedelta(days=29), today, key
    if key == "quarter":
        return date(today.year, 3 * ((today.month - 1) // 3) + 1, 1), today, key
    if key == "year":
        return date(today.year, 1, 1), today, key
    return ms, today, "month"


def period_label(d_from, d_to):
    return f"{d_from:%d/%m}–{d_to:%d/%m/%Y}" if d_from.year == d_to.year else f"{d_from:%d/%m/%Y}–{d_to:%d/%m/%Y}"


def period_qs(d_from, d_to):
    return f"from={d_from:%Y-%m-%d}&to={d_to:%Y-%m-%d}"


# ------------------------------------------------------------------ dữ liệu từng KPI
OTIF_RULE = ("On-time: điểm giao có giao hàng (Đã giao / Giao một phần) và ngày giao ≤ ngày khách cần. "
             "In-full: giao đủ số lượng trên xe. Điểm giao THẤT BẠI (khách vắng, từ chối…) tính là KHÔNG đúng hạn và "
             "KHÔNG đủ; lần giao lại thành công được tính là một điểm giao riêng.")


def otif_data(d_from, d_to):
    from apps.sales.models import DeliveryStop
    stops = DeliveryStop.objects.filter(delivered_at__date__range=(d_from, d_to)).select_related("so__customer", "trip")
    rows, ot, inf, both = [], 0, 0, 0
    for s in stops:
        delivered = s.status in ("DELIVERED", "PARTIAL")  # giao thất bại không bao giờ là "đúng hạn" (STT 46)
        on_time = delivered and timezone.localtime(s.delivered_at).date() <= s.so.requested_date
        in_full = s.status == "DELIVERED"
        ot += on_time
        inf += in_full
        both += on_time and in_full
        rows.append({"s": s, "on_time": on_time, "in_full": in_full})
    n = len(rows)
    pct = lambda x: (x / n * 100) if n else None  # noqa: E731
    return {"rows": rows, "n": n, "ot": pct(ot), "if": pct(inf), "otif": pct(both), "rule": OTIF_RULE}


def leadtime_data(d_from, d_to):
    from apps.purchasing.models import GoodsReceipt
    all_grs = GoodsReceipt.objects.filter(status="POSTED", posted_at__date__range=(d_from, d_to),
                                          lead_time_days__isnull=False).select_related("po__supplier")
    excluded = [g for g in all_grs if g.exclude_from_stats]
    grs = [g for g in all_grs if not g.exclude_from_stats]
    by_sup = defaultdict(list)
    for g in grs:
        by_sup[g.po.supplier].append(float(g.lead_time_days))
    rows = [{"supplier": s, "n": len(v), "avg": statistics.fmean(v), "std": statistics.pstdev(v) if len(v) > 1 else 0,
             "min": min(v), "max": max(v)} for s, v in by_sup.items()]
    rows.sort(key=lambda r: -r["avg"])
    allv = [x for v in by_sup.values() for x in v]
    return {"rows": rows, "avg": statistics.fmean(allv) if allv else None, "grs": grs, "excluded": excluded}


def wait_data(d_from, d_to):
    from apps.purchasing.models import UnloadingSlot
    slots = UnloadingSlot.objects.filter(checked_in_at__date__range=(d_from, d_to)) \
        .select_related("trip__vehicle", "dock", "trip__po__supplier")
    rows = []
    for s in slots:
        w = s.wait_minutes
        if w is not None:
            unload = int((s.done_at - s.checked_in_at).total_seconds() // 60) if s.done_at else None
            rows.append({"s": s, "wait": w, "unload": unload})
    waits = [r["wait"] for r in rows]
    return {"rows": rows, "avg": statistics.fmean(waits) if waits else None, "max": max(waits) if waits else None,
            "over30": sum(1 for w in waits if w > 30)}


def count_lines(d_from, d_to):
    from apps.inventory.models import InventoryCountLine
    return list(InventoryCountLine.objects.filter(count__status="APPROVED", count__updated_at__date__range=(d_from, d_to),
                                                  final_qty__isnull=False)
                .select_related("count__warehouse", "product__base_uom", "location").order_by("-count_id", "location__code"))


def count_accuracy(d_from, d_to):
    lines = count_lines(d_from, d_to)
    n = len(lines)
    ok = sum(1 for l in lines if (l.variance_pct or 0) <= 1)
    return (ok / n * 100) if n else None


def _tracked():
    from apps.inventory.models import SimulatedDemand
    from apps.masterdata.models import Product
    sim = SimulatedDemand.objects.values("product_id")  # mã demo dữ liệu mô phỏng không vào KPI / báo cáo
    return list(Product.objects.filter(is_active=True, track_rop=True).exclude(pk__in=sim))


def stockout_pct(d_to):
    """KPI-6: tỷ lệ mã theo dõi ROP có khả dụng ≤ 0. Hôm nay: số liệu tức thời; ngày cũ: ảnh chụp tác vụ đêm gần nhất."""
    from apps.inventory.models import ReplenishmentSnapshot
    from apps.inventory.services import stock_summary
    prods = _tracked()
    if not prods:
        return None
    if d_to >= timezone.localdate():
        summ = stock_summary(prods)
        return sum(1 for s in summ.values() if s["available"] <= 0) / len(summ) * 100
    snaps = {}
    for s in ReplenishmentSnapshot.objects.filter(product__in=prods, date__lte=d_to, date__gt=d_to - timedelta(days=7)) \
            .order_by("date"):
        snaps[s.product_id] = s  # bản chụp gần ngày d_to nhất
    if not snaps:
        return None
    return sum(1 for s in snaps.values() if s.on_hand - s.reserved <= 0) / len(snaps) * 100


def stock_value_at(d):
    """Giá trị tồn (giá vốn BQ hiện tại) tại cuối ngày d = tồn hiện tại − bút toán sau ngày d."""
    from apps.inventory.models import StockMove, StockQuant
    from apps.masterdata.models import Product
    on_hand = defaultdict(lambda: D0)
    for pid, q in StockQuant.objects.values_list("product_id").annotate(s=Sum("on_hand")):
        on_hand[pid] += q or D0
    if d < timezone.localdate():
        for pid, q in StockMove.objects.filter(at__date__gt=d).values_list("product_id").annotate(s=Sum("qty")):
            on_hand[pid] -= q or D0
    cost = dict(Product.objects.filter(pk__in=list(on_hand)).values_list("pk", "avg_cost"))
    return sum((max(q, D0) * (cost.get(pid) or D0) for pid, q in on_hand.items()), D0)


def doi(d_from, d_to):
    """KPI-7: số ngày tồn kho = giá trị tồn cuối kỳ / (giá vốn hàng xuất trong kỳ / số ngày)."""
    from apps.inventory.models import StockMove
    days = (d_to - d_from).days + 1
    cogs = -(StockMove.objects.filter(move_type__in=["OUT_SALE", "OUT_INTERNAL"], at__date__range=(d_from, d_to))
             .aggregate(s=Sum(F("qty") * F("unit_cost")))["s"] or D0)
    if not cogs:
        return None
    return float(stock_value_at(d_to)) / (float(cogs) / days)


# ------------------------------------------------------------------ 6 KPI (định nghĩa dùng chung)
KPI_DEFS = [
    # code, nhãn, đơn vị, mục tiêu, cao hơn là tốt, trang chi tiết, hàm(d_from, d_to)
    ("KPI-1", "OTIF (giao đúng hạn & đủ)", "%", 95, True, "/app/reports/otif", lambda a, b: otif_data(a, b)["otif"]),
    ("KPI-2", "Độ chính xác tồn kho (kiểm kê)", "%", 98, True, "/app/reports/count-accuracy", count_accuracy),
    ("KPI-3", "Lead time Inbound TB", " ngày", 3, False, "/app/reports/leadtime", lambda a, b: leadtime_data(a, b)["avg"]),
    ("KPI-4", "Thời gian chờ hạ tải TB", " phút", 30, False, "/app/reports/unloading", lambda a, b: wait_data(a, b)["avg"]),
    # KPI-5 (mốc hành trình đúng giờ) đã bỏ – STT 73
    ("KPI-6", "Tỷ lệ mã hàng hết hàng", "%", 2, False, "/app/reports/rop", lambda a, b: stockout_pct(b)),
    ("KPI-7", "Số ngày tồn kho (DOI)", " ngày", 30, False, "/app/reports/nxt", doi),
]
TREND_WEEKS = 8


def kpi_cards(d_from, d_to, user=None, trend=True):
    """6 thẻ KPI cho kỳ [d_from, d_to]: value, target, ok, prev (kỳ trước cùng độ dài), delta, better (True = tốt lên),
    spark (8 tuần kết thúc ở d_to), url chi tiết kèm đúng kỳ."""
    from apps.core.permissions import can_open
    span = (d_to - d_from).days + 1
    p_to = d_from - timedelta(days=1)
    p_from = p_to - timedelta(days=span - 1)
    weeks = [(d_to - timedelta(days=7 * k + 6), d_to - timedelta(days=7 * k)) for k in range(TREND_WEEKS - 1, -1, -1)]
    out = []
    for code, label, unit, target, higher, url, fn in KPI_DEFS:
        v, prev = fn(d_from, d_to), fn(p_from, p_to)
        delta = (v - prev) if (v is not None and prev is not None) else None
        card = {"code": code, "label": label, "unit": unit, "target": target, "higher": higher, "value": v,
                "prev": prev, "delta": delta,
                "better": None if not delta else (delta > 0) == higher,
                "ok": None if v is None else (v >= target if higher else v <= target),
                "url": f"{url}?{period_qs(d_from, d_to)}" if (user is None or can_open(user, url)) else None,
                "period": period_label(d_from, d_to), "prev_period": period_label(p_from, p_to)}
        if trend:
            card["spark"] = [fn(a, b) for a, b in weeks]
            card["spark_weeks"] = [f"{a:%d/%m}–{b:%d/%m}" for a, b in weeks]
        out.append(card)
    return out


def sparkline_points(values, w=120, h=32, pad=3):
    """Toạ độ SVG polyline cho sparkline (bỏ qua tuần không có dữ liệu)."""
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return ""
    lo, hi = min(v for _, v in pts), max(v for _, v in pts)
    rng = (hi - lo) or 1
    n = max(len(values) - 1, 1)
    return " ".join(f"{pad + i * (w - 2 * pad) / n:.1f},{h - pad - (v - lo) / rng * (h - 2 * pad):.1f}" for i, v in pts)
