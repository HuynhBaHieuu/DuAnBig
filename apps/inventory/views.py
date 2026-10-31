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
from .services import calc_param, post_stock_move, run_replenishment, stock_summary, transfer

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
    rp, chart = None, None
    if sel:
        rp, _ = ReplenishmentParam.objects.get_or_create(product_id=sel_id, defaults={"z": param("SERVICE_Z")})
        today = timezone.localdate()
        start = today - timedelta(days=rp.history_days)
        daily = {}
        for at, q in StockMove.objects.filter(product_id=sel_id, move_type__in=["OUT_SALE", "OUT_INTERNAL"],
                                              at__date__gte=start).values_list("at", "qty"):
            d = timezone.localtime(at).date()
            daily[d] = daily.get(d, 0) + float(-q)
        labels = [(start + timedelta(days=i)) for i in range(rp.history_days + 1)]
        snaps = {s.date: s for s in sel["product"].rop_snapshots.filter(date__gte=start)}
        chart = {
            "labels": [d.strftime("%d/%m") for d in labels],
            "demand": [daily.get(d, 0) for d in labels],
            "projected": [float(snaps[d].projected) if d in snaps else None for d in labels],
            "rop": float(sel["rop"]), "ss": float(sel["ss"]), "davg": float(sel["d_avg"]),
        }
    return render(request, "inventory/replenishment.html", {
        "title": "Tham số SS / ROP", "rows": rows, "sel": sel, "rp": rp, "chart": chart, "sel_id": sel_id})


@role_required("DIRECTOR", "PURCHASER")
@require_POST
@business_action
def replenishment_save(request, pk):
    rp = get_object_or_404(ReplenishmentParam, product_id=pk)
    op = request.POST.get("op")
    if op == "run_all":
        res = run_replenishment()
        messages.success(request, f"Đã tính lại {res['products']} sản phẩm."
                         + (f" Sinh PR tự động {res['auto_pr']} ({res['lines']} dòng)." if res["auto_pr"] else ""))
        return redirect(f"/app/inventory/replenishment?product={pk}")
    rp.method = request.POST.get("method", rp.method)
    rp.z = Decimal(request.POST.get("z") or rp.z)
    rp.review_days = int(request.POST.get("review_days") or rp.review_days)
    rp.history_days = int(request.POST.get("history_days") or rp.history_days)
    rp.lead_time_days = Decimal(request.POST.get("lead_time_days") or rp.lead_time_days)
    rp.manual_ss = Decimal(request.POST["manual_ss"]) if request.POST.get("manual_ss") else None
    rp.manual_rop = Decimal(request.POST["manual_rop"]) if request.POST.get("manual_rop") else None
    rp.save()
    calc_param(rp.product)
    messages.success(request, f"Đã lưu & tính lại SS/ROP cho {rp.product.code}.")
    return redirect(f"/app/inventory/replenishment?product={pk}")


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
        rows = [(timezone.localtime(m.at).strftime("%d/%m/%Y %H:%M"), m.product.code, m.product.name, m.location.code,
                 m.get_move_type_display(), m.note, m.qty, m.balance_after,
                 *( [m.unit_cost] if request.user.can_see_cost else [])) for m in qs[:5000]]
        head = ["Thời gian", "Mã SP", "Tên SP", "Vị trí", "Loại", "Chứng từ", "SL", "Tồn sau"] + \
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
