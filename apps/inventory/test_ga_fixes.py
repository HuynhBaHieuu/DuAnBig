"""Test các lỗi trong sheet "Gà" của file test hệ thống (STT 18–22)."""
from datetime import timedelta
from decimal import Decimal as D

from django.test import Client
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


class Stt22CheckinWindow(tf.SlotBase):
    def setUp(self):
        super().setUp()
        self.dock = m.Dock.objects.get(code="BEN-1")

    def make_slot(self, start, minutes=75, dock=None, status="CONFIRMED"):
        t = self.trip(start)
        t.status = "ARRIVED"
        t.save()
        return UnloadingSlot.objects.create(trip=t, dock=dock or self.dock, start_at=start,
                                            end_at=start + timedelta(minutes=minutes), status=status)

    def test_early_more_than_1h_needs_reason(self):
        slot = self.make_slot(timezone.now() + timedelta(hours=6))
        with self.assertRaisesMessage(BusinessError, "lý do"):
            ps.check_in(slot, self.warehouse_user, enforce_window=True)
        gr = ps.check_in(slot, self.warehouse_user, reason="Xe chạy đêm tới sớm", enforce_window=True)
        slot.refresh_from_db()
        self.assertEqual((slot.status, slot.checkin_reason), ("CHECKED_IN", "Xe chạy đêm tới sớm"))
        self.assertTrue(gr.code.startswith("GRN"))

    def test_late_more_than_1h_needs_reason(self):
        slot = self.make_slot(timezone.now() - timedelta(hours=4))   # slot kết thúc ~2h45' trước
        with self.assertRaisesMessage(BusinessError, "lý do"):
            ps.check_in(slot, self.warehouse_user, enforce_window=True)

    def test_within_tolerance_no_reason(self):
        slot = self.make_slot(timezone.now() + timedelta(minutes=30))
        ps.check_in(slot, self.warehouse_user, enforce_window=True)
        self.assertEqual(UnloadingSlot.objects.get(pk=slot.pk).status, "CHECKED_IN")

    def test_early_but_dock_busy_must_wait(self):
        busy = self.make_slot(timezone.now() - timedelta(minutes=20), status="CHECKED_IN")
        slot = self.make_slot(timezone.now() + timedelta(minutes=40))
        st = ps.checkin_state(slot)
        self.assertEqual(st["blocker"].pk, busy.pk)
        with self.assertRaisesMessage(BusinessError, "phải đợi"):
            ps.check_in(slot, self.warehouse_user, reason="x", enforce_window=True)
        # bến đã xong -> trống -> cho check-in sớm
        UnloadingSlot.objects.filter(pk=busy.pk).update(status="DONE")
        ps.check_in(slot, self.warehouse_user, enforce_window=True)

    def test_early_dock_booked_overlap_must_wait(self):
        self.make_slot(timezone.now() + timedelta(minutes=30), minutes=60)       # lịch khác chồng khoảng hạ tải sớm
        slot = self.make_slot(timezone.now() + timedelta(hours=2), minutes=75)
        self.assertIsNotNone(ps.checkin_state(slot)["blocker"])

    def test_view_requires_reason(self):
        slot = self.make_slot(timezone.now() + timedelta(hours=6))
        c = Client()
        c.force_login(self.warehouse_user)
        r = c.post(f"/app/warehouse/slots/{slot.pk}", {"op": "checkin"}, HTTP_REFERER="/app/warehouse/slots")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(UnloadingSlot.objects.get(pk=slot.pk).status, "CONFIRMED")
        r = c.post(f"/app/warehouse/slots/{slot.pk}", {"op": "checkin", "reason": "Tới sớm, bến trống"})
        self.assertEqual(UnloadingSlot.objects.get(pk=slot.pk).status, "CHECKED_IN")
        html = c.get(f"/app/inbound/trips/{slot.trip_id}").content.decode()
        self.assertIn("Tới sớm, bến trống", html)
