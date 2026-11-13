"""Test cho các lỗi trong test/test tracon.docx (STT 3, 9, 11, 16, 17)."""
from datetime import datetime, time, timedelta
from decimal import Decimal as D
from unittest import mock

from django.test import Client
from django.utils import timezone

from apps.core.history import doc_history
from apps.masterdata import models as m
from apps.purchasing import services as ps
from apps.purchasing.models import PurchaseOrder, PurchaseOrderLine, UnloadingSlot
from apps.sales.models import SalesOrder

from .models import ReplenishmentJobRun, ReplenishmentParam
from .services import run_replenishment
from .test_qt1_qt2 import PRICE, TODAY, Base

TZ = timezone.get_current_timezone


def at(day, hh, mm=0):
    return timezone.make_aware(datetime.combine(day, time(hh, mm)), TZ())


class Stt9ConfirmTriggersPR(Base):
    def test_confirm_via_detail_page_creates_pr_and_shows_check(self):
        """Xác nhận đơn B 15 t từ trang chi tiết (nút "Xác nhận đơn") -> PR có ngay, sự kiện SO_CONFIRMED,
        chứng từ gây ra = mã đơn; trang đơn hiển thị kết quả kiểm tra ROP và thông báo có mã PR."""
        self.adjust(-30)
        so = self.so(15, confirm=False)
        c = Client()
        c.force_login(self.dispatcher)
        with self.captureOnCommitCallbacks(execute=True):
            c.post(f"/app/sales/orders/{so.pk}", {"op": "submit"})
        pr = self.auto_prs().get()
        self.assertEqual((pr.trigger_event, pr.trigger_ref_code, pr.lines.get().qty), ("SO_CONFIRMED", so.code, D(150)))
        html = c.get(f"/app/sales/orders/{so.pk}").content.decode()
        self.assertIn(pr.code, html)                      # thông báo + khối "Kiểm tra bổ sung tồn"
        self.assertIn("Kiểm tra bổ sung tồn (ROP)", html)
        self.assertIn("Tạo PR", html)

    def test_director_approval_branch_creates_pr(self):
        self.adjust(-30)
        self.customer.credit_limit = 1  # vượt hạn mức -> chờ Giám đốc duyệt
        self.customer.save()
        so = self.so(15)
        self.assertEqual(so.status, "PENDING_APPROVAL")
        self.assertEqual(self.auto_prs().count(), 0)       # chờ duyệt: chưa giữ hàng, chưa PR
        self.act(__import__("apps.sales.services", fromlist=["x"]).approve_so, so, self.director)
        pr = self.auto_prs().get()
        self.assertEqual((pr.trigger_event, pr.trigger_ref_code), ("SO_APPROVED", so.code))


class Stt3NightlyJob(Base):
    def test_nightly_after_events_creates_zero_pr_and_logs_run(self):
        self.after_b3()                                    # PR đã sinh tại sự kiện xác nhận đơn
        res = run_replenishment()
        self.assertEqual(res["lines"], 0)                  # tác vụ đêm tạo 0 PR
        run = ReplenishmentJobRun.objects.get()
        self.assertEqual((run.source, run.products, run.pr_created, run.locked_skipped, run.recalculated),
                         ("CRON", 1, 0, 1, 0))             # vật tư Khóa thủ công: không tính lại
        self.assertIsNotNone(run.finished_at)

    def test_nightly_skips_locked_recalc(self):
        rp = ReplenishmentParam.objects.get(product=self.product)
        rp.rop = D(999)
        rp.save()
        with mock.patch("apps.inventory.services.calc_param") as calc:
            run_replenishment()
        calc.assert_not_called()
        rp.refresh_from_db()
        self.assertEqual(rp.eff_rop, D(60))

    def test_nightly_catches_missed_case(self):
        self.adjust(-50)  # tồn 50 ≤ ROP 60 nhưng kiểm tra bị "lọt" (gọi trực tiếp, không qua sự kiện)
        self.assertEqual(self.auto_prs().count(), 0)
        res = run_replenishment()
        self.assertEqual(res["lines"], 1)
        self.assertEqual(ReplenishmentJobRun.objects.get().pr_created, 1)


class Stt11History(Base):
    def test_so_header_and_history_tab(self):
        so = self.so(10, confirm=False)
        c = Client()
        c.force_login(self.dispatcher)
        # sửa đơn nháp: 10 -> 12 (ghi cũ -> mới)
        c.post(f"/app/sales/orders/{so.pk}/edit", {"customer": self.customer.pk, "site": self.site.pk,
                                                    "requested_date": TODAY().isoformat(), "product": [self.product.pk],
                                                    "qty": ["12"], "price": ["2000000"]})
        with self.captureOnCommitCallbacks(execute=True):
            c.post(f"/app/sales/orders/{so.pk}", {"op": "submit"})
        so.refresh_from_db()
        self.assertIsNotNone(so.confirmed_at)
        html = c.get(f"/app/sales/orders/{so.pk}").content.decode()
        self.assertIn("Xác nhận <b>", html)                # giờ + người xác nhận ở đầu trang
        self.assertIn("Lịch sử", html)
        items = doc_history(so)
        changes = [ch for it in items for ch in it["changes"]]
        self.assertTrue(any(k.endswith("Số lượng") and old == "10" and new == "12" for k, old, new in changes), changes)
        self.assertTrue(any(it["action"] == "Xác nhận & giữ hàng" and it["user"] == self.dispatcher for it in items))

    def test_history_hides_price_for_warehouse(self):
        so = self.so(10, confirm=False)
        line = so.lines.get()
        line.unit_price = 2_100_000
        line.save()
        c = Client()
        c.force_login(self.director)
        self.assertIn("Đơn giá", c.get(f"/app/sales/orders/{so.pk}").content.decode())
        from django.template import Context, Template
        out = Template("{% doc_history so %}").render(Context({"so": so, "user": self.warehouse_user}))
        self.assertNotIn("Đơn giá", out)

    def test_detail_pages_render_history_tab(self):
        po, gr = self.receive_direct(30)
        c = Client()
        c.force_login(self.director)
        for url in (f"/app/purchase/orders/{po.pk}", f"/app/warehouse/receipts/{gr.pk}"):
            r = c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn("lich-su", r.content.decode())


class SlotBase(Base):
    def setUp(self):
        super().setUp()
        self.day = TODAY() + timedelta(days=2)  # luôn ở tương lai so với giờ chạy test
        po = PurchaseOrder.objects.create(code="PO-T", supplier=self.supplier, order_date=TODAY(), warehouse=self.wh,
                                          route=self.route, created_by=self.purchaser, status="SENT")
        self.pol = PurchaseOrderLine.objects.create(po=po, product=self.product, qty=D(300), unit_price=PRICE)
        self.po = po

    def trip(self, eta, ton=30):
        t = ps.create_inbound_trip(self.po, self.route, self.vehicle, self.driver, eta - timedelta(hours=3),
                                   {self.pol: ton})
        t.eta = eta
        t.save()
        return t


class Stt16SlotWindow(SlotBase):
    def test_eta_0141_goes_to_0600_same_day(self):
        s = ps.propose_slot(self.trip(at(self.day, 1, 41)))
        self.assertEqual(timezone.localtime(s.start_at), at(self.day, 6))

    def test_eta_2030_75min_goes_to_0600_next_day(self):
        t = self.trip(at(self.day, 20, 30))
        self.assertEqual(ps.slot_duration(t), timedelta(minutes=75))
        s = ps.propose_slot(t)
        self.assertEqual(timezone.localtime(s.start_at), at(self.day + timedelta(days=1), 6))

    def test_eta_0500_goes_to_0600(self):
        s = ps.propose_slot(self.trip(at(self.day, 5)))
        self.assertEqual(timezone.localtime(s.start_at), at(self.day, 6))

    def test_busy_dock_takes_next_free_slot(self):
        s1 = ps.propose_slot(self.trip(at(self.day, 1)))
        s2 = ps.propose_slot(self.trip(at(self.day, 2)))
        self.assertEqual(timezone.localtime(s2.start_at), timezone.localtime(s1.end_at))

    def test_end_must_fit_before_close(self):
        s = ps.propose_slot(self.trip(at(self.day, 19, 50)))  # 19:50 + 75' = 21:05 > 21:00
        self.assertEqual(timezone.localtime(s.start_at), at(self.day + timedelta(days=1), 6))

    def test_drag_outside_window_rejected(self):
        s = ps.propose_slot(self.trip(at(self.day, 9)))
        dock = m.Dock.objects.get(code="BEN-1")
        from apps.core.services import BusinessError
        with self.assertRaises(BusinessError):
            ps.move_slot(s, dock, at(self.day, 20, 0))       # kết thúc 21:15
        with self.assertRaises(BusinessError):
            ps.move_slot(s, dock, at(self.day, 5, 30))
        ps.move_slot(s, dock, at(self.day, 19, 45))          # kết thúc đúng 21:00 – hợp lệ

    def test_params_shared_and_existing_night_slots_moved(self):
        t = self.trip(at(self.day, 9))
        dock = m.Dock.objects.get(code="BEN-1")
        bad = UnloadingSlot.objects.create(trip=t, dock=dock, start_at=at(self.day, 23, 30),
                                           end_at=at(self.day, 23, 30) + timedelta(minutes=75), status="CONFIRMED")
        self.assertEqual(ps.normalize_slots(), 1)
        bad.refresh_from_db()
        self.assertEqual((timezone.localtime(bad.start_at), bad.status), (at(self.day + timedelta(days=1), 6), "PROPOSED"))
        from apps.core.models import SystemParam
        SystemParam.objects.create(key="SLOT_DAY_START", value="07:00")
        self.assertEqual(ps.slot_hours(), (time(7), time(21)))
        c = Client()
        c.force_login(self.warehouse_user)
        html = c.get(f"/app/warehouse/slots?date={self.day:%Y-%m-%d}").content.decode()
        self.assertIn('data-time="07:00"', html)
        self.assertNotIn('data-time="06:45"', html)
        self.assertNotIn('data-time="21:00"', html)            # ô cuối 20:45–21:00


class Stt17MilestoneLabel(SlotBase):
    def test_early_label_and_replan(self):
        planned = timezone.now() + timedelta(hours=12)
        t = ps.create_inbound_trip(self.po, self.route, self.vehicle, self.driver, planned, {self.pol: 30})
        ms = list(t.milestones.order_by("seq"))
        ps.record_milestone(t, ms[0], self.taixe)              # thực tế sớm ~12 giờ
        ms = list(t.milestones.order_by("seq"))
        kind, mins = ms[0].timing()
        self.assertEqual(kind, "EARLY")
        self.assertGreater(mins, 11 * 60)
        # các mốc sau tính lại theo mốc thực tế (giữ khoảng cách kế hoạch gốc)
        self.assertEqual(ms[1].replanned_at, ms[0].actual_at + (ms[1].planned_at - ms[0].planned_at))
        c = Client()
        c.force_login(self.purchaser)
        html = c.get(f"/app/inbound/trips/{t.pk}").content.decode()
        self.assertIn("Sớm 1", html)
        self.assertNotIn(">Đúng giờ<", html)

    def test_thresholds(self):
        t = ps.create_inbound_trip(self.po, self.route, self.vehicle, self.driver, timezone.now(), {self.pol: 30})
        msx = t.milestones.order_by("seq").first()
        for delta, expect in ((-31, "EARLY"), (-30, "ONTIME"), (0, "ONTIME"), (30, "ONTIME"), (31, "LATE")):
            msx.actual_at = msx.planned_at + timedelta(minutes=delta)
            self.assertEqual(msx.timing()[0], expect, delta)
        from apps.core.templatetags.tracon import milestone_badge
        msx.actual_at = msx.planned_at + timedelta(minutes=45)
        self.assertIn("Trễ 45 phút", milestone_badge(msx))
        msx.actual_at = msx.planned_at - timedelta(hours=12, minutes=4)
        self.assertIn("Sớm 12 giờ 4 phút", milestone_badge(msx))
