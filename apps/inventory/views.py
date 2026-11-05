from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.permissions import role_required
from apps.core.services import BusinessError, audit, next_doc_no, param
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
    return render(request, "inventory/_quants.html", {"quants": quants, "pk": pk,
                                                      "locations": Location.objects.filter(is_active=True)})


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
        "title": "Tham số SS / ROP", "rows": rows, "sel": sel, "rp": rp, "chart": chart, "sel_id": sel_id,
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


def _rop_report(rp, sel):
    """Báo cáo SS/ROP: chuỗi nhu cầu theo ngày + công thức Excel AVERAGE / STDEV.S / SQRT."""
    import io

    from django.http import HttpResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font

    days, raw, used, dropped, cut = demand_series(rp)
    wb = Workbook()
    ws = wb.active
    ws.title = "NhuCau"
    ws.append(["Ngày", "Nhu cầu gốc", "Nhu cầu dùng tính (đã loại đột biến)"])
    for d, r, u in zip(days, raw, used):
        ws.append([d, r, u])
    n = len(days) + 1
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width, ws.column_dimensions["C"].width = 14, 16, 36

    p = wb.create_sheet("SS_ROP")
    rows = [
        ("Sản phẩm", f"{rp.product.code} – {rp.product.name}"),
        ("Phương pháp", rp.get_method_display()),
        ("Z", float(rp.z)),
        ("Lead time LT (ngày)", float(rp.lead_time_days)),
        ("σLT (ngày)", float(rp.lt_std_days)),
        ("D̄ – nhu cầu TB/ngày", f"=AVERAGE(NhuCau!C2:C{n})"),
        ("σD – độ lệch chuẩn/ngày", f"=STDEV.S(NhuCau!C2:C{n})"),
        ("SS tự tính", "=B3*SQRT(B4*B7^2+B6^2*B5^2)" if rp.method == "FULL"
         else ("=B3*B7*SQRT(B4)" if rp.method == "DEMAND" else float(rp.manual_ss or 0))),
        ("ROP tự tính", "=B6*B4+B8"),
        ("Khóa thủ công", "Có" if rp.manual_lock else "Không"),
        ("SS áp dụng", float(rp.manual_ss) if rp.manual_lock else "=B8"),
        ("ROP áp dụng", float(rp.manual_rop) if rp.manual_lock else "=B9"),
        ("MOQ", float(rp.moq)),
        ("Chu kỳ đặt (ngày)", rp.review_days),
        ("Q áp dụng", float(rp.manual_q) if rp.manual_q is not None else "=MAX(B13,ROUNDUP(B6*B14,0))"),
        ("Tồn dự báo", float(sel["projected"])),
        ("Lượng PR = max(Q; ROP + Q − dự báo)", "=MAX(B15,B12+B15-B16)"),
    ]
    if rp.round_by_truck:
        rows.append(("Tải 1 xe (đv gốc)", float(rp.truck_qty)))
        rows.append(("Lượng PR làm tròn theo xe", "=CEILING(B17,B18)"))
    for r in rows:
        p.append(list(r))
    for c in p["A"]:
        c.font = Font(bold=True)
    p.column_dimensions["A"].width, p.column_dimensions["B"].width = 38, 40
    if dropped:
        x = wb.create_sheet("LoaiDotBien")
        x.append(["Ngày", "SL xuất", "Chứng từ", f"Ngưỡng loại = {cut:.1f}"])
        for d, q, note in dropped:
            x.append([d, q, note])
    buf = io.BytesIO()
    wb.save(buf)
    resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="ss_rop_{rp.product.code}.xlsx"'
    return resp


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


@role_required("DIRECTOR", "WAREHOUSE")
@business_action
def count_detail(request, pk):
    ic = get_object_or_404(InventoryCount, pk=pk)
    lines = ic.lines.select_related("product__base_uom", "location").order_by("location__code")
    if request.method == "POST":
        op = request.POST.get("op")
        if op == "cancel":
            if ic.status in ("APPROVED", "CANCELLED"):
                raise BusinessError("Không hủy được phiếu này.")
            ic.status, ic.cancel_reason = "CANCELLED", request.POST.get("reason", "")
            ic.save()
        elif op == "count" and ic.status in ("COUNTING", "RECOUNT"):
            limit = param("COUNT_VARIANCE_PCT")
            with transaction.atomic():
                need_recount = False
                for l in lines:
                    key = f"c_{l.pk}"
                    if key not in request.POST or request.POST[key] == "":
                        continue
                    v = Decimal(request.POST[key])
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
                    messages.warning(request, f"Có dòng lệch > {limit:g}% – cần đếm lại lần 2.")
                else:
                    ic.status = "PENDING_APPROVAL"
                    messages.success(request, "Đã hoàn tất đếm, chờ Director duyệt.")
                ic.save()
        elif op == "approve" and ic.status == "PENDING_APPROVAL":
            if request.user.role not in ("DIRECTOR", "ADMIN"):
                raise BusinessError("Chỉ Director được duyệt kiểm kê.")
            with transaction.atomic():
                adj = StockAdjustment.objects.create(code=next_doc_no("ADJ"), count=ic, reason=f"Kiểm kê {ic.code}",
                                                     approved_by=request.user, status="POSTED")
                for l in lines:
                    # tồn có thể đã thay đổi trong lúc đếm -> điều chỉnh theo chênh lệch so với tồn sổ lúc chụp
                    diff = (l.final_qty or 0) - l.book_qty
                    if diff:
                        StockAdjustmentLine.objects.create(adjustment=adj, product=l.product, location=l.location,
                                                           qty_diff=diff, unit_cost=l.product.avg_cost)
                        post_stock_move(l.product, l.location, diff, "ADJ", "ADJ", adj.pk, note=adj.code)
                ic.status, ic.approved_by = "APPROVED", request.user
                ic.save()
                audit("APPROVE", ic)
            messages.success(request, f"Đã duyệt và ghi bút toán điều chỉnh {adj.code}.")
        return redirect(f"/app/inventory/counts/{pk}")
    blind = ic.status in ("COUNTING", "RECOUNT") and request.user.role == "WAREHOUSE"
    adj_value = sum(((l.final_qty or 0) - l.book_qty) * l.product.avg_cost for l in lines if l.final_qty is not None)
    return render(request, "inventory/count_detail.html", {
        "title": f"Kiểm kê {ic.code}", "ic": ic, "lines": lines, "blind": blind, "adj_value": adj_value,
        "limit": param("COUNT_VARIANCE_PCT")})
