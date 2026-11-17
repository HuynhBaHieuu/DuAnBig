"""MH-70 → 76: 6 KPI dự án (KPI-5 đã bỏ), Dashboard ROP, Nhập–Xuất–Tồn, OTIF, Lead time, Thời gian chờ hạ tải."""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.db.models import Sum
from django.shortcuts import render
from django.utils import timezone

from apps.core.permissions import role_required
from apps.core.utils import excel_response
from apps.inventory.models import StockMove
from apps.inventory.services import stock_summary
from apps.masterdata.models import Product

from .kpi import (PERIODS, count_accuracy, count_lines, kpi_cards, leadtime_data, otif_data, period_label,
                  resolve_period, wait_data)

REPORT_ROLES = ("DIRECTOR", "PURCHASER", "ACCOUNTANT", "DISPATCHER")
D0 = Decimal(0)


def _range(request, days=30):
    """Kỳ báo cáo: có ?period / ?from&to (vd. bấm từ thẻ KPI trên Dashboard) -> đúng kỳ đó (resolve_period dùng chung);
    không có -> mặc định `days` ngày gần nhất của từng báo cáo chi tiết."""
    if request.GET.get("period") or (request.GET.get("from") and request.GET.get("to")):
        d_from, d_to, _ = resolve_period(request)
        return d_from, d_to
    today = timezone.localdate()
    return today - timedelta(days=days - 1), today


@role_required(*REPORT_ROLES)
def index(request):
    """6 KPI dự án – cùng hàm (kpi.kpi_cards) và cùng kỳ mặc định (tháng này) với Dashboard (test Gà STT 79)."""
    d_from, d_to, key = resolve_period(request)
    return render(request, "reports/index.html", {"title": "Báo cáo KPI dự án", "kpis": kpi_cards(d_from, d_to, request.user),
                                                  "d_from": d_from, "d_to": d_to, "period": key, "periods": PERIODS,
                                                  "period_text": period_label(d_from, d_to)})


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
def count_accuracy_report(request):
    """KPI-2: từng dòng kiểm kê đã duyệt trong kỳ – tồn sổ, số đếm, % lệch (đạt khi lệch ≤ 1%)."""
    d_from, d_to = _range(request)
    lines = count_lines(d_from, d_to)
    counts = {}
    for l in lines:
        c = counts.setdefault(l.count_id, {"ic": l.count, "n": 0, "ok": 0})
        c["n"] += 1
        c["ok"] += (l.variance_pct or 0) <= 1
    for c in counts.values():
        c["pct"] = c["ok"] / c["n"] * 100 if c["n"] else None
    return render(request, "reports/count_accuracy.html", {
        "title": "KPI-2 · Độ chính xác tồn kho", "lines": lines, "counts": list(counts.values()),
        "value": count_accuracy(d_from, d_to), "d_from": d_from, "d_to": d_to})


@role_required(*REPORT_ROLES)
def unloading(request):
    d_from, d_to = _range(request)
    return render(request, "reports/unloading.html", {"title": "Thời gian chờ hạ tải", "d": wait_data(d_from, d_to),
                                                      "d_from": d_from, "d_to": d_to})
