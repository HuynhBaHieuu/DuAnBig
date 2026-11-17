"""Test sheet "Gà" STT 91–118: định dạng số, PR hàng đếm, báo cáo, mã mô phỏng, nhật ký, tham số, cổng KH."""
from datetime import timedelta
from decimal import Decimal as D

from django.template import Context, Template
from django.test import Client, SimpleTestCase
from django.utils import timezone

from apps.core import fmt
from apps.core.audit_view import changes_list
from apps.core.models import AuditLog, User
from apps.inventory import test_tracon_fixes as tf
from apps.inventory.replenishment import EffectiveParams, calc_order_qty


class NumberFormat(SimpleTestCase):
    def test_stt92_qty_by_unit(self):
        self.assertEqual(fmt.qty(D("431.596"), "CAY"), "432")
        self.assertEqual(fmt.qty(D("566.487"), "BAO"), "566")
        self.assertEqual(fmt.qty(D("30.000"), "TAN"), "30")
        self.assertEqual(fmt.qty(D("6.900"), "TAN"), "6,9")
        self.assertEqual(fmt.qty(D("1234.5"), "TAN"), "1.234,5")
        self.assertEqual(fmt.qty_unit(D("15.000"), "BAO"), "15 bao")

    def test_stt93_decimal_comma_everywhere(self):
        html = Template("{{ a|num:1 }}|{{ b|pct }}|{{ c|num:-2 }}|{{ d|qty:'CAY' }}|{{ e|num:2 }}").render(
            Context({"a": 12.6, "b": 12.6, "c": 1.25, "d": D("373.396"), "e": 604.8}))
        self.assertEqual(html, "12,6|12,6%|1,25|373|604,80")

    def test_stt95_count_units_round_up(self):
        p = EffectiveParams(SS=D(10), ROP=D("94.596"), Q=D("300.4"), truck_qty=D(0), round_to_truck=False, LT=D(1),
                            whole=True)
        raw, qty = calc_order_qty(p, D("21.6"))
        self.assertEqual(qty, D(374))
        self.assertEqual(qty, qty.to_integral_value())
        p.whole = False
        self.assertNotEqual(calc_order_qty(p, D("21.6"))[1] % 1, 0)

    def test_stt109_audit_changes_readable(self):
        rows = changes_list({"Full name": ["—", "Nguyễn Văn An"], "forecast_data": {"weeks": [1] * 200},
                             "Ghi chú": "x" * 500})
        self.assertEqual(rows[0], ("Họ tên", "—", "Nguyễn Văn An"))
        self.assertEqual(rows[1][2], "(dữ liệu tính toán)")
        self.assertEqual(rows[2][2], "(dữ liệu tính toán)")


class Ga91To118(tf.Base):
    def client_for(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_stt94_alert_numbers_use_units(self):
        self.adjust(-70)                                                  # tồn 30 tấn ≤ ROP -> PR tự động
        self.so(15)
        from apps.core.models import Alert
        texts = " ".join(f"{a.title} {a.message}" for a in Alert.objects.all())
        self.assertNotRegex(texts, r"\d\.\d{3}\b(?! ₫)")               # không còn "15.000" / "58.200000000000"

    def test_stt99_101_103_reports_share_period_and_sort(self):
        c = self.client_for(self.director)
        r = c.get("/app/reports/unloading")
        self.assertEqual(r.context["period"], "month")                  # STT 101: mặc định Tháng này ở mọi tab
        self.assertIn("period=month", r.context["period_qs"])
        self.assertContains(r, "/app/reports/otif?period=month")       # chuyển tab giữ kỳ
        r = c.get("/app/reports/otif?period=30d")
        labels = r.context["chart"]["labels"]
        self.assertEqual(labels, sorted(labels, key=lambda s: (s[3:], s[:2])))  # STT 99: ngày tăng dần
        self.assertIn("page_obj", r.context)                            # STT 103: chia trang

    def test_stt104_118_rop_tab_removed(self):
        r = self.client_for(self.director).get("/app/reports/rop")
        self.assertRedirects(r, "/app/inventory/replenishment", fetch_redirect_response=False)
        self.assertNotContains(self.client_for(self.director).get("/app/reports/"), "Dashboard ROP")

    def test_stt102_simulated_product_excluded(self):
        from apps.inventory.models import SimulatedDemand
        SimulatedDemand.objects.create(product=self.product, week_start=timezone.localdate() - timedelta(days=7), qty=5)
        self.adjust(-100)                                                 # hết hàng
        r = self.client_for(self.director).get("/app/inventory/stock")
        self.assertEqual(r.context["counts"]["STOCKOUT"], 0)
        self.assertContains(r, "Dữ liệu mô phỏng")
        r = self.client_for(self.director).get("/app/inventory/stock?level=low")
        self.assertEqual(r.context["rows"], [])

    def test_stt106_108_117_texts(self):
        c = self.client_for(self.director)
        self.assertNotContains(c.get("/app/inventory/stock"), "MH-10")
        kpis = [k["code"] for k in c.get("/app/reports/").context["kpis"]]
        self.assertEqual(kpis, [f"KPI-{i}" for i in range(1, 7)])
        admin = User.objects.create_user("admin", password="x", role="ADMIN")
        r = self.client_for(admin).get("/app/admin/params")
        self.assertContains(r, "50.000.000")
        self.assertContains(r, "Đơn mua (PO) từ mức này trở lên cần Giám đốc duyệt")
        self.assertNotContains(r, "SLOT_PROPOSE_HOURS")

    def test_stt109_audit_hides_system_tables(self):
        AuditLog.objects.create(action="UPDATE", model="inventory.StockQuant", object_repr="StockQuant object (3)")
        AuditLog.objects.create(action="UPDATE", model="sales.SalesOrder", object_repr="SO-1", changes={"Trạng thái": ["Nháp", "Đã xác nhận"]})
        admin = User.objects.create_user("admin", password="x", role="ADMIN")
        r = self.client_for(admin).get("/app/admin/audit")
        html = r.content.decode()
        self.assertNotIn("StockQuant", html)
        self.assertIn("Đơn bán", html)
        self.assertIn("Nháp → <b>Đã xác nhận</b>", html)
        self.assertIn("Sửa", html)

    def test_stt91_portal_mobile_menu(self):
        from apps.masterdata.models import UserSiteAccess
        kh = User.objects.create_user("kh", password="x", role="CUSTOMER", customer=self.customer)
        UserSiteAccess.objects.create(user=kh, site=self.site)
        c = self.client_for(kh)
        for url in ("/portal/", "/portal/order", "/portal/orders", "/portal/documents", "/portal/debts"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertContains(r, "portal-burger")
            self.assertNotContains(r, "MH-9")
        self.assertContains(c.get("/portal/order"), 'data-label="Vật tư"')
