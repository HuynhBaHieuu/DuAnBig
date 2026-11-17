import math
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.db.models import Case, IntegerField, Q, When
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.permissions import role_required
from apps.core.services import BusinessError, audit, next_doc_no, param, raise_alert, resolve_alerts
from apps.core.utils import business_action, excel_response, paginate
from apps.masterdata.models import Location, Product, ProductCategory, Warehouse

from .models import (InventoryCount, InventoryCountLine, ReplenishmentJobRun, ReplenishmentParam, StockAdjustment, StockAdjustmentLine,
                     StockMove, StockQuant)
from .services import calc_param, post_stock_move, rop_explain, run_replenishment, stock_summary, transfer
from .simulation import SIM_CODE
from apps.core.templatetags.tracon import qty as fmt_qty
from apps.core.fmt import money as _money, qty_unit as _qu, qty as _q, num as _fnum

INTERNAL = ("DIRECTOR", "PURCHASER", "DISPATCHER", "WAREHOUSE", "ACCOUNTANT")
LEVEL_LABEL = {"OK": ("Bình thường", "success"), "BELOW_ROP": ("Chạm ROP", "warn"), "BELOW_SS": ("Dưới SS", "danger"),
               "STOCKOUT": ("Hết hàng", "danger")}


def _bar(s):
    top = max(float(s["projected"]), float(s["rop"]) * 1.5, float(s["ss"]) * 2, 1)
    s["bar"] = {"fill": max(0, min(100, float(s["projected"]) / top * 100)),
                "ss": float(s["ss"]) / top * 100, "rop": float(s["rop"]) / top * 100}
    # Màu thanh theo TỒN DỰ KIẾN (chính là chỉ số được vẽ và so với vạch SS/ROP), không theo Khả dụng:
    # ≤ SS (hoặc ≤ 0) đỏ · ≤ ROP cam · trên ROP xanh
    proj, ss, rop = s["projected"], s["ss"], s["rop"]
    s["bar"]["tone"] = "danger" if proj <= 0 or (ss and proj <= ss) else ("warn" if rop and proj <= rop else "success")
    s["level_label"], s["level_tone"] = LEVEL_LABEL[s["level"]]
    return s


def _mark_simulated(rows):
    """Gà STT 102: đánh dấu mã có dữ liệu mô phỏng (SIM-PCB40) – không tính vào thẻ đếm, lọc "Cần đặt", không gợi ý PR."""
    from .models import SimulatedDemand
    sim_ids = set(SimulatedDemand.objects.values_list("product_id", flat=True).distinct())
    for r in rows:
        r["simulated"] = r["product"].pk in sim_ids
        # Gà STT 111: "Lượng PR gợi ý" chỉ có nghĩa khi mã đã chạm ROP
        r["show_pr"] = r["level"] != "OK" and not r["simulated"] and r["product"].track_rop
    return rows


# ------------------------------------------------------------------ Tồn kho
@role_required(*INTERNAL)
def stock(request):
    # sản phẩm ngừng kinh doanh nhưng còn tồn vẫn phải hiện (có nhãn) – không để hàng "biến mất" tới khi kiểm kê
    with_stock = StockQuant.objects.filter(on_hand__gt=0).values("product_id")
    products = Product.objects.filter(Q(is_active=True) | Q(pk__in=with_stock)).select_related("base_uom", "category")
    q = request.GET.get("q", "").strip()
    if q:
        products = products.filter(Q(code__icontains=q) | Q(name__icontains=q))
    if request.GET.get("cat"):
        products = products.filter(category_id=request.GET["cat"])
    rows = [_bar(s) for s in stock_summary(list(products)).values()]
    _mark_simulated(rows)
    level = request.GET.get("level")
    if level == "low":
        rows = [r for r in rows if r["level"] != "OK" and not r["simulated"]]
    elif level:
        rows = [r for r in rows if r["level"] == level and not r["simulated"]]
    rows.sort(key=lambda r: ({"STOCKOUT": 0, "BELOW_SS": 1, "BELOW_ROP": 2, "OK": 3}[r["level"]], r["product"].code))

    if request.GET.get("export") == "xlsx":
        head = ["Mã", "Tên", "ĐVT", "Tồn thực", "Đã giữ", "Khả dụng", "Đang về", "Đã đề xuất", "Tồn dự kiến", "SS", "ROP",
                "Lượng PR gợi ý", "Số ngày đủ", "Tình trạng"]
        if request.user.can_see_cost:
            head.append("Giá trị tồn")
        data = []
        for r in rows:
            p = r["product"]
            line = [p.code, p.name, p.base_uom.code, r["on_hand"], r["reserved"], r["available"], r["incoming"],
                    r["pipeline"], r["projected"], r["ss"], r["rop"], r["suggested_q"],
                    round(float(r["days_cover"]), 1) if r["days_cover"] else None, r["level_label"]]
            if request.user.can_see_cost:
                line.append(r["on_hand"] * p.avg_cost)
            data.append(line)
        return excel_response(f"ton_kho_{timezone.localdate():%Y%m%d}", head, data, "Tồn kho")

    # Gà STT 102 / 111: mã dữ liệu mô phỏng không vào thẻ đếm / tổng; không cộng lẫn tấn, bao, cây -> chỉ tổng giá trị
    real = [r for r in rows if not r["simulated"]]
    value = sum((r["on_hand"] * r["product"].avg_cost for r in real), Decimal(0))
    counts = {k: sum(1 for r in real if r["level"] == k) for k in LEVEL_LABEL}
    return render(request, "inventory/stock.html", {
        "title": "Tồn kho", "rows": rows, "value": value, "counts": counts,
        "categories": ProductCategory.objects.filter(is_active=True), "levels": LEVEL_LABEL,
    })


@role_required(*INTERNAL)
def stock_locations(request, pk):
    """HTMX: chi tiết tồn theo vị trí (FIFO)."""
    quants = StockQuant.objects.filter(product_id=pk, on_hand__gt=0).select_related("location__warehouse", "product__base_uom") \
        .order_by("in_date_first")
    product = get_object_or_404(Product.objects.select_related("base_uom"), pk=pk)
    return render(request, "inventory/_quants.html", {
        "quants": quants, "pk": pk, "product": product,
        "can_edit": request.user.role in ("WAREHOUSE", "DIRECTOR", "ADMIN"),
        # vị trí Lưu trữ lên đầu (mặc định chọn), khu Cách ly / Chờ xuất xếp sau – tránh cất nhầm hàng tốt vào Q-01
        "locations": Location.objects.filter(is_active=True).select_related("warehouse")
        .annotate(_o=Case(When(loc_type="STORAGE", then=0), When(loc_type="STAGING_OUT", then=1), default=2,
                          output_field=IntegerField()))
        .order_by("_o", "warehouse__name", "code")})


@role_required("WAREHOUSE", "DIRECTOR")
@require_POST
@business_action
def stock_quant_edit(request, pk):
    """Sửa / Xoá tồn tại 1 vị trí: đặt lại SL thực tế (op=set) hoặc đưa về 0 (op=delete).

    Không sửa trực tiếp stock_quant: lập phiếu điều chỉnh ADJ theo chênh lệch (adjustments.create_adjustment):
    dưới ngưỡng ghi sổ ngay, vượt ngưỡng ADJ_APPROVAL_THRESHOLD thì chờ Giám đốc duyệt (test Gà STT 34)."""
    from .adjustments import create_adjustment

    op = request.POST.get("op")
    q = get_object_or_404(StockQuant.objects.select_related("product__base_uom", "location"), pk=pk)
    prod, loc = q.product, q.location
    if op == "delete":
        new_qty = Decimal(0)
    elif op == "set":
        new_qty = _num(request, "qty")
        if new_qty is None:
            raise BusinessError("Nhập số lượng mới.")
    else:
        raise BusinessError("Thao tác không hợp lệ.")
    diff = new_qty - q.on_hand
    if diff == 0:
        messages.info(request, "Số lượng không đổi.")
        return redirect(request.META.get("HTTP_REFERER") or "/app/inventory/stock")
    label = f"{'Xoá' if op == 'delete' else 'Sửa'} tồn {loc.code}"
    adj = create_adjustment(request.user, request.POST.get("reason"), [(prod, loc, diff, prod.avg_cost)], label)
    _adj_message(request, adj, f"{label} {prod.code}: {fmt_qty(q.on_hand)} → {fmt_qty(new_qty)} {prod.base_uom.code}")
    return redirect(request.META.get("HTTP_REFERER") or "/app/inventory/stock")


def _adj_message(request, adj, what):
    if adj.status == "PENDING_APPROVAL":
        messages.warning(request, f"{what}: giá trị {_money(adj.value)} vượt ngưỡng duyệt – phiếu {adj.code} đang chờ "
                                  "Giám đốc duyệt, CHƯA ghi sổ kho.")
    else:
        messages.success(request, f"{what} (phiếu {adj.code}, đã ghi sổ).")


@role_required("WAREHOUSE", "DIRECTOR")
@require_POST
@business_action
def stock_add(request):
    """Thêm tồn thực (nhập điều chỉnh tăng) – vd. khai báo tồn đầu kỳ, hàng phát hiện thừa.

    Lập phiếu ADJ (adjustments.create_adjustment): dưới ngưỡng ghi sổ ngay và tự giữ cho đơn backorder; vượt ngưỡng
    chờ Giám đốc duyệt."""
    from .adjustments import create_adjustment

    p = get_object_or_404(Product, pk=request.POST["product"])
    if not request.POST.get("location"):
        raise BusinessError("Chọn vị trí cất hàng.")
    loc = get_object_or_404(Location, pk=request.POST["location"], is_active=True)
    qty = _num(request, "qty")
    if not qty or qty <= 0:
        raise BusinessError("Số lượng thêm phải lớn hơn 0.")
    cost = _num(request, "unit_cost") if request.user.can_see_cost else None
    if cost is None:
        cost = p.avg_cost or p.standard_cost
    adj = create_adjustment(request.user, request.POST.get("reason"), [(p, loc, qty, cost)], "Thêm tồn thực")
    _adj_message(request, adj, f"Thêm {fmt_qty(qty)} {p.base_uom.code} {p.code} vào {loc.code}")
    return redirect(request.META.get("HTTP_REFERER") or "/app/inventory/stock")


@role_required("WAREHOUSE", "DIRECTOR", "ACCOUNTANT")
def adjustments(request):
    qs = StockAdjustment.objects.select_related("requested_by", "approved_by", "count").prefetch_related(
        "lines__product__base_uom", "lines__location")
    if request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"])
    page, qs_str = paginate(request, qs, 30)
    return render(request, "inventory/adjustments.html", {
        "title": "Phiếu điều chỉnh tồn (ADJ)", "page_obj": page, "qs": qs_str, "statuses": StockAdjustment.STATUS,
        "pending": StockAdjustment.objects.filter(status="PENDING_APPROVAL").count()})


@role_required("WAREHOUSE", "DIRECTOR", "ACCOUNTANT")
@business_action
def adjustment_detail(request, pk):
    from .adjustments import post_adjustment, reject_adjustment

    adj = get_object_or_404(StockAdjustment.objects.select_related("requested_by", "approved_by", "count"), pk=pk)
    if request.method == "POST":
        if request.user.role not in ("DIRECTOR", "ADMIN"):
            raise BusinessError("Chỉ Giám đốc duyệt phiếu điều chỉnh tồn.")
        op = request.POST.get("op")
        if op == "approve":
            post_adjustment(adj, request.user)
            messages.success(request, f"Đã duyệt và ghi sổ phiếu {adj.code}.")
        elif op == "cancel":
            reject_adjustment(adj, request.user, request.POST.get("reason"))
            messages.success(request, f"Đã từ chối phiếu {adj.code}.")
        return redirect(request.path)
    return render(request, "inventory/adjustment_detail.html", {
        "title": adj.code, "adj": adj, "lines": adj.lines.select_related("product__base_uom", "location"),
        "is_director": request.user.role in ("DIRECTOR", "ADMIN")})


@role_required("WAREHOUSE", "DIRECTOR")
@require_POST
@business_action
def stock_transfer(request):
    p = get_object_or_404(Product, pk=request.POST["product"])
    src = get_object_or_404(Location, pk=request.POST["from"])
    dst = get_object_or_404(Location, pk=request.POST["to"])
    if src == dst:
        raise BusinessError("Vị trí đích phải khác vị trí nguồn.")
    qty = _num(request, "qty")  # số âm -> lỗi "không được âm"
    if not qty or qty <= 0:
        raise BusinessError("Số lượng chuyển phải lớn hơn 0.")
    transfer(p, src, dst, qty, note="Chuyển vị trí")
    if "QUARANTINE" in (src.loc_type, dst.loc_type):  # vào / ra khu cách ly làm đổi tồn dùng được -> kiểm tra ROP
        from .replenishment import Trigger, fire_checks
        fire_checks([p.pk], Trigger("STOCK_ADJUSTED", "", f"Chuyển {src.code}→{dst.code}", request.user))
    messages.success(request, f"Đã chuyển {_qu(qty, p)} {p.code} từ {src} sang {dst}.")
    return redirect(request.META.get("HTTP_REFERER") or "/app/inventory/stock")


# ------------------------------------------------------------------ MH-12 SS/ROP
@role_required("DIRECTOR", "PURCHASER")
def replenishment(request):
    products = list(Product.objects.filter(is_active=True, track_rop=True).select_related("base_uom"))
    summary = {pid: _bar(s) for pid, s in stock_summary(products).items()}
    _mark_simulated(summary.values())
    if request.GET.get("export") == "all":
        return _rop_report_all(summary)
    rows = list(summary.values())
    if request.GET.get("level") == "low":
        rows = [r for r in rows if r["level"] != "OK" and not r["simulated"]]
    rows.sort(key=lambda r: ({"STOCKOUT": 0, "BELOW_SS": 1, "BELOW_ROP": 2, "OK": 3}[r["level"]], r["product"].code))
    sel_id = int(request.GET.get("product") or (rows[0]["product"].pk if rows else 0))
    sel = summary.get(sel_id)
    rp = fr = v = demand = chart = None
    if sel:
        rp, _ = ReplenishmentParam.objects.get_or_create(product_id=sel_id, defaults={"z": param("SERVICE_Z")})
        if rp.calculated_at is None or timezone.localtime(rp.calculated_at).date() < timezone.localdate():
            rp, _ = calc_param(rp.product)  # tham số cũ hơn hôm nay -> tính lại để màn hình khớp calc_param
        if request.GET.get("export") == "xlsx":
            return _rop_report(rp, sel)
        demand, fr, v = rop_explain(rp)
        chart = _forecast_chart(fr)
    calc = None
    if rp:
        q, proj = rp.order_q, sel["position"]  # SL PR tính từ position = tồn dự kiến + đã đề xuất (giống hàm lõi)
        raw = max(q, sel["rop"] + q - proj, Decimal(0))
        calc = {"q": q, "raw": raw, "pr": rp.pr_qty(proj), "proj": proj, "rop": sel["rop"],
                "trips": (rp.pr_qty(proj) / rp.truck_qty) if rp.truck_qty else None}
    months = ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "T10", "T11", "T12"]
    return render(request, "inventory/replenishment.html", {
        "title": "Tham số mốc đặt hàng lại", "rows": rows, "sel": sel, "rp": rp, "chart": chart, "sel_id": sel_id,
        "fr": fr, "v": v, "demand": demand, "calc": calc, "can_lock": request.user.role in LOCK_ROLES,
        "wape_pct": _pct(fr.wape) if fr else None, "wape_base_pct": _pct(fr.wape_baseline) if fr else None,
        "can_sim": request.user.role in ("DIRECTOR", "ADMIN"), "sim_code": SIM_CODE,
        "seasonal": list(zip(months, fr.seasonal)) if fr and fr.seasonal else None,
        "job_runs": ReplenishmentJobRun.objects.select_related("user")[:10]})


def _forecast_chart(fr):
    """Biểu đồ "Dự báo và thực tế" theo tuần: thực tế, dự báo 1 tuần trước trong cửa sổ backtest, dự báo 13 tuần tới."""
    hist = fr.history[-104:]
    bt = {w: f for w, _, f in fr.backtest}
    fut_weeks = [fr.first_forecast_week + timedelta(days=7 * k) for k in range(len(fr.forecast_weeks))] \
        if fr.first_forecast_week else []
    labels = [w.strftime("%d/%m/%y") for w, _ in hist] + [w.strftime("%d/%m/%y") for w in fut_weeks]
    n_h = len(hist)
    return {"labels": labels,
            "actual": [round(y, 2) for _, y in hist] + [None] * len(fut_weeks),
            "backtest": [round(bt[w], 2) if w in bt else None for w, _ in hist] + [None] * len(fut_weeks),
            "future": [None] * max(0, n_h - 1) + ([round(hist[-1][1], 2)] if hist else [])
            + [round(x, 2) for x in fr.forecast_weeks]}


LOCK_ROLES = ("DIRECTOR", "PURCHASER")
AUDIT_FIELDS = ["method", "z", "review_days", "lead_time_days", "demand_source", "exclude_outliers",
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
    from .replenishment import Trigger, fire_checks
    op = request.POST.get("op")
    if op in ("import_csv", "clear_sim", "create_sim"):
        return _simulation_action(request, pk, op)
    rp = get_object_or_404(ReplenishmentParam.objects.select_related("product"), product_id=pk)
    if op == "run_all":
        res = run_replenishment(trigger=Trigger("NIGHTLY_JOB", "", "Chạy tay", request.user))
        messages.success(request, f"Đã dự báo & tính lại SS/ROP cho {res['recalculated']} sản phẩm "
                         f"({res['locked_skipped']} vật tư Khóa thủ công vẫn dùng số nhập tay)."
                         + (f" Sinh PR tự động {res['auto_pr']} ({res['lines']} dòng)." if res["auto_pr"] else ""))
        return redirect(f"/app/inventory/replenishment?product={pk}")
    old = {f: getattr(rp, f) for f in AUDIT_FIELDS}
    rp.method = request.POST.get("method", rp.method)
    rp.z = _num(request, "z", default=rp.z)
    rp.review_days = _num(request, "review_days", int, rp.review_days)
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
                raise BusinessError(f"ROP ({_q(rp.manual_rop, rp.product)}) phải lớn hơn hoặc bằng SS ({_q(rp.manual_ss, rp.product)}).")
        else:
            rp.manual_ss = rp.manual_rop = None
            rp.lock_reason = ""

    with transaction.atomic():
        rp.save()
        rp, _ = calc_param(rp.product)
        if rp.d_avg == 0 and rp.manual_q is None:
            raise BusinessError("Vật tư chưa có nhu cầu (dự báo = 0): bắt buộc nhập Q tay.")
        rp.suggested_q = stock_summary([rp.product])[rp.product_id]["suggested_q"]
        rp.save(update_fields=["suggested_q"])
        changes = {f: [str(old[f]), str(getattr(rp, f))] for f in AUDIT_FIELDS if old[f] != getattr(rp, f)}
        if changes:
            audit("UPDATE", rp, changes)
        fire_checks([rp.product_id], Trigger("PARAM_SAVED", "", rp.product.code, request.user))
    messages.success(request, f"Đã lưu, dự báo & tính lại SS/ROP cho {rp.product.code}.")
    return redirect(f"/app/inventory/replenishment?product={pk}")


def _simulation_action(request, pk, op):
    """Dữ liệu mô phỏng (đặc tả mục 8, 9): chỉ Giám đốc / admin. Nhập CSV week_start,qty; xóa; tạo mã demo SIM-PCB40."""
    from .models import SimulatedDemand
    from .simulation import create_sim_product, load_simulated, parse_csv

    if request.user.role not in ("DIRECTOR", "ADMIN"):
        raise BusinessError("Chỉ Giám đốc / admin nhập dữ liệu mô phỏng.")
    if op == "create_sim":
        p = create_sim_product()
        calc_param(p)
        audit("CREATE", p, {"Dữ liệu mô phỏng": "Tạo mã demo + nạp 104 tuần mô phỏng"})
        messages.success(request, f"Đã tạo mã demo {p.code} và nạp 104 tuần nhu cầu mô phỏng. Dữ liệu này chỉ dùng để "
                                  "kiểm chứng mô hình – xóa mã trước khi chạy thật.")
        return redirect(f"/app/inventory/replenishment?product={p.pk}")
    product = get_object_or_404(Product, pk=pk)
    if op == "import_csv":
        f = request.FILES.get("csv")
        if not f:
            raise BusinessError("Chọn file CSV (week_start,qty).")
        if f.size > 1024 * 1024:
            raise BusinessError("File CSV quá lớn (tối đa 1 MB).")
        n = load_simulated(product, parse_csv(f.read()))
        msg = f"Đã nhập {n} tuần nhu cầu mô phỏng cho {product.code}"
    else:
        n = SimulatedDemand.objects.filter(product=product).delete()[0]
        msg = f"Đã xóa {n} tuần dữ liệu mô phỏng của {product.code} – dự báo quay lại dùng đơn thật"
    audit("UPDATE", product, {"Dữ liệu mô phỏng": msg})
    calc_param(product)
    messages.success(request, msg + ".")
    return redirect(f"/app/inventory/replenishment?product={pk}")


def _xlsx_response(wb, filename):
    import io

    from django.http import HttpResponse
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


def _pct(x):
    return None if x is None or math.isinf(x) else round(x * 100, 2)


def _rop_report(rp, sel):
    """Báo cáo SS/ROP 1 sản phẩm – CHỈ XUẤT GIÁ TRỊ (đặc tả mục 8), cùng hàm với màn hình & calc_param:
    sheet SS_ROP theo bảng mục 5, NhuCau theo tuần (thực tế, dự báo, sai số), MuaVu 12 chỉ số."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    demand, fr, v = rop_explain(rp)
    wb = Workbook()
    p = wb.active
    p.title = "SS_ROP"
    p.append(["Chỉ số", "Giá trị", "Cách tính"])
    _style_head(p)
    base_note = " (DỮ LIỆU MÔ PHỎNG)" if demand.simulated else ""
    pr = max(v["q"], v["rop_eff"] + v["q"] - float(sel["position"]))
    truck = float(rp.truck_qty) if rp.round_by_truck and rp.truck_qty else None
    rows = [
        ("Sản phẩm", f"{rp.product.code} – {rp.product.name}{base_note}", ""),
        ("Mô hình dự báo", fr.label, "MA < 8 tuần · Holt 8–103 tuần · Holt-Winters ≥ 104 tuần"),
        ("Số tuần dữ liệu", fr.n_weeks, "Tuần đủ, không gồm tuần hiện tại"),
        ("α", fr.alpha, "Chọn bằng backtest (WAPE nhỏ nhất)"),
        ("β", fr.beta, ""),
        ("WAPE (%)", _pct(fr.wape), "Σ|e| / Σy trong cửa sổ đánh giá"),
        ("WAPE cách cũ (%)", _pct(fr.wape_baseline), "Dự báo = TB 13 tuần trước"),
        ("Giảm sai số (%)", round(fr.improvement, 1) if fr.improvement is not None else None, "1 − WAPE / WAPE cách cũ"),
        ("σe tuần", round(fr.sigma_e_week, 3), "√(Σe² / (H − 1))"),
        ("σe ngày", round(v["sigma_e_day"], 3), "σe tuần / √7"),
        ("Phương pháp SS", rp.get_method_display(), ""),
        ("Z", v["z"], "Hệ số mức phục vụ"),
        ("Lead time LT (ngày)", round(v["lt"], 2), "TB lead time thực tế (bỏ phiếu < LT_MIN_DAYS, chuyến test)"),
        ("σLT (ngày)", round(v["lt_std"], 2), "Độ lệch chuẩn mẫu lead time"),
        ("D_LT – nhu cầu dự báo trong LT", round(v["d_lt"], 3), "Tổng dự báo LT ngày tới (dự báo tuần / 7 mỗi ngày)"),
        ("F_ngày", round(v["f_day"], 3), "D_LT / LT"),
        ("SS tự tính", round(v["ss"], 3), {"FULL": "Z·√(LT·σe_ngày² + F_ngày²·σLT²)", "DEMAND": "Z·σe_ngày·√LT"}
         .get(rp.method, "Nhập tay")),
        ("ROP tự tính", round(v["rop"], 3), "D_LT + SS"),
        ("Khóa thủ công", "Có" if rp.manual_lock else "Không", rp.lock_reason),
        ("SS áp dụng", round(v["ss_eff"], 3), "Số khóa tay nếu bật khóa, ngược lại SS tự tính"),
        ("ROP áp dụng", round(v["rop_eff"], 3), "Số khóa tay nếu bật khóa, ngược lại ROP tự tính"),
        ("MOQ", float(rp.moq), "Lượng đặt tối thiểu NCC"),
        ("Chu kỳ đặt (ngày)", rp.review_days, ""),
        ("Dự báo trong chu kỳ đặt", round(v["d_review"], 3), "Tổng dự báo review_days ngày tới"),
        ("Q áp dụng", v["q"], "Q nhập tay, hoặc max(MOQ; dự báo chu kỳ đặt)"),
        ("Position (tồn dự kiến + đã đề xuất)", float(sel["position"]),
         "Tồn thực + đang về − đã giữ − thiếu chưa giữ + PR/PO chưa duyệt"),
        ("Lượng PR", round(pr, 3), "max(Q; ROP + Q − position)"),
    ]
    if truck:
        rows.append(("Tải 1 xe (đv gốc)", truck, f"{rp.truck_ton} tấn/xe"))
        rows.append(("Lượng PR làm tròn theo xe", math.ceil(pr / truck) * truck, "Làm tròn lên theo chuyến xe"))
    for r in rows:
        p.append(list(r))
    for row in p.iter_rows(min_row=2):
        row[0].font = Font(bold=True)
        if isinstance(row[1].value, float):
            row[1].number_format = "#,##0.000"
    p.column_dimensions["A"].width, p.column_dimensions["B"].width, p.column_dimensions["C"].width = 36, 40, 60

    ws = wb.create_sheet("NhuCau")
    ws.append(["Tuần (thứ Hai)", "Thực tế", "Dự báo 1 tuần trước (backtest)", "Sai số e = thực tế − dự báo",
               "Dự báo 13 tuần tới"])
    _style_head(ws)
    bt = {w: f for w, _, f in fr.backtest}
    for w, y in fr.history:
        f = bt.get(w)
        ws.append([w, y, round(f, 3) if f is not None else None, round(y - f, 3) if f is not None else None, None])
        ws.cell(ws.max_row, 1).number_format = "yyyy-mm-dd"
    if fr.first_forecast_week:
        for k, f in enumerate(fr.forecast_weeks):
            ws.append([fr.first_forecast_week + timedelta(days=7 * k), None, None, None, round(f, 3)])
            ws.cell(ws.max_row, 1).number_format = "yyyy-mm-dd"
    for col, wdt in zip("ABCDE", (16, 12, 28, 26, 20)):
        ws.column_dimensions[col].width = wdt

    ms = wb.create_sheet("MuaVu")
    ms.append(["Tháng", "Chỉ số mùa"])
    _style_head(ms)
    for m in range(12):
        ms.append([f"T{m + 1}", round(fr.seasonal[m], 4) if fr.seasonal else None])
    if not fr.seasonal:
        ms.append(["", "Chưa đủ 104 tuần dữ liệu – chưa tính mùa vụ"])
    if demand.dropped:
        x = wb.create_sheet("LoaiDotBien")
        x.append(["Ngày / tuần", "SL", "Chứng từ", f"Ngưỡng loại = {demand.cut:.1f}" if demand.cut else ""])
        _style_head(x)
        for d, q, note in demand.dropped:
            x.append([d, q, note])
    return _xlsx_response(wb, f"ss_rop_{rp.product.code}")


def _rop_report_all(summary):
    """Báo cáo SS/ROP TẤT CẢ sản phẩm – chỉ giá trị, 1 dòng / sản phẩm (cùng hàm với màn hình & calc_param)."""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "TongHop"
    head = ["Mã SP", "Tên SP", "ĐVT", "Dữ liệu", "Mô hình", "Số tuần", "α", "β", "WAPE %", "WAPE cách cũ %",
            "σe tuần", "Phương pháp SS", "Z", "LT (ngày)", "σLT (ngày)", "D_LT", "SS tự tính", "ROP tự tính", "Khóa tay",
            "SS áp dụng", "ROP áp dụng", "MOQ", "Chu kỳ (ngày)", "Q áp dụng", "Khả dụng", "Đang về",
            "Position (dự kiến + đề xuất)", "Lượng PR", "Tình trạng"]
    ws.append(head)
    _style_head(ws)
    level_txt = {"OK": "Đủ hàng", "BELOW_ROP": "Dưới ROP", "BELOW_SS": "Dưới SS", "STOCKOUT": "Hết hàng"}
    params = {rp.product_id: rp for rp in ReplenishmentParam.objects.filter(product_id__in=summary)}
    for pid, s in sorted(summary.items(), key=lambda kv: kv[1]["product"].code):
        rp = params.get(pid) or ReplenishmentParam.objects.create(product_id=pid, z=param("SERVICE_Z"))
        demand, fr, v = rop_explain(rp)
        pr = max(v["q"], v["rop_eff"] + v["q"] - float(s["position"]))
        truck = float(rp.truck_qty) if rp.round_by_truck and rp.truck_qty else None
        ws.append([rp.product.code, rp.product.name, rp.product.base_uom.code,
                   "Mô phỏng" if demand.simulated else "Đơn thật", fr.model, fr.n_weeks, fr.alpha, fr.beta,
                   _pct(fr.wape), _pct(fr.wape_baseline), round(fr.sigma_e_week, 3), rp.get_method_display(), v["z"],
                   round(v["lt"], 2), round(v["lt_std"], 2), round(v["d_lt"], 3), round(v["ss"], 3), round(v["rop"], 3),
                   "Có" if rp.manual_lock else "Không", round(v["ss_eff"], 3), round(v["rop_eff"], 3), float(rp.moq),
                   rp.review_days, v["q"], float(s["available"]), float(s["incoming"]), float(s["position"]),
                   math.ceil(pr / truck) * truck if truck else round(pr, 3), level_txt[s["level"]]])
    widths = {"A": 12, "B": 30, "E": 8, "L": 24}
    for i in range(1, len(head) + 1):
        L = get_column_letter(i)
        ws.column_dimensions[L].width = widths.get(L, 12)
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
    qs = InventoryCount.objects.select_related("warehouse", "created_by")
    st = request.GET.get("status")
    if st == "OPEN":  # Dashboard: phiếu đang dở (Đang đếm / Đếm lại)
        qs = qs.filter(status__in=["COUNTING", "RECOUNT"])
    elif st:
        qs = qs.filter(status=st)
    page, qs_str = paginate(request, qs)
    return render(request, "inventory/counts.html", {"title": "Kiểm kê", "page_obj": page, "qs": qs_str,
                                                     "warehouses": Warehouse.objects.filter(is_active=True)})


def _approve_count(ic, lines, user, auto=False):
    """Duyệt phiếu kiểm kê -> sinh phiếu điều chỉnh ADJ + bút toán kho theo chênh lệch.
    auto=True: tự ghi sổ vì giá trị điều chỉnh dưới ngưỡng ADJ_APPROVAL_THRESHOLD (STT 76) – không có người duyệt."""
    reason = f"Kiểm kê {ic.code}" + (" (tự ghi sổ – dưới ngưỡng duyệt)" if auto else "")
    adj = StockAdjustment.objects.create(code=next_doc_no("ADJ"), count=ic, reason=reason,
                                         requested_by=ic.created_by, approved_by=None if auto else user,
                                         approved_at=timezone.now(), status="POSTED")
    for l in lines:
        # tồn có thể đã thay đổi trong lúc đếm -> điều chỉnh theo chênh lệch so với tồn sổ lúc chụp
        diff = (l.final_qty or 0) - l.book_qty
        if diff:
            StockAdjustmentLine.objects.create(adjustment=adj, product=l.product, location=l.location,
                                               qty_diff=diff, unit_cost=l.product.avg_cost)
            post_stock_move(l.product, l.location, diff, "ADJ", "ADJ", adj.pk, note=adj.code)
    ic.status, ic.approved_by = "APPROVED", None if auto else user
    ic.save()
    resolve_alerts("COUNT_APPROVAL", "inventory_count", ic.pk)
    audit("APPROVE", ic, {"auto": "Tự ghi sổ – giá trị điều chỉnh dưới ngưỡng duyệt"} if auto else None)
    from .adjustments import release_excess_reservations
    from .replenishment import Trigger, fire_checks
    trig = Trigger("COUNT_APPROVED", "IC", ic.code, user)
    # STT 60: kiểm kê làm tồn thực < đã giữ -> bỏ giữ phần thiếu (đơn ưu tiên thấp trước) và đưa sang phân bổ
    for l in {l.product_id: l for l in lines if (l.final_qty or 0) < l.book_qty}.values():
        release_excess_reservations(l.product, trig)
    fire_checks([l.product_id for l in lines if (l.final_qty or 0) != l.book_qty], trig)
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
                    messages.warning(request, f"Có dòng lệch > {_fnum(limit, 1, True)}% – cần đếm lại lần 2 trước khi duyệt.")
                else:
                    ic.status = "PENDING_APPROVAL"
                    ic.save()
                    # STT 76: tổng giá trị điều chỉnh < ngưỡng ADJ -> tự ghi sổ, không chờ Giám đốc
                    value = sum((abs((l.final_qty or 0) - l.book_qty) * l.product.avg_cost for l in lines), Decimal(0))
                    if op == "count_approve":
                        adj = _approve_count(ic, lines, request.user)
                        messages.success(request, f"Đã lưu số đếm, duyệt phiếu và ghi bút toán điều chỉnh {adj.code}.")
                    elif value < Decimal(param("ADJ_APPROVAL_THRESHOLD") or 0):
                        adj = _approve_count(ic, lines, request.user, auto=True)
                        messages.success(request, f"Đã hoàn tất đếm. Giá trị điều chỉnh {_money(value)} dưới ngưỡng duyệt – "
                                                  f"tự ghi sổ{f' ({adj.code})' if adj.lines.exists() else ''}.")
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
