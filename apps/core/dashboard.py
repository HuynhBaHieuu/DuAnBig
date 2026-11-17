"""Dữ liệu Dashboard (test Gà STT 79–88). View core.dashboard chỉ ghép các khối này theo vai trò.

- Hàng "Việc chờ bạn" theo vai trò, số 0 thì ẩn, bấm mở đúng danh sách đã lọc (STT 80, 87).
- 6 thẻ KPI dùng chung apps/reports/kpi.py với trang Báo cáo (STT 79, 81, 88).
- Sức khỏe tồn kho: 10 mã rủi ro nhất, bullet chart tồn dự kiến / SS / ROP (STT 82).
- Công nợ phải thu theo tuổi nợ, mỗi khách 1 dòng (STT 83).
- Nhu cầu & doanh thu 12 tháng + dự báo các tháng tới từ module forecast (STT 84).
- Hàng về hôm nay – mai theo bến; xu hướng OTIF & chờ hạ tải theo tuần có vạch mục tiêu (STT 85).
- Cảnh báo gộp theo loại (STT 86). Không dùng biểu đồ tròn / vành khuyên / 3D (STT 88).
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.db.models import Count, F, Sum
from django.utils import timezone

D0 = Decimal(0)

ALERT_LABELS = {
    "PO_NOT_ACK": "PO nhà máy chưa nhận", "STALE_INBOUND": "Chuyến Inbound kẹt", "STALE_DELIVERY": "Chuyến giao kẹt",
    "STALE_COUNT": "Kiểm kê đang dở lâu", "STALE_ALLOCATION": "Phân bổ chờ lâu", "MILESTONE_LATE": "Chuyến trễ mốc",
    "TRIP_INCIDENT": "Sự cố chuyến", "HIT_ROP": "Chạm điểm đặt hàng lại", "ALLOCATION": "Phân bổ thiếu hàng",
    "PO_APPROVAL": "PO chờ duyệt", "SO_APPROVAL": "Đơn bán chờ duyệt", "PRICE_APPROVAL": "Giá mua chờ duyệt",
    "ADJ_APPROVAL": "Điều chỉnh tồn chờ duyệt", "COUNT_APPROVAL": "Kiểm kê chờ duyệt", "CLAIM_OPEN": "Khiếu nại mới",
    "BILL_MISMATCH": "Hóa đơn mua lệch 3 bên", "PORTAL_ORDER": "Đơn từ cổng khách hàng", "NOTIFY": "Thông báo",
    "GRN_VARIANCE": "Phiếu nhập chờ duyệt", "BELOW_ROP": "Dưới điểm đặt hàng lại", "BELOW_SS": "Dưới tồn an toàn",
    "STOCKOUT": "Hết hàng",
}


# ------------------------------------------------------------------ Việc chờ bạn (STT 80, 87)
def queue(user, today=None):
    from apps.accounting.models import Bill, Invoice
    from apps.accounting.services import invoiceable_lines
    from apps.inventory.models import InventoryCount, StockAdjustment
    from apps.inventory.services import stock_summary
    from apps.masterdata.models import SupplierPrice
    from apps.purchasing.models import Claim, GoodsReceipt, InboundTrip, PurchaseOrder, PurchaseRequisition, UnloadingSlot
    from apps.sales.models import Allocation, DeliveryTrip, Picking, SalesOrder, SalesOrderLine, SalesReturn
    from apps.sales.services import orders_waiting_delivery

    today = today or timezone.localdate()
    role = user.role
    items = []

    def add(label, count, url, icon, tone="primary"):
        if count:                                           # số 0 thì ẩn
            items.append({"label": label, "count": count, "url": url, "icon": icon, "tone": tone})

    if role in ("DIRECTOR", "ADMIN"):
        add("PO chờ duyệt", PurchaseOrder.objects.filter(status="PENDING_APPROVAL").count(),
            "/app/purchase/orders?status=PENDING_APPROVAL", "shopping-cart", "warn")
        add("Đơn bán chờ duyệt", SalesOrder.objects.filter(status="PENDING_APPROVAL").count(),
            "/app/sales/orders?status=PENDING_APPROVAL", "receipt", "warn")
        add("Khiếu nại chờ duyệt", Claim.objects.filter(status="OPEN").count(), "/app/warehouse/claims?status=OPEN",
            "message-square-warning", "danger")
        add("Phân bổ chờ duyệt", Allocation.objects.filter(status="PROPOSED").count(),
            "/app/sales/allocations?status=PROPOSED", "split", "danger")
        add("Điều chỉnh tồn chờ duyệt", StockAdjustment.objects.filter(status="PENDING_APPROVAL").count(),
            "/app/inventory/adjustments?status=PENDING_APPROVAL", "file-diff", "warn")
        add("Kiểm kê chờ duyệt", InventoryCount.objects.filter(status="PENDING_APPROVAL").count(),
            "/app/inventory/counts?status=PENDING_APPROVAL", "clipboard-check", "warn")
        add("Giá mua chờ duyệt", SupplierPrice.objects.filter(status="PENDING").count(),
            "/app/master/supplier-prices/?status=PENDING", "tag", "warn")
        add("Hóa đơn mua lệch 3 bên", Bill.objects.filter(status="MISMATCH").count(),
            "/app/accounting/bills?status=MISMATCH", "file-check-2", "danger")
        add("Phiếu nhập chờ duyệt (cũ)", GoodsReceipt.objects.filter(status="PENDING_APPROVAL").count(),
            "/app/warehouse/receipts?status=PENDING_APPROVAL", "package-check", "warn")
    elif role == "PURCHASER":
        summary = stock_summary()
        add("PR chờ xử lý", PurchaseRequisition.objects.filter(status="SUBMITTED").count(),
            "/app/purchase/requisitions?status=SUBMITTED", "file-plus-2")
        add("PO nhà máy chưa nhận", PurchaseOrder.objects.filter(status="SENT", supplier_ack_at__isnull=True).count(),
            "/app/purchase/orders?ack=pending", "factory", "warn")
        add("Mã chạm ROP / dưới SS", sum(1 for s in summary.values() if s["level"] != "OK" and s["product"].track_rop),
            "/app/inventory/replenishment?level=low", "trending-down", "danger")
        add("Xe đang về", InboundTrip.objects.filter(status__in=["LOADED", "IN_TRANSIT", "ARRIVED"]).count(),
            "/app/inbound/board", "truck")
        add("Khiếu nại cần gửi", Claim.objects.filter(status="APPROVED").count(), "/app/warehouse/claims?status=APPROVED",
            "send", "warn")
    elif role == "WAREHOUSE":
        add("Lịch hạ tải hôm nay", UnloadingSlot.objects.filter(start_at__date=today)
            .exclude(status__in=["CANCELLED", "DONE"]).count(), f"/app/warehouse/slots?date={today:%Y-%m-%d}",
            "calendar-clock")
        add("Phiếu cần soạn", Picking.objects.exclude(status="DONE").count(), "/app/fulfillment/picking", "package-open",
            "warn")
        add("Kiểm kê đang dở", InventoryCount.objects.filter(status__in=["COUNTING", "RECOUNT"]).count(),
            "/app/inventory/counts?status=OPEN", "clipboard-check", "warn")
        add("Phiếu xuất chờ ghi sổ", DeliveryTrip.objects.filter(status="READY").count(), "/app/fulfillment/picking",
            "package-minus")
        add("Phiếu nhập nháp", GoodsReceipt.objects.filter(status="DRAFT").count(), "/app/warehouse/receipts?status=DRAFT",
            "package-check")
        add("Hàng trả chờ nhập lại", SalesReturn.objects.filter(status="DRAFT").count(), "/app/fulfillment/returns",
            "undo-2", "danger")
    elif role == "DISPATCHER":
        add("Đơn chờ xử lý", SalesOrder.objects.filter(status="DRAFT").count(), "/app/sales/orders?status=DRAFT",
            "inbox", "warn")
        add("Đơn thiếu hàng", SalesOrderLine.objects.filter(atp_status__in=["SHORT", "BACKORDER"],
                                                            so__status__in=["CONFIRMED", "PARTIAL"])
            .values("so").distinct().count(), "/app/sales/orders?stock=short", "package-x", "danger")
        add("Chuyến giao hôm nay", DeliveryTrip.objects.filter(planned_date=today).exclude(status="CANCELLED").count(),
            "/app/fulfillment/planning", "route")
        add("Đơn chờ xếp chuyến", orders_waiting_delivery().count(), "/app/fulfillment/planning", "calendar-plus")
    elif role == "ACCOUNTANT":
        week = today + timedelta(days=7)
        add("Hóa đơn mua lệch 3 bên", Bill.objects.filter(status="MISMATCH").count(),
            "/app/accounting/bills?status=MISMATCH", "file-check-2", "danger")
        add("Phải thu đến hạn trong tuần", Invoice.objects.filter(status__in=["POSTED", "PARTIAL"],
                                                                  due_date__range=(today, week)).count(),
            "/app/accounting/debts?kind=AR", "hand-coins", "warn")
        add("Phải trả đến hạn trong tuần", Bill.objects.filter(status__in=["POSTED", "PARTIAL"],
                                                               due_date__range=(today, week)).count(),
            "/app/accounting/debts?kind=AP", "wallet", "warn")
        add("Điểm giao chờ lập hóa đơn", invoiceable_lines().values("stop").distinct().count(),
            "/app/accounting/invoices/new", "file-plus")
        add("Hóa đơn mua chờ ghi sổ", Bill.objects.filter(status__in=["MATCHED", "ACCEPTED"]).count(),
            "/app/accounting/bills?status=MATCHED", "book-check")
    return items


# ------------------------------------------------------------------ Sức khỏe tồn kho (STT 82)
def stock_health(top=10):
    """10 mã rủi ro nhất: sắp theo số ngày đủ bán tăng dần (khả dụng ≤ 0 = 0 ngày; chưa có nhu cầu xếp cuối).
    Mỗi dòng 1 thang riêng (đơn vị khác nhau): thanh = tồn dự kiến, 2 vạch = SS và ROP."""
    from apps.inventory.models import SimulatedDemand, StockQuant
    from apps.inventory.services import stock_summary
    from apps.masterdata.models import Product

    sim = SimulatedDemand.objects.values("product_id")
    prods = list(Product.objects.filter(is_active=True, track_rop=True).exclude(pk__in=sim).select_related("base_uom"))
    summ = stock_summary(prods)
    rows = []
    for s in summ.values():
        avail = float(s["available"])
        days = 0.0 if avail <= 0 else (float(s["days_cover"]) if s["days_cover"] is not None else None)
        proj, ss, rop = float(s["projected"]), float(s["ss"]), float(s["rop"])
        top_v = max(proj, rop * 1.25, ss * 1.5, 1.0)
        tone = "danger" if proj <= max(ss, 0) else ("warn" if proj <= rop else "success")
        rows.append({"product": s["product"], "projected": proj, "ss": ss, "rop": rop, "days": days,
                     "level": s["level"], "tone": tone,
                     "tone_label": {"danger": "Dưới SS", "warn": "Chạm ROP", "success": "An toàn"}[tone],
                     "w": max(0.0, min(100.0, proj / top_v * 100)), "ss_x": ss / top_v * 100, "rop_x": rop / top_v * 100,
                     "url": f"/app/inventory/replenishment?product={s['product'].pk}"})
    rows.sort(key=lambda r: (r["days"] is None, r["days"] if r["days"] is not None else 0, r["projected"] - r["rop"]))
    value = StockQuant.objects.aggregate(s=Sum(F("on_hand") * F("product__avg_cost")))["s"] or D0
    return {"rows": rows[:top], "n_risk": sum(1 for r in rows if r["tone"] != "success"), "n": len(rows),
            "stock_value": value}


# ------------------------------------------------------------------ Công nợ phải thu theo tuổi nợ (STT 83)
AGING_BANDS = [("current", "Trong hạn"), ("d30", "1–30 ngày"), ("d60", "31–60 ngày"), ("over60", "Trên 60 ngày")]
AGING_COLORS = ["#E39470", "#DA6A3C", "#BB4A18", "#82300E"]  # thang 1 sắc, nhạt -> đậm (đã kiểm định trên nền thẻ)


def ar_aging(limit=8):
    from apps.accounting.services import aging
    from apps.core.services import param
    rows, totals = aging("AR")
    block = int(param("OVERDUE_DAYS_BLOCK") or 30)
    out = []
    for r in rows:
        b = {"current": float(r["current"]), "d30": float(r["d30"]), "d60": float(r["d60"]),
             "over60": float(r["d90"] + r["over"])}
        total = sum(b.values())
        if total <= 0:
            continue
        out.append({"partner": r["partner"], "bands": b, "total": total,
                    "blocked": (b["d60"] + b["over60"]) > 0 if block <= 30 else b["over60"] > 0})
    out.sort(key=lambda r: -r["total"])
    scale = max((r["total"] for r in out[:limit]), default=1) or 1
    for r in out[:limit]:
        r["segs"] = [{"key": k, "label": l, "value": r["bands"][k], "w": r["bands"][k] / scale * 100, "color": c}
                     for (k, l), c in zip(AGING_BANDS, AGING_COLORS) if r["bands"][k] > 0]
    return {"rows": out[:limit], "more": max(0, len(out) - limit), "total": float(totals["total"]),
            "bands": [{"label": l, "color": c, "value": sum(r["bands"][k] for r in out)}
                      for (k, l), c in zip(AGING_BANDS, AGING_COLORS)], "block_days": block}


# ------------------------------------------------------------------ Nhu cầu & doanh thu 12 tháng + dự báo (STT 84)
def _month_add(d, n):
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def demand_12m(d_to, today=None):
    """Thực tế 12 tháng (đơn đã xác nhận, theo ngày đặt – cùng nguồn với dự báo) theo tấn và theo tiền, nối tiếp dự
    báo các tháng tới (13 tuần) từ apps/inventory/forecast.py. Mã có dữ liệu mô phỏng không tính vào đây."""
    from django.db.models.functions import Coalesce

    from apps.inventory.forecast import forecast, monday
    from apps.inventory.models import SimulatedDemand
    from apps.inventory.services import ORDER_STATUSES, weekly_demand
    from apps.masterdata.models import Product
    from apps.sales.models import SalesOrderLine

    today = today or timezone.localdate()
    last_m = d_to.replace(day=1)
    first_m = _month_add(last_m, -11)
    months = [_month_add(first_m, i) for i in range(12)]
    sim = set(SimulatedDemand.objects.values_list("product_id", flat=True).distinct())
    weight = {pk: (float(w) / 1000 if w else 1.0) for pk, w in Product.objects.values_list("pk", "unit_weight_kg")}
    ton, money = defaultdict(float), defaultdict(float)
    price_sum, price_qty = defaultdict(float), defaultdict(float)
    start = timezone.make_aware(datetime.combine(first_m, datetime.min.time()))
    for pid, q, price, dt in SalesOrderLine.objects.filter(so__status__in=ORDER_STATUSES) \
            .exclude(product_id__in=sim) \
            .annotate(dt=Coalesce("so__confirmed_at", "so__approved_at", "so__created_at")) \
            .filter(dt__gte=start).values_list("product_id", "qty", "unit_price", "dt"):
        m = timezone.localtime(dt).date().replace(day=1)
        if m > last_m:
            continue
        ton[m] += float(q) * weight.get(pid, 1.0)
        money[m] += float(q) * float(price)
        price_sum[pid] += float(q) * float(price)
        price_qty[pid] += float(q)
    fc_ton, fc_money = defaultdict(float), defaultdict(float)
    cur_m = today.replace(day=1)
    has_fc = last_m == cur_m
    if has_fc:  # dự báo chỉ có ý nghĩa khi kỳ xem kết thúc ở tháng hiện tại
        prods = list(Product.objects.filter(is_active=True).exclude(pk__in=sim))
        demand = weekly_demand(prods, today)
        cur_w = monday(today)
        for p in prods:
            d = demand[p.pk]
            if not d.weeks:
                continue
            fr = forecast(d.weeks, d.values)
            unit_price = price_sum[p.pk] / price_qty[p.pk] if price_qty[p.pk] else 0.0
            for k, f in enumerate(fr.forecast_weeks):
                w = cur_w + timedelta(days=7 * k)
                m = (w + timedelta(days=3)).replace(day=1)  # tháng của tuần = tháng của ngày thứ Năm
                fc_ton[m] += f * weight.get(p.pk, 1.0)
                fc_money[m] += f * unit_price
        # tháng hiện tại: thực tế các tuần đã qua của tháng + dự báo các tuần còn lại
        done_ton, done_money = defaultdict(float), defaultdict(float)
        for pid, q, price, dt in SalesOrderLine.objects.filter(so__status__in=ORDER_STATUSES) \
                .exclude(product_id__in=sim) \
                .annotate(dt=Coalesce("so__confirmed_at", "so__approved_at", "so__created_at")) \
                .filter(dt__gte=timezone.make_aware(datetime.combine(cur_m, datetime.min.time())),
                        dt__lt=timezone.make_aware(datetime.combine(cur_w, datetime.min.time()))) \
                .values_list("product_id", "qty", "unit_price", "dt"):
            done_ton[cur_m] += float(q) * weight.get(pid, 1.0)
            done_money[cur_m] += float(q) * float(price)
        fc_ton[cur_m] += done_ton[cur_m]
        fc_money[cur_m] += done_money[cur_m]
    fut = sorted(m for m in fc_ton if m > last_m)[:3] if has_fc else []
    labels = [f"{m:%m/%Y}" + (" (đến nay)" if m == cur_m else "") for m in months] + [f"{m:%m/%Y}" for m in fut]
    n_act = len(months)

    def series(act, fc):
        actual = [round(act.get(m, 0.0), 1) for m in months] + [None] * len(fut)
        line = [None] * (n_act - 1) + ([round(fc.get(last_m, 0.0), 1)] if has_fc else [None]) + \
            [round(fc.get(m, 0.0), 1) for m in fut]
        return actual, line
    t_act, t_fc = series(ton, fc_ton)
    m_act, m_fc = series({k: v / 1e6 for k, v in money.items()}, {k: v / 1e6 for k, v in fc_money.items()})
    return {"labels": labels, "ton": {"actual": t_act, "forecast": t_fc}, "money": {"actual": m_act, "forecast": m_fc},
            "has_forecast": has_fc}


# ------------------------------------------------------------------ Hàng về hôm nay – mai theo bến (STT 85)
def arrivals(today=None, hours=48):
    from apps.masterdata.models import Dock
    from apps.purchasing.models import InboundTrip, UnloadingSlot
    from apps.purchasing.services import slot_duration

    today = today or timezone.localdate()
    t0 = timezone.make_aware(datetime.combine(today, datetime.min.time()))
    t1 = t0 + timedelta(hours=hours)
    span = (t1 - t0).total_seconds()
    docks = list(Dock.objects.filter(is_active=True).order_by("warehouse_id", "code"))
    lanes = {d.pk: {"name": d.code, "bars": []} for d in docks}
    lanes[None] = {"name": "Chưa xếp bến", "bars": []}
    trips = InboundTrip.objects.exclude(status__in=["RECEIVED", "CANCELLED"]).select_related("vehicle", "po__supplier")
    slots = {s.trip_id: s for s in UnloadingSlot.objects.filter(trip__in=trips).exclude(status="CANCELLED")}
    now = timezone.now()
    rows = []
    for t in trips:
        s = slots.get(t.pk)
        start = s.start_at if s else t.eta
        if start is None:
            continue
        end = s.end_at if s else start + slot_duration(t)
        if end <= t0 or start >= t1:
            continue
        left = max(0.0, (start - t0).total_seconds() / span * 100)
        right = min(100.0, (end - t0).total_seconds() / span * 100)
        late = t.late_active or (s is None and t.eta and t.eta < now)
        bar = {"trip": t, "slot": s, "start": start, "end": end, "left": left, "w": max(1.2, right - left), "late": late,
               "url": f"/app/inbound/trips/{t.pk}"}
        lanes[s.dock_id if s else None]["bars"].append(bar)
        rows.append(bar)
    ticks = [{"x": h / hours * 100, "label": (t0 + timedelta(hours=h)).strftime("%H:%M") if h % 24 else
              (t0 + timedelta(hours=h)).strftime("%d/%m")} for h in range(0, hours + 1, 6)]
    now_x = (now - t0).total_seconds() / span * 100 if t0 <= now <= t1 else None
    out = [v for k, v in lanes.items() if k is not None] + ([lanes[None]] if lanes[None]["bars"] else [])
    return {"lanes": out, "ticks": ticks, "now_x": now_x, "rows": sorted(rows, key=lambda r: r["start"]),
            "from": t0, "to": t1}


# ------------------------------------------------------------------ Xu hướng theo tuần (STT 85)
def weekly_trends(d_to, weeks=12):
    from apps.reports.kpi import otif_data, wait_data
    wk = [(d_to - timedelta(days=7 * k + 6), d_to - timedelta(days=7 * k)) for k in range(weeks - 1, -1, -1)]
    labels = [f"{b:%d/%m}" for _, b in wk]
    otif = [otif_data(a, b)["otif"] for a, b in wk]
    wait = [wait_data(a, b)["avg"] for a, b in wk]
    r = lambda v: None if v is None else round(v, 1)  # noqa: E731
    return {"labels": labels, "weeks": [f"{a:%d/%m}–{b:%d/%m}" for a, b in wk],
            "otif": [r(v) for v in otif], "wait": [r(v) for v in wait], "otif_target": 95, "wait_target": 30}


# ------------------------------------------------------------------ Cảnh báo gộp theo loại (STT 86)
def alert_groups(user):
    from .context_processors import alerts_for
    rows = list(alerts_for(user).values("alert_type").annotate(n=Count("id")).order_by("-n"))
    return [{"type": r["alert_type"], "label": ALERT_LABELS.get(r["alert_type"], r["alert_type"]), "n": r["n"],
             "url": f"/app/alerts?type={r['alert_type']}"} for r in rows]


# ================================================================== Bố cục mới (theo mẫu ArgoLogics, tông cam – đen)
def _weeks_back(d_to, n):
    """n tuần (thứ Hai) kết thúc ở tuần chứa d_to."""
    from apps.inventory.forecast import monday
    last = monday(d_to)
    return [last - timedelta(days=7 * k) for k in range(n - 1, -1, -1)]


def volume(d_from, d_to, weeks=26):
    """Khối "Khối lượng đơn hàng": đường nhu cầu theo tuần (tấn, đơn đã xác nhận theo ngày đặt) + 2 số lớn của kỳ:
    tấn đã xác nhận và tỷ lệ giao thành công (điểm giao Đã giao / Giao một phần trên tổng điểm giao đã xử lý)."""
    from django.db.models.functions import Coalesce

    from apps.inventory.forecast import monday
    from apps.inventory.models import SimulatedDemand
    from apps.inventory.services import ORDER_STATUSES
    from apps.masterdata.models import Product
    from apps.sales.models import DeliveryStop, SalesOrderLine

    wk = _weeks_back(d_to, weeks)
    start = timezone.make_aware(datetime.combine(min(wk[0], d_from), datetime.min.time()))
    weight = {pk: (float(w) / 1000 if w else 1.0) for pk, w in Product.objects.values_list("pk", "unit_weight_kg")}
    sim = SimulatedDemand.objects.values("product_id")
    by_week, period_ton = defaultdict(float), 0.0
    for pid, q, dt in SalesOrderLine.objects.filter(so__status__in=ORDER_STATUSES).exclude(product_id__in=sim) \
            .annotate(dt=Coalesce("so__confirmed_at", "so__approved_at", "so__created_at")) \
            .filter(dt__gte=start).values_list("product_id", "qty", "dt"):
        d = timezone.localtime(dt).date()
        t = float(q) * weight.get(pid, 1.0)
        by_week[monday(d)] += t
        if d_from <= d <= d_to:
            period_ton += t
    stops = DeliveryStop.objects.filter(delivered_at__date__range=(d_from, d_to))
    n = stops.count()
    ok = stops.filter(status__in=["DELIVERED", "PARTIAL"]).count()
    return {"labels": [f"{w:%d/%m}" for w in wk], "values": [round(by_week.get(w, 0.0), 1) for w in wk],
            "period_ton": period_ton, "success_pct": (ok / n * 100) if n else None, "stops": n}


def fleet(today=None):
    """Khối "Đội xe": 3 ô số + 1 chuyến đang chạy nổi bật (xe, tài xế, liên hệ)."""
    from apps.masterdata.models import Vehicle
    from apps.purchasing.models import InboundTrip
    from apps.sales.models import DeliveryTrip

    today = today or timezone.localdate()
    out_now = DeliveryTrip.objects.filter(status="IN_TRANSIT")
    in_now = InboundTrip.objects.filter(status__in=["LOADED", "IN_TRANSIT", "ARRIVED"])
    feat = out_now.select_related("vehicle", "driver").order_by("departed_at").first()
    kind = "out"
    if feat is None:
        feat = DeliveryTrip.objects.filter(planned_date=today).exclude(status__in=["CANCELLED", "COMPLETED"]) \
            .select_related("vehicle", "driver").first()
    if feat is None:
        feat, kind = in_now.select_related("vehicle", "driver", "po__supplier").order_by("eta").first(), "in"
    trip = None
    if feat is not None:
        trip = {"code": feat.code, "plate": feat.vehicle.plate, "status": feat.get_status_display(),
                "driver": feat.driver.name, "phone": feat.driver.phone,
                "initials": "".join(w[0] for w in feat.driver.name.split()[-2:]).upper(),
                "url": f"/app/inbound/trips/{feat.pk}" if kind == "in" else "/app/fulfillment/planning",
                "kind": "Inbound" if kind == "in" else "Giao hàng"}
    return {"total": Vehicle.objects.filter(is_active=True).count(), "out_now": out_now.count(), "in_now": in_now.count(),
            "today": DeliveryTrip.objects.filter(planned_date=today).exclude(status="CANCELLED").count(), "trip": trip}


SO_STEPS = ["Đã gửi", "Đã xác nhận", "Đang giao", "Đã giao"]
IB_STEPS = [("Chờ bốc", ("PLANNED", "AT_FACTORY")), ("Đang về", ("LOADED", "IN_TRANSIT")),
            ("Tới kho", ("ARRIVED", "UNLOADING")), ("Đã nhập", ("RECEIVED",))]


def latest(user):
    """Khối "mới nhất": đơn bán mới nhất (Giám đốc / Điều phối / Kế toán) hoặc chuyến Inbound mới nhất (Thu mua /
    Thủ kho) – mã, trạng thái, địa chỉ, thanh tiến trình theo bước."""
    from apps.purchasing.models import InboundTrip
    from apps.sales.models import SalesOrder

    if user.role in ("PURCHASER", "WAREHOUSE"):
        t = InboundTrip.objects.exclude(status="CANCELLED").select_related("po__supplier", "po__warehouse", "vehicle") \
            .order_by("-id").first()
        if t is None:
            return None
        step = next((i for i, (_, sts) in enumerate(IB_STEPS) if t.status in sts), 0)
        return {"title": "Chuyến Inbound mới nhất", "code": t.code, "status": t.get_status_display(),
                "line1": t.po.supplier.name, "line2": f"Xe {t.vehicle.plate} · về {t.po.warehouse.name}",
                "steps": [s for s, _ in IB_STEPS], "step": step, "pct": (step + 1) / len(IB_STEPS) * 100,
                "url": f"/app/inbound/trips/{t.pk}", "all_url": "/app/inbound/board"}
    so = SalesOrder.objects.exclude(status="CANCELLED").select_related("customer", "site").order_by("-id").first()
    if so is None:
        return None
    _, _, reached = so.customer_status
    return {"title": "Đơn bán mới nhất", "code": so.code, "status": so.get_status_display(),
            "line1": so.customer.name, "line2": so.site.address or so.site.name, "steps": SO_STEPS,
            "step": max(reached - 1, 0), "pct": max(reached, 1) / len(SO_STEPS) * 100,
            "url": f"/app/sales/orders/{so.pk}", "all_url": "/app/sales/orders"}


def sales_overview(user, d_from, d_to, weeks=26):
    """Khối "Doanh thu": doanh thu hóa đơn bán (chưa VAT) của kỳ, % tăng / giảm so với kỳ trước cùng độ dài, cột theo
    tuần (26 tuần gần nhất) – tuần thuộc kỳ đang xem tô màu nhấn. Vai trò không được xem giá (Thủ kho) thấy khối
    lượng nhập kho theo tuần thay cho tiền."""
    from apps.accounting.models import Invoice
    from apps.inventory.forecast import monday
    from apps.purchasing.models import GoodsReceiptLine

    wk = _weeks_back(d_to, weeks)
    span = (d_to - d_from).days + 1
    p_to = d_from - timedelta(days=1)
    p_from = p_to - timedelta(days=span - 1)
    money = user.can_see_price
    if money:
        rows = Invoice.objects.filter(invoice_type="SALE", invoice_date__gte=min(wk[0], p_from)) \
            .exclude(status__in=["DRAFT", "CANCELLED"]).values_list("invoice_date", "subtotal")
        pts = [(d, float(v)) for d, v in rows]
    else:
        rows = GoodsReceiptLine.objects.filter(gr__status="POSTED", gr__posted_at__date__gte=min(wk[0], p_from)) \
            .values_list("gr__posted_at", "qty_received", "product__unit_weight_kg")
        pts = [(timezone.localtime(d).date(), float(q) * (float(w) / 1000 if w else 1.0)) for d, q, w in rows]
    by_week = defaultdict(float)
    cur = prev = 0.0
    for d, v in pts:
        by_week[monday(d)] += v
        if d_from <= d <= d_to:
            cur += v
        elif p_from <= d <= p_to:
            prev += v
    bars = [{"label": f"{w:%d/%m}", "v": by_week.get(w, 0.0), "on": monday(d_from) <= w <= d_to} for w in wk]
    top = max((b["v"] for b in bars), default=0) or 1
    for b in bars:
        b["h"] = b["v"] / top * 100
        b["v_text"] = f"{b['v'] / 1e6:,.1f} triệu ₫" if money else f"{b['v']:,.1f} tấn"
    return {"money": money, "value": cur, "prev": prev, "growth": ((cur - prev) / prev * 100) if prev else None,
            "bars": bars, "ticks": [b["label"] for i, b in enumerate(bars) if i % 5 == 0],
            "title": "Doanh thu" if money else "Nhập kho", "url": "/app/accounting/invoices" if money else
            "/app/warehouse/receipts", "range": f"{wk[0]:%d/%m} – {d_to:%d/%m/%Y}"}


def hero_image_url():
    """Ảnh doanh nghiệp đầu Dashboard (Giám đốc / Admin tải lên) – None thì dùng hình minh họa mặc định."""
    from django.core.files.storage import default_storage

    from .models import SystemParam
    row = SystemParam.objects.filter(key="DASHBOARD_HERO_IMAGE").first()
    return default_storage.url(row.value) if row and row.value else None
