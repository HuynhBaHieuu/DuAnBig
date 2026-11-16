"""Dữ liệu nhu cầu MÔ PHỎNG 2 năm để demo / kiểm chứng mô hình dự báo (đặc tả dự báo mục 9).

- Mã riêng SIM-PCB40 (không dùng mã thật). File CSV: week_start,qty (104 dòng, thứ Hai, số tấn) –
  static/demo/SIM-PCB40.csv, tạo bằng generate_rows() với seed cố định nên luôn giống nhau.
- Nhu cầu tuần = mức nền × (1 + tăng trưởng)^(tuần/52) × hệ số tháng × (1 + nhiễu ±15%), thỉnh thoảng cộng 1 đơn
  công trình lớn gấp 3–5 lần, tuần Tết gần bằng 0.
- Ghi chú khi báo cáo: "Dữ liệu mô phỏng 2 năm với các tham số trên, dùng để kiểm chứng mô hình do hệ thống vận
  hành chưa đủ 1 năm". Trước khi chạy thật: xóa mã SIM-PCB40 và dữ liệu của nó (nút trên màn SS/ROP).
"""
import csv
import io
import random
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction

from apps.core.services import BusinessError

from .forecast import monday, week_month

SIM_CODE = "SIM-PCB40"
# Hệ số tháng gợi ý (Quảng Trị: mùa mưa tháng 9–12)
MONTH_FACTORS = [0.8, 0.7, 1.2, 1.25, 1.2, 1.15, 1.1, 1.1, 0.9, 0.8, 0.85, 1.15]
BASE_TON = 120.0          # mức nền (tấn / tuần)
GROWTH = 0.08             # tăng trưởng 8% / năm
NOISE = 0.15              # nhiễu ±15%
TET_WEEKS = {date(2025, 1, 27), date(2026, 2, 16)}  # tuần Tết (thứ Hai) – nhu cầu gần bằng 0
FIRST_WEEK = date(2024, 11, 18)
WEEKS = 104
SEED = 2026


def generate_rows(first=FIRST_WEEK, weeks=WEEKS, seed=SEED):
    """[(week_start, qty)] – 104 tuần mô phỏng, tái lập được (seed cố định)."""
    rnd = random.Random(seed)
    rows = []
    for t in range(weeks):
        w = first + timedelta(days=7 * t)
        q = BASE_TON * (1 + GROWTH) ** (t / 52) * MONTH_FACTORS[week_month(w) - 1] * (1 + rnd.uniform(-NOISE, NOISE))
        if rnd.random() < 0.04:                       # thỉnh thoảng 1 đơn công trình lớn gấp 3–5 lần
            q += BASE_TON * rnd.uniform(3, 5)
        if w in TET_WEEKS:
            q = rnd.uniform(0, 3)                     # tuần Tết gần bằng 0
        rows.append((w, round(q, 1)))
    return rows


def rows_to_csv(rows):
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["week_start", "qty"])
    for w, q in rows:
        wr.writerow([w.isoformat(), f"{q:g}"])
    return buf.getvalue()


def parse_csv(raw):
    """CSV `week_start,qty` -> [(thứ Hai, Decimal)]. Chấp nhận YYYY-MM-DD hoặc DD/MM/YYYY, dấu phẩy thập phân."""
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    out, seen = [], set()
    for i, row in enumerate(csv.reader(io.StringIO(text)), 1):
        if not row or not "".join(row).strip():
            continue
        if i == 1 and not row[0].strip()[:1].isdigit():
            continue                                  # dòng tiêu đề
        if len(row) < 2:
            raise BusinessError(f"Dòng {i}: cần 2 cột week_start,qty.")
        d_raw, q_raw = row[0].strip(), row[1].strip().replace(",", ".")
        d = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                d = datetime.strptime(d_raw, fmt).date()
                break
            except ValueError:
                pass
        if d is None:
            raise BusinessError(f"Dòng {i}: ngày '{d_raw}' không hợp lệ (dùng YYYY-MM-DD).")
        try:
            q = Decimal(q_raw)
        except InvalidOperation:
            raise BusinessError(f"Dòng {i}: số lượng '{row[1]}' không hợp lệ.")
        if q < 0:
            raise BusinessError(f"Dòng {i}: số lượng không được âm.")
        w = monday(d)
        if w in seen:
            raise BusinessError(f"Dòng {i}: trùng tuần {w:%d/%m/%Y}.")
        seen.add(w)
        out.append((w, q))
    if not out:
        raise BusinessError("File CSV không có dòng dữ liệu.")
    return out


@transaction.atomic
def load_simulated(product, rows):
    """Thay toàn bộ dữ liệu mô phỏng của mã bằng rows."""
    from .models import SimulatedDemand
    SimulatedDemand.objects.filter(product=product).delete()
    SimulatedDemand.objects.bulk_create([SimulatedDemand(product=product, week_start=w, qty=q) for w, q in rows])
    return len(rows)


@transaction.atomic
def create_sim_product():
    """Tạo (hoặc lấy lại) mã demo SIM-PCB40 + tham số SS/ROP, nạp file mô phỏng 2 năm."""
    from apps.core.services import param
    from apps.masterdata.models import Product, ProductCategory, Uom

    from .models import ReplenishmentParam

    p = Product.objects.filter(code=SIM_CODE).first()
    if p is None:
        uom = Uom.objects.filter(code="TAN").first() or Uom.objects.create(code="TAN", name="Tấn")
        cat = ProductCategory.objects.filter(code="DEMO").first() or ProductCategory.objects.create(
            code="DEMO", name="Dữ liệu mô phỏng (demo dự báo)")
        p = Product.objects.create(code=SIM_CODE, name="Xi măng PCB40 – MÔ PHỎNG (demo dự báo)", category=cat,
                                   base_uom=uom, unit_weight_kg=1000, standard_cost=1, avg_cost=1, track_rop=True)
    ReplenishmentParam.objects.get_or_create(product=p, defaults={"z": param("SERVICE_Z"), "lead_time_days": 3,
                                                                  "round_by_truck": True, "truck_ton": 30})
    load_simulated(p, generate_rows())
    return p
