"""Tạo dữ liệu mẫu đầy đủ luồng nghiệp vụ: python manage.py seed_demo [--reset]

Dữ liệu được tạo bằng chính các hàm dịch vụ (post_stock_move, submit_po, record_milestone, confirm_pod...)
nên trạng thái luôn nhất quán với logic hệ thống.
"""
import base64
import io
import random
from datetime import timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

PASSWORD = "Tracon@123"


def fake_image(text, color=(230, 95, 43)):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (480, 320), color)
    d = ImageDraw.Draw(img)
    d.rectangle([16, 16, 464, 304], outline="white", width=3)
    d.text((32, 140), text, fill="white")
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return ContentFile(buf.getvalue(), name="photo.jpg")


def fake_signature():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (400, 160), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    pts = [(20, 110), (60, 40), (90, 120), (130, 50), (170, 115), (210, 60), (260, 100), (320, 70), (380, 90)]
    d.line(pts, fill=(6, 6, 6, 255), width=4, joint="curve")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


class Command(BaseCommand):
    help = "Tạo dữ liệu demo cho TRACON 80 (tài khoản mật khẩu mặc định Tracon@123)."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Xóa toàn bộ DB và migrate lại (CHỈ dùng cho SQLite dev)")

    def handle(self, *args, **opts):
        from django.conf import settings
        if opts["reset"]:
            if "sqlite" not in settings.DATABASES["default"]["ENGINE"]:
                self.stderr.write("--reset chỉ dùng với SQLite. Với Supabase hãy dùng: manage.py flush")
                return
            call_command("flush", interactive=False)
        from apps.core.models import User
        if User.objects.filter(username="director").exists():
            self.stdout.write(self.style.WARNING("Đã có dữ liệu demo (user 'director'). Dùng --reset (SQLite) hoặc flush để tạo lại."))
            return
        random.seed(80)
        with transaction.atomic():
            self.masters()
            self.users()
            self.history()
        # các luồng chạy ngoài 1 transaction lớn để giống thao tác thực
        self.purchasing_flows()
        self.sales_flows()
        self.accounting_flows()
        self.misc()
        self.stdout.write(self.style.SUCCESS("✔ Đã tạo dữ liệu demo. Đăng nhập với mật khẩu: " + PASSWORD))
        for u in User.objects.order_by("role"):
            self.stdout.write(f"   {u.username:<12} {u.get_role_display()}")

    # ------------------------------------------------------------------ Danh mục
    def masters(self):
        from apps.masterdata import models as m
        today = timezone.localdate()
        self.wh = m.Warehouse.objects.create(code="KBD", name="Kho tổng Bình Dương", address="KCN Sóng Thần, Dĩ An, Bình Dương")
        self.loc = {}
        for code, zone, typ, cap in [("A-01", "A", "STORAGE", 200), ("A-02", "A", "STORAGE", 200), ("A-03", "A", "STORAGE", 150),
                                     ("B-01", "B", "STORAGE", 300), ("B-02", "B", "STORAGE", 300), ("C-01", "C", "STORAGE", 400),
                                     ("C-02", "C", "STORAGE", 400), ("D-01", "D", "STORAGE", 100), ("Q-01", "Q", "QUARANTINE", 50),
                                     ("OUT-01", "OUT", "STAGING_OUT", 100)]:
            self.loc[code] = m.Location.objects.create(warehouse=self.wh, code=code, zone=zone, loc_type=typ, capacity_ton=cap)
        for i in range(1, 5):
            m.Dock.objects.create(warehouse=self.wh, code=f"BEN-{i}", name=f"Bến hạ tải {i}")

        u = {c: m.Uom.objects.create(code=c, name=n) for c, n in [("BAO", "Bao"), ("CAY", "Cây"), ("M3", "Mét khối"), ("VIEN", "Viên"), ("TAN", "Tấn")]}
        cat = {c: m.ProductCategory.objects.create(code=c, name=n) for c, n in
               [("XM", "Xi măng"), ("THEP", "Sắt thép"), ("CL", "Cát đá"), ("GACH", "Gạch"), ("ONG", "Ống nước")]}

        # Tuyến & mốc hành trình
        def route(code, name, o, d, km, hours, ms):
            r = m.Route.objects.create(code=code, name=name, origin=o, destination=d, distance_km=km, std_hours=hours)
            for i, (mc, mn, off, ph) in enumerate(ms, 1):
                m.RouteMilestone.objects.create(route=r, seq=i, code=mc, name=mn, planned_offset_min=off, photo_required=ph)
            return r
        self.r_xm = route("R-KG", "Kiên Giang → Kho BD", "Nhà máy xi măng Kiên Lương", "Kho BD", 310, 8,
                          [("FACTORY", "Tới nhà máy", 60, True), ("LOADED", "Bốc hàng xong", 180, True),
                           ("CHECKPOINT", "Qua trạm Cần Thơ", 330, False), ("GATE", "Tới cổng kho", 480, True)])
        self.r_thep = route("R-PM", "Phú Mỹ → Kho BD", "Nhà máy thép Phú Mỹ", "Kho BD", 95, 3,
                            [("FACTORY", "Tới nhà máy", 30, True), ("LOADED", "Bốc hàng xong", 90, True),
                             ("CHECKPOINT", "Qua trạm Long Thành", 140, False), ("GATE", "Tới cổng kho", 180, True)])
        self.r_da = route("R-BH", "Biên Hòa → Kho BD", "Mỏ đá Tân Đông Hiệp", "Kho BD", 40, 2,
                          [("FACTORY", "Tới mỏ", 20, True), ("LOADED", "Bốc hàng xong", 60, True),
                           ("CHECKPOINT", "Qua cầu Đồng Nai", 90, False), ("GATE", "Tới cổng kho", 120, True)])

        S = m.Supplier.objects.create
        self.s_xm = S(code="NCC-XM", name="Công ty CP Xi măng Phương Nam", tax_code="0301234567", phone="0283 888 1001",
                      address="Kiên Lương, Kiên Giang", default_route=self.r_xm, payment_terms_days=30)
        self.s_thep = S(code="NCC-TH", name="Công ty TNHH Thép Miền Đông", tax_code="0302345678", phone="0254 777 2002",
                        address="KCN Phú Mỹ, BR-VT", default_route=self.r_thep, payment_terms_days=45)
        self.s_da = S(code="NCC-DA", name="Công ty VLXD Đá Biên Hòa", tax_code="3601234567", phone="0251 666 3003",
                      address="Tân Đông Hiệp, Dĩ An", default_route=self.r_da, payment_terms_days=15)
        self.s_ong = S(code="NCC-ON", name="Công ty Nhựa Sài Gòn Xanh", tax_code="0303456789", phone="0283 555 4004",
                       address="Q.12, TP.HCM", default_route=self.r_da, payment_terms_days=30)

        # (code, tên, nhóm, đvt, kg/đv, giá vốn, giá bán, giá sàn, NCC, nhu cầu/ngày)
        spec = [
            ("XM-PCB40", "Xi măng PCB40 bao 50kg", "XM", "BAO", 50, 85000, 98000, 92000, self.s_xm, (180, 360)),
            ("XM-PC50", "Xi măng PC50 bao 50kg", "XM", "BAO", 50, 92000, 106000, 99000, self.s_xm, (60, 160)),
            ("THEP-D10", "Thép thanh vằn D10 (11,7m)", "THEP", "CAY", 7.22, 115000, 132000, 124000, self.s_thep, (60, 180)),
            ("THEP-D16", "Thép thanh vằn D16 (11,7m)", "THEP", "CAY", 18.47, 290000, 330000, 312000, self.s_thep, (40, 110)),
            ("CAT-VANG", "Cát vàng bê tông", "CL", "M3", 1500, 380000, 450000, 420000, self.s_da, (8, 26)),
            ("DA-1X2", "Đá 1x2 xanh", "CL", "M3", 1600, 320000, 390000, 360000, self.s_da, (8, 22)),
            ("GACH-DAC", "Gạch đặc tuynel 8x18", "GACH", "VIEN", 2.2, 1200, 1500, 1350, self.s_da, (2500, 7000)),
            ("ONG-PVC90", "Ống PVC D90 (4m)", "ONG", "CAY", 6, 145000, 175000, 160000, self.s_ong, (10, 40)),
        ]
        self.pl = m.PriceList.objects.create(code="PL-STD", name="Bảng giá tiêu chuẩn 2026")
        self.prod, self.demand = {}, {}
        for code, name, c, uo, kg, cost, price, floor, sup, dem in spec:
            p = m.Product.objects.create(code=code, name=name, category=cat[c], base_uom=u[uo], default_supplier=sup,
                                         unit_weight_kg=kg, standard_cost=cost, avg_cost=cost)
            m.SupplierPrice.objects.create(supplier=sup, product=p, price=cost, valid_from=today - timedelta(days=180))
            m.PriceListLine.objects.create(price_list=self.pl, product=p, price=price, floor_price=floor)
            self.prod[code] = p
            self.demand[code] = dem
        m.ProductUom.objects.create(product=self.prod["THEP-D10"], uom=u["TAN"], factor=Decimal("138.504"))

        C = m.Carrier.objects.create
        self.c1 = C(code="NX-MP", name="Vận tải Minh Phát", phone="0909 111 222", tax_code="0311112222")
        self.c2 = C(code="NX-SV", name="Logistics Sao Việt", phone="0918 333 444", tax_code="0313334444")
        for car, rates in [(self.c1, [(self.r_xm, 4500000, 60000), (self.r_thep, 1800000, 45000), (self.r_da, 900000, 30000)]),
                           (self.c2, [(self.r_xm, 4300000, 65000), (self.r_thep, 1700000, 50000), (self.r_da, 850000, 35000)])]:
            ct = m.CarrierContract.objects.create(code=f"HD-{car.code}", name=f"Hợp đồng vận chuyển {car.name} 2026",
                                                  carrier=car, valid_from=today - timedelta(days=300))
            for r, trip, ton in rates:
                m.CarrierRate.objects.create(contract=ct, route=r, rate_per_trip=trip, rate_per_ton=ton)
        V = m.Vehicle.objects.create
        self.v = {
            "51C-123.45": V(plate="51C-123.45", vehicle_type="Tải thùng 15T", capacity_ton=15),
            "51C-678.90": V(plate="51C-678.90", vehicle_type="Tải thùng 8T", capacity_ton=8),
            "51D-111.11": V(plate="51D-111.11", vehicle_type="Đầu kéo 30T", capacity_ton=30, carrier=self.c1),
            "51D-222.22": V(plate="51D-222.22", vehicle_type="Đầu kéo 30T", capacity_ton=30, carrier=self.c1),
            "61C-333.33": V(plate="61C-333.33", vehicle_type="Tải 20T", capacity_ton=20, carrier=self.c2),
            "61C-444.44": V(plate="61C-444.44", vehicle_type="Tải 20T", capacity_ton=20, carrier=self.c2),
        }
        Dr = m.Driver.objects.create
        self.d = {
            "TX01": Dr(code="TX01", name="Nguyễn Văn An", phone="0901 000 001", license_no="790123456", license_class="FC", license_expiry=today + timedelta(days=700)),
            "TX02": Dr(code="TX02", name="Trần Văn Bình", phone="0901 000 002", license_no="790123457", license_class="C", license_expiry=today + timedelta(days=400)),
            "TX03": Dr(code="TX03", name="Lê Văn Cường", phone="0902 000 003", license_no="790223458", license_class="FC", license_expiry=today + timedelta(days=900), carrier=self.c1),
            "TX04": Dr(code="TX04", name="Phạm Văn Dũng", phone="0902 000 004", license_no="790223459", license_class="FC", license_expiry=today + timedelta(days=500), carrier=self.c2),
            "TX05": Dr(code="TX05", name="Hoàng Văn Em", phone="0902 000 005", license_no="790223460", license_class="C", license_expiry=today - timedelta(days=10), carrier=self.c2),
        }
        Cu = m.Customer.objects.create
        self.k1 = Cu(code="KH-001", name="Công ty CP Xây dựng Hưng Thịnh", customer_type="B2B", tax_code="0314567890", phone="0283 999 0001",
                     email="muahang@hungthinh.example", address="Q.9, TP. Thủ Đức", credit_limit=3_000_000_000, payment_terms_days=30, price_list=self.pl)
        self.k2 = Cu(code="KH-002", name="Công ty TNHH Nhà Đẹp Việt", customer_type="B2B", tax_code="0315678901", phone="0283 999 0002",
                     address="Q.2, TP. Thủ Đức", credit_limit=150_000_000, payment_terms_days=15, price_list=self.pl)
        self.k3 = Cu(code="CT-NB", name="Khối công trình nội bộ TRACON 80", customer_type="INTERNAL", address="Bình Dương")
        Si = m.Site.objects.create
        self.site = {
            "HT01": Si(code="CT-HT01", name="Chung cư Hưng Thịnh Riverside", customer=self.k1, address="Đường Liên Phường, TP. Thủ Đức",
                       priority=2, penalty_date=today + timedelta(days=10), contact_name="Anh Tuấn", contact_phone="0938 100 200"),
            "HT02": Si(code="CT-HT02", name="Nhà xưởng Tân Uyên", customer=self.k1, address="KCN Nam Tân Uyên, Bình Dương", priority=3,
                       contact_name="Chị Lan", contact_phone="0938 300 400"),
            "ND01": Si(code="CT-ND01", name="Biệt thự Thảo Điền", customer=self.k2, address="Thảo Điền, TP. Thủ Đức", priority=4),
            "NB01": Si(code="CT-NB01", name="Dự án KDC Phú Mỹ (nội bộ)", customer=self.k3, address="Phú Mỹ, BR-VT", priority=1,
                       penalty_date=today + timedelta(days=5), contact_name="Chỉ huy trưởng Hòa", contact_phone="0977 555 666"),
        }

    def users(self):
        from apps.core.models import User
        from apps.masterdata.models import UserSiteAccess
        mk = lambda un, role, name, **kw: User.objects.create_user(un, f"{un}@tracon80.example", PASSWORD, role=role, full_name=name, **kw)  # noqa: E731
        self.u_admin = mk("admin", "ADMIN", "Quản trị hệ thống", is_staff=True, is_superuser=True)
        self.u_dir = mk("director", "DIRECTOR", "Võ Minh Quang")
        self.u_pur = mk("purchaser", "PURCHASER", "Đặng Thu Hà")
        self.u_dis = mk("dispatcher", "DISPATCHER", "Bùi Quốc Huy")
        self.u_wh = mk("warehouse", "WAREHOUSE", "Ngô Văn Kho")
        self.u_acc = mk("accountant", "ACCOUNTANT", "Lý Thị Kế")
        self.u_drv = mk("taixe", "DRIVER", "Nguyễn Văn An", driver=self.d["TX01"])
        mk("taixe2", "DRIVER", "Trần Văn Bình", driver=self.d["TX02"])
        self.u_cus = mk("khachhang", "CUSTOMER", "Anh Tuấn – Hưng Thịnh", customer=self.k1)
        for s in ("HT01", "HT02"):
            UserSiteAccess.objects.create(user=self.u_cus, site=self.site[s])

    # ------------------------------------------------------------------ Lịch sử 90 ngày (nhu cầu xuất kho)
    def history(self):
        from apps.inventory.models import ReplenishmentSnapshot
        from apps.inventory.services import post_stock_move
        now = timezone.now()
        low = {"THEP-D16", "ONG-PVC90", "GACH-DAC"}  # cố ý để thiếu -> dưới ROP / phân bổ
        locs = {"XM": ["A-01", "A-02"], "THEP": ["B-01", "B-02"], "CL": ["C-01", "C-02"], "GACH": ["A-03"], "ONG": ["D-01"]}
        for code, p in self.prod.items():
            lo, hi = self.demand[code]
            avg = (lo + hi) / 2
            pl = locs[p.category.code]
            bal = Decimal(int(avg * 25))
            post_stock_move(p, self.loc[pl[0]], bal, "IN_PURCHASE", "GRN", 0, unit_cost=p.standard_cost,
                            note="Tồn đầu kỳ", at=now - timedelta(days=92))
            k = 0
            for day in range(90, 0, -1):
                at = now - timedelta(days=day, hours=random.randint(0, 6))
                if bal < avg * 6 and not (code in low and day < 35):
                    k += 1
                    q = Decimal(int(avg * random.uniform(18, 26)))
                    cost = p.standard_cost * Decimal(random.uniform(0.97, 1.03))
                    post_stock_move(p, self.loc[pl[k % len(pl)]], q, "IN_PURCHASE", "GRN", 0, unit_cost=int(cost),
                                    note="Nhập mua (lịch sử)", at=at - timedelta(hours=3))
                    bal += q
                out = Decimal(int(random.uniform(lo, hi) * (0.4 if day % 7 == 0 else 1)))
                out = min(out, bal)
                remain = out
                from apps.inventory.models import StockQuant
                for qt in StockQuant.objects.filter(product=p, on_hand__gt=0, location__loc_type="STORAGE").order_by("in_date_first"):
                    if remain <= 0:
                        break
                    t = min(qt.on_hand, remain)
                    post_stock_move(p, qt.location, -t, "OUT_SALE" if day % 5 else "OUT_INTERNAL", "GIN", 0,
                                    note="Xuất bán (lịch sử)", at=at)
                    remain -= t
                bal -= out
                if day <= 45:
                    ReplenishmentSnapshot.objects.create(product=p, date=timezone.localdate() - timedelta(days=day), on_hand=bal,
                                                         reserved=0, incoming=0, projected=bal, demand=out, d_avg=0, ss=0, rop=0)

    # ------------------------------------------------------------------ Mua hàng & Inbound
    def make_po(self, supplier, items, note=""):
        from apps.core.services import next_doc_no
        from apps.purchasing.models import PurchaseOrder, PurchaseOrderLine
        from apps.purchasing.services import latest_price
        po = PurchaseOrder.objects.create(code=next_doc_no("PO"), supplier=supplier, order_date=timezone.localdate(),
                                          pickup_date=timezone.localdate(), route=supplier.default_route, warehouse=self.wh,
                                          created_by=self.u_pur, note=note)
        for code, q in items:
            p = self.prod[code]
            PurchaseOrderLine.objects.create(po=po, product=p, qty=q, unit_price=latest_price(supplier, p))
        po.recalc_total()
        po.save()
        return po

    def approve_send(self, po, sent_days_ago=0):
        from apps.purchasing import services as ps
        ps.submit_po(po, self.u_pur)
        if po.status == "PENDING_APPROVAL":
            ps.approve_po(po, self.u_dir)
        ps.send_po(po, timezone.localdate())
        if sent_days_ago:
            po.sent_at = timezone.now() - timedelta(days=sent_days_ago)
            po.save(update_fields=["sent_at"])

    def trip(self, po, vehicle, driver, dep_offset_h, ratio=1):
        from apps.purchasing.services import create_inbound_trip
        qty = {l: (l.qty * Decimal(ratio)).quantize(Decimal(1)) for l in po.lines.all()}
        return create_inbound_trip(po, po.route, self.v[vehicle], self.d[driver],
                                   timezone.now() + timedelta(hours=dep_offset_h), qty)

    def run_milestones(self, trip, upto, user=None):
        from apps.purchasing.services import record_milestone
        for ms in trip.milestones.order_by("seq"):
            if ms.seq > upto:
                break
            trip.refresh_from_db()
            record_milestone(trip, ms, user or self.u_drv, photo=fake_image(f"{trip.code} - {ms.name}") if ms.photo_required else None,
                             note="Cập nhật từ di động")

    def purchasing_flows(self):
        from apps.purchasing import services as ps
        from apps.purchasing.models import GoodsReceipt, PurchaseRequisition, PurchaseRequisitionLine, TripMilestone, UnloadingSlot
        from apps.core.services import next_doc_no
        now = timezone.now()

        # 1) Lịch sử: các PO đã nhận đủ để có lead time / thời gian chờ hạ tải
        hist = [(self.s_xm, [("XM-PCB40", 300)], "51D-111.11", "TX03", 4.2, 25),
                (self.s_xm, [("XM-PC50", 280)], "51D-222.22", "TX03", 3.6, 18),
                (self.s_thep, [("THEP-D10", 900)], "61C-333.33", "TX04", 2.1, 40),
                (self.s_thep, [("THEP-D10", 600)], "61C-444.44", "TX04", 2.8, 12),
                (self.s_da, [("CAT-VANG", 12)], "61C-333.33", "TX04", 1.2, 35),
                (self.s_da, [("DA-1X2", 12)], "51D-111.11", "TX03", 0.9, 8)]
        self.hist_pos = []
        for sup, items, veh, drv, lt, wait in hist:
            po = self.make_po(sup, items, "Đơn lịch sử")
            self.approve_send(po, sent_days_ago=lt)
            t = self.trip(po, veh, drv, -10)
            self.run_milestones(t, 99, self.u_pur)
            t.refresh_from_db()
            slot = UnloadingSlot.objects.get(trip=t)
            gr = ps.check_in(slot, self.u_wh)
            ps.submit_grn(gr, self.u_wh)
            # dời mốc thời gian để báo cáo chờ hạ tải có ý nghĩa
            UnloadingSlot.objects.filter(pk=slot.pk).update(checked_in_at=t.arrived_at + timedelta(minutes=wait),
                                                            done_at=t.arrived_at + timedelta(minutes=wait + 50))
            self.hist_pos.append(po)

        # 2) PO lớn chờ Director duyệt (≥ 50 triệu)
        po = self.make_po(self.s_xm, [("XM-PCB40", 800), ("XM-PC50", 300)], "Dự trữ mùa xây dựng")
        ps.submit_po(po, self.u_pur)

        # 3) PO đã duyệt chưa gửi NCC
        po = self.make_po(self.s_ong, [("ONG-PVC90", 150)])
        ps.submit_po(po, self.u_pur)

        # 4) Chuyến chờ bốc (xuất phát sau 2h)
        po = self.make_po(self.s_xm, [("XM-PCB40", 500)])
        self.approve_send(po)
        self.trip(po, "51D-111.11", "TX03", 2)

        # 5) Chuyến đang chạy của tài xế demo (taixe) – đã qua 2 mốc
        po = self.make_po(self.s_thep, [("THEP-D16", 400), ("THEP-D10", 300)])
        self.approve_send(po)
        t = self.trip(po, "51C-123.45", "TX01", -2)
        self.run_milestones(t, 2)

        # 6) Chuyến trễ mốc (không cập nhật mốc từ lúc xuất phát)
        po = self.make_po(self.s_thep, [("THEP-D16", 300)])
        self.approve_send(po)
        self.trip(po, "61C-333.33", "TX04", -3)

        # 7) Chuyến đã tới cổng – chờ thủ kho check-in
        po = self.make_po(self.s_da, [("CAT-VANG", 10), ("DA-1X2", 6)])
        self.approve_send(po, sent_days_ago=1.5)
        t = self.trip(po, "51D-222.22", "TX03", -3)
        self.run_milestones(t, 99, self.u_pur)

        # 8) GRN lệch > 2% chờ Director duyệt
        po = self.make_po(self.s_ong, [("ONG-PVC90", 200)])
        self.approve_send(po, sent_days_ago=2.5)
        t = self.trip(po, "61C-444.44", "TX04", -5)
        self.run_milestones(t, 99, self.u_pur)
        t.refresh_from_db()
        gr = ps.check_in(UnloadingSlot.objects.get(trip=t), self.u_wh)
        line = gr.lines.first()
        line.qty_received = line.qty_expected - 12
        line.qty_damaged = 3
        line.save()
        ps.submit_grn(gr, self.u_wh)

        # 9) PR thủ công đang chờ lập PO
        pr = PurchaseRequisition.objects.create(code=next_doc_no("PR"), status="SUBMITTED", source="MANUAL",
                                                need_date=timezone.localdate() + timedelta(days=5), requested_by=self.u_pur,
                                                note="Bổ sung cho công trình Phú Mỹ")
        PurchaseRequisitionLine.objects.create(pr=pr, product=self.prod["DA-1X2"], qty=30, suggested_supplier=self.s_da)

    # ------------------------------------------------------------------ Bán hàng & giao hàng
    def make_so(self, customer, site, items, days=2, source="INTERNAL", weekly=False, user=None):
        from apps.core.services import next_doc_no
        from apps.sales.models import SalesOrder, SalesOrderLine
        from apps.sales.services import default_price
        so = SalesOrder.objects.create(code=next_doc_no("SO"), customer=customer, site=site,
                                       order_type="INTERNAL" if customer.customer_type == "INTERNAL" else "B2B",
                                       source=source, is_weekly_plan=weekly, order_date=timezone.localdate(),
                                       requested_date=timezone.localdate() + timedelta(days=days), created_by=user or self.u_dis)
        for code, q, *price in items:
            p = self.prod[code]
            SalesOrderLine.objects.create(so=so, product=p, qty=q, unit_price=price[0] if price else default_price(customer, p))
        so.recalc_total()
        so.save()
        return so

    def deliver(self, so, vehicle, driver, finish=True, partial=False):
        from apps.sales import services as ss
        trip = ss.create_trip(self.v[vehicle], self.d[driver], timezone.localdate())
        ss.add_order_to_trip(trip, so)
        pk = ss.release_picking(trip, self.u_dis)
        gi = ss.confirm_picking(pk, self.u_wh, {})
        ss.post_goods_issue(gi, self.u_wh)
        if finish:
            stop = trip.stops.first()
            delivered = {}
            for i, dl in enumerate(stop.lines.all()):
                delivered[dl.pk] = dl.qty_planned - (5 if partial and i == 0 else 0)
            ss.confirm_pod(stop, self.u_drv, delivered, "Anh Tuấn", photo=fake_image(f"POD {so.code}", (104, 178, 102)),
                           signature_data=fake_signature(), note="Khách nhận thiếu 5 do hàng móp" if partial else "")
        return trip

    def sales_flows(self):
        from apps.sales import services as ss
        from apps.accounting.models import Invoice, InvoiceLine
        from apps.core.services import next_doc_no
        today = timezone.localdate()

        # Đơn đã giao xong (để lập hóa đơn) – 1 đơn giao đủ, 1 đơn giao thiếu
        so = self.make_so(self.k1, self.site["HT01"], [("XM-PCB40", 120), ("THEP-D10", 60)], days=0)
        ss.submit_so(so, self.u_dis)
        self.deliver(so, "51C-123.45", "TX02")
        self.so_done = so
        so = self.make_so(self.k1, self.site["HT02"], [("CAT-VANG", 4), ("DA-1X2", 3)], days=-1)
        ss.submit_so(so, self.u_dis)
        self.deliver(so, "51C-123.45", "TX02", partial=False)
        so2 = self.make_so(self.k1, self.site["HT01"], [("THEP-D10", 50), ("XM-PC50", 40)], days=0)
        ss.submit_so(so2, self.u_dis)
        self.deliver(so2, "51C-678.90", "TX02", partial=True)

        # Đơn nội bộ đã giao
        so = self.make_so(self.k3, self.site["NB01"], [("XM-PCB40", 100)], days=0)
        ss.submit_so(so, self.u_dis)
        self.deliver(so, "51C-123.45", "TX02")

        # Chuyến đang giao của tài xế demo – chờ POD
        so = self.make_so(self.k1, self.site["HT02"], [("XM-PCB40", 80), ("THEP-D10", 40)], days=0)
        ss.submit_so(so, self.u_dis)
        self.deliver(so, "51C-678.90", "TX01", finish=False)

        # Đơn đã xác nhận – chờ xếp chuyến (kế hoạch giao)
        for site, items, d in [("HT01", [("XM-PCB40", 150), ("CAT-VANG", 5)], 1), ("HT02", [("THEP-D10", 80)], 2),
                               ("NB01", [("DA-1X2", 6), ("XM-PC50", 60)], 1)]:
            cust = self.site[site].customer
            so = self.make_so(cust, self.site[site], items, days=d)
            ss.submit_so(so, self.u_dis)

        # Nợ quá hạn cho KH-002 -> đơn mới bị chặn chờ duyệt
        inv = Invoice.objects.create(code=next_doc_no("INV"), customer=self.k2, status="POSTED",
                                     invoice_date=today - timedelta(days=75), due_date=today - timedelta(days=60),
                                     subtotal=80_000_000, vat_amount=6_400_000, total=86_400_000)
        InvoiceLine.objects.create(invoice=inv, product=self.prod["XM-PCB40"], qty=816, unit_price=98000, amount=80_000_000)
        so = self.make_so(self.k2, self.site["ND01"], [("XM-PCB40", 200), ("THEP-D10", 100, 120000)], days=3)
        ss.submit_so(so, self.u_dis)  # -> PENDING_APPROVAL (quá hạn + dưới giá sàn)

        # Thiếu hàng D16: 2 đơn tranh chấp -> đề xuất phân bổ cho Director
        avail = ss.available_qty(self.prod["THEP-D16"])
        so = self.make_so(self.k1, self.site["HT01"], [("THEP-D16", int(avail * Decimal("0.7")) + 40)], days=2)
        ss.submit_so(so, self.u_dis)
        so = self.make_so(self.k3, self.site["NB01"], [("THEP-D16", int(avail * Decimal("0.6")) + 30)], days=3)
        ss.submit_so(so, self.u_dis)

        # Đơn từ cổng khách hàng (nháp chờ điều phối xác nhận)
        self.make_so(self.k1, self.site["HT02"], [("ONG-PVC90", 20), ("GACH-DAC", 2000)], days=4, source="PORTAL", user=self.u_cus)
        self.make_so(self.k3, self.site["NB01"], [("XM-PCB40", 400), ("CAT-VANG", 20), ("DA-1X2", 18)], days=7,
                     source="PORTAL", weekly=True, user=self.u_cus)

    # ------------------------------------------------------------------ Kế toán
    def accounting_flows(self):
        from apps.accounting import services as acs
        from apps.accounting.models import Bill, Invoice
        from apps.sales.models import SalesReturn
        today = timezone.localdate()

        lines = list(acs.invoiceable_lines(self.k1))
        stops = {}
        for dl in lines:
            stops.setdefault(dl.stop_id, []).append(dl.pk)
        invs = []
        for ids in stops.values():
            inv = acs.create_invoice(self.k1, ids)
            acs.post_invoice(inv)
            invs.append(inv)
        # dời ngày 1 hóa đơn về quá khứ để có tuổi nợ
        if invs:
            Invoice.objects.filter(pk=invs[0].pk).update(invoice_date=today - timedelta(days=50), due_date=today - timedelta(days=20))
        acs.record_payment("IN", self.k1, invs[0].total // 2 if invs else 50_000_000, today, note="Thanh toán đợt 1", user=self.u_acc)

        # Hóa đơn mua: khớp / lệch giá / cước
        po = self.hist_pos[0]
        b = acs.create_goods_bill(self.s_xm, "0001234", today - timedelta(days=10),
                                  [(l, l.qty_received, l.unit_price) for l in po.lines.all()])
        acs.post_bill(b)
        Bill.objects.filter(pk=b.pk).update(due_date=today - timedelta(days=5))
        po = self.hist_pos[2]
        acs.create_goods_bill(self.s_thep, "TMD-8899", today - timedelta(days=2),
                              [(l, l.qty_received, l.unit_price + 3000) for l in po.lines.all()])  # lệch giá -> MISMATCH
        po = self.hist_pos[4]
        acs.create_goods_bill(self.s_da, "DBH-552", today, [(l, l.qty_received, l.unit_price) for l in po.lines.all()])
        trips = [t for p in self.hist_pos for t in p.trips.filter(carrier=self.c1)]
        if trips:
            fb = acs.create_freight_bill(self.c1, "MP-2026-77", today - timedelta(days=3), [(t, t.freight_amount) for t in trips])
            if fb.status == "MATCHED":
                acs.post_bill(fb)
                acs.record_payment("OUT", self.c1, fb.total // 3, today, note="Tạm ứng cước", user=self.u_acc)

    def misc(self):
        from apps.core.services import next_doc_no
        from apps.inventory.models import InventoryCount, InventoryCountLine, StockQuant
        from apps.inventory.services import run_replenishment
        from apps.purchasing.services import scan_inbound
        ic = InventoryCount.objects.create(code=next_doc_no("IC"), warehouse=self.wh, status="COUNTING",
                                           note="Kiểm kê định kỳ khu A", created_by=self.u_wh)
        for q in StockQuant.objects.filter(location__zone="A", location__loc_type="STORAGE", on_hand__gt=0):
            InventoryCountLine.objects.create(count=ic, product=q.product, location=q.location, book_qty=q.on_hand)
        res = run_replenishment()
        n = scan_inbound()
        self.stdout.write(f"SS/ROP: {res} · chuyến trễ mốc: {n}")
