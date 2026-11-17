"""Báo cáo: 6 KPI dự án (KPI-1 → KPI-6), Nhập–Xuất–Tồn, OTIF, Lead time, Thời gian chờ hạ tải, Chính xác tồn.
Mọi tab dùng chung một kỳ xem (resolve_period, mặc định Tháng này) – Gà STT 101."""
from collections import defaultdict
from decimal import Decimal

from django.db.models import Sum
from django.shortcuts import redirect, render
from django.utils import timezone

from apps.core.permissions import role_required
from apps.core.utils import excel_response, paginate
from apps.inventory.models import StockMove
from apps.masterdata.models import Product

from .kpi import (PERIODS, count_accuracy, count_lines, kpi_cards, leadtime_data, otif_data, period_label,
                  resolve_period, wait_data)

REPORT_ROLES = ("DIRECTOR", "PURCHASER", "ACCOUNTANT", "DISPATCHER")
D0 = Decimal(0)


def _range(request):
    """Gà STT 101: mọi tab báo cáo dùng chung resolve_period (mặc định Tháng này) – cùng KPI-4 thì tab KPI và tab
    Chờ hạ tải ra cùng một số. Trả về (d_from, d_to, ctx) – ctx cho bộ chọn kỳ và link giữ kỳ giữa các tab."""
    d_from, d_to, key = resolve_period(request)
    keep = {"period": key}
    if key == "custom":
        keep.update({"from": d_from.isoformat(), "to": d_to.isoformat()})
    from urllib.parse import urlencode
    return d_from, d_to, {"d_from": d_from, "d_to": d_to, "period": key, "periods": PERIODS,
                          "period_text": period_label(d_from, d_to), "period_qs": urlencode(keep)}


@role_required(*REPORT_ROLES)
def index(request):
    """6 KPI dự án – cùng hàm (kpi.kpi_cards) và cùng kỳ mặc định (tháng này) với Dashboard (test Gà STT 79)."""
    d_from, d_to, ctx = _range(request)
    return render(request, "reports/index.html", {"title": "Báo cáo KPI dự án", "kpis": kpi_cards(d_from, d_to, request.user),
                                                  **ctx})


def models_qty_cost():
    from django.db.models import F
    return F("qty") * F("unit_cost")


def models_onhand_cost():
    from django.db.models import F
    return F("on_hand") * F("product__avg_cost")


@role_required(*REPORT_ROLES)
def rop(request):
    """Gà STT 104 / 118: bỏ tab "Dashboard ROP" (trùng khối Sức khỏe tồn kho trên Dashboard; biểu đồ cộng lẫn bao, cây,
    tấn không so sánh được) – link cũ trỏ về trang SS/ROP (Thu mua, Giám đốc) hoặc Tồn kho."""
    if request.user.role in ("DIRECTOR", "PURCHASER", "ADMIN"):
        return redirect("/app/inventory/replenishment")
    return redirect("/app/inventory/stock")


@role_required(*REPORT_ROLES)
def nxt(request):
    """Nhập – Xuất – Tồn theo kỳ."""
    d_from, d_to, ctx = _range(request)
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
    return render(request, "reports/nxt.html", {"title": "Báo cáo Nhập – Xuất – Tồn", "rows": rows, "export": True, **ctx})


@role_required(*REPORT_ROLES)
def otif(request):
    d_from, d_to, ctx = _range(request)
    data = otif_data(d_from, d_to)
    by_day = defaultdict(lambda: [0, 0])
    for r in data["rows"]:
        d = timezone.localtime(r["s"].delivered_at).date()
        by_day[d][0] += 1
        by_day[d][1] += r["on_time"] and r["in_full"]
    days = sorted(by_day)  # Gà STT 99: trục ngày tăng dần (cũ bên trái, mới bên phải)
    chart = {"labels": [f"{d:%d/%m}" for d in days], "values": [round(by_day[d][1] / by_day[d][0] * 100, 1) for d in days]}
    if request.GET.get("export") == "xlsx":
        return excel_response(f"OTIF_{d_from:%Y%m%d}_{d_to:%Y%m%d}",
                              ["Đơn", "Khách hàng", "Công trình", "Chuyến", "Ngày cần", "Giao lúc", "Đúng hạn", "Đủ hàng"],
                              [[r["s"].so.code, r["s"].so.customer.name, r["s"].site.name, r["s"].trip.code,
                                r["s"].so.requested_date, timezone.localtime(r["s"].delivered_at).replace(tzinfo=None),
                                "Có" if r["on_time"] else "Không", "Có" if r["in_full"] else "Không"] for r in data["rows"]],
                              "OTIF")
    page, qs = paginate(request, data["rows"], 50)  # Gà STT 103: 50 dòng / trang
    return render(request, "reports/otif.html", {"title": "Báo cáo OTIF", "d": data, "chart": chart, "page_obj": page,
                                                 "qs": qs, "export": True, **ctx})


@role_required(*REPORT_ROLES)
def leadtime(request):
    d_from, d_to, ctx = _range(request)
    return render(request, "reports/leadtime.html", {"title": "Báo cáo Lead time", "d": leadtime_data(d_from, d_to), **ctx})


@role_required(*REPORT_ROLES)
def count_accuracy_report(request):
    """KPI-2: từng dòng kiểm kê đã duyệt trong kỳ – tồn sổ, số đếm, % lệch (đạt khi lệch ≤ 1%)."""
    d_from, d_to, ctx = _range(request)
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
        "value": count_accuracy(d_from, d_to), **ctx})


@role_required(*REPORT_ROLES)
def unloading(request):
    d_from, d_to, ctx = _range(request)
    return render(request, "reports/unloading.html", {"title": "Thời gian chờ hạ tải", "d": wait_data(d_from, d_to), **ctx})
