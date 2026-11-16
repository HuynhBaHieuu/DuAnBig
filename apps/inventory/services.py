"""Dịch vụ tồn kho. MỌI thay đổi tồn kho phải đi qua post_stock_move()."""
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.core.middleware import get_current_user
from apps.core.services import BusinessError, next_doc_no, param, raise_alert, resolve_alerts
from apps.masterdata.models import Location, Product

from .models import ReplenishmentParam, ReplenishmentSnapshot, StockMove, StockQuant

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
    """Chuyển vị trí = 2 bút toán TRF trong cùng transaction. SL phải > 0 (số âm không được hiểu là chuyển ngược)."""
    try:
        qty = Decimal(qty)
    except Exception:
        raise BusinessError("Số lượng chuyển không hợp lệ.")
    if not qty.is_finite() or qty <= 0:
        raise BusinessError("Số lượng chuyển phải lớn hơn 0.")
    with transaction.atomic():
        ref = int(timezone.now().timestamp())
        post_stock_move(product, from_loc, -Decimal(qty), "TRF", "TRF", ref, note=note)
        post_stock_move(product, to_loc, Decimal(qty), "TRF", "TRF", ref, note=note)


# ----------------------------------------------------------------------------------------------
# Chỉ số tồn: on_hand, reserved, available, incoming, projected, days_cover
# ----------------------------------------------------------------------------------------------
def stock_summary(products=None):
    """Chỉ số tồn cho màn hình / báo cáo – đọc đúng công thức của apps.inventory.replenishment.stock_metrics.

    Mức (level): STOCKOUT khả dụng ≤ 0 · BELOW_SS khả dụng < SS ("Dưới SS") · BELOW_ROP tồn dự báo ≤ ROP
    ("Chạm ROP", trùng điều kiện mở cảnh báo HIT_ROP) · OK ("Bình thường")."""
    from .replenishment import calc_order_qty, effective_params, stock_metrics

    qs = list(products if products is not None else Product.objects.filter(is_active=True))
    ids = [p.pk for p in qs]
    metrics = stock_metrics(qs)
    params = {p.product_id: p for p in ReplenishmentParam.objects.filter(product_id__in=ids)}
    open_alerts = set(ReplenishmentAlertModel().objects.filter(product_id__in=ids, closed_at__isnull=True)
                      .values_list("product_id", flat=True))
    out = {}
    for p in qs:
        m = metrics[p.pk]
        rp = params.get(p.pk)
        av, proj = m["available"], m["projected"]
        d_avg = rp.d_avg if rp else D0
        ss = rp.eff_ss if rp else D0
        rop = rp.eff_rop if rp else D0
        sugg = calc_order_qty(effective_params(p, rp), m["position"])[1] if rp else D0
        tracked = p.track_rop and rp is not None
        out[p.pk] = {
            "product": p, **m, "d_avg": d_avg, "ss": ss, "rop": rop,
            "days_cover": (av / d_avg) if d_avg else None, "locked": bool(rp and rp.manual_lock),
            "suggested_q": sugg, "hit_rop_alert": p.pk in open_alerts,
            "level": "STOCKOUT" if av <= 0 else ("BELOW_SS" if tracked and av < ss else
                                                 ("BELOW_ROP" if tracked and proj <= rop else "OK")),
        }
    return out


def ReplenishmentAlertModel():
    from .models import ReplenishmentAlert
    return ReplenishmentAlert


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
# SS / ROP (tác vụ đêm: Vercel Cron 18:00 UTC = 01:00–02:00 giờ VN)
#   Nhu cầu tuần (đơn đã xác nhận theo ngày đặt, hoặc dữ liệu mô phỏng) -> dự báo (forecast.py: MA / Holt /
#   Holt-Winters) -> SS, ROP, Q từ dự báo và σe (forecast.ss_rop_q); LT, σLT từ lead time thực tế các GRN.
#   PR = max(Q; ROP + Q − position) – giữ nguyên (replenishment.calc_order_qty).
#   Khóa thủ công: vẫn dự báo để tham khảo nhưng ngưỡng áp dụng là số nhập tay.
# ----------------------------------------------------------------------------------------------
ORDER_STATUSES = ("CONFIRMED", "PARTIAL", "DELIVERED", "CLOSED")  # bỏ Nháp, Chờ duyệt, Đã hủy
MAX_HISTORY_WEEKS = 156  # tối đa 3 năm lịch sử cho dự báo
def outlier_cut(values):
    """Ngưỡng phiếu xuất đột biến (modified z-score > 3,5): trung vị + 3,5 × 1,4826 × MAD, tối thiểu 3 × trung vị.

    Dùng trung vị / MAD vì không bị chính các phiếu đột biến kéo lệch như trung bình hay IQR. Dưới 4 phiếu thì không loại."""
    if len(values) < 4:
        return None
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    return max(med + 3.5 * 1.4826 * mad, 3 * med)


class WeeklyDemand:
    """Chuỗi nhu cầu tuần đủ của 1 mã: weeks (thứ Hai), values (tuần không có đơn = 0, KHÔNG gồm tuần hiện tại),
    simulated (dữ liệu mô phỏng), dropped [(ngày, SL, chứng từ)] dòng đơn đột biến đã loại, cut ngưỡng loại."""

    def __init__(self, weeks=None, values=None, simulated=False, dropped=None, cut=None):
        self.weeks, self.values = weeks or [], values or []
        self.simulated, self.dropped, self.cut = simulated, dropped or [], cut


def weekly_demand(products, today=None, params=None):
    """{product_id: WeeklyDemand} – đọc nhu cầu cho TẤT CẢ mã bằng một vài truy vấn (không truy vấn trong vòng lặp).

    Nguồn (đặc tả mục 3):
      - Mã có dữ liệu SimulatedDemand -> CHỈ dùng dữ liệu mô phỏng (không trộn với đơn thật).
      - demand_source = ORDER (mặc định): SalesOrderLine.qty của đơn CONFIRMED / PARTIAL / DELIVERED / CLOSED, ngày nhu
        cầu = Coalesce(confirmed_at, approved_at, created_at) đổi sang giờ Việt Nam. Gồm đơn nội bộ & cổng KH.
      - ALL / SALE (lựa chọn cũ): lượng xuất kho theo phiếu.
    Đơn đột biến: quy tắc MAD (outlier_cut) áp cho TỪNG DÒNG đơn trước khi gộp tuần. Tuần bắt đầu thứ Hai."""
    from django.db.models.functions import Coalesce

    from apps.sales.models import SalesOrderLine

    from .forecast import fill_weeks, monday
    from .models import SimulatedDemand

    today = today or timezone.localdate()
    products = list(products)
    ids = [p.pk for p in products]
    params = params or {rp.product_id: rp for rp in ReplenishmentParam.objects.filter(product_id__in=ids)}
    cur = monday(today)
    last = cur - timedelta(days=7)                         # tuần đủ cuối cùng (tuần hiện tại chưa hết -> bỏ)
    floor = cur - timedelta(days=7 * MAX_HISTORY_WEEKS)
    out = {}

    sim = defaultdict(dict)
    for pid, w, q in SimulatedDemand.objects.filter(product_id__in=ids, week_start__lt=cur) \
            .values_list("product_id", "week_start", "qty"):
        sim[pid][monday(w)] = sim[pid].get(monday(w), 0.0) + float(q)
    for pid, by_week in sim.items():
        weeks, vals = fill_weeks(by_week, min(by_week), last)
        rp = params.get(pid)
        # dữ liệu mô phỏng là tổng theo tuần (không có dòng đơn): tuần đột biến (đơn công trình lớn) thay bằng trung vị
        # các tuần lân cận – giữ nhu cầu nền của tuần, bỏ phần đơn lớn để không làm lệch dự báo / chỉ số mùa
        cut = outlier_cut(vals) if (rp is None or rp.exclude_outliers) else None
        dropped = []
        if cut is not None:
            clean = []
            for i, v in enumerate(vals):
                if v > cut:
                    nb = [vals[j] for j in range(max(0, i - 4), min(len(vals), i + 5)) if j != i and vals[j] <= cut]
                    clean.append(statistics.median(nb) if nb else cut)
                    dropped.append((weeks[i], v, "Tuần đột biến (mô phỏng)"))
                else:
                    clean.append(v)
            vals = clean
        out[pid] = WeeklyDemand(weeks, vals, simulated=True, dropped=dropped, cut=cut)

    src = {pid: (params[pid].demand_source if pid in params else "ORDER") for pid in ids if pid not in sim}
    lines = defaultdict(list)                              # pid -> [(ngày, SL, chứng từ)]
    order_ids = [pid for pid, s in src.items() if s == "ORDER"]
    if order_ids:
        qs = SalesOrderLine.objects.filter(product_id__in=order_ids, so__status__in=ORDER_STATUSES) \
            .annotate(dt=Coalesce("so__confirmed_at", "so__approved_at", "so__created_at")) \
            .filter(dt__gte=timezone.make_aware(datetime.combine(floor, datetime.min.time()))) \
            .values_list("product_id", "qty", "dt", "so__code")
        for pid, q, dt, code in qs:
            lines[pid].append((timezone.localtime(dt).date(), float(q), code))
    move_ids = [pid for pid, s in src.items() if s in ("ALL", "SALE")]
    if move_ids:
        for pid, q, at, note, mt in StockMove.objects.filter(product_id__in=move_ids, move_type__in=OUT_DEMAND_TYPES,
                                                             at__date__gte=floor) \
                .values_list("product_id", "qty", "at", "note", "move_type"):
            if src[pid] == "SALE" and mt != "OUT_SALE":
                continue
            lines[pid].append((timezone.localtime(at).date(), float(-q), note))
    for pid in src:
        rows = [r for r in lines.get(pid, []) if r[0] < cur]
        rp = params.get(pid)
        cut = outlier_cut([q for _, q, _ in rows]) if (rp is None or rp.exclude_outliers) else None
        by_week, dropped = defaultdict(float), []
        for d, q, ref in rows:
            if cut is not None and q > cut:
                dropped.append((d, q, ref))
            else:
                by_week[monday(d)] += q
        if rows:
            weeks, vals = fill_weeks(by_week, min(monday(d) for d, _, _ in rows), last)
        else:
            weeks, vals = [], []
        out[pid] = WeeklyDemand(weeks, vals, dropped=dropped, cut=cut)
    return out


def lead_times(product):
    """(LT, σLT) – TB & độ lệch chuẩn MẪU của 20 phiếu nhập gần nhất; bỏ phiếu đánh dấu loại khỏi thống kê và phiếu
    có lead time < LT_MIN_DAYS (mặc định 0,25 ngày – PO lập & nhập trong vài giờ khi test). Dưới 3 phiếu -> None."""
    from apps.purchasing.models import GoodsReceiptLine
    lt_min = Decimal(str(param("LT_MIN_DAYS") or 0))
    lts = [float(x) for x in GoodsReceiptLine.objects.filter(product=product, gr__status="POSTED",
                                                              gr__lead_time_days__isnull=False,
                                                              gr__lead_time_days__gte=lt_min,
                                                              gr__exclude_from_stats=False)
           .order_by("-gr__posted_at").values_list("gr__lead_time_days", flat=True)[:20]]
    if len(lts) < 3:
        return None, None
    return statistics.fmean(lts), (statistics.stdev(lts) if len(lts) > 1 else 0.0)


def _dec(x, places="0.001"):
    return Decimal(str(x)).quantize(Decimal(places)) if x is not None and math.isfinite(x) else None


def calc_param(product, today=None, demand=None):
    """Dự báo nhu cầu + tính SS, ROP, Q cho 1 mã (mọi công thức ở apps/inventory/forecast.py). Lưu kết quả lần tính
    gần nhất vào ReplenishmentParam. Trả về (rp, ForecastResult)."""
    from .forecast import forecast, ss_rop_q

    today = today or timezone.localdate()
    rp, _ = ReplenishmentParam.objects.get_or_create(product=product, defaults={"z": param("SERVICE_Z")})
    demand = demand or weekly_demand([product], today, {product.pk: rp})[product.pk]
    fr = forecast(demand.weeks, demand.values)
    lt, lt_std = lead_times(product)
    if lt is None:  # LT thực tế chỉ dùng khi đã có ≥ 3 chuyến nhập hợp lệ, ngược lại giữ lead time mặc định
        lt, lt_std = float(rp.lead_time_days or 3), 0.0
    v = ss_rop_q(fr, method=rp.method, z=rp.z, lt=lt, lt_std=lt_std, today=today, review_days=rp.review_days,
                 moq=rp.moq, manual_ss=rp.manual_ss, manual_q=rp.manual_q)
    rp.d_avg, rp.d_std = _dec(v["f_day"]), _dec(v["sigma_e_day"])   # giữ D̄ = F_ngày cho các màn cũ
    rp.lead_time_days, rp.lt_std_days = _dec(lt, "0.01"), _dec(lt_std, "0.01")
    rp.ss, rp.rop, rp.d_lt, rp.d_review = _dec(v["ss"]), _dec(v["rop"]), _dec(v["d_lt"]), _dec(v["d_review"])
    rp.sigma_e_week = _dec(fr.sigma_e_week)
    rp.forecast_model, rp.n_weeks = fr.model, fr.n_weeks
    rp.alpha, rp.beta = _dec(fr.alpha, "0.01"), _dec(fr.beta, "0.01")
    rp.wape, rp.wape_baseline = _dec(fr.wape, "0.0001"), _dec(fr.wape_baseline, "0.0001")
    rp.forecast_data = {"weeks": [round(x, 3) for x in fr.forecast_weeks],
                        "first_week": fr.first_forecast_week.isoformat() if fr.first_forecast_week else None,
                        "seasonal": [round(x, 4) for x in fr.seasonal] if fr.seasonal else None,
                        "simulated": demand.simulated}
    rp.calculated_at = timezone.now()
    rp.save()
    return rp, fr


def rop_explain(rp, today=None):
    """(WeeklyDemand, ForecastResult, chỉ số SS/ROP/Q) cho màn SS/ROP và file Excel – cùng dữ liệu, cùng hàm
    forecast.ss_rop_q và cùng LT / σLT đang lưu như calc_param, nên số trên màn hình, Excel và calc_param khớp nhau."""
    from .forecast import forecast, ss_rop_q

    today = today or timezone.localdate()
    demand = weekly_demand([rp.product], today, {rp.product_id: rp})[rp.product_id]
    fr = forecast(demand.weeks, demand.values)
    v = ss_rop_q(fr, method=rp.method, z=rp.z, lt=rp.lead_time_days, lt_std=rp.lt_std_days, today=today,
                 review_days=rp.review_days, moq=rp.moq, manual_ss=rp.manual_ss, manual_q=rp.manual_q)
    v["ss_eff"] = float(rp.manual_ss) if rp.manual_lock and rp.manual_ss is not None else v["ss"]
    v["rop_eff"] = float(rp.manual_rop) if rp.manual_lock and rp.manual_rop is not None else v["rop"]
    return demand, fr, v


def run_replenishment(today=None, trigger=None):
    """Tác vụ đêm (Vercel Cron) / nút "Chạy tính toàn bộ ngay" – PR chính vẫn sinh ngay tại các điểm kích hoạt
    (xác nhận đơn, duyệt PO, nhập kho...). Thứ tự (đặc tả dự báo mục 7):
      1. Lấy nhu cầu tuần của MỌI mã (đơn thật hoặc mô phỏng) bằng một lần đọc;
      2. Dự báo -> calc_param cho mọi mã theo dõi ROP. Mã Khóa thủ công VẪN được dự báo để tham khảo, nhưng ngưỡng
         áp dụng vẫn là số nhập tay (eff_ss / eff_rop);
      3. Gọi hàm lõi check_replenishment cho MỌI vật tư theo dõi ROP để bắt nhu cầu đến hạn và các trường hợp lọt.
    Mỗi lần chạy ghi 1 dòng ReplenishmentJobRun: giờ bắt đầu / kết thúc, số vật tư, số PR tạo."""
    from .models import ReplenishmentJobRun
    from .replenishment import Trigger, safe_check

    today = today or timezone.localdate()
    trigger = trigger or Trigger("NIGHTLY_JOB")
    run = ReplenishmentJobRun.objects.create(started_at=timezone.now(), user=trigger.user_obj,
                                             source="MANUAL" if trigger.user_obj else "CRON")
    products = list(Product.objects.filter(is_active=True, track_rop=True).select_related("rop_param"))
    prs, recalculated, locked = [], 0, 0
    try:
        with transaction.atomic():
            params = {rp.product_id: rp for rp in ReplenishmentParam.objects.filter(product__in=products)}
            demand = weekly_demand(products, today, params)       # 1 lần đọc nhu cầu cho mọi mã
            for p in products:
                rp = params.get(p.pk)
                if rp is not None and rp.manual_lock:
                    locked += 1                                   # vẫn dự báo, nhưng ngưỡng dùng số nhập tay
                calc_param(p, today, demand[p.pk])
                recalculated += 1
            summary = stock_summary(products)
            for p in products:
                s = summary[p.pk]
                rp = ReplenishmentParam.objects.get(product=p)
                rp.suggested_q = s["suggested_q"]
                rp.save(update_fields=["suggested_q"])
                y_demand = -(StockMove.objects.filter(product=p, move_type__in=OUT_DEMAND_TYPES,
                                                       at__date=today - timedelta(days=1)).aggregate(s=Sum("qty"))["s"] or D0)
                ReplenishmentSnapshot.objects.update_or_create(
                    product=p, date=today,
                    defaults=dict(on_hand=s["on_hand"], reserved=s["reserved"], incoming=s["incoming"],
                                  projected=s["projected"], demand=y_demand, d_avg=s["d_avg"], ss=s["ss"], rop=s["rop"]))
        prs = [pr for pr in (safe_check(p.pk, trigger) for p in sorted(products, key=lambda x: x.pk)) if pr]
    except Exception as e:  # ghi lỗi vào nhật ký chạy rồi ném tiếp
        run.error = f"{type(e).__name__}: {e}"[:2000]
        raise
    finally:
        run.finished_at = timezone.now()
        run.products, run.recalculated, run.locked_skipped = len(products), recalculated, locked
        run.pr_created, run.pr_codes = len(prs), ", ".join(pr.code for pr in prs)
        run.save()
    return {"products": len(products), "recalculated": recalculated, "locked_skipped": locked,
            "auto_pr": ", ".join(pr.code for pr in prs) or None, "lines": len(prs), "run_id": run.pk}
