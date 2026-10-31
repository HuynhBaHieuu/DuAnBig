"""MH-70 → 76: 7 KPI dự án, Dashboard ROP, Nhập–Xuất–Tồn, OTIF, Lead time, Thời gian chờ hạ tải."""
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Q, Sum
from django.shortcuts import render
from django.utils import timezone

from apps.core.permissions import role_required
from apps.core.utils import excel_response
from apps.inventory.models import InventoryCount, InventoryCountLine, StockMove, StockQuant
from apps.inventory.services import stock_summary
from apps.masterdata.models import Product
from apps.purchasing.models import GoodsReceipt, InboundTrip, TripMilestone, UnloadingSlot
from apps.sales.models import DeliveryStop

REPORT_ROLES = ("DIRECTOR", "PURCHASER", "ACCOUNTANT", "DISPATCHER")
D0 = Decimal(0)


def _range(request, days=30):
    today = timezone.localdate()
    f = request.GET.get("from")
    t = request.GET.get("to")
    d_from = datetime.strptime(f, "%Y-%m-%d").date() if f else today - timedelta(days=days - 1)
    d_to = datetime.strptime(t, "%Y-%m-%d").date() if t else today
    return d_from, d_to


def otif_data(d_from, d_to):
    stops = DeliveryStop.objects.filter(delivered_at__date__range=(d_from, d_to)).select_related("so__customer", "trip")
    rows, ot, inf, both = [], 0, 0, 0
    for s in stops:
        on_time = timezone.localtime(s.delivered_at).date() <= s.so.requested_date
        in_full = s.status == "DELIVERED"
        ot += on_time
        inf += in_full
        both += on_time and in_full
        rows.append({"s": s, "on_time": on_time, "in_full": in_full})
    n = len(rows)
    pct = lambda x: (x / n * 100) if n else None  # noqa: E731
    return {"rows": rows, "n": n, "ot": pct(ot), "if": pct(inf), "otif": pct(both)}


def leadtime_data(d_from, d_to):
    grs = GoodsReceipt.objects.filter(status="POSTED", posted_at__date__range=(d_from, d_to), lead_time_days__isnull=False) \
        .select_related("po__supplier")
    by_sup = defaultdict(list)
    for g in grs:
        by_sup[g.po.supplier].append(float(g.lead_time_days))
    rows = [{"supplier": s, "n": len(v), "avg": statistics.fmean(v), "std": statistics.pstdev(v) if len(v) > 1 else 0,
             "min": min(v), "max": max(v)} for s, v in by_sup.items()]
    rows.sort(key=lambda r: -r["avg"])
    allv = [x for v in by_sup.values() for x in v]
    return {"rows": rows, "avg": statistics.fmean(allv) if allv else None, "grs": grs}


def wait_data(d_from, d_to):
    slots = UnloadingSlot.objects.filter(checked_in_at__date__range=(d_from, d_to)).select_related("trip__vehicle", "dock", "trip__po__supplier")
    rows = []
    for s in slots:
        w = s.wait_minutes
        if w is not None:
            unload = int((s.done_at - s.checked_in_at).total_seconds() // 60) if s.done_at else None
            rows.append({"s": s, "wait": w, "unload": unload})
    waits = [r["wait"] for r in rows]
    return {"rows": rows, "avg": statistics.fmean(waits) if waits else None, "max": max(waits) if waits else None,
            "over30": sum(1 for w in waits if w > 30)}


def milestone_ontime(d_from, d_to):
    ms = TripMilestone.objects.filter(actual_at__date__range=(d_from, d_to))
    n = ms.count()
    late = sum(1 for m in ms if m.delay_min and m.delay_min > 60)
    return ((n - late) / n * 100) if n else None


def count_accuracy(d_from, d_to):
    lines = InventoryCountLine.objects.filter(count__status="APPROVED", count__updated_at__date__range=(d_from, d_to),
                                              final_qty__isnull=False)
    n = lines.count()
    ok = sum(1 for l in lines if (l.variance_pct or 0) <= 1)
    return (ok / n * 100) if n else None


@role_required(*REPORT_ROLES)
def index(request):
    d_from, d_to = _range(request)
    otif = otif_data(d_from, d_to)
    lt = leadtime_data(d_from, d_to)
    wait = wait_data(d_from, d_to)
    summ = stock_summary(list(Product.objects.filter(is_active=True, track_rop=True)))
    stockout = (sum(1 for s in summ.values() if s["available"] <= 0) / len(summ) * 100) if summ else None
    days = (d_to - d_from).days + 1
    cogs = -(StockMove.objects.filter(move_type__in=["OUT_SALE", "OUT_INTERNAL"], at__date__range=(d_from, d_to))
             .aggregate(s=Sum(models_qty_cost()))["s"] or D0)
    stock_val = StockQuant.objects.aggregate(s=Sum(models_onhand_cost()))["s"] or D0
    doi = (float(stock_val) / (float(cogs) / days)) if cogs else None
    kpis = [
        {"code": "KPI-1", "label": "OTIF (giao đúng hạn & đủ)", "value": otif["otif"], "unit": "%", "target": 95, "higher": True, "url": "/app/reports/otif"},
        {"code": "KPI-2", "label": "Độ chính xác tồn kho (kiểm kê)", "value": count_accuracy(d_from, d_to), "unit": "%", "target": 98, "higher": True, "url": "/app/inventory/counts"},
        {"code": "KPI-3", "label": "Lead time Inbound TB", "value": lt["avg"], "unit": " ngày", "target": 3, "higher": False, "url": "/app/reports/leadtime"},
        {"code": "KPI-4", "label": "Thời gian chờ hạ tải TB", "value": wait["avg"], "unit": " phút", "target": 30, "higher": False, "url": "/app/reports/unloading"},
        {"code": "KPI-5", "label": "Tỷ lệ mốc hành trình đúng giờ", "value": milestone_ontime(d_from, d_to), "unit": "%", "target": 90, "higher": True, "url": "/app/inbound/board?view=table"},
        {"code": "KPI-6", "label": "Tỷ lệ mã hàng hết hàng", "value": stockout, "unit": "%", "target": 2, "higher": False, "url": "/app/reports/rop"},
        {"code": "KPI-7", "label": "Số ngày tồn kho (DOI)", "value": doi, "unit": " ngày", "target": 30, "higher": False, "url": "/app/reports/nxt"},
    ]
    for k in kpis:
        v = k["value"]
        k["ok"] = None if v is None else (v >= k["target"] if k["higher"] else v <= k["target"])
    return render(request, "reports/index.html", {"title": "Báo cáo KPI dự án", "kpis": kpis, "d_from": d_from, "d_to": d_to})


def models_qty_cost():
    from django.db.models import F
    return F("qty") * F("unit_cost")


def models_onhand_cost():
    from django.db.models import F
    return F("on_hand") * F("product__avg_cost")


@role_required(*REPORT_ROLES)
def rop(request):
    from apps.inventory.views import _bar
    rows = [_bar(s) for s in stock_summary(list(Product.objects.filter(is_active=True, track_rop=True).select_related("base_uom"))).values()]
    rows.sort(key=lambda r: (float(r["projected"]) - float(r["rop"])))
    counts = defaultdict(int)
    for r in rows:
        counts[r["level"]] += 1
    return render(request, "reports/rop.html", {"title": "Dashboard ROP", "rows": rows, "counts": dict(counts)})


@role_required(*REPORT_ROLES)
def nxt(request):
    """Nhập – Xuất – Tồn theo kỳ."""
    d_from, d_to = _range(request)
    products = Product.objects.filter(is_active=True).select_related("base_uom")
    moves = StockMove.objects.filter(at__date__range=(d_from, d_to))
    opening = defaultdict(lambda: D0)
    for pid, q in StockMove.objects.filter(at__date__lt=d_from).values_list("product_id").annotate(s=Sum("qty")):
        opening[pid] = q
    inq, outq = defaultdict(lambda: D0), defaultdict(lambda: D0)
    for pid, q in moves.filter(qty__gt=0).exclude(move_type="TRF").values_list("product_id").annotate(s=Sum("qty")):
        inq[pid] = q
    for pid, q in moves.filter(qty__lt=0).exclude(move_type="TRF").values_list("product_id").annotate(s=Sum("qty")):
        outq[pid] = -q
    rows = []
    for p in products:
        o, i, x = opening[p.pk], inq[p.pk], outq[p.pk]
        if o or i or x:
            rows.append({"p": p, "open": o, "in": i, "out": x, "close": o + i - x, "value": (o + i - x) * p.avg_cost})
    if request.GET.get("export") == "xlsx":
        head = ["Mã", "Tên", "ĐVT", "Tồn đầu", "Nhập", "Xuất", "Tồn cuối"] + (["Giá trị"] if request.user.can_see_cost else [])
        data = [[r["p"].code, r["p"].name, r["p"].base_uom.code, r["open"], r["in"], r["out"], r["close"]]
                + ([r["value"]] if request.user.can_see_cost else []) for r in rows]
        return excel_response(f"NXT_{d_from:%Y%m%d}_{d_to:%Y%m%d}", head, data, "NXT")
    return render(request, "reports/nxt.html", {"title": "Báo cáo Nhập – Xuất – Tồn", "rows": rows, "d_from": d_from, "d_to": d_to})


@role_required(*REPORT_ROLES)
def otif(request):
    d_from, d_to = _range(request)
    data = otif_data(d_from, d_to)
    by_day = defaultdict(lambda: [0, 0])
    for r in data["rows"]:
        d = timezone.localtime(r["s"].delivered_at).strftime("%d/%m")
        by_day[d][0] += 1
        by_day[d][1] += r["on_time"] and r["in_full"]
    chart = {"labels": list(by_day), "values": [round(v[1] / v[0] * 100, 1) for v in by_day.values()]}
    return render(request, "reports/otif.html", {"title": "Báo cáo OTIF", "d": data, "chart": chart, "d_from": d_from, "d_to": d_to})


@role_required(*REPORT_ROLES)
def leadtime(request):
    d_from, d_to = _range(request, 90)
    return render(request, "reports/leadtime.html", {"title": "Báo cáo Lead time", "d": leadtime_data(d_from, d_to),
                                                     "d_from": d_from, "d_to": d_to})


@role_required(*REPORT_ROLES)
def unloading(request):
    d_from, d_to = _range(request)
    return render(request, "reports/unloading.html", {"title": "Thời gian chờ hạ tải", "d": wait_data(d_from, d_to),
                                                      "d_from": d_from, "d_to": d_to})
