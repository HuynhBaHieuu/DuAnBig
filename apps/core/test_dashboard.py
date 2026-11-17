"""Test sheet "Gà" STT 78–88 – Dashboard."""
import glob
import re
from datetime import timedelta
from decimal import Decimal as D

from django.conf import settings
from django.test import Client, SimpleTestCase
from django.utils import timezone

from apps.core.models import Alert, User
from apps.inventory import test_tracon_fixes as tf
from apps.purchasing.models import PurchaseOrder


class Stt78TemplateComments(SimpleTestCase):
    def test_no_multiline_hash_comments(self):
        """{# #} của Django chỉ dùng cho 1 dòng – ghi chú nhiều dòng bị in ra màn hình."""
        bad = []
        for p in glob.glob(str(settings.BASE_DIR / "templates" / "**" / "*.html"), recursive=True):
            s = open(p, encoding="utf-8").read()
            bad += [p for m in re.finditer(r"\{#(.*?)#\}", s, re.S) if "\n" in m.group(1)]
            bad += [p for line in s.splitlines() if "{#" in line and "#}" not in line]
        self.assertEqual(bad, [])


class DashboardTest(tf.Base):
    def client_for(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_roles_render_without_comment_leak_or_donut(self):
        admin = User.objects.create_user("admin", password="x", role="ADMIN")
        acc = User.objects.create_user("acc", password="x", role="ACCOUNTANT")
        for u in (admin, self.director, self.purchaser, self.dispatcher, self.warehouse_user, acc):
            r = self.client_for(u).get("/app/dashboard")
            self.assertEqual(r.status_code, 200, u.role)
            html = r.content.decode()
            self.assertNotIn("{#", html)                                  # STT 78
            self.assertNotIn("doughnut", html)                            # STT 88: không biểu đồ tròn / vành khuyên
            self.assertIn("Việc chờ bạn", html)                           # STT 80
            self.assertIn("KPI-1", html)                                  # STT 81

    def test_same_kpi_function_and_period_as_report(self):
        """STT 79: Dashboard và Báo cáo KPI cùng hàm, cùng kỳ mặc định (tháng này), ghi kỳ trên thẻ."""
        today = timezone.localdate()
        text = f"{today.replace(day=1):%d/%m}–{today:%d/%m/%Y}"
        d = self.client_for(self.director).get("/app/dashboard")
        r = self.client_for(self.director).get("/app/reports/")
        d_kpi = {k["code"]: k["value"] for k in d.context["kpis"]}
        r_kpi = {k["code"]: k["value"] for k in r.context["kpis"]}
        self.assertEqual(d_kpi, r_kpi)
        self.assertIn(text, d.content.decode())
        self.assertIn(text, r.content.decode())
        self.assertNotIn("KPI-5", d_kpi)

    def test_queue_hides_zero_and_links_filtered(self):
        """STT 80: số 0 thì ẩn; bấm mở danh sách đã lọc."""
        q = self.client_for(self.director).get("/app/dashboard").context["queue"]
        self.assertFalse(any(i["label"] == "PO chờ duyệt" for i in q))
        PurchaseOrder.objects.create(code="PO-Q", supplier=self.supplier, order_date=timezone.localdate(),
                                     warehouse=self.wh, status="PENDING_APPROVAL")
        q = self.client_for(self.director).get("/app/dashboard").context["queue"]
        item = next(i for i in q if i["label"] == "PO chờ duyệt")
        self.assertEqual((item["count"], item["url"]), (1, "/app/purchase/orders?status=PENDING_APPROVAL"))
        r = self.client_for(self.director).get(item["url"])
        self.assertEqual(len(r.context["page_obj"]), 1)

    def test_period_selector_applies_to_kpis(self):
        """STT 88: một bộ chọn kỳ cho cả trang; thẻ KPI link sang báo cáo kèm đúng kỳ."""
        c = self.client_for(self.director)
        r = c.get("/app/dashboard?period=prev_month")
        last = timezone.localdate().replace(day=1) - timedelta(days=1)
        self.assertEqual(r.context["d_to"], last)
        k1 = next(k for k in r.context["kpis"] if k["code"] == "KPI-1")
        self.assertIn(f"to={last:%Y-%m-%d}", k1["url"])
        self.assertEqual(len(k1["spark"]), 8)                             # STT 81: sparkline 8 tuần

    def test_stock_health_and_demand_blocks(self):
        self.adjust(-70)                                                    # tồn 30 ≤ ROP 60 -> rủi ro
        r = self.client_for(self.director).get("/app/dashboard")
        rows = r.context["stock"]["rows"]
        self.assertEqual(rows[0]["product"], self.product)                  # STT 82
        self.assertIn("/app/inventory/replenishment?product=", rows[0]["url"])
        dm = r.context["demand"]                                            # STT 84
        self.assertEqual(len(dm["ton"]["actual"]), len(dm["labels"]))
        self.assertTrue(dm["has_forecast"])

    def test_alert_groups(self):
        """STT 86: cảnh báo gộp theo loại, bấm xem chi tiết."""
        for i in range(3):
            Alert.objects.create(alert_type="PO_NOT_ACK", title=f"PO {i}", ref_type="po", ref_id=i)
        Alert.objects.create(alert_type="STALE_INBOUND", title="Chuyến kẹt", ref_type="inbound_trip", ref_id=1)
        r = self.client_for(self.director).get("/app/dashboard")
        g = {x["type"]: x["n"] for x in r.context["alert_groups"]}
        self.assertEqual((g["PO_NOT_ACK"], g["STALE_INBOUND"]), (3, 1))
        page = self.client_for(self.director).get("/app/alerts?type=PO_NOT_ACK")
        self.assertEqual(len(page.context["page_obj"]), 3)

    def test_po_not_ack_only_when_factory_can_confirm(self):
        """STT 86: không cảnh báo PO của NCC chưa có tài khoản nhà máy / PO gửi trước khi có tài khoản / đã có hàng về."""
        from apps.core.stale import scan_stale
        old = timezone.now() - timedelta(days=3)
        po = PurchaseOrder.objects.create(code="PO-ACK", supplier=self.supplier, order_date=timezone.localdate(),
                                          warehouse=self.wh, status="SENT", sent_at=old)
        scan_stale()
        self.assertFalse(Alert.objects.filter(alert_type="PO_NOT_ACK", is_resolved=False).exists())  # chưa có TK nhà máy
        u = User.objects.create_user("nm", password="x", role="SUPPLIER", supplier=self.supplier)
        scan_stale()
        self.assertFalse(Alert.objects.filter(alert_type="PO_NOT_ACK", is_resolved=False).exists())  # gửi trước khi có TK
        User.objects.filter(pk=u.pk).update(date_joined=old - timedelta(days=1))
        scan_stale()
        self.assertTrue(Alert.objects.filter(alert_type="PO_NOT_ACK", ref_id=po.pk, is_resolved=False).exists())

    def test_aging_rows_flag_blocked(self):
        """STT 83: mỗi khách 1 dòng theo tuổi nợ, đánh dấu khách quá hạn > 30 ngày."""
        from apps.accounting.models import Invoice
        today = timezone.localdate()
        Invoice.objects.create(code="INV-A", customer=self.customer, invoice_date=today - timedelta(days=80),
                               due_date=today - timedelta(days=45), subtotal=D(100), total=D(100), status="POSTED")
        Invoice.objects.create(code="INV-B", customer=self.customer, invoice_date=today, due_date=today + timedelta(days=5),
                               subtotal=D(50), total=D(50), status="POSTED")
        acc = User.objects.create_user("acc2", password="x", role="ACCOUNTANT")
        ag = self.client_for(acc).get("/app/dashboard").context["aging"]
        row = ag["rows"][0]
        self.assertEqual((row["bands"]["current"], row["bands"]["d60"], row["blocked"]), (50.0, 100.0, True))

    def test_template_layout_and_hero_image(self):
        """Dashboard theo mẫu: hero ảnh doanh nghiệp (chỉ Giám đốc/Admin đổi), đội xe, đơn mới nhất, doanh thu."""
        import io
        from django.core.files.storage import default_storage
        from PIL import Image
        from apps.core.models import SystemParam
        c = self.client_for(self.director)
        html = c.get("/app/dashboard").content.decode()
        for cls in ("dz-hero", "dz-volume", "dz-fleet", "dz-sales"):
            self.assertIn(cls, html)
        self.assertNotIn("has-photo", html)                                 # chưa có ảnh -> hình minh họa
        b = io.BytesIO(); Image.new("RGB", (40, 20), (230, 95, 43)).save(b, "PNG"); b.seek(0); b.name = "Ảnh kho Đà Nẵng (1).png"
        self.assertEqual(c.post("/app/dashboard/hero", {"image": b}).status_code, 302)
        name = SystemParam.objects.get(key="DASHBOARD_HERO_IMAGE").value
        self.assertTrue(name.isascii() and " " not in name, name)       # Supabase từ chối key có dấu / khoảng trắng
        try:
            self.assertIn("has-photo", c.get("/app/dashboard").content.decode())
            self.assertEqual(self.client_for(self.purchaser).post("/app/dashboard/hero", {"op": "reset"}).status_code, 403)
            c.post("/app/dashboard/hero", {"op": "reset"})
            self.assertFalse(SystemParam.objects.filter(key="DASHBOARD_HERO_IMAGE").exists())
        finally:
            if default_storage.exists(name):
                default_storage.delete(name)

    def test_hero_storage_error_shows_message_not_500(self):
        """Kho ảnh từ chối (vd. Supabase "Invalid key") -> báo lỗi trên Dashboard, không lỗi 500."""
        import io
        from unittest import mock
        from PIL import Image
        from apps.core.services import BusinessError
        b = io.BytesIO(); Image.new("RGB", (40, 20)).save(b, "PNG"); b.seek(0); b.name = "x.png"
        with mock.patch("django.core.files.storage.default_storage.save", side_effect=BusinessError("Không lưu được ảnh")):
            r = self.client_for(self.director).post("/app/dashboard/hero", {"image": b}, follow=True)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Không lưu được ảnh")
