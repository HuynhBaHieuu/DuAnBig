"""Filter hiển thị: tiền VND, số lượng, ẩn giá theo vai trò, badge trạng thái."""
from decimal import Decimal

from django import template
from django.utils import timezone
from django.utils.html import format_html

register = template.Library()


def _fmt(n, decimals=0):
    try:
        n = Decimal(n)
    except Exception:
        return n
    s = f"{n:,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


@register.filter
def vnd(value):
    """1234567 -> 1.234.567 ₫"""
    if value is None or value == "":
        return "—"
    return f"{_fmt(value)} ₫"


@register.filter
def money_short(value):
    """Rút gọn: 1,2 tỷ / 350 tr."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return value
    if abs(v) >= 1e9:
        return f"{v / 1e9:.2f} tỷ".replace(".", ",")
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f} tr".replace(".", ",")
    return _fmt(v)


@register.filter
def qty(value):
    """Số lượng: bỏ phần thập phân thừa."""
    if value is None or value == "":
        return "—"
    try:
        d = Decimal(value)
    except Exception:
        return value
    if d == d.to_integral():
        return _fmt(d, 0)
    return _fmt(d, 3).rstrip("0").rstrip(",")


@register.filter
def pct(value, decimals=1):
    try:
        return f"{float(value):.{int(decimals)}f}%".replace(".", ",")
    except (TypeError, ValueError):
        return "—"


@register.simple_tag(takes_context=True)
def price(context, value):
    """Ẩn giá với WAREHOUSE / DRIVER."""
    user = context.get("user")
    if user is None or not getattr(user, "can_see_price", False):
        return "•••"
    return vnd(value)


@register.filter
def vn_dt(value):
    if not value:
        return "—"
    return timezone.localtime(value).strftime("%d/%m/%Y %H:%M")


@register.filter
def vn_d(value):
    if not value:
        return "—"
    return value.strftime("%d/%m/%Y")


@register.filter
def dur(minutes):
    """Số phút -> '3 ngày 4 giờ' / '2 giờ 15 phút' / '45 phút'."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return "—"
    d, rest = divmod(m, 1440)
    h, mi = divmod(rest, 60)
    if d:
        return f"{d} ngày {h} giờ" if h else f"{d} ngày"
    if h:
        return f"{h} giờ {mi} phút" if mi else f"{h} giờ"
    return f"{mi} phút"


@register.filter
def hm(value):
    if not value:
        return "—"
    return timezone.localtime(value).strftime("%H:%M")


TONE = {
    # tím = mới/nháp, cam = đang xử lý/chờ, xanh = hoàn tất, đỏ = hủy/lỗi
    "DRAFT": "gray", "PLANNED": "primary", "SUBMITTED": "primary", "PROPOSED": "primary", "SENT": "primary",
    "PENDING_APPROVAL": "warn", "PENDING": "warn", "COUNTING": "warn", "RECOUNT": "warn", "AT_FACTORY": "warn",
    "LOADED": "warn", "IN_TRANSIT": "warn", "PICKING": "warn", "IN_PROGRESS": "warn", "PARTIAL": "warn",
    "MISMATCH": "danger", "SHORT": "warn", "ARRIVED": "info", "UNLOADING": "info", "CONFIRMED": "info",
    "READY": "info", "CHECKED_IN": "info", "APPROVED": "info", "MATCHED": "info", "ACCEPTED": "info",
    "POSTED": "success", "RECEIVED": "success", "DELIVERED": "success", "COMPLETED": "success", "DONE": "success",
    "PAID": "success", "CLOSED": "success", "OK": "success", "CONVERTED": "success", "SETTLED": "success",
    "CANCELLED": "danger", "REJECTED": "danger", "FAILED": "danger", "BACKORDER": "danger", "EXPIRED": "danger",
    "OPEN": "warn", "SCHEDULED": "primary",
}


@register.simple_tag
def status_badge(obj, field="status"):
    code = getattr(obj, field, "")
    label = getattr(obj, f"get_{field}_display", lambda: code)()
    return format_html('<span class="badge badge-{}">{}</span>', TONE.get(code, "gray"), label)


@register.simple_tag
def code_badge(code, label=None):
    return format_html('<span class="badge badge-{}">{}</span>', TONE.get(code, "gray"), label or code)


@register.filter
def get_item(d, key):
    return d.get(key) if hasattr(d, "get") else None


@register.filter
def sub(a, b):
    return (a or 0) - (b or 0)


@register.filter
def mul(a, b):
    return (a or 0) * (b or 0)


@register.filter
def clamp_pct(value):
    try:
        return max(0, min(100, float(value)))
    except (TypeError, ValueError):
        return 0
