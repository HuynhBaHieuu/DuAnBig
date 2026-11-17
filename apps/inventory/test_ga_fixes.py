"""Test các lỗi trong sheet "Gà" của file test hệ thống (STT 18–22)."""
from datetime import timedelta
from decimal import Decimal as D

from django.test import Client, TestCase
from django.utils import timezone

from apps.core.services import BusinessError
from apps.masterdata import models as m
from apps.purchasing import services as ps
from apps.purchasing.models import UnloadingSlot

from . import test_tracon_fixes as tf
from .services import stock_summary
from .views import _bar

TODAY = timezone.localdate


class Stt18PartialReserveForecast(tf.Base):
    """SO đặt 230, giữ 195, thiếu 35 -> dự báo −35 (không phải 0), PR = 240 t (không phải 210 t)."""

    def setUp(self):
        self.build(stock=195)

    def test_partial_reserve_negative_forecast_and_pr(self):
        so = self.so(230, days=10)                     # cần giao xa hơn hôm nay + LT (3 ngày) vẫn phải tính
        line = so.lines.get()
        self.assertEqual((line.reserved_qty, line.backorder_qty), (D(195), D(35)))
        x = self.metrics()
        self.assertEqual((x["reserved"], x["demand_unreserved"], x["projected"]), (D(195), D(35), D(-35)))
        pr = self.auto_prs().get()
        # max(Q 140; ROP 60 + Q 140 − (−35)) = 235 -> làm tròn xe 30 t = 240 t
        self.assertEqual(pr.lines.get().qty, D(240))
        self.assertEqual(pr.snapshot["projected"], -35.0)

    def test_receipt_reserves_backorder(self):
        so = self.so(230, days=10)
        self.receive_direct(30)                        # hàng về 30 t -> giữ tiếp cho phần thiếu
        line = so.lines.get()
        self.assertEqual((line.reserved_qty, line.backorder_qty), (D(225), D(5)))
        self.assertEqual(self.metrics()["projected"], D(-5))

    def test_delivered_part_not_subtracted_twice(self):
        """reserved_qty là lũy kế (gồm phần đã giao): giữ 45/50, đã giao 45 -> vẫn thiếu 5."""
        so = self.so(50)
        line = so.lines.get()
        line.reserved_qty, line.delivered_qty = D(45), D(45)
        line.save()
        line.refresh_from_db()
        self.assertEqual(line.backorder_qty, D(5))
        self.assertEqual(self.metrics()["demand_unreserved"], D(5))


class Stt20PodQtyInput(tf.Base):
    def test_qty_input_filter(self):
        from apps.core.templatetags.tracon import qty_input
        self.assertEqual(qty_input(D("15.000")), "15")
        self.assertEqual(qty_input(D("150.000")), "150")
        self.assertEqual(qty_input(D("12.500")), "12.5")
        self.assertEqual(qty_input(D("0.125")), "0.125")


class Stt21BarColorByProjected(tf.Base):
    def test_bar_tone_uses_projected(self):
        s = {"projected": D(180), "available": D(0), "ss": D(20), "rop": D(60), "level": "STOCKOUT"}
        self.assertEqual(_bar(s)["bar"]["tone"], "success")      # dự báo 180 > ROP 60 -> xanh dù khả dụng 0
        self.assertEqual(_bar({**s, "projected": D(50)})["bar"]["tone"], "warn")
        self.assertEqual(_bar({**s, "projected": D(20)})["bar"]["tone"], "danger")
        self.assertEqual(_bar({**s, "projected": D(-35)})["bar"]["tone"], "danger")

    def test_stock_page_renders(self):
        self.so(50)
        c = Client()
        c.force_login(self.warehouse_user)
        r = c.get("/app/inventory/stock")
        self.assertEqual(r.status_code, 200)
        self.assertIn("background:var(--", r.content.decode())




# ======================================================================= Sheet "Gà" STT 23–77 (đợt 2)
class Stt74GateCheckin(tf.SlotBase):
    """Xe tới cổng -> vào bến trống đầu tiên ngay (không chờ giờ lịch); mọi bến bận -> chờ; muộn -> bắt lý do."""

    def setUp(self):
        super().setUp()
        self.dock = m.Dock.objects.get(code="BEN-1")
        self.dock2 = m.Dock.objects.create(warehouse=self.wh, code="BEN-2", name="Bến 2")

    def make_slot(self, start, minutes=75, dock=None, status="CONFIRMED"):
        t = self.trip(start)
        t.status = "IN_TRANSIT"
        t.save()
        return UnloadingSlot.objects.create(trip=t, dock=dock or self.dock, start_at=start,
                                            end_at=start + timedelta(minutes=minutes), status=status)

    def test_early_arrival_goes_in_now(self):
        slot = self.make_slot(timezone.now() + timedelta(hours=4))        # lịch 4 giờ nữa, bến trống
        ps.gate_checkin(slot.trip, self.warehouse_user)
        slot.refresh_from_db()
        self.assertEqual(slot.status, "CHECKED_IN")
        self.assertLess(abs((slot.start_at - timezone.now()).total_seconds()), 120)

    def test_own_dock_busy_uses_first_free_dock(self):
        self.make_slot(timezone.now() - timedelta(minutes=10), status="CHECKED_IN")   # BEN-1 đang hạ tải
        slot = self.make_slot(timezone.now() + timedelta(hours=1))
        ps.gate_checkin(slot.trip, self.warehouse_user)
        slot.refresh_from_db()
        self.assertEqual((slot.status, slot.dock_id), ("CHECKED_IN", self.dock2.pk))

    def test_all_docks_busy_must_wait(self):
        self.make_slot(timezone.now() - timedelta(minutes=10), status="CHECKED_IN")
        self.make_slot(timezone.now() - timedelta(minutes=10), dock=self.dock2, status="CHECKED_IN")
        slot = self.make_slot(timezone.now() + timedelta(hours=1))
        with self.assertRaisesMessage(BusinessError, "Tất cả bến đang bận"):
            ps.gate_checkin(slot.trip, self.warehouse_user)

    def test_stt89_busy_gate_records_arrival_and_wait(self):
        """STT 89: bến bận vẫn ghi giờ tới cổng (xe "Chờ ngoài cổng"); check-in sau không ghi đè -> KPI-4 > 0."""
        from apps.purchasing.models import InboundTrip
        from apps.reports.kpi import wait_data
        b1 = self.make_slot(timezone.now() - timedelta(minutes=10), status="CHECKED_IN")
        b2 = self.make_slot(timezone.now() - timedelta(minutes=10), dock=self.dock2, status="CHECKED_IN")
        slot = self.make_slot(timezone.now() + timedelta(minutes=30))
        c = Client()
        c.force_login(self.warehouse_user)
        r = c.post(f"/app/warehouse/slots/{slot.pk}", {"op": "checkin"}, follow=True)
        self.assertContains(r, "chờ ngoài cổng")
        t = InboundTrip.objects.get(pk=slot.trip_id)
        self.assertEqual((t.status, t.get_status_display()), ("ARRIVED", "Chờ ngoài cổng"))
        self.assertIsNotNone(t.arrived_at)
        arrived = t.arrived_at - timedelta(minutes=25)                   # xe đã chờ 25 phút
        InboundTrip.objects.filter(pk=t.pk).update(arrived_at=arrived)
        UnloadingSlot.objects.filter(pk__in=[b1.pk, b2.pk]).update(status="DONE", end_at=timezone.now() - timedelta(minutes=1))
        ps.gate_checkin(t, self.warehouse_user)                          # có bến trống: chỉ ghi checked_in_at
        t.refresh_from_db()
        slot.refresh_from_db()
        self.assertEqual(t.arrived_at, arrived)
        self.assertEqual(slot.status, "CHECKED_IN")
        self.assertGreaterEqual(slot.wait_minutes, 25)
        rows = wait_data(TODAY(), TODAY())["rows"]
        self.assertIn(slot.wait_minutes, [x["wait"] for x in rows])

    def test_late_needs_reason_and_trip_without_slot(self):
        slot = self.make_slot(timezone.now() - timedelta(hours=4))
        with self.assertRaisesMessage(BusinessError, "lý do"):
            ps.gate_checkin(slot.trip, self.warehouse_user)
        ps.gate_checkin(slot.trip, self.warehouse_user, reason="Kẹt xe QL1A")
        t = self.trip(timezone.now() + timedelta(hours=1))      # chuyến chưa có lịch: tạo lịch tại bến trống
        t.status = "IN_TRANSIT"
        t.save()
        c = Client()
        c.force_login(self.warehouse_user)
        c.post(f"/app/warehouse/gate/{t.pk}")
        self.assertEqual(UnloadingSlot.objects.get(trip=t).status, "CHECKED_IN")

    def test_slot_only_on_near_milestone(self):
        t = ps.create_inbound_trip(self.po, self.route, self.vehicle, self.driver, timezone.now(), {self.pol: 30})
        ms = list(t.milestones.order_by("seq"))
        self.assertEqual([x.code for x in ms], ["DEPART", "NEAR"])                 # STT 73: 2 mốc chuẩn
        self.assertTrue(ms[0].photo_required)
        with self.captureOnCommitCallbacks(execute=True):
            ps.record_milestone(t, ms[0], self.taixe, photo=_img())
        self.assertFalse(UnloadingSlot.objects.filter(trip=t).exists())           # STT 74: chưa xếp bến
        with self.captureOnCommitCallbacks(execute=True):
            ps.record_milestone(t, ms[1], self.taixe)
        self.assertTrue(UnloadingSlot.objects.filter(trip=t).exists())            # báo "còn 2 giờ" -> xếp bến


def _img():
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile("can.png", b"\x89PNG\r\n\x1a\n" + b"0" * 200, content_type="image/png")


class Stt73EtaIncident(tf.SlotBase):
    def test_incident_delay_not_added_twice(self):
        t = ps.create_inbound_trip(self.po, self.route, self.vehicle, self.driver, timezone.now(), {self.pol: 30})
        depart, near = list(t.milestones.order_by("seq"))
        with self.captureOnCommitCallbacks(execute=True):
            ps.record_milestone(t, depart, self.taixe, photo=_img())
        ps.report_incident(t, self.taixe, "TRAFFIC", "Kẹt cầu", 90)
        t.refresh_from_db()
        depart.refresh_from_db()
        std = timedelta(hours=float(self.route.std_hours))
        self.assertLess(abs((t.eta - (depart.actual_at + std + timedelta(minutes=90))).total_seconds()), 60)
        with self.captureOnCommitCallbacks(execute=True):
            ps.record_milestone(t, near, self.taixe)
        t.refresh_from_db()
        near.refresh_from_db()
        # từ mốc mới: ETA = thực tế mốc + 2 giờ, KHÔNG cộng 90' của sự cố thêm lần nữa
        self.assertLess(abs((t.eta - (near.actual_at + timedelta(minutes=120))).total_seconds()), 60)

    def test_duplicate_milestone_is_idempotent(self):
        t = ps.create_inbound_trip(self.po, self.route, self.vehicle, self.driver, timezone.now(), {self.pol: 30})
        depart = t.milestones.order_by("seq").first()
        _, created = ps.record_milestone(t, depart, self.taixe, photo=_img())
        _, again = ps.record_milestone(t, depart, self.taixe, photo=_img())
        self.assertEqual((created, again), (True, False))


class Stt75GrnPostsNow(tf.Base):
    def test_variance_posts_immediately_and_claims_need_director(self):
        from apps.purchasing.models import GoodsReceipt, GoodsReceiptLine, PurchaseOrder, PurchaseOrderLine
        po = PurchaseOrder.objects.create(code="PO-X", supplier=self.supplier, order_date=TODAY(), warehouse=self.wh,
                                          status="SENT")
        pol = PurchaseOrderLine.objects.create(po=po, product=self.product, qty=25, unit_price=tf.PRICE)
        gr = GoodsReceipt.objects.create(code="GRN-X", po=po, warehouse=self.wh, received_at=timezone.now())
        GoodsReceiptLine.objects.create(gr=gr, po_line=pol, product=self.product, location=self.loc,
                                        qty_expected=25, qty_received=24, qty_damaged=1, unit_cost=tf.PRICE)
        self.act(ps.submit_grn, gr, self.warehouse_user)
        gr.refresh_from_db()
        self.assertEqual(gr.status, "POSTED")                                     # không treo chờ duyệt
        claims = {c.claim_type: c for c in gr.claims.all()}
        # STT 58: thiếu = 25 − 24 = 1 (nhà xe), hỏng = 1 (NCC) – không đòi trùng
        self.assertEqual((claims["SHORTAGE"].qty, claims["DAMAGE"].qty), (D(1), D(1)))
        self.assertEqual({c.status for c in claims.values()}, {"OPEN"})
        pol.refresh_from_db()
        self.assertEqual(pol.qty_open, D(0))                                      # STT 61: không còn "đang về 2 t"
        with self.assertRaisesMessage(BusinessError, "Giám đốc"):
            ps.claim_action(claims["SHORTAGE"], self.purchaser, "approve")
        ps.claim_action(claims["SHORTAGE"], self.director, "approve", amount=1_500_000)
        ps.claim_action(claims["DAMAGE"], self.director, "backfill")              # NCC giao bù -> PO mở lại 1 t
        pol.refresh_from_db()
        self.assertEqual(pol.qty_open, D(1))


class Stt76CountAutoPost(tf.Base):
    def test_small_variance_auto_posts(self):
        from .models import InventoryCount
        c = Client()
        c.force_login(self.warehouse_user)
        c.post("/app/inventory/counts", {"warehouse": self.wh.pk})
        ic = InventoryCount.objects.get()
        line = ic.lines.get()
        with self.captureOnCommitCallbacks(execute=True):
            c.post(f"/app/inventory/counts/{ic.pk}", {"op": "count", f"c_{line.pk}": "100"})
        ic.refresh_from_db()
        self.assertEqual(ic.status, "APPROVED")                                   # lệch 0 < ngưỡng -> tự ghi sổ


class Stt77PortalAutoConfirm(tf.Base):
    def setUp(self):
        super().setUp()
        from apps.core.models import User
        self.cust_user = User.objects.create_user("kh", password="x", role="CUSTOMER", customer=self.customer)

    def post_order(self, qty):
        c = Client()
        c.force_login(self.cust_user)
        with self.captureOnCommitCallbacks(execute=True):
            c.post("/portal/order", {"site": self.site.pk, "product": [self.product.pk], "qty": [str(qty)],
                                     "date": [TODAY().isoformat()]})
        from apps.sales.models import SalesOrder
        return SalesOrder.objects.order_by("-id").first()

    def test_valid_order_auto_confirmed_short_order_waits(self):
        so = self.post_order(10)
        self.assertEqual((so.status, so.lines.get().reserved_qty), ("CONFIRMED", D(10)))
        self.assertEqual(so.customer_status[1], "Đã xác nhận")
        big = self.post_order(500)
        self.assertEqual(big.status, "DRAFT")                                     # thiếu hàng -> chờ điều phối
        self.assertEqual(big.customer_status[1], "Đã gửi")

    def test_past_date_rejected(self):
        c = Client()
        c.force_login(self.cust_user)
        c.post("/portal/order", {"site": self.site.pk, "product": [self.product.pk], "qty": ["5"],
                                 "date": [(TODAY() - timedelta(days=3)).isoformat()]})
        from apps.sales.models import SalesOrder
        self.assertFalse(SalesOrder.objects.exists())                             # STT 55

    def test_customer_cancel_unconfirmed_only(self):
        big = self.post_order(500)
        c = Client()
        c.force_login(self.cust_user)
        c.post(f"/portal/orders/{big.pk}", {"op": "cancel", "reason": "đặt nhầm"})
        big.refresh_from_db()
        self.assertEqual(big.status, "CANCELLED")                                 # STT 57


class Stt25AllocationContention(tf.Base):
    def test_high_priority_after_low_holder_creates_al(self):
        from apps.sales.models import Allocation
        self.adjust(-70)                                                          # tồn 30
        low = self.so(25, days=5)                                                 # ưu tiên thấp giữ 25
        hi_site = m.Site.objects.create(customer=self.customer, code="CT-HI", name="Ưu tiên cao", priority=1)
        high = self.so(25, days=1, site=hi_site)                                  # cần sớm hơn: giữ 5, thiếu 20
        al = Allocation.objects.get(status="PROPOSED")
        lines = {l.so_line.so_id: l for l in al.lines.all()}
        self.assertEqual((lines[high.pk].proposed_qty, lines[low.pk].held_qty), (D(20), D(25)))
        from apps.sales import services as ss
        self.act(ss.approve_allocation, al, self.director, {l.pk: l.proposed_qty for l in al.lines.all()})
        self.assertEqual((high.lines.get().reserved_qty, low.lines.get().reserved_qty), (D(25), D(5)))


class Stt33And43Permissions(tf.Base):
    def test_dispatcher_cannot_change_credit_limit(self):
        c = Client()
        c.force_login(self.dispatcher)
        self.customer.credit_limit = 100
        self.customer.save()
        data = {"code": "KH", "name": "Khách", "customer_type": "B2B", "credit_limit": "999999999",
                "payment_terms_days": "30"}
        c.post(f"/app/master/customers/{self.customer.pk}", data)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.credit_limit, 100)

    def test_purchaser_price_pending_until_director(self):
        c = Client()
        c.force_login(self.purchaser)
        c.post("/app/master/supplier-prices/new", {"supplier": self.supplier.pk, "product": self.product.pk,
                                                   "price": "1", "min_qty": "0", "valid_from": TODAY().isoformat()})
        sp = m.SupplierPrice.objects.get()
        self.assertEqual(sp.status, "PENDING")
        self.assertEqual(ps.latest_price(self.supplier, self.product), self.product.standard_cost)


class Stt34AdjustmentApproval(tf.Base):
    def test_warehouse_large_adjustment_pending(self):
        from .adjustments import create_adjustment, post_adjustment
        adj = create_adjustment(self.warehouse_user, "Phát hiện thừa khi đếm", [(self.product, self.loc, D(10), None)])
        self.assertEqual(adj.status, "PENDING_APPROVAL")                          # 10 t × 1,6 tr ≥ 5 tr
        self.assertEqual(self.metrics()["on_hand"], D(100))
        with self.captureOnCommitCallbacks(execute=True):
            post_adjustment(adj, self.director)
        self.assertEqual(self.metrics()["on_hand"], D(110))


class Stt63AmountWords(tf.Base):
    def test_words(self):
        from apps.core.templatetags.tracon import vnd_words
        self.assertEqual(vnd_words(1_250_000), "Một triệu hai trăm năm mươi nghìn đồng")
        self.assertEqual(vnd_words(105_021), "Một trăm lẻ năm nghìn không trăm hai mươi mốt đồng")


class Stt64FactoryIsolation(tf.Base):
    def test_other_supplier_po_404(self):
        from apps.core.models import User
        from apps.purchasing.models import PurchaseOrder
        other = m.Supplier.objects.create(code="NCC-B", name="NCC B")
        po_a = PurchaseOrder.objects.create(code="PO-A", supplier=self.supplier, order_date=TODAY(), warehouse=self.wh,
                                            status="SENT", sent_at=timezone.now())
        po_b = PurchaseOrder.objects.create(code="PO-B", supplier=other, order_date=TODAY(), warehouse=self.wh,
                                            status="SENT", sent_at=timezone.now())
        u = User.objects.create_user("nhamay", password="x", role="SUPPLIER", supplier=self.supplier)
        c = Client()
        c.force_login(u)
        self.assertEqual(c.get(f"/factory/orders/{po_b.pk}").status_code, 404)
        self.assertEqual(c.get("/app/dashboard").status_code, 302)                # không vào được khu nội bộ
        c.post(f"/factory/orders/{po_a.pk}", {"note": "giao trễ 1 ngày"})
        po_a.refresh_from_db()
        self.assertEqual((po_a.supplier_ack_by, po_a.supplier_ack_note), (u, "giao trễ 1 ngày"))


class Stt90SeedDemo(TestCase):
    def test_seed_demo_runs_with_two_milestones(self):
        """STT 90: seed_demo chạy được theo quy trình 2 mốc và có số liệu chờ hạ tải (KPI-4)."""
        from io import StringIO
        from django.core.management import call_command
        from apps.purchasing.models import InboundTrip
        from apps.reports.kpi import wait_data
        call_command("seed_demo", stdout=StringIO())
        waits = [x["wait"] for x in wait_data(TODAY() - timedelta(days=30), TODAY())["rows"]]
        self.assertTrue(any(w > 0 for w in waits))
        self.assertTrue(InboundTrip.objects.filter(status="ARRIVED", arrived_at__isnull=False).exists())
