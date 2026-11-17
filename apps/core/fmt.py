"""Định dạng số kiểu Việt Nam dùng chung cho template, cảnh báo, thông báo (Gà STT 92–95).

- Dấu "." ngăn nghìn, dấu "," thập phân: 1.234.567 · 12,6% · 1,25 ngày.
- Số lượng theo đơn vị: hàng đếm theo cái (bao, cây, mét, viên…) -> số nguyên; tấn, m³ -> tối đa 1 chữ số thập phân;
  bỏ số 0 thừa ("30 tấn", không phải "30.00 tấn").
Giá trị thô cho ô nhập / JS / CSS vẫn dùng dấu "." (floatformat, qty_input) – các hàm ở đây chỉ để HIỂN THỊ.
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# Đơn vị đếm theo cái: không có nửa bao / nửa cây
COUNT_UNITS = {"BAO", "CAY", "MET", "M", "VIEN", "CAI", "CHIEC", "TAM", "CUON", "BO", "THANH", "HOP", "THUNG", "KIEN"}
UNIT_LABEL = {"TAN": "tấn", "BAO": "bao", "CAY": "cây", "MET": "mét", "M": "mét", "M3": "m³", "VIEN": "viên",
              "CAI": "cái", "KG": "kg"}


def _dec(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value)) if not isinstance(value, Decimal) else value
    except (InvalidOperation, ValueError, TypeError):
        return None


def unit_code(unit):
    """'CAY' | Uom | Product | None -> mã đơn vị in hoa."""
    if unit is None or unit == "":
        return ""
    if hasattr(unit, "base_uom"):
        unit = unit.base_uom
    return str(getattr(unit, "code", unit) or "").upper()


def is_count_unit(unit):
    return unit_code(unit) in COUNT_UNITS


def num(value, decimals=0, trim=False):
    """1234567.891 -> '1.234.568' ; num(12.5, 1) -> '12,5' ; trim=True bỏ ',0' thừa. None -> '—'."""
    d = _dec(value)
    if d is None:
        return "—"
    decimals = max(int(decimals), 0)
    d = d.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    if d == 0:
        d = abs(d)  # không in "-0"
    s = f"{d:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    if trim and decimals:
        s = s.rstrip("0").rstrip(",")
    return s


def qty_decimals(unit):
    """Số chữ số thập phân hiển thị theo đơn vị: đếm theo cái -> 0; tấn / m³ / khác -> 1."""
    return 0 if is_count_unit(unit) else 1


def qty(value, unit=None):
    """Số lượng theo đơn vị (bỏ số 0 thừa): qty(431.596, 'CAY') -> '432' ; qty(30, 'TAN') -> '30' ;
    qty(6.9, 'TAN') -> '6,9'. Không biết đơn vị: tối đa 1 chữ số thập phân."""
    return num(value, qty_decimals(unit), trim=True)


def qty_unit(value, unit):
    """'15 bao', '6,9 tấn' – dùng trong nội dung cảnh báo / thông báo."""
    label = UNIT_LABEL.get(unit_code(unit), unit_code(unit).lower())
    return f"{qty(value, unit)} {label}".strip()


def pct(value, decimals=1):
    d = _dec(value)
    return "—" if d is None else f"{num(d, decimals)}%"


def money(value):
    d = _dec(value)
    return "—" if d is None else f"{num(d, 0)} ₫"


def round_count(value, unit):
    """Làm tròn LÊN số nguyên cho hàng đếm theo cái (PR, gợi ý đặt hàng); đơn vị khác giữ nguyên."""
    d = _dec(value) or Decimal(0)
    if is_count_unit(unit) and d != d.to_integral_value():
        return d.to_integral_value(rounding="ROUND_CEILING")
    return d
