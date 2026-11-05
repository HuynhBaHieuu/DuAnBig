from decimal import Decimal as D

from django.test import SimpleTestCase

from apps.core.storage import ascii_filename
from apps.core.utils import fold
from apps.masterdata.models import Product

from .models import ReplenishmentParam
from .services import outlier_cut


class ReplenishmentMathTest(SimpleTestCase):
    def rp(self, **kw):
        base = dict(product=Product(code="T", unit_weight_kg=D(1000)), ss=D(20), rop=D(60), d_avg=D(0), review_days=7)
        base.update(kw)
        return ReplenishmentParam(**base)

    def test_pr_qty_spec_case(self):
        # Test case của QA: tồn dự báo 55, ROP 60, Q 140 -> 145 -> làm tròn theo xe 30 t = 150 (5 chuyến)
        rp = self.rp(manual_q=D(140))
        self.assertEqual(rp.pr_qty(D(55)), D(145))
        rp.round_by_truck, rp.truck_ton = True, D(30)
        self.assertEqual(rp.pr_qty(D(55)), D(150))

    def test_q_default_and_moq(self):
        rp = self.rp(d_avg=D("3.2"), moq=D(10))
        self.assertEqual(rp.order_q, D(23))          # max(10; ceil(3.2 × 7))
        rp.moq = D(100)
        self.assertEqual(rp.order_q, D(100))

    def test_fresh_param_defaults(self):
        # tham số vừa get_or_create: các trường mặc định còn là int, không được lỗi
        rp = ReplenishmentParam(product=Product(code="N", unit_weight_kg=D(1000)))
        self.assertEqual(rp.order_q, D(0))
        self.assertEqual(rp.pr_qty(D(0)), D(0))

    def test_manual_lock(self):
        rp = self.rp(manual_ss=D(50), manual_rop=D(90))
        self.assertEqual((rp.eff_ss, rp.eff_rop), (D(20), D(60)))   # chưa khóa -> dùng số tự tính
        rp.manual_lock = True
        self.assertEqual((rp.eff_ss, rp.eff_rop), (D(50), D(90)))

    def test_truck_qty_in_bags(self):
        rp = self.rp(product=Product(code="XM", unit_weight_kg=D(50)), truck_ton=D(30))
        self.assertEqual(rp.truck_qty, D(600))

    def test_outlier_cut(self):
        self.assertIsNone(outlier_cut([10, 20, 30]))
        cut = outlier_cut([40, 60, 55, 80, 70, 65, 50, 4119, 6494])
        self.assertTrue(80 < cut < 4119)
        self.assertGreater(outlier_cut([10, 10, 10, 12]), 12)  # IQR = 0 không loại nhầm giá trị thường


class TextTest(SimpleTestCase):
    def test_fold(self):
        self.assertIn(fold("ly"), fold("Lý Thị Kế"))
        self.assertEqual(fold("Đặng"), "dang")

    def test_ascii_filename(self):
        self.assertEqual(ascii_filename("Ảnh chụp màn hình (1).PNG"), "anh-chup-man-hinh-1.png")
