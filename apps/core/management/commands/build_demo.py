"""Dựng bộ dữ liệu demo TRACON 80 – Kho tổng Đà Nẵng – vào một file SQLite riêng.

Khác seed_demo: lịch sử ~26 tuần được sinh bằng CHÍNH các hàm nghiệp vụ, chạy theo đồng hồ giả lập từng ngày
(đơn bán -> giữ hàng -> PR tự động -> PO -> duyệt -> nhà máy xác nhận -> chuyến 2 mốc -> xe tới cổng -> phiếu nhập ->
khiếu nại; giao hàng + POD; hóa đơn, thu chi; kiểm kê; tác vụ đêm SS/ROP). Kết thúc ở trạng thái sẵn sàng cho
kịch bản demo 18 bước (mã XM-PCB40 tồn 100 t, khóa SS 20 / ROP 60 / Q 140, không có PR / PO / đơn mở).

Chạy (PowerShell) – chỉ chạy trên SQLite, KHÔNG đụng Supabase:
    $env:USE_SQLITE="1"; $env:SQLITE_NAME="demo.sqlite3"; $env:SUPABASE_URL=""
    python manage.py migrate
    python manage.py build_demo
    Remove-Item Env:USE_SQLITE, Env:SQLITE_NAME, Env:SUPABASE_URL
Sau đó nạp lên database thật bằng: python manage.py load_demo demo.sqlite3 --yes
"""
import base64
import heapq
import io
import itertools
import math
import random
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.core.files.storage import Storage, default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone as djtz

PASSWORD = "Tracon@123"
DEMO_CODE = "XM-PCB40"
D0 = Decimal(0)

# ------------------------------------------------------------------ Ảnh minh họa dùng chung cho lịch sử
# Lịch sử có hàng trăm ảnh mốc / POD: dùng vài ảnh minh họa cố định thay vì upload từng ảnh. load_demo đưa các
# file này lên kho ảnh (Supabase Storage hoặc thư mục media) đúng tên dưới đây.
DEMO_FILES = {
    "milestones": ["demo/phieu-can-1.jpg", "demo/phieu-can-2.jpg", "demo/phieu-can-3.jpg"],
    "incidents": ["demo/su-co-1.jpg"],
    "pod": ["demo/giao-hang-1.jpg", "demo/giao-hang-2.jpg", "demo/giao-hang-3.jpg"],
    "signatures": ["demo/chu-ky-1.png", "demo/chu-ky-2.png"],
}


def _truck_photo(title, color, seed):
    from PIL import Image, ImageDraw, ImageFilter
    w, h = 640, 480
    rnd = random.Random(seed)
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h // 2):
        k = y / (h / 2)
        d.line([(0, y), (w, y)], fill=(int(120 + 80 * k), int(170 + 50 * k), int(225 + 20 * k)))
    d.rectangle([0, h // 2, w, h], fill=(118, 112, 102))
    for x in range(0, w, 6):
        for y in range(h // 2, h, 6):
            v = rnd.randint(-14, 14)
            d.point((x, y), fill=(118 + v, 112 + v, 102 + v))
    x0 = rnd.randint(70, 160)
    d.rounded_rectangle([x0, 170, x0 + 300, 330], 8, fill=(205, 205, 200), outline=(80, 80, 80), width=2)
    d.rounded_rectangle([x0 + 300, 210, x0 + 400, 330], 10, fill=color, outline=(60, 60, 60), width=2)
    d.rectangle([x0 + 320, 222, x0 + 385, 262], fill=(160, 200, 230))
    for cx in (x0 + 60, x0 + 230, x0 + 350):
        d.ellipse([cx - 32, 300, cx + 32, 364], fill=(30, 30, 30))
        d.ellipse([cx - 14, 318, cx + 14, 346], fill=(150, 150, 150))
    img = img.filter(ImageFilter.GaussianBlur(0.8))
    d = ImageDraw.Draw(img)
    d.rectangle([0, h - 46, w, h], fill=(0, 0, 0))
    d.text((14, h - 36), title, fill=(255, 220, 120))
    d.text((14, h - 20), "TRACON 80 - anh minh hoa du lieu demo", fill="white")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def _signature(seed):
    from PIL import Image, ImageDraw
    rnd = random.Random(seed)
    img = Image.new("RGBA", (400, 160), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    pts = [(20 + i * 45, rnd.randint(40, 120)) for i in range(9)]
    d.line(pts, fill=(6, 6, 6, 255), width=4, joint="curve")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def demo_file_bytes():
    """{tên file: bytes} của các ảnh minh họa (load_demo gọi để đưa lên kho ảnh)."""
    out = {}
    for i, n in enumerate(DEMO_FILES["milestones"]):
        out[n] = _truck_photo(f"Roi nha may - phieu can / niem phong #{i + 1}", (230, 95, 43), n)
    out[DEMO_FILES["incidents"][0]] = _truck_photo("Su co tren duong", (200, 60, 60), "inc")
    for i, n in enumerate(DEMO_FILES["pod"]):
        out[n] = _truck_photo(f"Giao hang tai cong trinh #{i + 1}", (104, 178, 102), n)
    for i, n in enumerate(DEMO_FILES["signatures"]):
        out[n] = _signature(n)
    return out


class DemoStorage(Storage):
    """Kho ảnh giả khi dựng lịch sử: không lưu gì, trả về tên một ảnh minh họa cố định theo loại ảnh."""

    def __init__(self):
        self.n = Counter()

    def get_available_name(self, name, max_length=None):
        return name

    def _save(self, name, content):
        kind = name.replace("\\", "/").split("/")[0]
        files = DEMO_FILES.get(kind) or DEMO_FILES["milestones"]
        self.n[kind] += 1
        return files[self.n[kind] % len(files)]

    def exists(self, name):
        return False

    def delete(self, name):
        pass

    def url(self, name):
        return f"/files/{name}"


# ------------------------------------------------------------------ Danh mục Đà Nẵng
ROUTES = [  # mã, tên, nơi đi, km, giờ chuẩn
    ("R-HM", "Hoàng Mai (Nghệ An) → Kho Đà Nẵng", "Nhà máy Xi măng Vicem Hoàng Mai, Nghệ An", 540, 11),
    ("R-NS", "Nghi Sơn (Thanh Hóa) → Kho Đà Nẵng", "Nhà máy Xi măng Nghi Sơn, Thanh Hóa", 600, 12.5),
    ("R-HS", "Tổng kho Hoa Sen Home Đà Nẵng → Kho Đà Nẵng", "Tổng kho Hoa Sen Home, Đà Nẵng", 18, 1),
]
RETURN_HOURS = {"R-HM": 11, "R-NS": 12.5, "R-HS": 0.5}  # xe chạy rỗng quay lại nhà máy
SUPPLIERS = [  # mã, tên, địa chỉ, tuyến, hạn thanh toán, tài khoản nhà máy
    ("NCC-HM", "Công ty Cổ phần Xi măng Vicem Hoàng Mai", "Hoàng Mai, Nghệ An", "R-HM", 30, "nhamay"),
    ("NCC-NS", "Công ty Xi măng Nghi Sơn", "Nghi Sơn, Thanh Hóa", "R-NS", 30, "nhamay_ns"),
    ("NCC-HS", "Hệ thống Siêu thị & Tổng kho Hoa Sen Home (Tập đoàn Hoa Sen)", "Đà Nẵng", "R-HS", 15, "nhamay_hs"),
]
# mã, tên, nhóm, đvt, kg/đv, giá mua, giá bán, giá sàn, NCC, nhu cầu TB ngày thường, vị trí, (cỡ đơn, bước, min, max)
PRODUCTS = [
    (DEMO_CODE, "Xi măng rời PCB40 Vicem Hoàng Mai", "XM", "TAN", 1000, 1_600_000, 2_050_000, 1_950_000,
     "NCC-HM", 16, "B-01", (25, 1, 8, 45)),
    ("XM-HM40B", "Xi măng PCB40 Vicem Hoàng Mai (bao 50 kg)", "XM", "BAO", 50, 80_000, 108_000, 102_000,
     "NCC-HM", 520, "A-01", (250, 10, 50, 400)),
    ("XM-NS40B", "Xi măng PCB40 Nghi Sơn (bao 50 kg)", "XM", "BAO", 50, 82_000, 110_000, 104_000,
     "NCC-NS", 380, "A-02", (220, 10, 50, 400)),
    ("XM-NS50B", "Xi măng PC50 Nghi Sơn (bao 50 kg)", "XM", "BAO", 50, 90_000, 120_000, 113_000,
     "NCC-NS", 140, "A-03", (120, 10, 30, 300)),
    ("THEP-D10", "Thép thanh vằn D10 (cây 11,7 m)", "THEP", "CAY", 7.22, 118_000, 135_000, 127_000,
     "NCC-HS", 260, "C-01", (150, 10, 30, 600)),
    ("THEP-D16", "Thép thanh vằn D16 (cây 11,7 m)", "THEP", "CAY", 18.47, 300_000, 342_000, 322_000,
     "NCC-HS", 90, "C-02", (60, 5, 10, 300)),
    ("TON-HS045", "Tôn lạnh màu Hoa Sen 0,45 mm", "TON", "MET", 4.0, 98_000, 115_000, 107_000,
     "NCC-HS", 180, "D-01", (120, 10, 20, 500)),
    ("ONG-HS90", "Ống nhựa PVC Hoa Sen D90 (cây 4 m)", "ONG", "CAY", 6, 150_000, 178_000, 165_000,
     "NCC-HS", 30, "D-02", (20, 2, 4, 80)),
]
# khách hàng: mã, tên, loại, địa chỉ, hạn mức, hạn TT, số ngày trả trễ so với hạn (âm = trả sớm)
CUSTOMERS = [
    ("KH-001", "Công ty CP Xây dựng Sông Hàn", "B2B", "Phường Hải Châu, TP. Đà Nẵng", 6_000_000_000, 30, -3),
    ("KH-002", "Công ty TNHH Xây dựng Ngũ Hành Sơn", "B2B", "Phường Ngũ Hành Sơn, TP. Đà Nẵng", 2_500_000_000, 30, 4),
    ("KH-003", "Công ty TNHH Nhà Đẹp Hội An", "B2B", "Hội An, TP. Đà Nẵng", 600_000_000, 15, 38),
    ("KH-004", "Công ty CP Đầu tư Xây dựng Hòa Xuân", "B2B", "Phường Hòa Xuân, TP. Đà Nẵng", 4_000_000_000, 30, 0),
    ("CT-NB", "Khối công trình nội bộ TRACON 80", "INTERNAL", "TP. Đà Nẵng", 0, 0, 0),
]
# công trình: mã, tên, khách, địa chỉ, ưu tiên, người nhận, SĐT, phút chạy từ kho
SITES = [
    ("CT-SH01", "Khu căn hộ Sông Hàn Riverside", "KH-001", "Phường An Hải, TP. Đà Nẵng", 2, "Anh Tuấn", "0905 100 200", 35),
    ("CT-SH02", "Nhà xưởng KCN Điện Nam – Điện Ngọc", "KH-001", "KCN Điện Nam – Điện Ngọc, TP. Đà Nẵng", 3,
     "Chị Lan", "0905 300 400", 55),
    ("CT-NHS01", "Khách sạn ven biển Mỹ Khê", "KH-002", "Đường Võ Nguyên Giáp, TP. Đà Nẵng", 3, "Anh Phong",
     "0914 220 330", 40),
    ("CT-NHS02", "Trường học Khu đô thị Hòa Quý", "KH-002", "Khu đô thị Hòa Quý, TP. Đà Nẵng", 3, "Anh Nhân",
     "0914 440 550", 45),
    ("CT-HA01", "Resort ven biển An Bàng", "KH-003", "Biển An Bàng, Hội An, TP. Đà Nẵng", 4, "Anh Thịnh",
     "0935 660 770", 75),
    ("CT-HX01", "Khu dân cư Hòa Xuân mở rộng", "KH-004", "Phường Hòa Xuân, TP. Đà Nẵng", 3, "Anh Hưng",
     "0935 880 990", 40),
    ("CT-NB01", "Dự án KDC Liên Chiểu (nội bộ)", "CT-NB", "Phường Liên Chiểu, TP. Đà Nẵng", 1, "Chỉ huy trưởng Hòa",
     "0977 555 666", 15),
    ("CT-NB02", "Cầu vượt Tam Kỳ (nội bộ)", "CT-NB", "Tam Kỳ, TP. Đà Nẵng", 2, "Chỉ huy trưởng Sơn", "0977 777 888", 120),
]
# vật tư -> [(công trình, trọng số)]
BUYERS = {
    DEMO_CODE: [("CT-SH01", 45), ("CT-HX01", 35), ("CT-NB01", 20)],
    "XM-HM40B": [("CT-SH01", 20), ("CT-SH02", 15), ("CT-NHS01", 15), ("CT-NHS02", 10), ("CT-HA01", 8),
                 ("CT-HX01", 12), ("CT-NB01", 10), ("CT-NB02", 10)],
    "XM-NS40B": [("CT-SH02", 20), ("CT-NHS01", 15), ("CT-NHS02", 15), ("CT-HA01", 10), ("CT-HX01", 15),
                 ("CT-NB01", 10), ("CT-NB02", 15)],
    "XM-NS50B": [("CT-SH01", 35), ("CT-NHS01", 25), ("CT-NB02", 25), ("CT-HX01", 15)],
    "THEP-D10": [("CT-SH01", 25), ("CT-SH02", 15), ("CT-NHS01", 15), ("CT-HX01", 15), ("CT-NB01", 15), ("CT-NB02", 15)],
    "THEP-D16": [("CT-SH01", 30), ("CT-NHS01", 20), ("CT-HX01", 20), ("CT-NB02", 30)],
    "TON-HS045": [("CT-SH02", 40), ("CT-HA01", 20), ("CT-NHS02", 20), ("CT-NB01", 20)],
    "ONG-HS90": [("CT-HA01", 30), ("CT-NHS01", 25), ("CT-NHS02", 25), ("CT-SH01", 20)],
}
SEASON = {1: 1.0, 2: 0.6, 3: 1.0, 4: 1.05, 5: 1.1, 6: 1.15, 7: 1.1, 8: 1.05, 9: 0.95, 10: 0.75, 11: 0.7, 12: 0.75}
DOW = [1.05, 1.05, 1.0, 1.0, 1.05, 0.8, 0.25]
CARRIERS = [  # mã, tên, cước/chuyến & /tấn theo tuyến
    ("NX-ST", "Công ty TNHH Vận tải Sơn Trà", {"R-HM": (6_500_000, 120_000), "R-NS": (7_000_000, 130_000),
                                             "R-HS": (600_000, 20_000)}),
    ("NX-HV", "Công ty CP Logistics Hải Vân", {"R-HM": (6_300_000, 125_000), "R-NS": (6_800_000, 135_000),
                                             "R-HS": (650_000, 18_000)}),
]
# biển số, loại xe, tải trọng, nhà xe, tài xế (mã, tên, hạng, còn hạn ngày)
TRACTORS = [
    ("43C-388.21", "NX-ST", ("TX03", "Lê Văn Cường", "FC", 900)),
    ("43C-391.05", "NX-ST", ("TX06", "Võ Văn Phúc", "FC", 640)),
    ("43C-402.77", "NX-ST", ("TX08", "Nguyễn Hữu Tài", "FC", 1100)),
    ("43C-415.30", "NX-ST", ("TX09", "Huỳnh Văn Khoa", "FC", 300)),
    ("37C-215.48", "NX-HV", ("TX04", "Phạm Văn Dũng", "FC", 500)),
    ("37C-219.63", "NX-HV", ("TX10", "Đặng Văn Long", "FC", 820)),
    ("36C-411.20", "NX-HV", ("TX11", "Trịnh Văn Quý", "FC", 410)),
    ("36C-418.92", "NX-HV", ("TX12", "Bùi Văn Sang", "FC", 760)),
]
# xe giao hàng nội bộ: biển số, loại, tải trọng, bồn xi măng rời?, tài xế
FLEET = [
    ("43C-600.88", "Đầu kéo + bồn xi măng rời 50T", 50, True, ("TX01", "Nguyễn Văn An", "FC", 700)),
    ("43C-611.29", "Xe bồn xi măng rời 35T", 35, True, ("TX13", "Mai Xuân Lộc", "FC", 950)),
    ("43C-215.36", "Tải thùng 15T", 15, False, ("TX02", "Trần Văn Bình", "C", 400)),
    ("43H-056.19", "Tải thùng 20T", 20, False, ("TX07", "Đỗ Văn Hải", "C", 520)),
    ("92C-104.58", "Tải thùng 20T", 20, False, ("TX14", "Phan Thanh Sơn", "C", 680)),
    ("43C-447.15", "Tải thùng 15T", 15, False, ("TX15", "Trương Công Minh", "C", 1200)),
    ("43C-318.72", "Tải thùng 8T", 8, False, ("TX16", "Lê Hữu Nghĩa", "C", 330)),
]
INTERNAL_TRACTOR = ("43C-512.66", "Đầu kéo 30T (xe công ty)", 30)
FAIL_NOTES = ["Công trình chưa có người nhận, hẹn giao lại", "Mưa lớn, công trình không cho xe vào",
              "Đường vào công trình đang thi công, xe không vào được"]
PARTIAL_NOTES = ["Rách bao khi bốc dỡ, khách không nhận", "Khách chỉ nhận đủ dùng trong ngày, phần còn lại giao sau"]
LATE_REASONS = ["Kẹt xe hầm Hải Vân", "Mưa lớn QL1A đoạn Quảng Trị", "Xe chờ cân tải trọng"]
INCIDENTS = [("TRAFFIC", "Kẹt xe hầm Hải Vân"), ("TRAFFIC", "Ùn tắc QL1A qua TP. Đông Hà"),
             ("WEATHER", "Mưa lớn, xe giảm tốc đoạn Quảng Bình"), ("BREAKDOWN", "Thủng lốp, thay lốp dự phòng")]


class Command(BaseCommand):
    help = "Dựng dữ liệu demo Đà Nẵng (~26 tuần lịch sử theo quy trình mới) vào database SQLite hiện tại."

    def add_arguments(self, parser):
        parser.add_argument("--weeks", type=int, default=26)
        parser.add_argument("--seed", type=int, default=80)
        parser.add_argument("--end", default="07:30", help="Giờ kết thúc lịch sử hôm nay (mặc định 07:30)")
        parser.add_argument("--today", help="Ngày chốt dữ liệu YYYY-MM-DD (mặc định hôm nay; dùng để kiểm thử)")

    # ================================================================== khung chạy
    def handle(self, *args, **opts):
        if connection.vendor != "sqlite":
            raise CommandError("build_demo chỉ chạy trên SQLite (đặt USE_SQLITE=1, SQLITE_NAME=demo.sqlite3). "
                               "Nạp lên Supabase bằng load_demo.")
        call_command("flush", interactive=False, verbosity=0)
        with connection.cursor() as c:
            c.execute("PRAGMA synchronous=OFF")
            c.execute("PRAGMA journal_mode=MEMORY")
        default_storage._wrapped = DemoStorage()
        self.r = random.Random(opts["seed"])
        self.q, self.cnt = [], itertools.count()
        self.err, self.last_err, self.stat = Counter(), {}, Counter()
        self.tractor_free = {}
        self.exact = set()  # đơn giao đúng đủ (không random thiếu / thất bại)
        today = datetime.strptime(opts["today"], "%Y-%m-%d").date() if opts["today"] else djtz.localdate()
        hh, mm = (int(x) for x in opts["end"].split(":"))
        self.END_DAY = today
        self.END = self.T(today, hh, mm)
        first = today - timedelta(days=today.weekday()) - timedelta(weeks=opts["weeks"])
        self.START_DAY = first
        self.now = self.T(first, 0, 0) - timedelta(days=1)
        from apps.core.services import BusinessError
        self.BusinessError = BusinessError
        with mock.patch.object(djtz, "now", lambda: self.now):
            self.masters()
            self.users()
            self.params()
            self.opening_stock()
            day = first
            while day <= today:
                self.push(self.T(day, 0, 1), self.day_events, day)
                day += timedelta(days=1)
            self.push(self.T(today - timedelta(days=5), 8, 20), self.demo_topup)
            self.push(self.T(today - timedelta(days=4), 7, 0), self.demo_trim)
            self.run_until(self.END)
            self.finish()
        self.report()

    def T(self, d, hh, mm=0):
        return djtz.make_aware(datetime.combine(d, time(hh, mm)))

    def push(self, when, fn, *args):
        heapq.heappush(self.q, (when, next(self.cnt), fn, args))

    def run_until(self, end):
        while self.q and self.q[0][0] <= end:
            when, _, fn, args = heapq.heappop(self.q)
            self.now = max(self.now, when)
            try:
                fn(*args)
            except self.BusinessError as e:
                self.err[fn.__name__] += 1
                self.last_err[fn.__name__] = str(e)
            self.as_user(None)

    def as_user(self, user):
        from apps.core.middleware import _local
        _local.request = SimpleNamespace(user=user, META={}) if user else None

    def tick(self, minutes):
        self.now += timedelta(minutes=minutes)

    def jit(self, a, b):
        return timedelta(minutes=self.r.uniform(a, b))

    @property
    def today(self):
        return djtz.localdate()

    def photo(self):
        return SimpleUploadedFile("anh.jpg", self.jpeg, content_type="image/jpeg")

    # ================================================================== danh mục
    def masters(self):
        from apps.masterdata import models as m
        self.now = self.T(self.START_DAY - timedelta(days=60), 9, 0)
        start = self.START_DAY - timedelta(days=60)
        self.jpeg = _truck_photo("anh", (230, 95, 43), "x")
        self.sig = "data:image/png;base64," + base64.b64encode(_signature("s")).decode()
        self.wh = m.Warehouse.objects.create(code="KDN", name="Kho tổng Đà Nẵng",
                                             address="KCN Hòa Khánh, TP. Đà Nẵng")
        self.loc = {}
        for code, zone, typ, cap in [("A-01", "A", "STORAGE", 600), ("A-02", "A", "STORAGE", 500),
                                     ("A-03", "A", "STORAGE", 300), ("A-04", "A", "STORAGE", 300),
                                     ("B-01", "B", "STORAGE", 400), ("B-02", "B", "STORAGE", 400),
                                     ("C-01", "C", "STORAGE", 200), ("C-02", "C", "STORAGE", 200),
                                     ("D-01", "D", "STORAGE", 80), ("D-02", "D", "STORAGE", 60),
                                     ("Q-01", "Q", "QUARANTINE", 50), ("OUT-01", "OUT", "STAGING_OUT", 100)]:
            self.loc[code] = m.Location.objects.create(warehouse=self.wh, code=code, zone=zone, loc_type=typ,
                                                       capacity_ton=cap)
        for i in range(1, 4):
            m.Dock.objects.create(warehouse=self.wh, code=f"BEN-{i}", name=f"Bến hạ tải {i}")
        uom = {c: m.Uom.objects.create(code=c, name=n) for c, n in
               [("TAN", "Tấn"), ("BAO", "Bao"), ("CAY", "Cây"), ("MET", "Mét")]}
        cat = {c: m.ProductCategory.objects.create(code=c, name=n) for c, n in
               [("XM", "Xi măng"), ("THEP", "Thép xây dựng"), ("TON", "Tôn lợp"), ("ONG", "Ống nhựa")]}
        self.route = {}
        for code, name, origin, km, hours in ROUTES:
            self.route[code] = m.Route.objects.create(code=code, name=name, origin=origin, destination="Kho tổng Đà Nẵng",
                                                      distance_km=km, std_hours=hours)
        self.sup = {}
        for code, name, addr, route, terms, _ in SUPPLIERS:
            self.sup[code] = m.Supplier.objects.create(code=code, name=name, address=addr, default_route=self.route[route],
                                                       payment_terms_days=terms)
        self.pl = m.PriceList.objects.create(code="PL-DN26", name="Bảng giá Đà Nẵng 2026", valid_from=start)
        self.prod, self.spec = {}, {}
        for (code, name, c, u, kg, cost, price, floor, sup, mean, loc, size) in PRODUCTS:
            p = m.Product.objects.create(code=code, name=name, category=cat[c], base_uom=uom[u],
                                         default_supplier=self.sup[sup], unit_weight_kg=Decimal(str(kg)), standard_cost=Decimal(cost),
                                         avg_cost=Decimal(cost))
            p.refresh_from_db()
            m.SupplierPrice.objects.create(supplier=self.sup[sup], product=p, price=cost, valid_from=start,
                                           status="APPROVED", approved_at=self.now)
            m.PriceListLine.objects.create(price_list=self.pl, product=p, price=price, floor_price=floor)
            self.prod[code] = p
            self.spec[code] = SimpleNamespace(mean=mean, loc=loc, size=size, floor=floor, sup=sup)
        m.ProductUom.objects.create(product=self.prod["XM-HM40B"], uom=uom["TAN"], factor=20)
        m.ProductUom.objects.create(product=self.prod["XM-NS40B"], uom=uom["TAN"], factor=20)
        self.carrier = {}
        for code, name, rates in CARRIERS:
            car = m.Carrier.objects.create(code=code, name=name)
            ct = m.CarrierContract.objects.create(code=f"HD-{code}-2026", name=f"Hợp đồng vận chuyển {name} 2026",
                                                  carrier=car, valid_from=start)
            for r, (trip, ton) in rates.items():
                m.CarrierRate.objects.create(contract=ct, route=self.route[r], rate_per_trip=trip, rate_per_ton=ton)
            self.carrier[code] = car
        today = self.END_DAY

        def driver(spec, carrier=None):
            code, name, cls, days = spec
            return m.Driver.objects.create(code=code, name=name, license_no=f"48{code[2:]}0{len(name):02d}317",
                                           license_class=cls, license_expiry=today + timedelta(days=days),
                                           carrier=carrier, phone=f"0905 {code[2:]}0 {len(name) * 7:03d}")
        self.tractors = []  # [(vehicle, driver, carrier)]
        for plate, car, dspec in TRACTORS:
            v = m.Vehicle.objects.create(plate=plate, vehicle_type="Đầu kéo 30T", capacity_ton=30,
                                         carrier=self.carrier[car])
            self.tractors.append((v, driver(dspec, self.carrier[car])))
        m.Driver.objects.create(code="TX05", name="Hoàng Văn Em", license_no="4805017317", license_class="C",
                                license_expiry=today - timedelta(days=12), carrier=self.carrier["NX-HV"])
        self.fleet = []  # [(vehicle, driver, is_tanker)]
        for plate, typ, cap, tanker, dspec in FLEET:
            v = m.Vehicle.objects.create(plate=plate, vehicle_type=typ, capacity_ton=cap)
            self.fleet.append((v, driver(dspec), tanker))
        m.Vehicle.objects.create(plate=INTERNAL_TRACTOR[0], vehicle_type=INTERNAL_TRACTOR[1],
                                 capacity_ton=INTERNAL_TRACTOR[2])
        self.cust = {}
        for code, name, typ, addr, limit, terms, lag in CUSTOMERS:
            self.cust[code] = m.Customer.objects.create(
                code=code, name=name, customer_type=typ, address=addr, credit_limit=limit, payment_terms_days=terms,
                price_list=self.pl if typ == "B2B" else None, email=f"muahang@{code.lower()}.example" if typ == "B2B" else "")
        self.pay_lag = {c[0]: c[6] for c in CUSTOMERS}
        self.site, self.travel = {}, {}
        for code, name, cus, addr, prio, contact, phone, minutes in SITES:
            self.site[code] = m.Site.objects.create(code=code, name=name, customer=self.cust[cus], address=addr,
                                                    priority=prio, contact_name=contact, contact_phone=phone,
                                                    penalty_date=today + timedelta(days=30 + 7 * prio))
            self.travel[self.site[code].pk] = minutes

    def users(self):
        from apps.core.models import User
        from apps.masterdata.models import UserSiteAccess
        drv = {d.code: d for _, d, _ in self.fleet}

        def mk(un, role, name, **kw):
            return User.objects.create_user(un, f"{un}@tracon80.example", PASSWORD, role=role, full_name=name, **kw)
        self.u = {
            "admin": mk("admin", "ADMIN", "Quản trị hệ thống", is_staff=True, is_superuser=True),
            "director": mk("director", "DIRECTOR", "Võ Minh Quang"),
            "purchaser": mk("purchaser", "PURCHASER", "Đặng Thu Hà"),
            "dispatcher": mk("dispatcher", "DISPATCHER", "Bùi Quốc Huy"),
            "warehouse": mk("warehouse", "WAREHOUSE", "Ngô Văn Kho"),
            "accountant": mk("accountant", "ACCOUNTANT", "Lý Thị Kế"),
            "taixe": mk("taixe", "DRIVER", drv["TX01"].name, driver=drv["TX01"]),
            "taixe2": mk("taixe2", "DRIVER", drv["TX02"].name, driver=drv["TX02"]),
            "khachhang": mk("khachhang", "CUSTOMER", "Anh Tuấn – Sông Hàn", customer=self.cust["KH-001"]),
        }
        for code, _, _, _, _, un in SUPPLIERS:
            self.u[un] = mk(un, "SUPPLIER", f"Phòng kinh doanh – {self.sup[code].name}"[:150], supplier=self.sup[code])
        self.factory_user = {self.sup[c[0]].pk: self.u[c[5]] for c in SUPPLIERS}
        for s in ("CT-SH01", "CT-SH02"):
            UserSiteAccess.objects.create(user=self.u["khachhang"], site=self.site[s])
        self.driver_user = {drv["TX01"].pk: self.u["taixe"], drv["TX02"].pk: self.u["taixe2"]}

    def params(self):
        """Tham số SS/ROP ban đầu: khóa tay theo nhu cầu kế hoạch (chưa đủ lịch sử để dự báo). Mã demo khóa
        SS 20 / ROP 60 / Q 140 suốt kỳ (theo kịch bản demo); mã khác mở khóa sau 10 tuần để mô hình tự tính."""
        from apps.inventory.replenishment import get_param
        self.init_stock = {}
        for code, p in self.prod.items():
            sp = self.spec[code]
            lt = 0.6 if sp.sup == "NCC-HS" else 1.5
            d = sp.mean * 0.886 * 1.1
            rp = get_param(p)
            rp.lead_time_days = Decimal(str(lt))
            rp.manual_lock = True
            bags = p.base_uom.code in ("BAO", "TAN")
            rp.round_by_truck, rp.truck_ton = bags, Decimal(30)
            if code == DEMO_CODE:
                rp.manual_ss, rp.manual_rop, rp.manual_q = Decimal(20), Decimal(60), Decimal(140)
                rp.lock_reason = "Mã demo – khóa theo kịch bản báo cáo (SS 20 / ROP 60 / Q 140)"
            else:
                ss = math.ceil(d * 0.9)
                rp.manual_ss, rp.manual_rop = Decimal(ss), Decimal(math.ceil(d * lt + ss))
                rp.manual_q = Decimal(math.ceil(d * 6))
                rp.lock_reason = "Giai đoạn đầu chưa đủ 8 tuần lịch sử – nhập tay theo kế hoạch"
            rp.save()
            self.init_stock[code] = int(rp.manual_rop + rp.manual_q * Decimal("0.6"))

    def opening_stock(self):
        from apps.inventory.services import post_stock_move
        self.now = self.T(self.START_DAY, 5, 0) - timedelta(days=1)
        self.as_user(self.u["warehouse"])
        for code, p in self.prod.items():
            post_stock_move(p, self.loc[self.spec[code].loc], self.init_stock[code], "ADJ", "ADJ", 0,
                            unit_cost=p.standard_cost, note="Tồn đầu kỳ – chuyển từ sổ kho cũ")
        self.as_user(None)

    # ================================================================== lịch mỗi ngày
    def day_events(self, d):
        T, wd = self.T, d.weekday()
        last = d == self.END_DAY
        self.push(T(d, 1, 0), self.nightly)
        if d == self.START_DAY + timedelta(days=70):
            self.push(T(d, 0, 30), self.unlock_params)
        if wd < 6:
            self.push(T(d, 6, 0), self.dispatch, 1)
            if not last:
                self.push(T(d, 12, 45), self.dispatch, 2)
                for h, mi in ((8, 30), (14, 0)):
                    self.push(T(d, h, mi), self.purchaser_round)
                for h, mi in ((9, 30), (11, 0), (15, 30), (17, 30)):
                    self.push(T(d, h, mi), self.director_round)
                for h, mi in ((8, 0), (10, 45), (14, 30)):
                    self.push(T(d, h, mi), self.dispatcher_round)
                self.push(T(d, 18, 0), self.post_returns)
        if wd < 5 and not last:
            self.push(T(d, 16, 30), self.accounting_round)
            if wd == 4:
                self.push(T(d, 15, 0), self.customer_payments)
            if wd == 2:
                self.push(T(d, 15, 0), self.supplier_payments)
        if wd == 5 and not last and ((d - self.START_DAY).days // 7) % 2 == 1:
            self.push(T(d, 13, 30), self.inventory_count, "AB" if ((d - self.START_DAY).days // 14) % 2 else "CD")
        if not last:
            self.make_orders(d)

    # ------------------------------------------------------------------ nhu cầu -> đơn bán
    def make_orders(self, d):
        r = self.r
        holiday = (d.month, d.day) in ((9, 2), (9, 1), (4, 30), (5, 1))
        storm = d.month in (10, 11) and r.random() < 0.06
        per_site = defaultdict(list)
        for code, p in self.prod.items():
            if code == DEMO_CODE and d >= self.END_DAY - timedelta(days=5):
                continue  # kịch bản demo: mã demo không còn đơn mở
            sp = self.spec[code]
            mean = sp.mean * SEASON[d.month] * DOW[d.weekday()] * (0.3 if holiday or storm else 1)
            total = mean * math.exp(r.gauss(0, 0.28) - 0.04)
            typ, step, lo, hi = sp.size
            n = max(1, round(total / typ))
            weights = [r.random() + 0.3 for _ in range(n)]
            sites, w = zip(*BUYERS[code])
            for k in range(n):
                q = total * weights[k] / sum(weights)
                q = min(hi, int(round(q / step)) * step)
                if q < lo:
                    continue
                per_site[r.choices(sites, w)[0]].append((p, Decimal(q)))
        for site, lines in per_site.items():
            bulk = [(p, q) for p, q in lines if p.code == DEMO_CODE]
            other = [(p, q) for p, q in lines if p.code != DEMO_CODE]
            groups = [[x] for x in bulk]
            cur, kg = [], 0
            for p, q in other:  # đơn hàng bao / thép tách theo xe tải ≤ 20 t
                w_ = float(q * p.unit_weight_kg)
                if cur and kg + w_ > 19000:
                    groups.append(cur)
                    cur, kg = [], 0
                cur.append((p, q))
                kg += w_
            if cur:
                groups.append(cur)
            for g in groups:
                if d.weekday() == 6:
                    when = self.T(d, 8, 0) + self.jit(0, 90)
                else:
                    when = self.T(d, 7, 20) + self.jit(0, 200)
                self.push(when, self.create_so, site, g)

    def create_so(self, site_code, lines):
        from apps.core.services import next_doc_no
        from apps.sales import services as ss
        from apps.sales.models import SalesOrder, SalesOrderLine
        r = self.r
        site = self.site[site_code]
        cust = site.customer
        portal = cust.code == "KH-001" and r.random() < 0.35
        user = self.u["khachhang"] if portal else self.u["dispatcher"]
        self.as_user(user)
        d = self.today
        x = r.random()
        req = d + timedelta(days=0 if (x < 0.08 and self.now.hour < 9 and not portal) else (1 if x < 0.75 else 2))
        if req.weekday() == 6:
            req += timedelta(days=1)
        so = SalesOrder.objects.create(code=next_doc_no("SO"), customer=cust, site=site,
                                       order_type="INTERNAL" if cust.customer_type == "INTERNAL" else "B2B",
                                       source="PORTAL" if portal else "INTERNAL", order_date=d, requested_date=req,
                                       created_by=user)
        for p, q in lines:
            price = ss.default_price(cust, p)
            if cust.code == "KH-002" and not portal and r.random() < 0.04:
                price = (Decimal(self.spec[p.code].floor) * Decimal("0.98")).quantize(Decimal(1000))
                so.note = "Giá dự án theo thỏa thuận – trình Giám đốc duyệt"
            SalesOrderLine.objects.create(so=so, product=p, qty=q, unit_price=price)
        so.recalc_total()
        so.save()
        if portal:
            res = ss.auto_submit_portal(so, user)
            self.stat[f"portal_{res}"] += 1
        else:
            ss.submit_so(so, user)
        self.stat["so"] += 1

    def dispatcher_round(self):
        from apps.sales import services as ss
        from apps.sales.models import SalesOrder
        self.as_user(self.u["dispatcher"])
        for so in SalesOrder.objects.filter(status="DRAFT", source="PORTAL"):
            ss.submit_so(so, self.u["dispatcher"])

    # ------------------------------------------------------------------ giao hàng
    def dispatch(self, wave):
        from apps.sales import services as ss
        from apps.sales.models import DeliveryTrip, SalesOrder
        d = self.today
        base = self.now
        last = d == self.END_DAY
        busy = set(DeliveryTrip.objects.exclude(status__in=["COMPLETED", "CANCELLED"]).values_list("vehicle_id", flat=True))
        todo = []
        for so in SalesOrder.objects.filter(status__in=["CONFIRMED", "PARTIAL"], requested_date__lte=d) \
                .select_related("site", "customer").order_by("requested_date", "id"):
            lines = [l for l in so.lines.select_related("product") if l.qty_to_plan > 0]
            if not lines:
                continue
            w = float(sum(l.qty_to_plan * l.product.unit_weight_kg for l in lines)) / 1000
            todo.append((so, w, any(l.product.code == DEMO_CODE for l in lines)))
        plans = []
        for v, drv, tanker in sorted(self.fleet, key=lambda x: -x[0].capacity_ton):
            if v.pk in busy or (last and (drv.code == "TX01" or v.plate == "43C-600.88")):
                continue  # hôm demo: xe bồn 50T và tài xế taixe để trống cho kịch bản
            cap, stops = float(v.capacity_ton), []
            for item in list(todo):
                so, w, bulk = item
                if bulk != tanker or w > cap or len(stops) >= (1 if tanker else 3):
                    continue
                stops.append(so)
                cap -= w
                todo.remove(item)
            if stops:
                plans.append((v, drv, stops))
        for k, (v, drv, stops) in enumerate(plans):
            t0 = base + timedelta(minutes=3 * k)
            self.now = t0
            self.as_user(self.u["dispatcher"])
            trip = ss.create_trip(v, drv, d)
            for so in stops:
                ss.add_order_to_trip(trip, so)
            self.now = t0 + timedelta(minutes=6)
            pk = ss.release_picking(trip, self.u["dispatcher"])
            self.now = t0 + timedelta(minutes=28)
            self.as_user(self.u["warehouse"])
            gi = ss.confirm_picking(pk, self.u["warehouse"], {})
            self.now = t0 + timedelta(minutes=38)
            ss.post_goods_issue(gi, self.u["warehouse"])
            t = self.now + self.jit(5, 15)
            prev = None
            for stop in trip.stops.order_by("seq"):
                leg = self.travel[stop.site_id] if prev is None else max(15, abs(self.travel[stop.site_id] - prev) + 10)
                t += timedelta(minutes=leg) + self.jit(-8, 25)
                self.push(t, self.pod, stop.pk)
                t += timedelta(minutes=30 if not stop.so.lines.filter(product__code=DEMO_CODE).exists() else 45)
                prev = self.travel[stop.site_id]
            self.stat["delivery_trip"] += 1

    def pod(self, stop_id):
        from apps.sales import services as ss
        from apps.sales.models import DeliveryStop
        stop = DeliveryStop.objects.select_related("trip", "site", "so").get(pk=stop_id)
        user = self.driver_user.get(stop.trip.driver_id, self.u["dispatcher"])
        self.as_user(user)
        x = 1 if stop.so_id in self.exact else self.r.random()
        receiver = stop.site.contact_name or "Chỉ huy công trình"
        if x < 0.02:
            ss.confirm_pod(stop, user, {}, receiver, note=self.r.choice(FAIL_NOTES), failed=True)
            self.stat["pod_failed"] += 1
            return
        delivered, note = {}, ""
        lines = list(stop.lines.select_related("so_line__product"))
        if x < 0.05:
            dl = self.r.choice(lines)
            unit = dl.so_line.product.base_uom.code
            cut = Decimal(self.r.choice([5, 10, 15, 20])) if unit in ("BAO", "CAY", "MET") else Decimal(1)
            if dl.qty_planned > cut:
                delivered[dl.pk] = dl.qty_planned - cut
                note = self.r.choice(PARTIAL_NOTES)
                self.stat["pod_partial"] += 1
        ss.confirm_pod(stop, user, delivered, receiver, photo=self.photo(), signature_data=self.sig, note=note)

    def post_returns(self):
        from apps.sales import services as ss
        from apps.sales.models import SalesReturn
        self.as_user(self.u["warehouse"])
        for ret in SalesReturn.objects.filter(status="DRAFT"):
            ss.post_sales_return(ret, self.u["warehouse"])

    # ------------------------------------------------------------------ mua hàng
    def purchaser_round(self):
        from apps.purchasing import services as ps
        from apps.purchasing.models import Claim, PurchaseRequisition
        pur = self.u["purchaser"]
        self.as_user(pur)
        for c in Claim.objects.filter(status="APPROVED"):
            ps.claim_action(c, pur, "send", note="Đã gửi biên bản cho đối tác")
        prs = list(PurchaseRequisition.objects.filter(status="SUBMITTED"))
        if not prs:
            return
        for po in ps.convert_prs_to_po(prs, self.wh, pur):
            self.tick(4)
            ps.submit_po(po, pur)
            self.stat["po"] += 1
            if po.status == "APPROVED":
                self.push(self.now + self.jit(15, 40), self.send_po, po.pk)

    def director_round(self):
        from apps.accounting import services as acs
        from apps.accounting.models import Bill
        from apps.purchasing import services as ps
        from apps.purchasing.models import Claim, PurchaseOrder
        from apps.sales import services as ss
        from apps.sales.models import Allocation, SalesOrder
        dr = self.u["director"]
        self.as_user(dr)
        for po in PurchaseOrder.objects.filter(status="PENDING_APPROVAL"):
            self.tick(2)
            ps.approve_po(po, dr)
            self.push(self.now + self.jit(15, 45), self.send_po, po.pk)
        for so in SalesOrder.objects.filter(status="PENDING_APPROVAL"):
            self.tick(2)
            ss.approve_so(so, dr)
        for al in Allocation.objects.filter(status="PROPOSED"):
            ss.approve_allocation(al, dr, {})
        for c in Claim.objects.filter(status="OPEN"):
            ps.claim_action(c, dr, "approve", note="Đồng ý khiếu nại theo biên bản kiểm nhận")
        for b in Bill.objects.filter(status="MISMATCH"):
            acs.accept_mismatch(b, dr, "Nhà máy điều chỉnh giá theo thông báo, đã đối chiếu với phòng mua hàng")

    def send_po(self, po_id):
        from apps.purchasing import services as ps
        from apps.purchasing.models import PurchaseOrder
        po = PurchaseOrder.objects.select_related("supplier").get(pk=po_id)
        self.as_user(self.u["purchaser"])
        local = po.supplier.code == "NCC-HS"
        pickup = self.today + timedelta(days=0 if local and self.now.hour < 13 else 1)
        ps.send_po(po, pickup)
        fu = self.factory_user.get(po.supplier_id)
        if fu:
            self.push(self.now + self.jit(20, 150), self.factory_ack, po.pk)
        self.push(self.now + self.jit(30, 75), self.plan_trips, po.pk)

    def factory_ack(self, po_id):
        from apps.core.services import audit
        from apps.purchasing.models import PurchaseOrder
        po = PurchaseOrder.objects.get(pk=po_id)
        if po.supplier_ack_at or po.status in ("CANCELLED", "DRAFT"):
            return
        fu = self.factory_user[po.supplier_id]
        self.as_user(fu)
        po.supplier_ack_at, po.supplier_ack_by, po.supplier_ack_note = self.now, fu, ""
        po.save(update_fields=["supplier_ack_at", "supplier_ack_by", "supplier_ack_note", "updated_at"])
        audit("APPROVE", po, {"Nhà máy xác nhận đã nhận đơn": "—"}, user=fu)

    def free_tractor(self):
        from apps.purchasing.models import InboundTrip
        active = set(InboundTrip.objects.exclude(status__in=["RECEIVED", "CANCELLED"]).values_list("vehicle_id", flat=True))
        free = [(v, drv) for v, drv in self.tractors if v.pk not in active
                and self.tractor_free.get(v.pk, self.now) <= self.now]
        return self.r.choice(free) if free else (None, None)

    def plan_trips(self, po_id):
        from apps.purchasing import services as ps
        from apps.purchasing.models import PurchaseOrder
        po = PurchaseOrder.objects.select_related("route", "supplier").get(pk=po_id)
        if po.status not in ("APPROVED", "SENT", "PARTIAL"):
            return
        self.as_user(self.u["purchaser"])
        items = [[pol, pol.qty - pol.qty_on_trips] for pol in po.lines.select_related("product") if pol.qty > pol.qty_on_trips]
        local = po.route.code == "R-HS"
        k = 0
        while items:
            v, drv = self.free_tractor()
            if v is None:
                self.push(self.now + timedelta(hours=3), self.plan_trips, po.pk)
                return
            room, load = Decimal(30000), {}
            for it in list(items):
                pol, rest = it
                kg = pol.product.unit_weight_kg
                fit = min(rest, Decimal(math.floor(room / kg)))
                if fit <= 0:
                    continue
                load[pol] = fit
                room -= fit * kg
                it[1] = rest - fit
                if it[1] <= 0:
                    items.remove(it)
            if not load:
                break
            if local:
                dep = (self.now + timedelta(hours=2)) if self.now.hour < 13 else self.T(self.today + timedelta(days=1), 7, 0)
                dep += timedelta(minutes=20 * k)
            else:
                dep = self.T(self.today + timedelta(days=1), 4, 0) + timedelta(minutes=35 * k)
                if self.r.random() < 0.08:
                    dep += timedelta(days=1)  # nhà máy chờ xếp lượt bốc
            dep = dep.replace(second=0, microsecond=0)
            trip = ps.create_inbound_trip(po, po.route, v, drv, dep, load)
            self.tractor_free[v.pk] = datetime.max.replace(tzinfo=dep.tzinfo)
            self.push(dep + self.jit(-10, 50), self.depart, trip.pk)
            self.stat["inbound_trip"] += 1
            k += 1

    def trip_user(self, trip):
        return self.driver_user.get(trip.driver_id, self.u["purchaser"])

    def depart(self, trip_id):
        from apps.purchasing import services as ps
        from apps.purchasing.models import InboundTrip
        trip = InboundTrip.objects.select_related("route").get(pk=trip_id)
        user = self.trip_user(trip)
        self.as_user(user)
        ms = trip.milestones.get(seq=1)
        ps.record_milestone(trip, ms, user, photo=self.photo(), note="Đã cân xe, niêm phong",
                            loaded_qty={tl.pk: tl.qty_planned for tl in trip.lines.all()})
        std = float(trip.route.std_hours) * 60
        delay = 0
        if std > 120 and self.r.random() < 0.12:
            typ, desc = self.r.choice(INCIDENTS)
            delay = self.r.choice([30, 45, 60, 90, 120])
            self.push(self.now + self.jit(90, std - 200), self.incident, trip.pk, typ, desc, delay)
        if std > 120:
            self.push(self.now + timedelta(minutes=std - 120 + delay) + self.jit(-15, 40), self.near, trip.pk)
        else:
            self.push(self.now + self.jit(2, 6), self.near, trip.pk)

    def incident(self, trip_id, typ, desc, delay):
        from apps.purchasing import services as ps
        from apps.purchasing.models import InboundTrip
        trip = InboundTrip.objects.get(pk=trip_id)
        if trip.status != "IN_TRANSIT":
            return
        user = self.trip_user(trip)
        self.as_user(user)
        ps.report_incident(trip, user, typ, desc, delay)

    def near(self, trip_id):
        from apps.purchasing import services as ps
        from apps.purchasing.models import InboundTrip
        trip = InboundTrip.objects.select_related("route").get(pk=trip_id)
        user = self.trip_user(trip)
        self.as_user(user)
        ps.record_milestone(trip, trip.milestones.get(seq=2), user)
        trip.incidents.update(is_resolved=True)
        std = float(trip.route.std_hours) * 60
        self.push(self.now + (timedelta(minutes=120) + self.jit(-20, 30) if std > 120 else self.jit(45, 70)),
                  self.arrive, trip.pk)

    def arrive(self, trip_id):
        from apps.purchasing.models import InboundTrip
        InboundTrip.objects.filter(pk=trip_id).update(arrived_at=self.now)  # giờ xe tới cổng
        self.gate(trip_id, 0)

    def gate(self, trip_id, attempt):
        from apps.purchasing import services as ps
        from apps.purchasing.models import InboundTrip, UnloadingSlot
        trip = InboundTrip.objects.get(pk=trip_id)
        wh = self.u["warehouse"]
        self.as_user(wh)
        slot = UnloadingSlot.objects.filter(trip=trip).exclude(status="CANCELLED").first()
        reason = ""
        if slot and (self.now - slot.end_at) > timedelta(minutes=60):
            reason = self.r.choice(LATE_REASONS)
        try:
            gr = ps.gate_checkin(trip, wh, reason)
        except self.BusinessError as e:
            if "bận" in str(e) and attempt < 80:
                self.push(self.now + timedelta(minutes=10), self.gate, trip_id, attempt + 1)
                return
            raise
        dur = (gr.trip.slot.end_at - gr.trip.slot.start_at).total_seconds() / 60
        self.push(self.now + timedelta(minutes=dur) + self.jit(-5, 20), self.unload, gr.pk)

    def unload(self, gr_id):
        from apps.purchasing import services as ps
        from apps.purchasing.models import GoodsReceipt
        gr = GoodsReceipt.objects.select_related("trip__route").get(pk=gr_id)
        if gr.status != "DRAFT":
            return
        self.as_user(self.u["warehouse"])
        for line in gr.lines.select_related("product"):
            x = self.r.random()
            unit = line.product.base_uom.code
            if unit == "BAO" and x < 0.08:
                line.qty_damaged = Decimal(self.r.choice([2, 3, 5, 8, 12]))  # rách bao
            elif unit == "TAN" and x < 0.06:
                line.qty_received = line.qty_expected - Decimal(self.r.choice(["0.2", "0.3", "0.5"]))  # hao hụt
            elif unit in ("CAY", "MET") and x < 0.03:
                line.qty_received = line.qty_expected - Decimal(self.r.choice([2, 5, 10]))
            line.save()
        ps.submit_grn(gr, self.u["warehouse"])
        trip = gr.trip
        self.tractor_free[trip.vehicle_id] = self.now + timedelta(hours=RETURN_HOURS[trip.route.code])
        self.stat["grn"] += 1

    # ------------------------------------------------------------------ kế toán
    def accounting_round(self):
        from apps.accounting import services as acs
        from apps.accounting.models import Bill, BillLine
        from apps.purchasing import services as ps
        from apps.purchasing.models import Claim, InboundTrip, PurchaseOrder
        acc = self.u["accountant"]
        self.as_user(acc)
        today = self.today
        for code, cust in self.cust.items():
            if cust.customer_type != "B2B":
                continue
            ids = list(acs.invoiceable_lines(cust).values_list("pk", flat=True))
            if ids:
                inv = acs.create_invoice(cust, ids)
                acs.post_invoice(inv)
                self.stat["invoice"] += 1
        billed = set(BillLine.objects.filter(po_line__isnull=False).values_list("po_line__po_id", flat=True))
        for po in PurchaseOrder.objects.filter(status="RECEIVED").exclude(pk__in=billed).select_related("supplier"):
            last = po.receipts.filter(status="POSTED").order_by("-posted_at").first()
            if not last or self.now - last.posted_at < timedelta(hours=12):
                continue
            bump = self.r.random() < 0.05
            lines = [(l, l.qty_received, l.unit_price * (Decimal("1.02") if bump else 1)) for l in po.lines.all()
                     if l.qty_received > 0]
            if not lines:
                continue
            no = f"{po.supplier.code[-2:]}{self.r.randint(1000000, 9999999)}"
            b = acs.create_goods_bill(po.supplier, no, today, [(l, q, p.quantize(Decimal(1))) for l, q, p in lines])
            if b.status == "MATCHED":
                acs.post_bill(b, acc)
        for b in Bill.objects.filter(status="ACCEPTED"):
            acs.post_bill(b, acc)
        if today.day <= 3 and today.weekday() < 5 and not getattr(self, "_freight_done", None) == (today.year, today.month):
            self._freight_done = (today.year, today.month)
            first_cur = today.replace(day=1)
            billed_trips = set(BillLine.objects.filter(inbound_trip__isnull=False).values_list("inbound_trip_id", flat=True))
            for car in self.carrier.values():
                trips = list(InboundTrip.objects.filter(carrier=car, status="RECEIVED", planned_departure__lt=self.T(first_cur, 0))
                             .exclude(pk__in=billed_trips))
                if trips:
                    fb = acs.create_freight_bill(car, f"{car.code[-2:]}-{first_cur:%Y%m}", today,
                                                 [(t, t.freight_amount) for t in trips])
                    if fb.status == "MATCHED":
                        acs.post_bill(fb, acc)
        for c in Claim.objects.filter(status="SENT", sent_at__lt=self.now - timedelta(days=5)):
            try:
                ps.claim_action(c, acc, "compensate", note="Đối tác đồng ý giảm trừ công nợ")
            except self.BusinessError:
                ps.claim_action(c, acc, "close", note="Xe nội bộ – ghi nhận hao hụt, không đòi bồi thường")
        for c in Claim.objects.filter(status="COMPENSATED", compensated_at__lt=self.now - timedelta(days=1)):
            ps.claim_action(c, acc, "close")

    def customer_payments(self):
        from apps.accounting import services as acs
        from apps.accounting.models import Invoice
        self.as_user(self.u["accountant"])
        today = self.today
        for code, cust in self.cust.items():
            if cust.customer_type != "B2B":
                continue
            lag = self.pay_lag[code] + self.r.randint(-2, 3)
            due = Invoice.objects.filter(customer=cust, invoice_type="SALE", status__in=["POSTED", "PARTIAL"],
                                         due_date__lte=today - timedelta(days=lag))
            amt = sum((i.total - i.paid_amount for i in due), D0)
            if amt > 0:
                acs.record_payment("IN", cust, amt, today, note="Khách chuyển khoản theo bảng kê công nợ",
                                   user=self.u["accountant"])

    def supplier_payments(self):
        from apps.accounting import services as acs
        from apps.accounting.models import Bill
        self.as_user(self.u["accountant"])
        today = self.today
        for partner in list(self.sup.values()) + list(self.carrier.values()):
            kind = partner._meta.model_name
            bills = Bill.objects.filter(**{kind: partner}, status__in=["POSTED", "PARTIAL"],
                                        due_date__lte=today + timedelta(days=3)).exclude(bill_type="CLAIM_CREDIT")
            amt = sum((b.total - b.paid_amount for b in bills), D0)
            if amt > 0:
                acs.record_payment("OUT", partner, amt, today, note="Thanh toán theo hạn hợp đồng",
                                   user=self.u["accountant"])

    # ------------------------------------------------------------------ kho
    def inventory_count(self, zones):
        from apps.core.services import next_doc_no
        from apps.inventory.models import InventoryCount, InventoryCountLine, StockQuant
        from apps.inventory.views import _approve_count
        wh = self.u["warehouse"]
        self.as_user(wh)
        ic = InventoryCount.objects.create(code=next_doc_no("IC"), warehouse=self.wh, status="COUNTING",
                                           note=f"Kiểm kê định kỳ khu {' + '.join(zones)}", created_by=wh)
        lines = []
        for q in StockQuant.objects.filter(location__zone__in=list(zones), location__loc_type="STORAGE", on_hand__gt=0) \
                .select_related("product"):
            x = self.r.random()
            diff = D0
            if q.product.code == DEMO_CODE and self.today >= self.END_DAY - timedelta(days=5):
                x = 1  # sát hôm demo: không điều chỉnh mã demo (giữ đúng 100 t)
            if x < 0.12:  # ~1/2 lệch nhỏ (≤ 1%: đạt), ~1/2 lệch 1,2–3% (không đạt KPI-2)
                pct = self.r.uniform(0.001, 0.008) if x < 0.06 else self.r.uniform(0.012, 0.03)
                step = Decimal("0.1") if q.product.base_uom.code == "TAN" else Decimal(1)
                diff = max(step, (q.on_hand * Decimal(pct)).quantize(step)) * self.r.choice([-1, -1, 1])
                if q.on_hand + diff < 0:
                    diff = D0
            lines.append(InventoryCountLine.objects.create(count=ic, product=q.product, location=q.location,
                                                           book_qty=q.on_hand, count1=q.on_hand + diff,
                                                           final_qty=q.on_hand + diff))
        self.tick(120)
        value = sum(abs((l.final_qty - l.book_qty) * l.product.avg_cost) for l in lines)
        auto = value < Decimal(5_000_000)
        user = wh if auto else self.u["director"]
        self.as_user(user)
        _approve_count(ic, lines, user, auto=auto)

    # ------------------------------------------------------------------ tác vụ nền
    def nightly(self):
        from apps.inventory.services import run_replenishment
        run_replenishment(self.today)

    def unlock_params(self):
        from apps.inventory.models import ReplenishmentParam
        ReplenishmentParam.objects.exclude(product__code=DEMO_CODE).update(
            manual_lock=False, manual_ss=None, manual_rop=None, manual_q=None, lock_reason="")

    # ------------------------------------------------------------------ đưa về trạng thái demo
    def metrics(self):
        from apps.inventory.replenishment import stock_metrics
        p = self.prod[DEMO_CODE]
        return stock_metrics([p])[p.pk]

    def demo_topup(self):
        """5 ngày trước hôm demo: đặt thêm 1 đợt cho mã demo để chắc chắn đủ ≥ 100 t sau khi giao hết đơn."""
        from apps.core.services import next_doc_no
        from apps.purchasing.models import PurchaseRequisition, PurchaseRequisitionLine
        m = self.metrics()
        if m["position"] >= 110:  # position đã trừ đơn đang giữ -> ≥ 110 là đủ còn ≥ 100 t sau khi giao hết
            return
        qty = Decimal(math.ceil((110 - float(m["position"])) / 30) * 30)
        self.as_user(self.u["purchaser"])
        pr = PurchaseRequisition.objects.create(code=next_doc_no("PR"), status="SUBMITTED", source="MANUAL",
                                                need_date=self.today + timedelta(days=2), requested_by=self.u["purchaser"],
                                                note="Bổ sung dự trữ xi măng rời trước đợt đổ bê tông")
        PurchaseRequisitionLine.objects.create(pr=pr, product=self.prod[DEMO_CODE], qty=qty,
                                               suggested_supplier=self.sup["NCC-HM"])

    def demo_trim(self):
        """4 ngày trước hôm demo: phần tồn trên 100 t xuất cho công trình nội bộ (giao trong 3 ngày sau) -> hôm demo còn đúng 100 t."""
        m = self.metrics()
        extra = m["on_hand"] - m["reserved"] + m["incoming"] - 100
        if m["incoming"] > 0:
            self.stdout.write(self.style.WARNING(f"Mã demo còn hàng đang về {m['incoming']} – kiểm tra lại."))
        while extra > 0:
            q = min(extra, Decimal(35)).quantize(Decimal("0.1"))
            self.create_so_internal(q)
            extra -= q

    def create_so_internal(self, q):
        from apps.core.services import next_doc_no
        from apps.sales import services as ss
        from apps.sales.models import SalesOrder, SalesOrderLine
        site = self.site["CT-NB01"]
        req = self.today + timedelta(days=1)
        if req.weekday() == 6:
            req += timedelta(days=1)
        self.as_user(self.u["dispatcher"])
        so = SalesOrder.objects.create(code=next_doc_no("SO"), customer=site.customer, site=site, order_type="INTERNAL",
                                       source="INTERNAL", order_date=self.today,
                                       requested_date=req, created_by=self.u["dispatcher"],
                                       note="Đổ bê tông móng – xuất từ tồn xi măng rời")
        p = self.prod[DEMO_CODE]
        SalesOrderLine.objects.create(so=so, product=p, qty=q, unit_price=ss.default_price(so.customer, p))
        so.recalc_total()
        so.save()
        ss.submit_so(so, self.u["dispatcher"])
        self.exact.add(so.pk)

    def finish(self):
        from apps.core.models import Alert, SystemParam
        from apps.inventory.services import calc_param, run_replenishment
        from apps.inventory.simulation import create_sim_product
        from apps.purchasing.models import GoodsReceipt
        self.now = self.END
        # xe đang hạ tải lúc chốt dữ liệu -> hạ xong (để bến trống cho kịch bản)
        for gr in GoodsReceipt.objects.filter(status="DRAFT"):
            self.unload(gr.pk)
        Alert.objects.filter(created_at__lt=self.END - timedelta(days=3)).update(is_resolved=True)
        self.as_user(self.u["director"])
        p = create_sim_product()
        calc_param(p)
        self.as_user(None)
        run_replenishment(self.END_DAY)
        SystemParam.objects.update_or_create(key="DEMO_BUILD_DATE", defaults={
            "value": self.END_DAY.isoformat(), "description": "Ngày dựng dữ liệu demo (load_demo dời ngày theo mốc này)"})

    def report(self):
        from apps.inventory.models import ReplenishmentParam
        from apps.purchasing.models import InboundTrip, PurchaseOrder, PurchaseRequisition
        from apps.sales.models import SalesOrderLine
        with mock.patch.object(djtz, "now", lambda: self.END):
            m = self.metrics()
            p = self.prod[DEMO_CODE]
            self.stdout.write(self.style.SUCCESS(
                f"✔ Dữ liệu demo {self.START_DAY:%d/%m/%Y} → {self.END:%d/%m/%Y %H:%M}: {dict(self.stat)}"))
            self.stdout.write(f"  Mã demo {DEMO_CODE}: tồn {m['on_hand']:g} · đã giữ {m['reserved']:g} · đang về "
                              f"{m['incoming']:g} · pipeline {m['pipeline']:g}")
            open_pr = PurchaseRequisition.objects.filter(status__in=["DRAFT", "SUBMITTED"], lines__product=p).count()
            open_po = PurchaseOrder.objects.filter(lines__product=p).exclude(
                status__in=["RECEIVED", "CLOSED", "CANCELLED"]).count()
            open_so = SalesOrderLine.objects.filter(product=p, so__status__in=["DRAFT", "PENDING_APPROVAL", "CONFIRMED",
                                                                                "PARTIAL"]).count()
            self.stdout.write(f"  PR mở {open_pr} · PO mở {open_po} · dòng đơn mở {open_so} · chuyến inbound đang chạy "
                              f"{InboundTrip.objects.exclude(status__in=['RECEIVED', 'CANCELLED']).count()}")
            ready = (m["on_hand"] == 100 and m["reserved"] == 0 and m["incoming"] == 0 and m["pipeline"] == 0
                     and not (open_pr or open_po or open_so))
            if ready:
                self.stdout.write(self.style.SUCCESS("  Sẵn sàng cho kịch bản demo 18 bước."))
            else:
                self.stdout.write(self.style.ERROR("  CHƯA đúng trạng thái demo (cần tồn 100 t, không PR/PO/đơn mở) – "
                                                   "chạy lại với --seed khác, vd. --seed 81."))
            for rp in ReplenishmentParam.objects.select_related("product").order_by("product__code"):
                self.stdout.write(f"  {rp.product.code:<10} SS {rp.eff_ss:>9.1f} ROP {rp.eff_rop:>9.1f} Q {rp.order_q:>8.0f} "
                                  f"LT {rp.lead_time_days} {rp.forecast_model or '-'} n={rp.n_weeks} "
                                  f"{'khóa' if rp.manual_lock else ''}")
            if self.err:
                self.stdout.write(self.style.WARNING(f"  Thao tác bị hệ thống chặn (bình thường nếu ít): {dict(self.err)}"))
                for k, v in self.last_err.items():
                    self.stdout.write(f"    {k}: {v[:160]}")
