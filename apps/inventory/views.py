from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.permissions import role_required
from apps.core.services import BusinessError, audit, next_doc_no, param, raise_alert, resolve_alerts
from apps.core.utils import business_action, excel_response, paginate
from apps.masterdata.models import Location, Product, ProductCategory, Warehouse

from .models import (InventoryCount, InventoryCountLine, ReplenishmentParam, StockAdjustment, StockAdjustmentLine,
                     StockMove, StockQuant)
from .services import calc_param, demand_series, post_stock_move, run_replenishment, stock_summary, transfer

INTERNAL = ("DIRECTOR", "PURCHASER", "DISPATCHER", "WAREHOUSE", "ACCOUNTANT")
LEVEL_LABEL = {"OK": ("Đủ hàng", "success"), "BELOW_ROP": ("Dưới ROP", "warn"), "BELOW_SS": ("Dưới SS", "danger"),
               "STOCKOUT": ("Hết hàng", "danger")}


def _bar(s):
    top = max(float(s["projected"]), float(s["rop"]) * 1.5, float(s["ss"]) * 2, 1)
    s["bar"] = {"fill": max(0, min(100, float(s["projected"]) / top * 100)),
                "ss": float(s["ss"]) / top * 100, "rop": float(s["rop"]) / top * 100}
    s["level_label"], s["level_tone"] = LEVEL_LABEL[s["level"]]
    return s


# ------------------------------------------------------------------ MH-10 Tồn kho
@role_required(*INTERNAL)
def stock(request):
    products = Product.objects.filter(is_active=True).select_related("base_uom", "category")
    q = request.GET.get("q", "").strip()
    if q:
        products = products.filter(Q(code__icontains=q) | Q(name__icontains=q))
    if request.GET.get("cat"):
        products = products.filter(category_id=request.GET["cat"])
    rows = [_bar(s) for s in stock_summary(list(products)).values()]
    level = request.GET.get("level")
    if level == "low":
        rows = [r for r in rows if r["level"] != "OK"]
    elif level:
        rows = [r for r in rows if r["level"] == level]
    rows.sort(key=lambda r: ({"STOCKOUT": 0, "BELOW_SS": 1, "BELOW_ROP": 2, "OK": 3}[r["level"]], r["product"].code))

    if request.GET.get("export") == "xlsx":
        head = ["Mã", "Tên", "ĐVT", "Tồn thực", "Đã giữ", "Khả dụng", "Đang về", "Dự báo", "SS", "ROP", "Số ngày đủ"]
        if request.user.can_see_cost:
            head.append("Giá trị tồn")
        data = []
        for r in rows:
            p = r["product"]
            line = [p.code, p.name, p.base_uom.code, r["on_hand"], r["reserved"], r["available"], r["incoming"],
                    r["projected"], r["ss"], r["rop"], round(float(r["days_cover"]), 1) if r["days_cover"] else None]
            if request.user.can_see_cost:
                line.append(r["on_hand"] * p.avg_cost)
            data.append(line)
        return excel_response(f"ton_kho_{timezone.localdate():%Y%m%d}", head, data, "Tồn kho")

    totals = {k: sum((r[k] for r in rows), Decimal(0)) for k in ("on_hand", "reserved", "available", "incoming")}
    value = sum((r["on_hand"] * r["product"].avg_cost for r in rows), Decimal(0))
    counts = {k: sum(1 for r in rows if r["level"] == k) for k in LEVEL_LABEL}
    return render(request, "inventory/stock.html", {
        "title": "Tồn kho", "rows": rows, "totals": totals, "value": value, "counts": counts,
        "categories": ProductCategory.objects.filter(is_active=True), "levels": LEVEL_LABEL,
    })


@role_required(*INTERNAL)
def stock_locations(request, pk):
    """HTMX: chi tiết tồn theo vị trí (FIFO)."""
    quants = StockQuant.objects.filter(product_id=pk, on_hand__gt=0).select_related("location__warehouse") \
        .order_by("in_date_first")
    product = get_object_or_404(Product.objects.select_related("base_uom"), pk=pk)
    return render(request, "inventory/_quants.html", {
        "quants": quants, "pk": pk, "product": product,
        "locations": Location.objects.filter(is_active=True).select_related("warehouse")
        .order_by("loc_type", "warehouse__name", "code")})


@role_required("WAREHOUSE", "DIRECTOR")
@require_POST
@business_action
def stock_add(request):
    """Thêm tồn thực (nhập điều chỉnh tăng) – vd. khai báo tồn đầu kỳ, hàng phát hiện thừa.

    Đi qua phiếu điều chỉnh ADJ + post_stock_move để có sổ kho / nhật ký; có hàng -> tự giữ cho đơn backorder."""
    from apps.sales.services import retry_backorders

    p = get_object_or_404(Product, pk=request.POST["product"])
    loc = get_object_or_404(Location, pk=request.POST["location"], is_active=True)
    qty = _num(request, "qty")
    if not qty or qty <= 0:
        raise BusinessError("Số lượng thêm phải lớn hơn 0.")
    reason = request.POST.get("reason", "").strip()
    if not reason:
        raise BusinessError("Phải nhập lý do thêm tồn.")
    cost = _num(request, "unit_cost") if request.user.can_see_cost else None
    with transaction.atomic():
        prod = Product.objects.select_for_update().get(pk=p.pk)
        if cost is None:
            cost = prod.avg_cost or prod.standard_cost
        total = StockQuant.objects.filter(product=prod).aggregate(s=Sum("on_hand"))["s"] or Decimal(0)
        if total + qty > 0:  # giá vốn bình quân gia quyền như nhập mua
            prod.avg_cost = ((total * prod.avg_cost + qty * cost) / (total + qty)).quantize(Decimal(1))
            prod.save(update_fields=["avg_cost", "updated_at"])
        adj = StockAdjustment.objects.create(code=next_doc_no("ADJ"), reason=f"Thêm tồn thực: {reason}",
                                             approved_by=request.user, status="POSTED")
        StockAdjustmentLine.objects.create(adjustment=adj, product=prod, location=loc, qty_diff=qty, unit_cost=cost)
        post_stock_move(prod, loc, qty, "ADJ", "ADJ", adj.pk, unit_cost=cost, note=f"{adj.code} – {reason}"[:255])
        audit("CREATE", adj, {"product": prod.code, "location": loc.code, "qty": str(qty), "reason": reason})
        retry_backorders(prod.pk)
    messages.success(request, f"Đã thêm {qty:g} {prod.base_uom.code} {prod.code} vào {loc.code} (phiếu {adj.code}).")
    return redirect(request.META.get("HTTP_REFERER") or "/app/inventory/stock")


@role_required("WAREHOUSE", "DIRECTOR")
@require_POST
@business_action
def stock_transfer(request):
    p = get_object_or_404(Product, pk=request.POST["product"])
    src = get_object_or_404(Location, pk=request.POST["from"])
    dst = get_object_or_404(Location, pk=request.POST["to"])
    if src == dst:
        raise BusinessError("Vị trí đích phải khác vị trí nguồn.")
    transfer(p, src, dst, Decimal(request.POST["qty"]), note="Chuyển vị trí")
    messages.success(request, f"Đã chuyển {request.POST['qty']} {p.code} từ {src} sang {dst}.")
    return redirect(request.META.get("HTTP_REFERER") or "/app/inventory/stock")


# ------------------------------------------------------------------ MH-12 SS/ROP
@role_required("DIRECTOR", "PURCHASER")
def replenishment(request):
    products = list(Product.objects.filter(is_active=True, track_rop=True).select_related("base_uom"))
    summary = {pid: _bar(s) for pid, s in stock_summary(products).items()}
    if request.GET.get("export") == "all":
        return _rop_report_all(summary)
    rows = list(summary.values())
    if request.GET.get("level") == "low":
        rows = [r for r in rows if r["level"] != "OK"]
    rows.sort(key=lambda r: ({"STOCKOUT": 0, "BELOW_SS": 1, "BELOW_ROP": 2, "OK": 3}[r["level"]], r["product"].code))
    sel_id = int(request.GET.get("product") or (rows[0]["product"].pk if rows else 0))
    sel = summary.get(sel_id)
    rp, chart, dropped = None, None, []
    if sel:
        rp, _ = ReplenishmentParam.objects.get_or_create(product_id=sel_id, defaults={"z": param("SERVICE_Z")})
        if request.GET.get("export") == "xlsx":
            return _rop_report(rp, sel)
        days, raw, used, dropped, _ = demand_series(rp, timezone.localdate() + timedelta(days=1))  # gồm cả hôm nay
        snaps = {s.date: s for s in sel["product"].rop_snapshots.filter(date__gte=days[0])} if days else {}
        chart = {
            "labels": [d.strftime("%d/%m") for d in days],
            "demand": used, "dropped": [r - u for r, u in zip(raw, used)],
            "projected": [float(snaps[d].projected) if d in snaps else None for d in days],
            "rop": float(sel["rop"]), "ss": float(sel["ss"]), "davg": float(sel["d_avg"]),
        }
    calc = None
    if rp:
        q, proj = rp.order_q, sel["projected"]
        raw = max(q, sel["rop"] + q - proj, Decimal(0))
        calc = {"q": q, "raw": raw, "pr": rp.pr_qty(proj), "proj": proj, "rop": sel["rop"],
                "trips": (rp.pr_qty(proj) / rp.truck_qty) if rp.truck_qty else None}
    return render(request, "inventory/replenishment.html", {
        "title": "Tham số mốc đặt hàng lại", "rows": rows, "sel": sel, "rp": rp, "chart": chart, "sel_id": sel_id,
        "dropped": dropped, "calc": calc, "can_lock": request.user.role in LOCK_ROLES})


LOCK_ROLES = ("DIRECTOR", "PURCHASER")
AUDIT_FIELDS = ["method", "z", "review_days", "history_days", "lead_time_days", "demand_source", "exclude_outliers",
                "manual_lock", "manual_ss", "manual_rop", "lock_reason", "moq", "manual_q", "round_by_truck",
                "truck_ton"]


def _num(request, name, cast=Decimal, default=None):
    v = (request.POST.get(name) or "").strip().replace(",", ".")
    if v == "":
        return default
    try:
        n = cast(v)
    except Exception:
        raise BusinessError(f"Giá trị '{v}' không hợp lệ.")
    if n < 0:
        raise BusinessError("Tham số không được âm.")
    return n


@role_required(*LOCK_ROLES)
@require_POST
@business_action
def replenishment_save(request, pk):
    rp = get_object_or_404(ReplenishmentParam.objects.select_related("product"), product_id=pk)
    op = request.POST.get("op")
    if op == "run_all":
        res = run_replenishment()
        messages.success(request, f"Đã tính lại {res['products']} sản phẩm."
                         + (f" Sinh PR tự động {res['auto_pr']} ({res['lines']} dòng)." if res["auto_pr"] else ""))
        return redirect(f"/app/inventory/replenishment?product={pk}")
    old = {f: getattr(rp, f) for f in AUDIT_FIELDS}
    rp.method = request.POST.get("method", rp.method)
    rp.z = _num(request, "z", default=rp.z)
    rp.review_days = _num(request, "review_days", int, rp.review_days)
    rp.history_days = max(7, _num(request, "history_days", int, rp.history_days))
    rp.lead_time_days = _num(request, "lead_time_days", default=rp.lead_time_days)
    rp.demand_source = request.POST.get("demand_source", rp.demand_source)
    rp.exclude_outliers = request.POST.get("exclude_outliers") == "on"
    rp.moq = _num(request, "moq", default=Decimal(0))
    rp.manual_q = _num(request, "manual_q")
    rp.round_by_truck = request.POST.get("round_by_truck") == "on"
    rp.truck_ton = _num(request, "truck_ton", default=Decimal(30))
    if rp.round_by_truck and not rp.truck_ton:
        raise BusinessError("Tải trọng xe chuẩn phải lớn hơn 0.")

    # Khóa thủ công: chỉ PURCHASER / DIRECTOR được bật/tắt (ADMIN sửa tham số khác, khóa giữ nguyên)
    if request.user.role in LOCK_ROLES:
        rp.manual_lock = request.POST.get("manual_lock") == "on"
        if rp.manual_lock:
            rp.manual_ss, rp.manual_rop = _num(request, "manual_ss"), _num(request, "manual_rop")
            rp.lock_reason = request.POST.get("lock_reason", "").strip()
            if rp.manual_ss is None or rp.manual_rop is None or not rp.lock_reason:
                raise BusinessError("Khóa thủ công bắt buộc nhập SS, ROP và Lý do.")
            if rp.manual_rop < rp.manual_ss:
                raise BusinessError(f"ROP ({rp.manual_rop:g}) phải lớn hơn hoặc bằng SS ({rp.manual_ss:g}).")
        else:
            rp.manual_ss = rp.manual_rop = None
            rp.lock_reason = ""

    with transaction.atomic():
        rp.save()
        rp, _ = calc_param(rp.product)
        if rp.d_avg == 0 and rp.manual_q is None:
            raise BusinessError("Vật tư chưa có lịch sử xuất (D̄ = 0): bắt buộc nhập Q tay.")
        rp.suggested_q = rp.pr_qty(stock_summary([rp.product])[rp.product_id]["projected"])
        rp.save(update_fields=["suggested_q"])
        changes = {f: [str(old[f]), str(getattr(rp, f))] for f in AUDIT_FIELDS if old[f] != getattr(rp, f)}
        if changes:
            audit("UPDATE", rp, changes)
    messages.success(request, f"Đã lưu & tính lại SS/ROP cho {rp.product.code}.")
    return redirect(f"/app/inventory/replenishment?product={pk}")


def _rop_values(rp, used, projected):
    """Tính sẵn mọi chỉ số SS/ROP bằng Python (khớp công thức Excel) để file mở ra là có số ngay.

    openpyxl không lưu được kết quả tính của công thức -> Excel ở chế độ Protected View / trình xem trên
    điện thoại hiển thị ô công thức trống. Vì vậy báo cáo ghi cả GIÁ TRỊ lẫn CÔNG THỨC."""
    import math
    import statistics

    z, lt, lt_std = float(rp.z), float(rp.lead_time_days), float(rp.lt_std_days)
    d_avg = statistics.fmean(used) if used else 0.0
    d_std = statistics.stdev(used) if len(used) > 1 else 0.0  # STDEV.S
    if rp.method == "FULL":
        ss = z * math.sqrt(lt * d_std ** 2 + d_avg ** 2 * lt_std ** 2)
    elif rp.method == "DEMAND":
        ss = z * d_std * math.sqrt(lt)
    else:
        ss = float(rp.manual_ss or 0)
    rop = d_avg * lt + ss
    ss_eff = float(rp.manual_ss) if rp.manual_lock and rp.manual_ss is not None else ss
    rop_eff = float(rp.manual_rop) if rp.manual_lock and rp.manual_rop is not None else rop
    q = float(rp.manual_q) if rp.manual_q is not None else max(float(rp.moq), math.ceil(round(d_avg * rp.review_days, 9)))
    pr = max(q, rop_eff + q - float(projected))
    truck = float(rp.truck_qty) if rp.round_by_truck else None
    pr_truck = math.ceil(pr / truck) * truck if truck else None
    return {"z": z, "lt": lt, "lt_std": lt_std, "d_avg": d_avg, "d_std": d_std, "ss": ss, "rop": rop,
            "ss_eff": ss_eff, "rop_eff": rop_eff, "q": q, "projected": float(projected), "pr": pr,
            "truck": truck, "pr_truck": pr_truck}


def _xlsx_response(wb, filename):
    import io

    from django.http import HttpResponse
    wb.calculation.fullCalcOnLoad = True  # Excel tính lại toàn bộ công thức khi mở file
    buf = io.BytesIO()
    wb.save(buf)
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{filename}.xlsx"'
    return resp


def _style_head(ws, row=1):
    from openpyxl.styles import Alignment, Font, PatternFill
    for c in ws[row]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="E65F2B")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _rop_report(rp, sel):
    """Báo cáo SS/ROP 1 sản phẩm: chuỗi nhu cầu theo ngày + mỗi chỉ số có giá trị tính sẵn và công thức Excel."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    days, raw, used, dropped, cut = demand_series(rp)
    v = _rop_values(rp, used, sel["projected"])
    wb = Workbook()
    ws = wb.active
    ws.title = "NhuCau"
    ws.append(["Ngày", "Nhu cầu gốc", "Nhu cầu dùng tính (đã loại đột biến)"])
    _style_head(ws)
    for d, r, u in zip(days, raw, used):
        ws.append([d, r, u])
        ws.cell(ws.max_row, 1).number_format = "yyyy-mm-dd"
    n = len(days) + 1
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width, ws.column_dimensions["C"].width = 14, 16, 36

    p = wb.create_sheet("SS_ROP", 0)
    p.append(["Chỉ số", "Giá trị (hệ thống tính)", "Excel tự tính (công thức)", "Công thức"])
    _style_head(p)
    ss_f = {"FULL": "=C4*SQRT(C5*C8^2+C7^2*C6^2)", "DEMAND": "=C4*C8*SQRT(C5)"}.get(rp.method, v["ss"])
    rows = [
        ("Sản phẩm", f"{rp.product.code} – {rp.product.name}", "", ""),
        ("Phương pháp", rp.get_method_display(), "", ""),
        ("Z", v["z"], "=B4", "Hệ số mức phục vụ"),
        ("Lead time LT (ngày)", v["lt"], "=B5", "TB lead time thực tế các GRN"),
        ("σLT (ngày)", v["lt_std"], "=B6", "Độ lệch chuẩn lead time"),
        ("D̄ – nhu cầu TB/ngày", v["d_avg"], f"=AVERAGE(NhuCau!C2:C{n})", "AVERAGE(nhu cầu/ngày)"),
        ("σD – độ lệch chuẩn/ngày", v["d_std"], f"=STDEV.S(NhuCau!C2:C{n})", "STDEV.S(nhu cầu/ngày)"),
        ("SS tự tính", v["ss"], ss_f, {"FULL": "Z × √(LT × σD² + D̄² × σLT²)", "DEMAND": "Z × σD × √LT"}
         .get(rp.method, "Nhập tay")),
        ("ROP tự tính", v["rop"], "=C7*C5+C9", "D̄ × LT + SS"),
        ("Khóa thủ công", "Có" if rp.manual_lock else "Không", "", rp.lock_reason),
        ("SS áp dụng", v["ss_eff"], float(rp.manual_ss) if rp.manual_lock and rp.manual_ss is not None else "=C9",
         "Số khóa tay nếu bật khóa, ngược lại SS tự tính"),
        ("ROP áp dụng", v["rop_eff"], float(rp.manual_rop) if rp.manual_lock and rp.manual_rop is not None else "=C10",
         "Số khóa tay nếu bật khóa, ngược lại ROP tự tính"),
        ("MOQ", float(rp.moq), "=B14", "Lượng đặt tối thiểu NCC"),
        ("Chu kỳ đặt (ngày)", rp.review_days, "=B15", ""),
        ("Q áp dụng", v["q"], float(rp.manual_q) if rp.manual_q is not None else "=MAX(C14,ROUNDUP(C7*C15,0))",
         "Q nhập tay, hoặc max(MOQ; D̄ × chu kỳ)"),
        ("Tồn dự báo", v["projected"], "=B17", "Khả dụng + đang về"),
        ("Lượng PR", v["pr"], "=MAX(C16,C13+C16-C17)", "max(Q; ROP + Q − dự báo)"),
    ]
    if rp.round_by_truck:
        rows.append(("Tải 1 xe (đv gốc)", v["truck"], "=B19", f"{rp.truck_ton} tấn/xe"))
        rows.append(("Lượng PR làm tròn theo xe", v["pr_truck"], "=CEILING(C18,C19)", "CEILING(PR; tải 1 xe)"))
    for r in rows:
        p.append(list(r))
    for row in p.iter_rows(min_row=2):
        row[0].font = Font(bold=True)
        for c in row[1:3]:
            if isinstance(c.value, float) or (isinstance(c.value, str) and c.value.startswith("=")):
                c.number_format = "#,##0.000"
    p.column_dimensions["A"].width, p.column_dimensions["B"].width = 30, 34
    p.column_dimensions["C"].width, p.column_dimensions["D"].width = 26, 44
    if dropped:
        x = wb.create_sheet("LoaiDotBien")
        x.append(["Ngày", "SL xuất", "Chứng từ", f"Ngưỡng loại = {cut:.1f}"])
        _style_head(x)
        for d, q, note in dropped:
            x.append([d, q, note])
    return _xlsx_response(wb, f"ss_rop_{rp.product.code}")


def _rop_report_all(summary):
    """Báo cáo SS/ROP TẤT CẢ sản phẩm: 1 dòng / sản phẩm, có giá trị tính sẵn + công thức Excel theo từng cột."""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "TongHop"
    nc = wb.create_sheet("NhuCau")
    head = ["Mã SP", "Tên SP", "ĐVT", "Phương pháp", "Z", "LT (ngày)", "σLT (ngày)", "D̄/ngày", "σD/ngày",
            "SS tự tính", "ROP tự tính", "Khóa tay", "SS áp dụng", "ROP áp dụng", "MOQ", "Chu kỳ (ngày)", "Q áp dụng",
            "Khả dụng", "Đang về", "Tồn dự báo", "Lượng PR", "Tình trạng",
            # cột công thức Excel tự tính lại (đối chiếu)
            "D̄ (Excel)", "σD (Excel)", "SS (Excel)", "ROP (Excel)", "Q (Excel)", "Lượng PR (Excel)"]
    ws.append(head)
    _style_head(ws)
    level_txt = {"OK": "Đủ hàng", "BELOW_ROP": "Dưới ROP", "BELOW_SS": "Dưới SS", "STOCKOUT": "Hết hàng"}
    params = {rp.product_id: rp for rp in ReplenishmentParam.objects.filter(product_id__in=summary)}
    col = 0
    for pid, s in sorted(summary.items(), key=lambda kv: kv[1]["product"].code):
        rp = params.get(pid) or ReplenishmentParam.objects.create(product_id=pid, z=param("SERVICE_Z"))
        days, _, used, _, _ = demand_series(rp)
        v = _rop_values(rp, used, s["projected"])
        col += 1
        L = get_column_letter(col)
        nc.cell(1, col, rp.product.code)
        nc.cell(2, col, f"{days[0]:%d/%m}–{days[-1]:%d/%m}" if days else "")
        for i, u in enumerate(used, start=3):
            nc.cell(i, col, u)
        rng = f"NhuCau!{L}3:{L}{max(3, len(used) + 2)}"
        r = ws.max_row + 1
        locked = rp.manual_lock and rp.manual_ss is not None and rp.manual_rop is not None
        ws.append([
            rp.product.code, rp.product.name, rp.product.base_uom.code, rp.get_method_display(), v["z"], v["lt"],
            v["lt_std"], v["d_avg"], v["d_std"], v["ss"], v["rop"], "Có" if locked else "Không", v["ss_eff"],
            v["rop_eff"], float(rp.moq), rp.review_days, v["q"], float(s["available"]), float(s["incoming"]),
            v["projected"], v["pr_truck"] if v["pr_truck"] is not None else v["pr"], level_txt[s["level"]],
            f"=AVERAGE({rng})", f"=STDEV.S({rng})",
            {"FULL": f"=E{r}*SQRT(F{r}*X{r}^2+W{r}^2*G{r}^2)", "DEMAND": f"=E{r}*X{r}*SQRT(F{r})"}.get(rp.method, v["ss"]),
            f"=W{r}*F{r}+Y{r}",
            float(rp.manual_q) if rp.manual_q is not None else f"=MAX(O{r},ROUNDUP(W{r}*P{r},0))",
            (f"=CEILING(MAX(AA{r},Z{r}+AA{r}-T{r}),{v['truck']})" if v["truck"] else f"=MAX(AA{r},Z{r}+AA{r}-T{r})")
            if not locked else (f"=CEILING(MAX(AA{r},{v['rop_eff']}+AA{r}-T{r}),{v['truck']})" if v["truck"]
                                else f"=MAX(AA{r},{v['rop_eff']}+AA{r}-T{r})"),
        ])
    last = ws.max_row
    for row in ws.iter_rows(min_row=2, max_row=last, min_col=5):
        for c in row:
            if c.column_letter != "V" and c.column_letter != "L":
                c.number_format = "#,##0.000"
    widths = {"A": 12, "B": 30, "D": 26, "V": 12}
    for i in range(1, len(head) + 1):
        L = get_column_letter(i)
        ws.column_dimensions[L].width = widths.get(L, 13)
    ws.freeze_panes = "C2"
    return _xlsx_response(wb, f"ss_rop_tat_ca_{timezone.localdate():%Y%m%d}")


# ------------------------------------------------------------------ Sổ kho
@role_required("DIRECTOR", "WAREHOUSE", "ACCOUNTANT", "PURCHASER")
def moves(request):
    qs = StockMove.objects.select_related("product", "location", "user")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(product__code__icontains=q) | Q(product__name__icontains=q) | Q(note__icontains=q))
    if request.GET.get("type"):
        qs = qs.filter(move_type=request.GET["type"])
    if request.GET.get("from"):
        qs = qs.filter(at__date__gte=request.GET["from"])
    if request.GET.get("to"):
        qs = qs.filter(at__date__lte=request.GET["to"])
    if request.GET.get("export") == "xlsx":
        # Ngày là kiểu Date (YYYY-MM-DD) để PivotTable gom nhóm được; SL nhập / xuất tách cột, đều là số dương
        rows = [(timezone.localtime(m.at).date(), m.product.code, m.product.name, m.location.code,
                 m.get_move_type_display(), m.note, m.qty if m.qty > 0 else 0, -m.qty if m.qty < 0 else 0,
                 m.balance_after, *([m.unit_cost] if request.user.can_see_cost else [])) for m in qs[:5000]]
        head = ["Ngày", "Mã SP", "Tên SP", "Vị trí", "Loại", "Chứng từ", "SL nhập", "SL xuất", "Tồn sau"] + \
               (["Đơn giá vốn"] if request.user.can_see_cost else [])
        return excel_response("so_kho", head, rows, "Sổ kho")
    page, qs_str = paginate(request, qs, 30)
    return render(request, "inventory/moves.html", {"title": "Sổ kho", "page_obj": page, "qs": qs_str,
                                                    "types": StockMove.MOVE_TYPES})


# ------------------------------------------------------------------ Kiểm kê (IC) – đếm mù 2 lần
@role_required("DIRECTOR", "WAREHOUSE")
def counts(request):
    if request.method == "POST":
        wh = get_object_or_404(Warehouse, pk=request.POST["warehouse"])
        with transaction.atomic():
            ic = InventoryCount.objects.create(code=next_doc_no("IC"), warehouse=wh, status="COUNTING",
                                               note=request.POST.get("note", ""), created_by=request.user)
            for qt in StockQuant.objects.filter(location__warehouse=wh, location__loc_type="STORAGE") \
                    .select_related("product", "location"):
                InventoryCountLine.objects.create(count=ic, product=qt.product, location=qt.location,
                                                  book_qty=qt.on_hand)
        messages.success(request, f"Đã tạo phiếu kiểm kê {ic.code}.")
        return redirect(f"/app/inventory/counts/{ic.pk}")
    page, qs_str = paginate(request, InventoryCount.objects.select_related("warehouse", "created_by"))
    return render(request, "inventory/counts.html", {"title": "Kiểm kê", "page_obj": page, "qs": qs_str,
                                                     "warehouses": Warehouse.objects.filter(is_active=True)})


def _approve_count(ic, lines, user):
    """Giám đốc duyệt phiếu kiểm kê -> sinh phiếu điều chỉnh ADJ + bút toán kho theo chênh lệch."""
    adj = StockAdjustment.objects.create(code=next_doc_no("ADJ"), count=ic, reason=f"Kiểm kê {ic.code}",
                                         approved_by=user, status="POSTED")
    for l in lines:
        # tồn có thể đã thay đổi trong lúc đếm -> điều chỉnh theo chênh lệch so với tồn sổ lúc chụp
        diff = (l.final_qty or 0) - l.book_qty
        if diff:
            StockAdjustmentLine.objects.create(adjustment=adj, product=l.product, location=l.location,
                                               qty_diff=diff, unit_cost=l.product.avg_cost)
            post_stock_move(l.product, l.location, diff, "ADJ", "ADJ", adj.pk, note=adj.code)
    ic.status, ic.approved_by = "APPROVED", user
    ic.save()
    resolve_alerts("COUNT_APPROVAL", "inventory_count", ic.pk)
    audit("APPROVE", ic)
    return adj


@role_required("DIRECTOR", "WAREHOUSE")
@business_action
def count_detail(request, pk):
    ic = get_object_or_404(InventoryCount, pk=pk)
    lines = list(ic.lines.select_related("product__base_uom", "location").order_by("location__code"))
    if request.method == "POST":
        op = request.POST.get("op")
        if op == "cancel":
            if ic.status in ("APPROVED", "CANCELLED"):
                raise BusinessError("Không hủy được phiếu này.")
            ic.status, ic.cancel_reason = "CANCELLED", request.POST.get("reason", "")
            ic.save()
        elif op in ("count", "count_approve") and ic.status in ("COUNTING", "RECOUNT"):
            if op == "count_approve" and request.user.role not in ("DIRECTOR", "ADMIN"):
                raise BusinessError("Chỉ Giám đốc được duyệt kiểm kê.")
            limit = param("COUNT_VARIANCE_PCT")
            with transaction.atomic():
                need_recount = False
                for l in lines:
                    key = f"c_{l.pk}"
                    if key not in request.POST or request.POST[key] == "":
                        continue
                    try:
                        v = Decimal(request.POST[key].replace(",", "."))
                    except Exception:
                        raise BusinessError(f"Số đếm '{request.POST[key]}' không hợp lệ.")
                    if v < 0:
                        raise BusinessError("Số đếm không được âm.")
                    if ic.status == "COUNTING":
                        l.count1 = v
                    else:
                        l.count2 = v
                    l.final_qty = l.count2 if l.count2 is not None else l.count1
                    l.save()
                    if ic.status == "COUNTING" and l.variance_pct is not None and l.variance_pct > limit:
                        need_recount = True
                missing = [l for l in lines if l.count1 is None]
                if missing:
                    raise BusinessError(f"Còn {len(missing)} dòng chưa nhập số đếm.")
                if ic.status == "COUNTING" and need_recount:
                    ic.status = "RECOUNT"
                    ic.save()
                    messages.warning(request, f"Có dòng lệch > {limit:g}% – cần đếm lại lần 2 trước khi duyệt.")
                else:
                    ic.status = "PENDING_APPROVAL"
                    ic.save()
                    if op == "count_approve":
                        adj = _approve_count(ic, lines, request.user)
                        messages.success(request, f"Đã lưu số đếm, duyệt phiếu và ghi bút toán điều chỉnh {adj.code}.")
                    else:
                        messages.success(request, "Đã hoàn tất đếm, chờ Giám đốc duyệt.")
                        raise_alert("COUNT_APPROVAL", f"Kiểm kê {ic.code} chờ duyệt", ic.warehouse.name,
                                    ref_type="inventory_count", ref_id=ic.pk, url=f"/app/inventory/counts/{ic.pk}",
                                    target_role="DIRECTOR")
        elif op == "approve" and ic.status == "PENDING_APPROVAL":
            if request.user.role not in ("DIRECTOR", "ADMIN"):
                raise BusinessError("Chỉ Giám đốc được duyệt kiểm kê.")
            with transaction.atomic():
                adj = _approve_count(ic, lines, request.user)
            messages.success(request, f"Đã duyệt và ghi bút toán điều chỉnh {adj.code}.")
        return redirect(f"/app/inventory/counts/{pk}")
    blind = ic.status in ("COUNTING", "RECOUNT") and request.user.role == "WAREHOUSE"
    adj_value = sum(((l.final_qty or 0) - l.book_qty) * l.product.avg_cost for l in lines if l.final_qty is not None)
    return render(request, "inventory/count_detail.html", {
        "title": f"Kiểm kê {ic.code}", "ic": ic, "lines": lines, "blind": blind, "adj_value": adj_value,
        "is_director": request.user.role in ("DIRECTOR", "ADMIN"),
        "limit": param("COUNT_VARIANCE_PCT")})
