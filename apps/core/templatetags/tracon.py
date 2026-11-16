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


_DIGITS = ["không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín"]


def _read3(n, full):
    """Đọc 1 nhóm 3 chữ số (0..999). full=True: đọc cả "không trăm" (nhóm không đứng đầu)."""
    h, t, u = n // 100, n // 10 % 10, n % 10
    out = []
    if h or full:
        out += [_DIGITS[h], "trăm"]
    if t == 0:
        if u and (h or full):
            out.append("lẻ")
    elif t == 1:
        out.append("mười")
    else:
        out += [_DIGITS[t], "mươi"]
    if u:
        if u == 1 and t > 1:
            out.append("mốt")
        elif u == 5 and t > 0:
            out.append("lăm")
        elif u == 4 and t > 1:
            out.append("tư")
        else:
            out.append(_DIGITS[u])
    return out


@register.simple_tag
def sysparam(key, default=""):
    """Giá trị tham số hệ thống (bảng system_param, mặc định settings.BUSINESS)."""
    from apps.core.services import param
    v = param(key, str)
    return default if v in (None, "") else v


@register.filter
def vnd_words(value):
    """Số tiền bằng chữ cho hóa đơn: 1250000 -> 'Một triệu hai trăm năm mươi nghìn đồng'."""
    try:
        n = int(Decimal(value or 0).quantize(Decimal(1)))
    except Exception:
        return ""
    neg, n = n < 0, abs(n)
    if n == 0:
        return "Không đồng"
    units = ["", "nghìn", "triệu", "tỷ", "nghìn tỷ", "triệu tỷ"]
    groups = []
    while n:
        groups.append(n % 1000)
        n //= 1000
    words = []
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if g == 0:
            continue
        words += _read3(g, full=i != len(groups) - 1)
        if units[i]:
            words.append(units[i])
    s = " ".join(words + ["đồng"])
    s = ("Âm " + s) if neg else s
    return s[0].upper() + s[1:]


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
def qty_input(value):
    """Giá trị cho ô nhập số (input type=number): 15.000 -> "15", 12.500 -> "12.5" – không dấu nghìn,
    chỉ giữ phần thập phân khi có (tránh đọc nhầm "15.000" thành 15 nghìn)."""
    if value is None or value == "":
        return ""
    try:
        d = Decimal(value)
    except Exception:
        return value
    if d == d.to_integral():
        return str(d.quantize(Decimal(1)))
    return format(d.normalize(), "f")


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
    """Số phút -> '3 ngày 23 giờ 34 phút' (≥ 24 giờ) / '2 giờ 15 phút' / '45 phút'."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return "—"
    sign, m = ("-" if m < 0 else ""), abs(m)
    d, rest = divmod(m, 1440)
    h, mi = divmod(rest, 60)
    if d:  # > 24 giờ: X ngày Y giờ Z phút
        parts = [f"{d} ngày"] + ([f"{h} giờ"] if h or mi else []) + ([f"{mi} phút"] if mi else [])
        return sign + " ".join(parts)
    if h:  # < 24 giờ: Y giờ Z phút
        return sign + (f"{h} giờ {mi} phút" if mi else f"{h} giờ")
    return sign + f"{mi} phút"


@register.filter
def can_open(user, url):
    """{% if user|can_open:"/app/inbound/board" %} – ẩn link tới trang vai trò này không có quyền."""
    from apps.core.permissions import can_open as _can
    return _can(user, url)


@register.simple_tag
def milestone_badge(m):
    """Nhãn mốc: lệch = thực tế − dự kiến. |lệch| ≤ 30' "Đúng giờ"; < −30' "Sớm X"; > +30' "Trễ X"."""
    from django.utils.html import format_html
    t = m.timing() if hasattr(m, "timing") else None
    if not t:
        return ""
    kind, mins = t
    if kind == "EARLY":
        return format_html('<span class="badge badge-info" title="Thực tế sớm hơn dự kiến">Sớm {}</span>', dur(mins))
    if kind == "LATE":
        tone = "danger" if mins > 60 else "warn"
        return format_html('<span class="badge badge-{}" title="Thực tế trễ hơn dự kiến">Trễ {}</span>', tone, dur(mins))
    return format_html('<span class="badge badge-success" title="Lệch {} so với dự kiến">Đúng giờ</span>',
                       dur(m.delay_min))


@register.filter
def hm(value):
    if not value:
        return "—"
    return timezone.localtime(value).strftime("%H:%M")


TONE = {
    # tím = mới/nháp, cam = đang xử lý/chờ, xanh = hoàn tất, đỏ = hủy/lỗi
    "TAO_PR": "warn", "CHUA_CHAM": "success", "DA_DU_DE_XUAT": "info", "DA_TU_CHOI_GAN_DAY": "gray", "KHONG_CAN": "gray", "LOI": "danger",
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


@register.inclusion_tag("partials/history.html", takes_context=True)
def doc_history(context, obj, title="Lịch sử", card=True):
    """Tab / thẻ "Lịch sử" của chứng từ: ai, làm gì, lúc nào, giá trị cũ → mới.
    Người không được xem giá (Thủ kho, Tài xế) không thấy các thay đổi về giá / tiền."""
    from apps.core.history import doc_history as _h
    from apps.core.signals import PRICE_WORDS
    items = _h(obj) if obj is not None else []
    user = context.get("user")
    if user is not None and not getattr(user, "can_see_price", True):
        for it in items:
            it["changes"] = [c for c in it["changes"] if not any(w in str(c[0]).lower() for w in PRICE_WORDS)]
        items = [it for it in items if it["changes"] or it["action"] != "Cập nhật"]
    return {"items": items, "title": title, "card": card}
