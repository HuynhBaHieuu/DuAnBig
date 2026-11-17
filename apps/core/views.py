from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.db.models import Q, Sum
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


# ------------------------------------------------------------------ Dashboard (MH-03) – test Gà STT 79–88
# Khối hiển thị theo vai trò (STT 87). Mọi khối lấy dữ liệu từ apps/core/dashboard.py, KPI từ apps/reports/kpi.py
# (dùng chung với trang Báo cáo). Một bộ chọn kỳ duy nhất cho cả trang (STT 88).
DASH_BLOCKS = {
    "DIRECTOR": ["stock", "aging", "demand", "arrivals", "trends", "alerts"],
    "ADMIN": ["stock", "aging", "demand", "arrivals", "trends", "alerts"],
    "PURCHASER": ["stock", "arrivals", "demand", "alerts"],
    "WAREHOUSE": ["arrivals", "trends", "alerts"],
    "DISPATCHER": ["trends", "demand", "alerts"],
    "ACCOUNTANT": ["aging", "demand", "alerts"],
}
DEMAND_UNIT = {"ACCOUNTANT": "money"}  # mặc định xem theo tấn; kế toán mặc định theo tiền


@login_required
def dashboard(request):
    from apps.accounting.models import Invoice
    from apps.purchasing.services import maybe_scan_inbound
    from apps.reports.kpi import PERIODS, kpi_cards, period_label, period_qs, resolve_period

    from . import dashboard as dash
    from .permissions import can_open
    from .stale import maybe_scan_stale

    maybe_scan_inbound()
    maybe_scan_stale()
    u = request.user
    role = u.role
    today = timezone.localdate()
    d_from, d_to, period = resolve_period(request)
    blocks = DASH_BLOCKS.get(role, ["alerts"])
    ctx = {"title": "Dashboard", "today": today, "d_from": d_from, "d_to": d_to, "period": period, "periods": PERIODS,
           "period_text": period_label(d_from, d_to), "pq": period_qs(d_from, d_to), "blocks": blocks,
           "queue": dash.queue(u, today), "kpis": kpi_cards(d_from, d_to, u),
           # bố cục theo mẫu (ảnh doanh nghiệp, khối lượng đơn, đội xe, mới nhất, doanh thu)
           "hero_url": dash.hero_image_url(), "can_hero": role in ("DIRECTOR", "ADMIN"),
           "volume": dash.volume(d_from, d_to), "fleet": dash.fleet(today), "latest": dash.latest(u),
           "sales": dash.sales_overview(u, d_from, d_to)}
    if "stock" in blocks:
        ctx["stock"] = dash.stock_health()
        ctx["stock_url"] = "/app/inventory/replenishment?level=low" if can_open(u, "/app/inventory/replenishment") else None
    if "aging" in blocks:
        ctx["aging"] = dash.ar_aging()
    if "demand" in blocks:
        ctx["demand"] = dash.demand_12m(d_to, today)
        ctx["demand_unit"] = request.GET.get("unit") or DEMAND_UNIT.get(role, "ton")
        rev = Invoice.objects.filter(invoice_type="SALE", invoice_date__range=(d_from, d_to)) \
            .exclude(status__in=["DRAFT", "CANCELLED"]).aggregate(s=Sum("subtotal"))["s"] or D0
        ctx["revenue"] = rev
    if "arrivals" in blocks:
        ctx["arrivals"] = dash.arrivals(today)
    if "trends" in blocks:
        ctx["trends"] = dash.weekly_trends(d_to)
    if "alerts" in blocks:
        ctx["alert_groups"] = dash.alert_groups(u)
    ctx["charts"] = {k: ctx[k] for k in ("demand", "trends", "volume") if k in ctx}
    ctx["links"] = {url: can_open(u, url) for url in ("/app/reports/otif", "/app/reports/unloading",
                                                         "/app/accounting/debts", "/app/warehouse/slots",
                                                         "/app/accounting/invoices", "/app/sales/orders",
                                                         "/app/warehouse/receipts", "/app/inbound/board",
                                                         "/app/fulfillment/planning")}
    return render(request, "core/dashboard.html", ctx)


@role_required("DIRECTOR")
@require_POST
def dashboard_hero(request):
    """Giám đốc / Admin đổi ảnh doanh nghiệp đầu Dashboard (thay chỗ bản đồ GPS của mẫu). Ảnh lưu như ảnh nghiệp vụ
    (bucket private, xem qua /files/ khi đã đăng nhập); op=reset quay lại hình minh họa mặc định."""
    from django.core.files.storage import default_storage

    from apps.sales.services import check_image

    from .services import BusinessError
    row = SystemParam.objects.filter(key="DASHBOARD_HERO_IMAGE").first()
    if request.POST.get("op") == "reset":
        if row:
            row.delete()
        messages.success(request, "Đã quay lại hình minh họa mặc định.")
        return redirect("/app/dashboard")
    f = request.FILES.get("image")
    try:
        if not f:
            raise BusinessError("Chọn ảnh doanh nghiệp (JPG / PNG).")
        check_image(f, "Ảnh doanh nghiệp")
    except BusinessError as e:
        messages.error(request, str(e))
        return redirect("/app/dashboard")
    name = default_storage.save(f"branding/{f.name}", f)
    SystemParam.objects.update_or_create(key="DASHBOARD_HERO_IMAGE",
                                         defaults={"value": name, "description": "Ảnh doanh nghiệp đầu Dashboard"})
    audit("UPDATE", None, {"Ảnh Dashboard": name}, model="SystemParam")
    messages.success(request, "Đã cập nhật ảnh doanh nghiệp trên Dashboard.")
    return redirect("/app/dashboard")


# ------------------------------------------------------------------ Cảnh báo / tìm kiếm
@login_required
def alerts(request):
    from .dashboard import ALERT_LABELS, alert_groups
    from .stale import maybe_scan_stale
    maybe_scan_stale()
    qs = alerts_for(request.user) if request.GET.get("all") != "1" else Alert.objects.all()
    typ = request.GET.get("type")
    if typ:
        qs = qs.filter(alert_type=typ)
    page, qs_str = paginate(request, qs, 30)
    for a in page:
        a.type_label = ALERT_LABELS.get(a.alert_type, a.alert_type)
    return render(request, "core/alerts.html", {"title": "Cảnh báo", "page_obj": page, "qs": qs_str,
                                                "groups": alert_groups(request.user), "type": typ,
                                                "type_label": ALERT_LABELS.get(typ, typ) if typ else None})


@login_required
@require_POST
def alert_resolve(request, pk):
    Alert.objects.filter(pk=pk).update(is_resolved=True)
    return redirect(request.META.get("HTTP_REFERER") or "/app/alerts")


@login_required
def search(request):
    from apps.masterdata.models import Customer, Product, Supplier
    from apps.inventory.models import StockAdjustment
    from apps.purchasing.models import Claim, GoodsReceipt, InboundTrip, PurchaseOrder
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
                                  (GoodsReceipt, "/app/warehouse/receipts/{}", "Phiếu nhập"),
                                  (Claim, "/app/warehouse/claims/{}", "Khiếu nại"),
                                  (StockAdjustment, "/app/inventory/adjustments/{}", "Điều chỉnh tồn")]:
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
    from .permissions import can_open
    results = [r for r in results if can_open(request.user, r["url"])]  # bỏ kết quả vai trò không có quyền mở
    return render(request, "core/search.html", {"title": f"Tìm kiếm “{q}”", "results": results, "q": q})


# ------------------------------------------------------------------ Quản trị (ADMIN)
class UserForm(forms.ModelForm):
    password = forms.CharField(required=False, widget=forms.PasswordInput, label="Mật khẩu (để trống nếu không đổi)")

    class Meta:
        model = User
        fields = ["username", "full_name", "email", "phone", "role", "driver", "customer", "supplier", "is_active"]
        labels = {"username": "Tên đăng nhập", "full_name": "Họ tên", "phone": "Điện thoại", "role": "Vai trò",
                  "driver": "Gắn tài xế (DRIVER)", "customer": "Gắn khách hàng (CUSTOMER)",
                  "supplier": "Gắn nhà cung cấp (SUPPLIER – tài khoản nhà máy)", "is_active": "Đang hoạt động"}

    def clean(self):
        c = super().clean()
        if c.get("role") == Role.DRIVER and not c.get("driver"):
            self.add_error("driver", "Tài khoản DRIVER phải gắn với 1 tài xế.")
        if c.get("role") == Role.CUSTOMER and not c.get("customer"):
            self.add_error("customer", "Tài khoản CUSTOMER phải gắn với 1 khách hàng.")
        if c.get("role") == Role.SUPPLIER and not c.get("supplier"):
            self.add_error("supplier", "Tài khoản nhà máy phải gắn với 1 nhà cung cấp trong Danh mục.")
        if c.get("role") != Role.SUPPLIER:
            c["supplier"] = None  # chỉ tài khoản nhà máy được gắn NCC (dữ liệu PO lọc theo trường này)
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
    from .stale import scan_stale
    return JsonResponse({"replenishment": run_replenishment(), "late_trips": scan_inbound(), "stale": scan_stale()})
