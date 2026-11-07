"""Bộ test móc xích QT1–QT2 theo test/logic.docx (mục 8).

Mọi test chạy đúng đường code thật: service / view thật (Client.post), ghi DB thật, không giả lập riêng hàm tính.
transaction.on_commit không chạy trong TestCase thường -> mỗi thao tác nghiệp vụ bọc trong
self.captureOnCommitCallbacks(execute=True) (helper `act`). Test đồng thời T3 dùng TransactionTestCase.
"""
import threading
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

from django.db import connection, transaction
from django.test import Client, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from apps.core.models import Alert, User
from apps.core.services import BusinessError, next_doc_no
from apps.masterdata import models as m
from apps.purchasing import services as ps
from apps.purchasing.models import (GoodsReceipt, GoodsReceiptLine, InboundTrip, PurchaseOrder, PurchaseOrderLine,
                                    PurchaseRequisition, UnloadingSlot)
from apps.sales import services as ss
from apps.sales.models import SalesOrder, SalesOrderLine

from .models import InventoryCount, InventoryCountLine, ReplenishmentAlert, ReplenishmentCheckLog, ReplenishmentParam
from .replenishment import (EffectiveParams, Trigger, calc_order_qty, check_replenishment, fire_checks, safe_check,
                            stock_metrics)
from .services import post_stock_move, run_replenishment, stock_summary

PRICE = D(1_600_000)
TODAY = timezone.localdate


class Fixture:
    """Dữ liệu chuẩn 8.2: XM-TEST-01 (tấn), Khóa thủ công SS 20 / ROP 60, Q tay 140, làm tròn xe 30 t, tồn 100 t."""

    def build(self, stock=100, supplier=True):
        mk = lambda u, r: User.objects.create_user(u, password="x", role=r, full_name=u.title())
        self.dispatcher, self.purchaser, self.director = mk("dispatcher", "DISPATCHER"), mk("purchaser", "PURCHASER"), mk("director", "DIRECTOR")
        self.warehouse_user = mk("warehouse", "WAREHOUSE")
        self.wh = m.Warehouse.objects.create(code="WH", name="Kho tổng")
        self.loc = m.Location.objects.create(warehouse=self.wh, code="A-01", zone="A", loc_type="STORAGE")
        self.qloc = m.Location.objects.create(warehouse=self.wh, code="Q-01", zone="Q", loc_type="QUARANTINE")
        m.Dock.objects.create(warehouse=self.wh, code="BEN-1", name="Bến 1")
        self.route = m.Route.objects.create(code="R1", name="NCC → Kho", origin="NCC", destination="Kho", std_hours=3)
        for seq, (code, off) in enumerate([("FACTORY", 0), ("LOADED", 60), ("GATE", 120)], 1):
            m.RouteMilestone.objects.create(route=self.route, seq=seq, code=code, name=code, planned_offset_min=off)
        self.supplier = m.Supplier.objects.create(code="NCC-XM", name="NCC xi măng", default_route=self.route)
        uom = m.Uom.objects.create(code="TAN", name="Tấn")
        cat = m.ProductCategory.objects.create(code="XM", name="Xi măng")
        self.product = m.Product.objects.create(code="XM-TEST-01", name="Xi măng test", category=cat, base_uom=uom,
                                                unit_weight_kg=1000, standard_cost=PRICE, avg_cost=PRICE,
                                                default_supplier=self.supplier if supplier else None, track_rop=True)
        ReplenishmentParam.objects.create(product=self.product, z=D("1.65"), manual_lock=True, manual_ss=20,
                                          manual_rop=60, lock_reason="test", manual_q=140, round_by_truck=True,
                                          truck_ton=30, lead_time_days=3)
        self.vehicle = m.Vehicle.objects.create(plate="51C-000.01", capacity_ton=30)
        self.driver = m.Driver.objects.create(code="TX1", name="Tài xế", license_expiry=TODAY() + timedelta(days=365))
        self.taixe = User.objects.create_user("taixe", password="x", role="DRIVER", driver=self.driver)
        self.customer = m.Customer.objects.create(code="KH", name="Khách", customer_type="B2B")
        self.site = m.Site.objects.create(customer=self.customer, code="CT1", name="Công trình 1", priority=3)
        if stock:
            post_stock_move(self.product, self.loc, D(stock), "ADJ", "ADJ", 0, note="Tồn đầu")


class Base(Fixture, TestCase):
    def setUp(self):
        self.build()

    # ---------------- helpers
    def act(self, fn, *a, **kw):
        """Chạy 1 thao tác nghiệp vụ và các callback on_commit của nó (giống request thật sau commit)."""
        with self.captureOnCommitCallbacks(execute=True):
            return fn(*a, **kw)

    def metrics(self):
        return stock_metrics([self.product])[self.product.pk]

    def auto_prs(self):
        return PurchaseRequisition.objects.filter(source="AUTO", lines__product=self.product).distinct()

    def assert_state(self, on_hand, reserved, incoming, projected, auto_pr):
        x = self.metrics()  # cùng hàm stock_metrics mà màn Tồn kho / SS-ROP dùng
        got = (x["on_hand"], x["reserved"], x["incoming"], x["projected"], self.auto_prs().count())
        self.assertEqual(got, (D(on_hand), D(reserved), D(incoming), D(projected), auto_pr))

    def so(self, qty, days=0, site=None, user=None, confirm=True):
        so = SalesOrder.objects.create(code=next_doc_no("SO"), customer=self.customer, site=site or self.site,
                                       order_date=TODAY(), requested_date=TODAY() + timedelta(days=days),
                                       created_by=user or self.dispatcher)
        SalesOrderLine.objects.create(so=so, product=self.product, qty=D(qty), unit_price=2_000_000)
        if confirm:
            self.act(ss.submit_so, so, user or self.dispatcher)
            so.refresh_from_db()
        return so

    def so_via_view(self, qty, days=0):
        """Dispatcher tạo + xác nhận đơn qua view thật (Client.post) – PR phải sinh ngay trong request này."""
        c = Client()
        c.force_login(self.dispatcher)
        with self.captureOnCommitCallbacks(execute=True):
            r = c.post("/app/sales/orders/new", {"customer": self.customer.pk, "site": self.site.pk,
                                                 "requested_date": (TODAY() + timedelta(days=days)).isoformat(),
                                                 "product": [self.product.pk], "qty": [str(qty)],
                                                 "price": ["2000000"], "submit": "1"})
        self.assertEqual(r.status_code, 302)
        return SalesOrder.objects.order_by("-id").first()

    def adjust(self, qty):
        self.act(post_stock_move, self.product, self.loc, D(qty), "ADJ", "ADJ", 0, note="điều chỉnh test")

    def deliver(self, so):
        """B2: lên kế hoạch giao -> phát lệnh soạn -> soạn -> xác nhận xuất kho."""
        trip = ss.create_trip(self.vehicle, self.driver, TODAY())
        ss.add_order_to_trip(trip, so)
        pick = ss.release_picking(trip, self.dispatcher)
        gi = ss.confirm_picking(pick, self.warehouse_user, {})
        self.act(ss.post_goods_issue, gi, self.warehouse_user)

    def receive_direct(self, qty, location=None):
        """PO thủ công đã duyệt + phiếu nhập (không chuyến) – dùng cho kịch bản phụ."""
        po = PurchaseOrder.objects.create(code=next_doc_no("PO"), supplier=self.supplier, order_date=TODAY(),
                                          warehouse=self.wh, route=self.route, created_by=self.purchaser)
        pol = PurchaseOrderLine.objects.create(po=po, product=self.product, qty=D(qty), unit_price=PRICE)
        self.act(ps.submit_po, po, self.purchaser)
        po.refresh_from_db()
        if po.status == "PENDING_APPROVAL":
            self.act(ps.approve_po, po, self.director)
        gr = GoodsReceipt.objects.create(code=next_doc_no("GRN"), po=po, warehouse=self.wh, received_at=timezone.now())
        GoodsReceiptLine.objects.create(gr=gr, po_line=pol, product=self.product, location=location or self.loc,
                                        qty_expected=D(qty), qty_received=D(qty), unit_cost=PRICE)
        self.act(ps.submit_grn, gr, self.warehouse_user)
        return po, gr

    def run_trip_to_grn(self, trip):
        for ms in trip.milestones.order_by("seq"):
            if ms.actual_at is None:
                self.act(ps.record_milestone, trip, ms, self.taixe)
                trip.refresh_from_db()
        slot = UnloadingSlot.objects.get(trip=trip)
        gr = ps.check_in(slot, self.warehouse_user)
        self.act(ps.submit_grn, gr, self.warehouse_user)
        return gr

    def last_log(self):
        return ReplenishmentCheckLog.objects.filter(product=self.product).order_by("-id").first()

    def open_alert(self):
        return ReplenishmentAlert.objects.filter(product=self.product, closed_at__isnull=True).first()

    def after_b3(self):
        """Trạng thái sau B3: tồn thực 70, đơn B giữ 15, PR tự động 150 SUBMITTED."""
        self.adjust(-30)
        so_b = self.so(15)
        self.assert_state(70, 15, 0, 55, 1)
        return so_b


# ======================================================================================= 8.3 B0–B9
class MainScenarioTest(Base):
    def test_b0_to_b9(self):
        # B0
        self.assert_state(100, 0, 0, 100, 0)
        row = stock_summary([self.product])[self.product.pk]
        self.assertEqual(row["level"], "OK")                       # badge "Bình thường"
        self.assertEqual(row["suggested_q"], D(150))               # Lượng PR gợi ý: max(140, 100) = 140 -> 150

        # B1: đơn A 30 t cần hôm nay
        a = self.so(30)
        self.assertEqual(a.status, "CONFIRMED")
        self.assertIsNotNone(a.confirmed_at)
        self.assertEqual(a.confirmed_by, self.dispatcher)
        self.assertEqual(a.lines.get().atp_status, "OK")          # dòng "Đủ"
        self.assert_state(100, 30, 0, 70, 0)

        # B2: giao đơn A -> xuất kho
        self.deliver(a)
        self.assert_state(70, 0, 0, 70, 0)                         # không trừ dự báo 2 lần
        self.assertTrue(self.product.moves.filter(qty=-30).exists())

        # B3: đơn B 15 t qua view thật -> PR ngay trong request xác nhận (lỗi L1)
        b = self.so_via_view(15)
        self.assert_state(70, 15, 0, 55, 1)
        pr = self.auto_prs().get()
        self.assertEqual((pr.source, pr.status, pr.get_source_display()), ("AUTO", "SUBMITTED", "Tự động (ROP)"))
        self.assertEqual(pr.lines.get().qty, D(150))
        self.assertEqual((pr.trigger_event, pr.trigger_ref_code, pr.trigger_user), ("SO_CONFIRMED", b.code, self.dispatcher))
        self.assertEqual((pr.snapshot["position"], pr.snapshot["raw"]), (55.0, 145.0))
        self.assertIsNone(pr.requested_by)                         # PR AUTO do hệ thống tạo
        self.assertTrue(Alert.objects.filter(target_role="PURCHASER", title__startswith=pr.code).exists())
        self.assertIsNotNone(self.open_alert())
        self.assertEqual(stock_summary([self.product])[self.product.pk]["level"], "BELOW_ROP")  # "Chạm ROP"

        # B4: đơn C 10 t -> position 195 > ROP, không tạo thêm
        self.so(10)
        self.assert_state(70, 25, 0, 45, 1)
        self.assertEqual(self.last_log().result, "DA_DU_DE_XUAT")
        self.assertEqual(self.last_log().position, D(195))

        # B5: Thu mua gộp PR tự động thành PO, gửi duyệt
        po = ps.convert_prs_to_po([pr], self.wh, self.purchaser)[0]
        self.act(ps.submit_po, po, self.purchaser)
        po.refresh_from_db()
        pr.refresh_from_db()
        self.assertEqual(po.status, "PENDING_APPROVAL")
        self.assertEqual(po.total_with_vat, D(264_000_000))
        self.assertEqual(pr.status, "CONVERTED")
        self.assert_state(70, 25, 0, 45, 1)
        self.assertEqual(self.metrics()["pipeline"], D(150))       # PR đã gộp chỉ tính ở PO

        # B6: Giám đốc duyệt PO -> cảnh báo đóng
        self.act(ps.approve_po, po, self.director)
        self.assert_state(70, 25, 150, 195, 1)
        self.assertIsNone(self.open_alert())
        self.assertEqual(ReplenishmentAlert.objects.get(product=self.product).closed_event, "PO_APPROVED")

        # B7: gửi NCC, lập 5 chuyến × 30 t, bấm mốc chuyến 1
        ps.send_po(PurchaseOrder.objects.get(pk=po.pk), TODAY())
        po.refresh_from_db()
        pol = po.lines.get()
        trips = [ps.create_inbound_trip(po, self.route, self.vehicle, self.driver, timezone.now(), {pol: 30})
                 for _ in range(5)]
        first = trips[0].milestones.order_by("seq").first()
        self.act(ps.record_milestone, trips[0], first, self.taixe)
        self.assert_state(70, 25, 150, 195, 1)

        # B8: nhập chuyến 1
        self.run_trip_to_grn(InboundTrip.objects.get(pk=trips[0].pk))
        self.assert_state(100, 25, 120, 195, 1)
        self.assertEqual(PurchaseOrder.objects.get(pk=po.pk).status, "PARTIAL")

        # B9: nhập 4 chuyến còn lại
        for t in trips[1:]:
            self.run_trip_to_grn(InboundTrip.objects.get(pk=t.pk))
        self.assert_state(220, 25, 0, 195, 1)
        self.assertEqual(PurchaseOrder.objects.get(pk=po.pk).status, "RECEIVED")


# ======================================================================================= 8.4 P1–P6
class SideScenarioTest(Base):
    def test_p1_far_need_date_still_triggers(self):
        self.so(45, days=5)
        self.assert_state(100, 45, 0, 55, 1)
        pr = self.auto_prs().get()
        self.assertEqual((pr.lines.get().qty, pr.trigger_event), (D(150), "SO_CONFIRMED"))

    def test_p2_top_up_pr(self):
        self.after_b3()
        big = self.so(200)
        line = big.lines.get()
        self.assertEqual((line.reserved_qty, line.backorder_qty), (D(55), D(145)))
        x = self.metrics()
        self.assertEqual((x["reserved"], x["demand_unreserved"], x["projected"]), (D(70), D(145), D(-145)))
        self.assertEqual(self.auto_prs().count(), 2)
        pr2 = self.auto_prs().order_by("-id").first()
        # pipeline / position lúc QUYẾT ĐỊNH tạo PR (trước khi PR thứ hai vào pipeline) – lưu trong snapshot
        self.assertEqual((pr2.snapshot["pipeline"], pr2.snapshot["position"]), (150.0, 5.0))
        self.assertEqual((pr2.lines.get().qty, pr2.snapshot["raw"], pr2.urgency), (D(210), 195.0, "URGENT"))

    def test_p3_cancel_order(self):
        b = self.after_b3()
        pr = self.auto_prs().get()
        self.act(ss.cancel_so, b, "Khách hủy", self.dispatcher)
        self.assert_state(70, 0, 0, 70, 1)
        self.assertIsNone(self.open_alert())
        self.assertEqual(ReplenishmentAlert.objects.get(product=self.product).closed_event, "SO_CANCELLED")
        pr.refresh_from_db()
        self.assertEqual(pr.status, "SUBMITTED")                   # PR không bị hủy ngầm
        b.refresh_from_db()
        self.assertEqual((b.cancelled_by, b.status), (self.dispatcher, "CANCELLED"))

    def test_p4_grn_fills_backorder_and_notifies(self):
        self.adjust(-60)                                           # tồn 40
        x = self.so(30, days=1)
        y = self.so(25, days=2)
        self.assertEqual(y.lines.get().reserved_qty, D(10))
        self.receive_direct(30)
        self.assertEqual(self.metrics()["on_hand"], D(70))
        yl = y.lines.get()
        self.assertEqual((yl.reserved_qty, yl.backorder_qty, yl.atp_status), (D(25), D(0), "OK"))
        self.assertTrue(Alert.objects.filter(target_role="DISPATCHER", title=f"{y.code} đã có hàng").exists())
        self.assertEqual(x.lines.get().reserved_qty, D(30))

    def test_p5_priority_by_need_date(self):
        self.adjust(-100)                                          # tồn 0
        n = self.so(20, days=3)                                    # xác nhận trước nhưng cần muộn hơn
        mm = self.so(20, days=1)
        self.receive_direct(25)
        self.assertEqual(mm.lines.get().reserved_qty, D(20))       # cần sớm hơn được giữ đủ trước
        self.assertEqual(n.lines.get().reserved_qty, D(5))

    def test_p6_recalc_no_new_pr(self):
        c = Client()
        c.force_login(self.purchaser)
        with self.captureOnCommitCallbacks(execute=True):
            r = c.post(f"/app/inventory/replenishment/{self.product.pk}", {
                "method": "FULL", "z": "1.65", "review_days": "7", "history_days": "90", "lead_time_days": "3",
                "demand_source": "ALL", "moq": "0", "manual_q": "140", "round_by_truck": "on", "truck_ton": "30",
                "manual_lock": "on", "manual_ss": "20", "manual_rop": "60", "lock_reason": "test"})
        self.assertEqual(r.status_code, 302)
        self.act(run_replenishment)
        rp = ReplenishmentParam.objects.get(product=self.product)
        self.assertEqual((rp.eff_ss, rp.eff_rop), (D(20), D(60)))
        self.assertEqual(self.auto_prs().count(), 0)
        self.assertTrue(ReplenishmentCheckLog.objects.filter(product=self.product, event="PARAM_SAVED").exists())

    def test_p6_variant_after_b3(self):
        self.after_b3()
        self.act(run_replenishment)
        self.assertEqual(self.auto_prs().count(), 1)
        self.assertEqual(ReplenishmentAlert.objects.filter(product=self.product, closed_at__isnull=True).count(), 1)
        self.assertEqual((self.last_log().event, self.last_log().result), ("NIGHTLY_JOB", "DA_DU_DE_XUAT"))


# ======================================================================================= 8.5 T1–T17
class RuleTest(Base):
    def test_t1_formula_components(self):
        post_stock_move(self.product, self.qloc, D(50), "ADJ", "ADJ", 0)        # hàng cách ly không tính tồn thực
        self.assertEqual(self.metrics()["on_hand"], D(100))
        po = PurchaseOrder.objects.create(code="PO-T1", supplier=self.supplier, order_date=TODAY(), warehouse=self.wh)
        PurchaseOrderLine.objects.create(po=po, product=self.product, qty=40, unit_price=PRICE)
        x = self.metrics()
        self.assertEqual((x["incoming"], x["pipeline"]), (D(0), D(40)))        # PO nháp chỉ ở pipeline
        po.status = "PENDING_APPROVAL"
        po.save()
        self.assertEqual((self.metrics()["incoming"], self.metrics()["pipeline"]), (D(0), D(40)))
        self.adjust(-100)
        self.so(10, days=30)                                                     # backorder cần sau hôm nay + LT
        self.assertEqual(self.metrics()["demand_unreserved"], D(0))
        self.so(5, days=1)
        self.assertEqual(self.metrics()["demand_unreserved"], D(5))
        # PR đã gộp (CONVERTED, có po_line) không đếm hai lần
        pr = self.auto_prs().order_by("id").first()
        before = self.metrics()["pipeline"]
        new_po = ps.convert_prs_to_po([pr], self.wh, self.purchaser)[0]
        self.assertEqual(self.metrics()["pipeline"], before)
        self.assertEqual(new_po.status, "DRAFT")

    def test_t2_idempotent(self):
        self.adjust(-45)                                                         # projected 55
        trig = Trigger("NIGHTLY_JOB")
        self.assertIsNotNone(check_replenishment(self.product.pk, trig))
        self.assertIsNone(check_replenishment(self.product.pk, trig))
        self.assertEqual(self.auto_prs().count(), 1)
        self.assertEqual(self.last_log().result, "DA_DU_DE_XUAT")

    def test_t4_rollback_no_side_effects(self):
        self.adjust(-30)
        so = self.so(15, confirm=False)
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    ss.submit_so(so, self.dispatcher)
                    raise RuntimeError("lỗi giữa chừng")
            except RuntimeError:
                pass
        self.assertEqual(SalesOrder.objects.get(pk=so.pk).status, "DRAFT")
        self.assertEqual(self.auto_prs().count(), 0)
        self.assertFalse(ReplenishmentAlert.objects.exists())
        self.assertFalse(ReplenishmentCheckLog.objects.filter(event="SO_CONFIRMED").exists())

    def test_t5_core_error_does_not_break_order(self):
        self.adjust(-30)
        with mock.patch("apps.inventory.replenishment.check_replenishment", side_effect=RuntimeError("hỏng")):
            so = self.so(15)
        self.assertEqual(so.status, "CONFIRMED")
        self.assertEqual(self.last_log().result, "LOI")
        self.assertEqual(self.auto_prs().count(), 0)
        self.act(run_replenishment)                                              # tác vụ đêm quét bù
        self.assertEqual(self.auto_prs().count(), 1)

    def test_t6_untracked_product(self):
        self.product.track_rop = False
        self.product.save()
        self.adjust(-30)
        self.so(15)
        self.assertEqual(self.auto_prs().count(), 0)
        self.assertFalse(ReplenishmentAlert.objects.exists())
        self.assertFalse(ReplenishmentCheckLog.objects.exists())

    def test_t7_reject_within_24h(self):
        self.after_b3()
        pr = self.auto_prs().get()
        ps.reject_pr(pr, self.purchaser, "chưa cần")
        pr.refresh_from_db()
        self.assertEqual(pr.rejected_position, D(55))
        self.act(fire_checks, [self.product.pk], Trigger("NIGHTLY_JOB"))
        self.assertEqual(self.last_log().result, "DA_TU_CHOI_GAN_DAY")
        self.assertEqual(self.auto_prs().exclude(status="REJECTED").count(), 0)
        self.so(10)                                                              # nhu cầu mới -> position 45 < 55
        self.assertEqual(self.auto_prs().exclude(status="REJECTED").count(), 1)

    def test_t7_reject_after_24h(self):
        self.after_b3()
        pr = self.auto_prs().get()
        ps.reject_pr(pr, self.purchaser, "chưa cần")
        PurchaseRequisition.objects.filter(pk=pr.pk).update(rejected_at=timezone.now() - timedelta(hours=25))
        self.assertIsNotNone(check_replenishment(self.product.pk, Trigger("NIGHTLY_JOB")))

    def test_t8_missing_supplier(self):
        self.product.default_supplier = None
        self.product.save()
        self.adjust(-30)
        self.so(15)
        pr = self.auto_prs().get()
        self.assertTrue(pr.missing_supplier)
        self.assertTrue(Alert.objects.filter(target_role="PURCHASER", title__contains="chưa có NCC").exists())

    def test_t9_rounding_and_q(self):
        p = EffectiveParams(SS=D(20), ROP=D(60), Q=D(140), truck_qty=D(30), round_to_truck=False, LT=D(3))
        self.assertEqual(calc_order_qty(p, 55), (D(145), D(145)))
        p.round_to_truck = True
        self.assertEqual(calc_order_qty(p, 55)[1], D(150))
        rp = ReplenishmentParam.objects.get(product=self.product)
        rp.manual_q, rp.moq, rp.d_avg, rp.review_days = None, D(10), D("3.2"), 7
        self.assertEqual(rp.order_q, D(23))                                      # max(MOQ, ceil(D̄ × chu kỳ))

    def test_t10_manual_lock_and_track_toggle(self):
        self.act(run_replenishment)
        rp = ReplenishmentParam.objects.get(product=self.product)
        self.assertEqual((rp.eff_ss, rp.eff_rop), (D(20), D(60)))
        rp.manual_lock = False
        rp.save()
        self.assertEqual((rp.eff_ss, rp.eff_rop), (rp.ss, rp.rop))
        c = Client()
        c.force_login(self.purchaser)
        with self.captureOnCommitCallbacks(execute=True):
            r = c.post(f"/app/master/products/{self.product.pk}", {
                "code": self.product.code, "name": self.product.name, "category": self.product.category_id,
                "base_uom": self.product.base_uom_id, "default_supplier": self.supplier.pk, "unit_weight_kg": "1000",
                "standard_cost": str(PRICE)})                                    # bỏ tick "Theo dõi SS/ROP"
        self.assertEqual(r.status_code, 302)
        self.product.refresh_from_db()
        self.assertFalse(self.product.track_rop)
        with self.captureOnCommitCallbacks(execute=True):
            c.post(f"/app/master/products/{self.product.pk}", {
                "code": self.product.code, "name": self.product.name, "category": self.product.category_id,
                "base_uom": self.product.base_uom_id, "default_supplier": self.supplier.pk, "unit_weight_kg": "1000",
                "standard_cost": str(PRICE), "track_rop": "on"})
        self.assertTrue(ReplenishmentCheckLog.objects.filter(product=self.product, event="PARAM_SAVED").exists())

    def test_t11_secondary_triggers(self):
        a = self.so(30)
        self.deliver(a)
        self.assertTrue(ReplenishmentCheckLog.objects.filter(event="ISSUE_POSTED").exists())
        # kiểm kê được duyệt làm tồn thực giảm 70 -> 50 => dự báo 50 ≤ ROP -> tạo PR với event COUNT_APPROVED
        ic = InventoryCount.objects.create(code="IC-T11", warehouse=self.wh, status="COUNTING", created_by=self.warehouse_user)
        line = InventoryCountLine.objects.create(count=ic, product=self.product, location=self.loc, book_qty=70)
        c = Client()
        c.force_login(self.director)
        with self.captureOnCommitCallbacks(execute=True):
            c.post(f"/app/inventory/counts/{ic.pk}", {"op": "count_approve", f"c_{line.pk}": "50"})
        ic.refresh_from_db()
        self.assertEqual(ic.status, "RECOUNT")                                   # 70 -> 50 lệch > 1%: đếm lại trước
        self.assertEqual(self.auto_prs().count(), 0)
        with self.captureOnCommitCallbacks(execute=True):                        # đếm lần 2 vẫn 50 -> duyệt
            c.post(f"/app/inventory/counts/{ic.pk}", {"op": "count_approve", f"c_{line.pk}": "50"})
        ic.refresh_from_db()
        self.assertEqual(ic.status, "APPROVED")
        self.assertEqual(self.metrics()["on_hand"], D(50))
        pr = self.auto_prs().get()
        self.assertEqual(pr.trigger_event, "COUNT_APPROVED")

    def test_t12_cancel_approved_po(self):
        self.after_b3()
        self.so(10)                                                              # đơn C -> dự báo 45
        pr = self.auto_prs().get()
        po = ps.convert_prs_to_po([pr], self.wh, self.purchaser)[0]
        self.act(ps.submit_po, po, self.purchaser)
        self.act(ps.approve_po, PurchaseOrder.objects.get(pk=po.pk), self.director)
        self.assertEqual(self.metrics()["projected"], D(195))
        self.act(ps.cancel_po, PurchaseOrder.objects.get(pk=po.pk), "NCC hết hàng", self.purchaser)
        x = self.metrics()
        self.assertEqual((x["incoming"], x["projected"]), (D(0), D(45)))
        self.assertIsNotNone(self.open_alert())
        new = self.auto_prs().order_by("-id").first()
        self.assertEqual((new.lines.get().qty, new.snapshot["raw"], new.trigger_event), (D(180), 155.0, "PO_CANCELLED"))

    def test_t13_allocation_order(self):
        self.adjust(-100)
        hi = m.Site.objects.create(customer=self.customer, code="CT-HI", name="Ưu tiên cao", priority=1)
        low_first = self.so(10, days=1)                                          # cùng ngày, ưu tiên thấp, xác nhận trước
        high = self.so(10, days=1, site=hi)
        low_second = self.so(10, days=1)
        self.receive_direct(15)
        got = [o.lines.get().reserved_qty for o in (high, low_first, low_second)]
        self.assertEqual(got, [D(10), D(5), D(0)])

    def test_t14_grn_posted_once(self):
        po, gr = self.receive_direct(30)
        with self.assertRaises(BusinessError):
            ps.submit_grn(GoodsReceipt.objects.get(pk=gr.pk), self.warehouse_user)
        self.assertEqual(self.metrics()["on_hand"], D(130))
        self.assertEqual(po.lines.get().qty_received, D(30))

    def test_t15_grn_into_quarantine(self):
        po = PurchaseOrder.objects.create(code="PO-T15", supplier=self.supplier, order_date=TODAY(), warehouse=self.wh,
                                          status="APPROVED")
        pol = PurchaseOrderLine.objects.create(po=po, product=self.product, qty=30, unit_price=PRICE)
        self.assertEqual(self.metrics()["projected"], D(130))
        gr = GoodsReceipt.objects.create(code="GRN-T15", po=po, warehouse=self.wh, received_at=timezone.now())
        GoodsReceiptLine.objects.create(gr=gr, po_line=pol, product=self.product, location=self.qloc,
                                        qty_expected=30, qty_received=30, unit_cost=PRICE)
        self.act(ps.submit_grn, gr, self.warehouse_user)
        x = self.metrics()
        self.assertEqual((x["on_hand"], x["incoming"], x["projected"]), (D(100), D(0), D(100)))
        self.assertEqual(pol.__class__.objects.get(pk=pol.pk).qty_received, D(30))
        self.assertTrue(ReplenishmentCheckLog.objects.filter(event="GRN_POSTED").exists())

    def test_t16_trip_over_remaining(self):
        po = PurchaseOrder.objects.create(code="PO-T16", supplier=self.supplier, order_date=TODAY(), warehouse=self.wh,
                                          status="SENT")
        pol = PurchaseOrderLine.objects.create(po=po, product=self.product, qty=30, unit_price=PRICE)
        trip = ps.create_inbound_trip(po, self.route, self.vehicle, self.driver, timezone.now(), {pol: 30})
        with self.assertRaises(BusinessError):
            ps.create_inbound_trip(po, self.route, self.vehicle, self.driver, timezone.now(),
                                   {pol.__class__.objects.get(pk=pol.pk): 5})
        before = self.metrics()
        self.act(ps.record_milestone, trip, trip.milestones.order_by("seq").first(), self.taixe)
        self.assertEqual(self.metrics(), before)

    def test_t17_traces_and_pr_list_column(self):
        b = self.after_b3()
        pr = self.auto_prs().get()
        self.assertTrue(all([pr.trigger_event, pr.trigger_ref_type, pr.trigger_ref_code, pr.trigger_user, pr.trigger_at]))
        self.assertTrue({"on_hand", "reserved", "incoming", "pipeline", "projected", "position", "SS", "ROP", "Q", "raw",
                         "qty"} <= set(pr.snapshot))
        self.assertFalse(any("price" in k or "cost" in k for k in pr.snapshot))  # snapshot không lưu giá
        b.refresh_from_db()
        self.assertTrue(b.confirmed_at and b.confirmed_by)
        self.assertTrue(b.history.filter(event="CONFIRMED").exists())
        al = self.open_alert()
        self.assertEqual((al.opened_event, al.opened_ref), ("SO_CONFIRMED", b.code))
        c = Client()
        c.force_login(self.purchaser)
        html = c.get("/app/purchase/requisitions").content.decode()
        self.assertIn("XM-TEST-01", html)                                        # cột Vật tư (lỗi L4)
        self.assertIn("Xác nhận đơn bán", html)
        self.assertIn(b.code, html)
        html = c.get(f"/app/purchase/requisitions/{pr.pk}").content.decode()
        self.assertIn("Vì sao có PR này", html)
        d = Client()
        d.force_login(self.dispatcher)
        html = d.get(f"/app/sales/orders/{b.pk}").content.decode()
        self.assertIn("Lịch sử đơn", html)
        self.assertIn(pr.code, html)


class QuantEditTest(Base):
    """Nút Sửa / Xoá tồn tại 1 vị trí (màn Tồn kho)."""

    def post(self, user, quant, **data):
        c = Client()
        c.force_login(user)
        with self.captureOnCommitCallbacks(execute=True):
            return c.post(f"/app/inventory/stock/quant/{quant.pk}", data, HTTP_REFERER="/app/inventory/stock")

    def quant(self, loc=None):
        from .models import StockQuant
        return StockQuant.objects.get(product=self.product, location=loc or self.loc)

    def test_set_and_delete_write_ledger_and_trigger(self):
        self.post(self.warehouse_user, self.quant(), op="set", qty="80", reason="Đếm lại")
        self.assertEqual(self.quant().on_hand, D(80))
        self.assertTrue(self.product.moves.filter(move_type="ADJ", qty=-20).exists())   # sổ kho ghi chênh lệch
        post_stock_move(self.product, self.qloc, D(30), "ADJ", "ADJ", 0)
        self.post(self.warehouse_user, self.quant(self.qloc), op="delete", reason="Hủy hàng lỗi")
        self.assertEqual(self.quant(self.qloc).on_hand, D(0))
        self.post(self.warehouse_user, self.quant(), op="set", qty="50", reason="Hao hụt")
        self.assertEqual(self.auto_prs().get().trigger_event, "STOCK_ADJUSTED")       # 50 ≤ ROP 60 -> PR

    def test_blocked_below_reserved_and_requires_reason(self):
        self.so(90)
        self.post(self.warehouse_user, self.quant(), op="set", qty="50", reason="x")
        self.assertEqual(self.quant().on_hand, D(100))                                  # đang giữ 90 -> chặn
        self.post(self.warehouse_user, self.quant(), op="delete", reason="")
        self.assertEqual(self.quant().on_hand, D(100))                                  # thiếu lý do -> chặn

    def test_forbidden_roles(self):
        r = self.post(self.dispatcher, self.quant(), op="delete", reason="x")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.quant().on_hand, D(100))


# ======================================================================================= T3 đồng thời
@skipUnlessDBFeature("has_select_for_update")
class ConcurrencyTest(Fixture, TransactionTestCase):
    """Hai request đồng thời xác nhận 2 đơn cùng vật tư: kết quả như chạy tuần tự, không có 2 PR trùng.
    Cần DB có SELECT ... FOR UPDATE (PostgreSQL); SQLite tự bỏ qua."""

    def test_t3_parallel_confirm(self):
        self.build()
        self.act = lambda fn, *a: fn(*a)
        post_stock_move(self.product, self.loc, D(-30), "ADJ", "ADJ", 0)
        orders = []
        for _ in range(2):
            so = SalesOrder.objects.create(code=next_doc_no("SO"), customer=self.customer, site=self.site,
                                           order_date=TODAY(), requested_date=TODAY())
            SalesOrderLine.objects.create(so=so, product=self.product, qty=D(10), unit_price=1)
            orders.append(so)
        errors = []

        def work(so):
            try:
                ss.submit_so(SalesOrder.objects.get(pk=so.pk), self.dispatcher)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                connection.close()

        threads = [threading.Thread(target=work, args=(o,)) for o in orders]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(PurchaseRequisition.objects.filter(source="AUTO").count(), 1)
        self.assertEqual(stock_metrics([self.product])[self.product.pk]["projected"], D(50))
