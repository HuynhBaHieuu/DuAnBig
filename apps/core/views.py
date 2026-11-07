from datetime import date, timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .context_processors import alerts_for
from .models import Alert, AuditLog, Role, SystemParam, User
from .permissions import role_required
from .services import audit, param
from .utils import fold, paginate

D0 = Decimal(0)


# ------------------------------------------------------------------ Đăng nhập (MH-01)
def login_view(request):
    if request.user.is_authenticated:
        return redirect(request.user.home_url)
    error = None
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        u = User.objects.filter(username__iexact=username).first()
        now = timezone.now()
        if u and u.locked_until and u.locked_until > now:
            mins = int((u.locked_until - now).total_seconds() // 60) + 1
            error = f"Tài khoản tạm khóa do đăng nhập sai nhiều lần. Thử lại sau {mins} phút."
        else:
            user = authenticate(request, username=u.username if u else username, password=password)
            if user and user.is_active:
                user.failed_login_count, user.locked_until = 0, None
                user.save(update_fields=["failed_login_count", "locked_until"])
                login(request, user)
                audit("LOGIN", user, user=user)
                return redirect(request.GET.get("next") or user.home_url)
            if u:
                u.failed_login_count += 1
                if u.failed_login_count >= param("LOGIN_MAX_FAIL", int):
                    u.locked_until = now + timedelta(minutes=param("LOGIN_LOCK_MIN"))
                    u.failed_login_count = 0
                    error = f"Sai mật khẩu {param('LOGIN_MAX_FAIL', int)} lần. Tài khoản bị khóa {param('LOGIN_LOCK_MIN', int)} phút."
                else:
                    left = param("LOGIN_MAX_FAIL", int) - u.failed_login_count
                    error = f"Sai tên đăng nhập hoặc mật khẩu. Còn {left} lần thử."
                u.save(update_fields=["failed_login_count", "locked_until"])
                audit("LOGIN_FAIL", u, user=u)
            else:
                error = "Sai tên đăng nhập hoặc mật khẩu."
    return render(request, "core/login.html", {"error": error})


@require_POST
def logout_view(request):
    logout(request)
    return redirect("/login")


@login_required
def home(request):
    return redirect(request.user.home_url)


@login_required
def profile(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    for f in form.fields.values():
        f.widget.attrs["class"] = "input"
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        messages.success(request, "Đã đổi mật khẩu.")
        return redirect("/app/profile")
    return render(request, "core/profile.html", {"form": form, "title": "Tài khoản của tôi"})


# ------------------------------------------------------------------ Dashboard (MH-03)
def _month_start():
    return timezone.localdate().replace(day=1)


def _otif(since, until=None):
    from apps.sales.models import DeliveryStop
    stops = DeliveryStop.objects.filter(delivered_at__date__gte=since).select_related("so")
    if until:
        stops = stops.filter(delivered_at__date__lte=until)
    total = ok = 0
    for s in stops:
        total += 1
        on_time = timezone.localtime(s.delivered_at).date() <= s.so.requested_date
        if on_time and s.status == "DELIVERED":
            ok += 1
    return (ok / total * 100) if total else None


def _series_by_day(qs, field, value_expr, days=14):
    start = timezone.localdate() - timedelta(days=days - 1)
    data = {r["d"]: r["v"] for r in qs.filter(**{f"{field}__date__gte": start})
            .annotate(d=TruncDate(field)).values("d").annotate(v=value_expr)}
    labels, values = [], []
    for i in range(days):
        d = start + timedelta(days=i)
        labels.append(d.strftime("%d/%m"))
        values.append(float(data.get(d) or 0))
    return labels, values


@login_required
def dashboard(request):
    from apps.accounting.models import Bill, Invoice
    from apps.accounting.services import aging, invoiceable_lines
    from apps.inventory.models import InventoryCount, StockMove, StockQuant
    from apps.inventory.services import stock_summary
    from apps.purchasing.models import GoodsReceipt, InboundTrip, PurchaseOrder, PurchaseRequisition, UnloadingSlot
    from apps.sales.models import Allocation, DeliveryTrip, Picking, SalesOrder, SalesOrderLine, SalesReturn
    from apps.sales.services import orders_waiting_delivery

    from apps.purchasing.services import maybe_scan_inbound
    maybe_scan_inbound()
    u = request.user
    role = u.role
    today = timezone.localdate()
    ms = _month_start()
    # Bộ lọc tháng cho KPI (mặc định tháng hiện tại): ?month=YYYY-MM
    sel_ms = ms
    try:
        y, m = map(int, (request.GET.get("month") or "").split("-"))
        sel_ms = date(y, m, 1)
    except (ValueError, TypeError):
        pass
    if sel_ms > ms:
        sel_ms = ms
    sel_me = (sel_ms + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    month_opts = []
    d = ms
    for _ in range(12):
        month_opts.append({"v": d.strftime("%Y-%m"), "l": f"Tháng {d:%m/%Y}" + (" (hiện tại)" if d == ms else "")})
        d = (d - timedelta(days=1)).replace(day=1)
    is_cur = sel_ms == ms
    mlabel = "tháng này" if is_cur else f"tháng {sel_ms:%m/%Y}"

    def otif_kpi():
        v = _otif(sel_ms, sel_me)
        if v is None:
            return {"label": f"OTIF {mlabel}", "value": "N/A", "icon": "target", "tone": "success",
                    "foot": "Chưa có chuyến giao hoàn tất trong tháng"}
        return {"label": f"OTIF {mlabel}", "value": f"{v:.1f}%", "icon": "target", "tone": "success",
                "foot": "Giao đúng hạn & đủ"}
    kpis, todos, charts = [], [], []

    def todo(label, count, url, tone="primary", icon="circle-dot"):
        if count:
            todos.append({"label": label, "count": count, "url": url, "tone": tone, "icon": icon})

    active_trips = InboundTrip.objects.exclude(status__in=["RECEIVED", "CANCELLED"])

    if role in ("DIRECTOR", "ADMIN"):
        # Doanh thu = hóa đơn bán đã ghi sổ (cùng điều kiện với biểu đồ 6 tháng để 2 số luôn khớp)
        rev_qs = Invoice.objects.filter(invoice_type="SALE").exclude(status__in=["DRAFT", "CANCELLED"])
        revenue = rev_qs.filter(invoice_date__gte=sel_ms, invoice_date__lte=sel_me)             .aggregate(s=Sum("subtotal"))["s"] or D0
        n_inv = rev_qs.filter(invoice_date__gte=sel_ms, invoice_date__lte=sel_me).count()
        stock_value = StockQuant.objects.aggregate(s=Sum(F("on_hand") * F("product__avg_cost")))["s"] or D0
        ar = aging("AR")[1]["total"]
        kpis = [
            {"label": f"Doanh thu {mlabel}", "value": revenue, "fmt": "money", "icon": "badge-dollar-sign",
             "tone": "primary", "foot": f"{sel_ms:%d/%m} – {min(sel_me, today):%d/%m/%Y} · {n_inv} hóa đơn"
             if n_inv else "Chưa có hóa đơn bán ghi sổ trong tháng"},
            {"label": "Giá trị tồn kho", "value": stock_value, "fmt": "money", "icon": "warehouse", "tone": "info",
             "foot": "Theo giá vốn bình quân"},
            otif_kpi(),
            {"label": "Công nợ phải thu", "value": ar, "fmt": "money", "icon": "hand-coins", "tone": "warn",
             "foot": "Tổng số dư"},
        ]
        todo("PO chờ duyệt (≥ 50 triệu)", PurchaseOrder.objects.filter(status="PENDING_APPROVAL").count(),
             "/app/purchase/orders?status=PENDING_APPROVAL", "warn", "shopping-cart")
        todo("SO vượt hạn mức / dưới giá sàn", SalesOrder.objects.filter(status="PENDING_APPROVAL").count(),
             "/app/sales/orders?status=PENDING_APPROVAL", "warn", "receipt")
        todo("Phân bổ thiếu hàng chờ duyệt", Allocation.objects.filter(status="PROPOSED").count(),
             "/app/sales/allocations", "danger", "split")
        todo("GRN lệch > 2% chờ duyệt", GoodsReceipt.objects.filter(status="PENDING_APPROVAL").count(),
             "/app/warehouse/receipts?status=PENDING_APPROVAL", "danger", "package-check")
        todo("Hóa đơn mua lệch 3 bên", Bill.objects.filter(status="MISMATCH").count(),
             "/app/accounting/bills?status=MISMATCH", "danger", "file-check-2")
        todo("Kiểm kê chờ duyệt", InventoryCount.objects.filter(status="PENDING_APPROVAL").count(),
             "/app/inventory/counts", "warn", "clipboard-check")
        # biểu đồ doanh thu 6 tháng
        start6 = (ms - timedelta(days=150)).replace(day=1)
        rev = {r["m"].strftime("%m/%Y") if hasattr(r["m"], "strftime") else str(r["m"]): float(r["v"] or 0)
               for r in Invoice.objects.filter(invoice_type="SALE", invoice_date__gte=start6)
               .exclude(status__in=["DRAFT", "CANCELLED"]).annotate(m=TruncMonth("invoice_date"))
               .values("m").annotate(v=Sum("subtotal"))}
        labels, d = [], start6
        while d <= ms:
            labels.append(d.strftime("%m/%Y"))
            d = (d + timedelta(days=32)).replace(day=1)
        charts.append({"id": "rev", "type": "bar", "title": "Doanh thu 6 tháng (triệu ₫)", "labels": labels,
                       "datasets": [{"label": "Doanh thu", "data": [round(rev.get(l, 0) / 1e6, 1) for l in labels],
                                     "color": "primary"}]})
        by_status = dict(active_trips.values_list("status").annotate(n=Count("id")))
        charts.append({"id": "trip", "type": "doughnut", "title": "Chuyến Inbound đang chạy",
                       "labels": [dict(InboundTrip.STATUS)[k] for k in by_status],
                       "datasets": [{"label": "Chuyến", "data": list(by_status.values())}]})

    elif role == "PURCHASER":
        summary = stock_summary()
        below = [s for s in summary.values() if s["level"] != "OK" and s["product"].track_rop]
        kpis = [
            {"label": "PR chờ xử lý", "value": PurchaseRequisition.objects.filter(status="SUBMITTED").count(),
             "icon": "file-plus-2", "tone": "primary"},
            {"label": "PO đang mở", "value": PurchaseOrder.objects.filter(status__in=["APPROVED", "SENT", "PARTIAL"]).count(),
             "icon": "shopping-cart", "tone": "info"},
            {"label": "Chuyến Inbound đang chạy", "value": active_trips.count(), "icon": "truck", "tone": "warn"},
            {"label": "Chuyến trễ mốc", "value": active_trips.filter(is_late=True, status__in=InboundTrip.ACTIVE).count(), "icon": "alarm-clock",
             "tone": "danger"},
        ]
        todo("Đề xuất mua (PR) cần lập PO", PurchaseRequisition.objects.filter(status="SUBMITTED").count(),
             "/app/purchase/requisitions?status=SUBMITTED", "primary", "file-plus-2")
        todo("PO đã duyệt – chưa gửi NCC", PurchaseOrder.objects.filter(status="APPROVED").count(),
             "/app/purchase/orders?status=APPROVED", "warn", "send")
        todo("PO đã gửi – chưa xếp chuyến",
             PurchaseOrder.objects.filter(status__in=["SENT", "PARTIAL"], lines__qty__gt=F("lines__qty_on_trips")).distinct().count(),
             "/app/purchase/orders?status=SENT", "info", "truck")
        todo("Chuyến trễ mốc hành trình", active_trips.filter(is_late=True, status__in=InboundTrip.ACTIVE).count(), "/app/inbound/board", "danger",
             "alarm-clock")
        todo("Sản phẩm dưới ROP", len(below), "/app/inventory/replenishment?level=low", "danger", "trending-down")
        labels, values = _series_by_day(PurchaseOrder.objects.exclude(status__in=["DRAFT", "CANCELLED"]),
                                        "created_at", Sum("total_amount"), 14)
        charts.append({"id": "po", "type": "bar", "title": "Giá trị PO 14 ngày (triệu ₫)", "labels": labels,
                       "datasets": [{"label": "PO", "data": [round(v / 1e6, 1) for v in values], "color": "primary"}]})
        charts.append({"id": "lv", "type": "doughnut", "title": "Tình trạng tồn theo ROP",
                       "labels": ["Đủ", "Dưới ROP", "Dưới SS", "Hết hàng"],
                       "datasets": [{"label": "SP", "data": [sum(1 for s in summary.values() if s["level"] == k)
                                                             for k in ["OK", "BELOW_ROP", "BELOW_SS", "STOCKOUT"]]}]})

    elif role == "DISPATCHER":
        open_so = SalesOrder.objects.filter(status__in=["CONFIRMED", "PARTIAL", "PENDING_APPROVAL"])
        short = SalesOrderLine.objects.filter(atp_status__in=["SHORT", "BACKORDER"],
                                              so__status__in=["CONFIRMED", "PARTIAL"]).values("so").distinct().count()
        kpis = [
            {"label": "Đơn bán đang mở", "value": open_so.count(), "icon": "receipt", "tone": "primary"},
            {"label": "Đơn thiếu hàng / backorder", "value": short, "icon": "package-x", "tone": "danger"},
            {"label": "Chuyến giao hôm nay", "value": DeliveryTrip.objects.filter(planned_date=today).exclude(status="CANCELLED").count(),
             "icon": "route", "tone": "warn"},
            otif_kpi(),
        ]
        todo("Đơn chờ xếp chuyến giao", orders_waiting_delivery().count(), "/app/fulfillment/planning", "primary", "route")
        todo("Đơn từ cổng khách hàng mới", SalesOrder.objects.filter(source="PORTAL", status="DRAFT").count(),
             "/app/sales/orders?status=DRAFT", "info", "globe")
        todo("Đơn có backorder", short, "/app/sales/orders?stock=short", "danger", "package-x")
        todo("Chuyến chưa phát lệnh soạn", DeliveryTrip.objects.filter(status="PLANNED").count(),
             "/app/fulfillment/planning", "warn", "clipboard-list")
        labels, values = _series_by_day(SalesOrder.objects.exclude(status="CANCELLED"), "created_at", Count("id"), 14)
        charts.append({"id": "so", "type": "line", "title": "Số đơn bán 14 ngày", "labels": labels,
                       "datasets": [{"label": "Đơn", "data": values, "color": "primary"}]})
        by = dict(open_so.values_list("status").annotate(n=Count("id")))
        charts.append({"id": "sost", "type": "doughnut", "title": "Đơn đang mở theo trạng thái",
                       "labels": [dict(SalesOrder.STATUS)[k] for k in by], "datasets": [{"label": "Đơn", "data": list(by.values())}]})

    elif role == "WAREHOUSE":
        slots_today = UnloadingSlot.objects.filter(start_at__date=today).exclude(status="CANCELLED")
        kpis = [
            {"label": "Lịch hạ tải hôm nay", "value": slots_today.count(), "icon": "calendar-clock", "tone": "primary"},
            {"label": "Xe tới cổng chờ hạ", "value": InboundTrip.objects.filter(status="ARRIVED").count(),
             "icon": "truck", "tone": "warn"},
            {"label": "Lệnh soạn chờ", "value": Picking.objects.exclude(status="DONE").count(), "icon": "package-open",
             "tone": "info"},
            {"label": "Phiếu xuất chờ ghi sổ", "value": DeliveryTrip.objects.filter(status="READY").count(),
             "icon": "package-minus", "tone": "success"},
        ]
        todo("Lịch hạ tải cần xác nhận", slots_today.filter(status="PROPOSED").count(), "/app/warehouse/slots", "primary",
             "calendar-check")
        todo("Xe tới cổng – cần check-in", InboundTrip.objects.filter(status="ARRIVED").count(), "/app/warehouse/slots",
             "warn", "log-in")
        todo("Phiếu nhập GRN nháp", GoodsReceipt.objects.filter(status="DRAFT").count(),
             "/app/warehouse/receipts?status=DRAFT", "info", "package-check")
        todo("Lệnh soạn hàng chờ thực hiện", Picking.objects.exclude(status="DONE").count(), "/app/fulfillment/picking",
             "warn", "package-open")
        todo("Hàng trả về chờ nhập lại", SalesReturn.objects.filter(status="DRAFT").count(), "/app/fulfillment/returns",
             "danger", "undo-2")
        todo("Phiếu kiểm kê đang đếm", InventoryCount.objects.filter(status__in=["COUNTING", "RECOUNT"]).count(),
             "/app/inventory/counts", "primary", "clipboard-check")
        l1, v_in = _series_by_day(StockMove.objects.filter(qty__gt=0, move_type__in=["IN_PURCHASE", "IN_RETURN"]), "at", Sum("qty"))
        _, v_out = _series_by_day(StockMove.objects.filter(qty__lt=0, move_type__in=["OUT_SALE", "OUT_INTERNAL"]), "at", Sum("qty"))
        charts.append({"id": "io", "type": "bar", "title": "Nhập / xuất 14 ngày (SL)", "labels": l1,
                       "datasets": [{"label": "Nhập", "data": v_in, "color": "primary"},
                                    {"label": "Xuất", "data": [abs(v) for v in v_out], "color": "warn"}]})

    elif role == "ACCOUNTANT":
        ar_rows, ar = aging("AR")
        ap_rows, ap = aging("AP")
        kpis = [
            {"label": "Phải thu", "value": ar["total"], "fmt": "money", "icon": "hand-coins", "tone": "primary"},
            {"label": "Phải trả", "value": ap["total"], "fmt": "money", "icon": "wallet", "tone": "info"},
            {"label": "Phải thu quá hạn", "value": ar["total"] - ar["current"], "fmt": "money", "icon": "alarm-clock",
             "tone": "danger"},
            {"label": "Dòng giao chờ lập HĐ", "value": invoiceable_lines().count(), "icon": "file-text", "tone": "warn"},
        ]
        todo("Điểm giao B2B chờ lập hóa đơn", invoiceable_lines().values("stop").distinct().count(),
             "/app/accounting/invoices/new", "primary", "file-plus")
        todo("Hóa đơn bán nháp chờ ghi sổ", Invoice.objects.filter(status="DRAFT").count(),
             "/app/accounting/invoices?status=DRAFT", "info", "file-text")
        todo("Hóa đơn mua khớp – chờ ghi sổ", Bill.objects.filter(status__in=["MATCHED", "ACCEPTED"]).count(),
             "/app/accounting/bills?status=MATCHED", "success", "file-check-2")
        todo("Hóa đơn mua lệch 3 bên", Bill.objects.filter(status="MISMATCH").count(),
             "/app/accounting/bills?status=MISMATCH", "danger", "alert-triangle")
        todo("Hóa đơn bán đến hạn 7 ngày tới",
             Invoice.objects.filter(status__in=["POSTED", "PARTIAL"], due_date__range=(today, today + timedelta(days=7))).count(),
             "/app/accounting/debts", "warn", "calendar")
        from apps.accounting.services import BUCKETS
        charts.append({"id": "aging", "type": "bar", "title": "Tuổi nợ (triệu ₫)", "labels": [b[1] for b in BUCKETS],
                       "datasets": [{"label": "Phải thu", "data": [round(float(ar[b]) / 1e6, 1) for b, _ in BUCKETS], "color": "primary"},
                                    {"label": "Phải trả", "data": [round(float(ap[b]) / 1e6, 1) for b, _ in BUCKETS], "color": "warn"}]})

    alerts = alerts_for(u)[:6]
    return render(request, "core/dashboard.html", {
        "title": "Dashboard", "kpis": kpis, "todos": todos, "charts": charts, "alerts": alerts, "today": today,
        "month_opts": month_opts if role in ("DIRECTOR", "ADMIN", "DISPATCHER") else None,
        "sel_month": sel_ms.strftime("%Y-%m"),
        "recent_trips": active_trips.select_related("vehicle", "po__supplier").order_by("eta")[:5]
        if role in ("ADMIN", "DIRECTOR", "PURCHASER", "WAREHOUSE") else None,
    })


# ------------------------------------------------------------------ Cảnh báo / tìm kiếm
@login_required
def alerts(request):
    qs = alerts_for(request.user) if request.GET.get("all") != "1" else Alert.objects.all()
    page, qs_str = paginate(request, qs, 30)
    return render(request, "core/alerts.html", {"title": "Cảnh báo", "page_obj": page, "qs": qs_str})


@login_required
@require_POST
def alert_resolve(request, pk):
    Alert.objects.filter(pk=pk).update(is_resolved=True)
    return redirect(request.META.get("HTTP_REFERER") or "/app/alerts")


@login_required
def search(request):
    from apps.masterdata.models import Customer, Product, Supplier
    from apps.purchasing.models import GoodsReceipt, InboundTrip, PurchaseOrder
    from apps.sales.models import SalesOrder

    q = request.GET.get("q", "").strip()
    if not q:  # xóa từ khóa -> quay lại trang danh sách trước đó (dữ liệu đầy đủ), không báo "không có kết quả"
        back = request.META.get("HTTP_REFERER", "")
        return redirect(back if back and "/app/search" not in back else "/app/dashboard")
    results = []
    if q:
        for model, url, label in [(PurchaseOrder, "/app/purchase/orders/{}", "Đơn mua"),
                                  (SalesOrder, "/app/sales/orders/{}", "Đơn bán"),
                                  (InboundTrip, "/app/inbound/trips/{}", "Chuyến Inbound"),
                                  (GoodsReceipt, "/app/warehouse/receipts/{}", "Phiếu nhập")]:
            for o in model.objects.filter(code__icontains=q)[:10]:
                results.append({"type": label, "code": o.code, "url": url.format(o.pk), "obj": o})
        for o in Product.objects.filter(Q(code__icontains=q) | Q(name__icontains=q))[:10]:
            results.append({"type": "Sản phẩm", "code": o.code, "name": o.name, "url": f"/app/inventory/stock?q={o.code}"})
        for o in Customer.objects.filter(Q(code__icontains=q) | Q(name__icontains=q))[:10]:
            results.append({"type": "Khách hàng", "code": o.code, "name": o.name, "url": f"/app/sales/orders?q={o.code}"})
        for o in Supplier.objects.filter(Q(code__icontains=q) | Q(name__icontains=q))[:10]:
            results.append({"type": "Nhà cung cấp", "code": o.code, "name": o.name, "url": f"/app/purchase/orders?q={o.code}"})
        if request.user.role == "ADMIN":
            for u in match_users(q)[:10]:
                results.append({"type": "Tài khoản", "code": u.username, "name": f"{u.display_name} · {u.get_role_display()}",
                                "url": f"/app/admin/users/{u.pk}"})
    return render(request, "core/search.html", {"title": f"Tìm kiếm “{q}”", "results": results, "q": q})


# ------------------------------------------------------------------ Quản trị (ADMIN)
class UserForm(forms.ModelForm):
    password = forms.CharField(required=False, widget=forms.PasswordInput, label="Mật khẩu (để trống nếu không đổi)")

    class Meta:
        model = User
        fields = ["username", "full_name", "email", "phone", "role", "driver", "customer", "is_active"]
        labels = {"username": "Tên đăng nhập", "full_name": "Họ tên", "phone": "Điện thoại", "role": "Vai trò",
                  "driver": "Gắn tài xế (DRIVER)", "customer": "Gắn khách hàng (CUSTOMER)", "is_active": "Đang hoạt động"}

    def clean(self):
        c = super().clean()
        if c.get("role") == Role.DRIVER and not c.get("driver"):
            self.add_error("driver", "Tài khoản DRIVER phải gắn với 1 tài xế.")
        if c.get("role") == Role.CUSTOMER and not c.get("customer"):
            self.add_error("customer", "Tài khoản CUSTOMER phải gắn với 1 khách hàng.")
        return c


def match_users(q, qs=None):
    """Tìm tài khoản theo tên đăng nhập, họ tên, ID, email, SĐT, tài xế / khách hàng liên kết.

    Lọc bằng Python để không phân biệt dấu ('ly' khớp 'Lý') trên cả SQLite lẫn PostgreSQL – bảng user nhỏ."""
    qs = qs if qs is not None else User.objects.select_related("driver", "customer")
    key = fold(q)
    if not key:
        return list(qs)
    return [u for u in qs if key in fold(" ".join(str(x) for x in (
        u.pk, u.username, u.full_name, u.first_name, u.last_name, u.email, u.phone,
        u.driver.name if u.driver else "", u.customer.name if u.customer else "", u.get_role_display())))]


@role_required("ADMIN")
def users(request):
    qs = User.objects.select_related("driver", "customer").order_by("role", "username")
    if request.GET.get("role"):
        qs = qs.filter(role=request.GET["role"])
    q = request.GET.get("q", "").strip()
    return render(request, "core/users.html", {"title": "Tài khoản & phân quyền", "users": match_users(q, qs),
                                               "roles": Role.choices, "now": timezone.now(), "q": q})


@role_required("ADMIN")
def user_edit(request, pk=None):
    obj = get_object_or_404(User, pk=pk) if pk else None
    form = UserForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        u = form.save(commit=False)
        if form.cleaned_data["password"]:
            u.set_password(form.cleaned_data["password"])
        elif not obj:
            u.set_password("Tracon@123")
        u.locked_until = None
        u.save()
        if u.role == Role.CUSTOMER:
            from apps.masterdata.models import UserSiteAccess
            ids = request.POST.getlist("sites")
            UserSiteAccess.objects.filter(user=u).exclude(site_id__in=ids).delete()
            for sid in ids:
                UserSiteAccess.objects.get_or_create(user=u, site_id=sid)
        messages.success(request, f"Đã lưu tài khoản {u.username}." + ("" if obj or form.cleaned_data["password"]
                                                                       else " Mật khẩu mặc định: Tracon@123"))
        return redirect("/app/admin/users")
    from apps.masterdata.models import Site
    sites = Site.objects.filter(customer=obj.customer) if obj and obj.customer_id else Site.objects.none()
    granted = set(obj.site_access.values_list("site_id", flat=True)) if obj else set()
    return render(request, "core/user_form.html", {"title": "Sửa tài khoản" if obj else "Tạo tài khoản", "form": form,
                                                   "obj": obj, "sites": sites, "granted": granted})


@role_required("ADMIN")
def params(request):
    from django.conf import settings
    if request.method == "POST":
        for key in settings.BUSINESS:
            v = request.POST.get(key)
            if v not in (None, ""):
                SystemParam.objects.update_or_create(key=key, defaults={"value": v})
        messages.success(request, "Đã lưu tham số hệ thống.")
        return redirect("/app/admin/params")
    rows = [{"key": k, "default": v, "value": param(k, str)} for k, v in settings.BUSINESS.items()]
    return render(request, "core/params.html", {"title": "Tham số hệ thống", "rows": rows})


@role_required("ADMIN")
def audit_log(request):
    qs = AuditLog.objects.select_related("user")
    if request.GET.get("q"):
        q = request.GET["q"]
        qs = qs.filter(Q(model__icontains=q) | Q(object_repr__icontains=q) | Q(user__username__icontains=q))
    if request.GET.get("action"):
        qs = qs.filter(action=request.GET["action"])
    page, qs_str = paginate(request, qs, 40)
    return render(request, "core/audit.html", {"title": "Nhật ký kiểm toán", "page_obj": page, "qs": qs_str})


# ------------------------------------------------------------------ Vercel Cron (thay Celery beat)
def cron_nightly(request):
    """Vercel Cron gọi mỗi đêm: tính SS/ROP + PR tự động, quét Inbound."""
    from django.conf import settings
    from django.http import HttpResponseForbidden, JsonResponse

    if settings.CRON_SECRET and request.headers.get("Authorization") != f"Bearer {settings.CRON_SECRET}":
        return HttpResponseForbidden("forbidden")
    from apps.inventory.services import run_replenishment
    from apps.purchasing.services import scan_inbound
    return JsonResponse({"replenishment": run_replenishment(), "late_trips": scan_inbound()})
