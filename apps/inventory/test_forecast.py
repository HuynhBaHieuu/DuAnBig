"""Kiểm thử & tiêu chí nghiệm thu – đặc tả "Sửa mô hình dự báo nhu cầu & SS/ROP" (mục 10)."""
from datetime import date, timedelta
from decimal import Decimal as D

from django.test import Client, SimpleTestCase
from django.utils import timezone

from apps.sales.models import SalesOrder, SalesOrderLine

from . import forecast as fc
from . import test_tracon_fixes as tf
from .models import ReplenishmentParam, SimulatedDemand
from .services import calc_param, lead_times, weekly_demand
from .simulation import MONTH_FACTORS, generate_rows, parse_csv

TODAY = timezone.localdate
W0 = date(2024, 1, 1)  # thứ Hai


def weeks(n, start=W0):
    return [start + timedelta(days=7 * i) for i in range(n)]


# ======================================================================= hàm thuần (không đọc DB)
class PureForecastTest(SimpleTestCase):
    def test_constant_series(self):
        r = fc.forecast(weeks(30), [10.0] * 30)
        self.assertEqual(r.model, "HOLT")
        self.assertAlmostEqual(r.forecast_weeks[0], 10, places=6)
        self.assertAlmostEqual(r.trend, 0, places=6)
        self.assertEqual(r.wape, 0)

    def test_linear_growth_holt_beats_moving_average(self):
        y = [float(i + 1) for i in range(40)]
        r = fc.forecast(weeks(40), y)
        self.assertLess(abs(r.forecast_weeks[0] - 41) / 41, 0.05)       # tuần tới sai dưới 5%
        self.assertLess(r.wape, r.wape_baseline / 10)                   # TB 13 tuần sai nhiều hơn rõ rệt

    def test_model_by_weeks(self):
        self.assertEqual(fc.forecast(weeks(5), [3.0, 4, 5, 6, 2]).model, "MA")
        self.assertEqual(fc.forecast(weeks(8), [3.0] * 8).model, "HOLT")
        self.assertEqual(fc.forecast(weeks(104), [3.0] * 104).model, "HW")
        ma = fc.forecast(weeks(4), [2.0, 4, 6, 8])
        self.assertEqual((ma.forecast_weeks[0], ma.wape), (5.0, None))
        self.assertAlmostEqual(ma.sigma_e_week, 2.581988, places=5)    # độ lệch chuẩn mẫu nhu cầu tuần

    def test_forecast_never_negative_and_13_weeks(self):
        r = fc.forecast(weeks(20), [float(40 - 2 * i) for i in range(20)])
        self.assertEqual(len(r.forecast_weeks), 13)
        self.assertTrue(all(x >= 0 for x in r.forecast_weeks))

    def test_simulated_file_seasonality_and_wape(self):
        """File mô phỏng mục 9: 12 chỉ số mùa lệch ≤ 0,1 so với bảng đặt; WAPE của HW < cách cũ.
        (Tuần đột biến được làm sạch như weekly_demand làm với dữ liệu mô phỏng.)"""
        import statistics

        from .services import outlier_cut
        rows = generate_rows()
        w, y = [r[0] for r in rows], [r[1] for r in rows]
        cut = outlier_cut(y)
        y = [statistics.median([y[j] for j in range(max(0, i - 4), min(len(y), i + 5)) if j != i and y[j] <= cut])
             if v > cut else v for i, v in enumerate(y)]
        r = fc.forecast(w, y)
        self.assertEqual(r.model, "HW")
        self.assertLessEqual(max(abs(a - b) for a, b in zip(r.seasonal, MONTH_FACTORS)), 0.1)
        self.assertLess(r.wape, r.wape_baseline)

    def test_ss_rop_q_formula(self):
        r = fc.forecast(weeks(30), [70.0] * 30)                          # 70 / tuần = 10 / ngày
        v = fc.ss_rop_q(r, method="FULL", z=1.65, lt=3, lt_std=0.5, today=date(2024, 8, 1), review_days=7, moq=0)
        self.assertAlmostEqual(v["d_lt"], 30, places=6)
        self.assertAlmostEqual(v["f_day"], 10, places=6)
        self.assertAlmostEqual(v["ss"], 1.65 * 10 * 0.5, places=6)       # σe = 0 -> chỉ còn phần lead time
        self.assertAlmostEqual(v["rop"], 30 + 8.25, places=6)
        self.assertEqual(v["q"], 70)

    def test_csv_parse(self):
        rows = parse_csv(b"week_start,qty\n2026-01-07,12.5\n12/01/2026,3\n")
        self.assertEqual(rows, [(date(2026, 1, 5), D("12.5")), (date(2026, 1, 12), D(3))])  # ngày bất kỳ -> thứ Hai
        with self.assertRaises(Exception):
            parse_csv(b"week_start,qty\n2026-01-05,1\n2026-01-06,2\n")   # trùng tuần


# ======================================================================= dữ liệu nhu cầu (DB)
class DemandDataTest(tf.Base):
    def order(self, qty, status, when):
        so = SalesOrder.objects.create(code=f"SO-F{SalesOrder.objects.count()}", customer=self.customer, site=self.site,
                                       order_date=when.date(), requested_date=when.date(), status=status)
        SalesOrder.objects.filter(pk=so.pk).update(confirmed_at=when if status != "DRAFT" else None)
        SalesOrderLine.objects.create(so=so, product=self.product, qty=D(qty), unit_price=1)
        return so

    def test_only_confirmed_orders_zero_weeks_and_no_current_week(self):
        now = timezone.now()
        mon = fc.monday(TODAY())
        at = lambda wk: timezone.make_aware(timezone.datetime.combine(mon - timedelta(days=7 * wk) + timedelta(days=1),
                                                                      timezone.datetime.min.time()).replace(hour=10))
        self.order(30, "CONFIRMED", at(3))
        self.order(20, "DELIVERED", at(1))
        self.order(99, "DRAFT", at(1))
        self.order(99, "PENDING_APPROVAL", at(1))
        self.order(99, "CANCELLED", at(1))
        self.order(5, "CONFIRMED", now)                                  # tuần hiện tại: không dùng huấn luyện
        d = weekly_demand([self.product])[self.product.pk]
        self.assertEqual(d.values, [30.0, 0.0, 20.0])                    # tuần không có đơn = 0
        self.assertEqual(d.weeks[-1], mon - timedelta(days=7))

    def test_short_confirmed_order_still_counts(self):
        so = self.so(500, days=1)                                         # thiếu hàng nhưng đã xác nhận
        SalesOrder.objects.filter(pk=so.pk).update(confirmed_at=timezone.now() - timedelta(days=7))
        d = weekly_demand([self.product])[self.product.pk]
        self.assertEqual(sum(d.values), 500.0)

    def test_lead_time_ignores_short_test_receipts(self):
        from apps.purchasing.models import GoodsReceipt, GoodsReceiptLine, PurchaseOrder, PurchaseOrderLine
        po = PurchaseOrder.objects.create(code="PO-LT", supplier=self.supplier, order_date=TODAY(), warehouse=self.wh)
        pol = PurchaseOrderLine.objects.create(po=po, product=self.product, qty=100, unit_price=1)
        for i, lt in enumerate(["0.02", "0.1", "0.2"]):
            gr = GoodsReceipt.objects.create(code=f"GRN-LT{i}", po=po, warehouse=self.wh, received_at=timezone.now(),
                                             status="POSTED", posted_at=timezone.now(), lead_time_days=D(lt))
            GoodsReceiptLine.objects.create(gr=gr, po_line=pol, product=self.product, location=self.loc,
                                            qty_expected=1, qty_received=1, unit_cost=1)
        self.assertEqual(lead_times(self.product), (None, None))         # 3 phiếu < 0,25 ngày: không đổi LT
        rp, _ = calc_param(self.product)
        self.assertEqual(rp.lead_time_days, D("3.00"))

    def test_locked_keeps_manual_threshold(self):
        rp, _ = calc_param(self.product)
        self.assertEqual((rp.eff_ss, rp.eff_rop), (D(20), D(60)))

    def test_simulated_isolated(self):
        for i, (w, q) in enumerate(generate_rows()[:20]):
            SimulatedDemand.objects.create(product=self.product, week_start=w, qty=q)
        self.so(10, days=1)
        d = weekly_demand([self.product])[self.product.pk]
        self.assertTrue(d.simulated)
        self.assertEqual(len(d.values), (fc.monday(TODAY()) - generate_rows()[0][0]).days // 7)
        other = tf.m.Product.objects.create(code="REAL", name="Thật", category=self.product.category,
                                            base_uom=self.product.base_uom, unit_weight_kg=1000, track_rop=True)
        self.assertFalse(weekly_demand([other])[other.pk].simulated)    # mã thật không đọc SimulatedDemand
        from apps.inventory.replenishment import check_replenishment, Trigger
        self.assertIsNone(check_replenishment(self.product.pk, Trigger("NIGHTLY_JOB")))  # mô phỏng: không tạo PR

    def test_screen_excel_calc_match(self):
        self.so(70, days=1)
        rp, _ = calc_param(self.product)
        from .services import rop_explain
        _, _, v = rop_explain(rp)
        self.assertAlmostEqual(float(rp.ss), v["ss"], places=2)
        self.assertAlmostEqual(float(rp.rop), v["rop"], places=2)
        c = Client()
        c.force_login(self.purchaser)
        r = c.get(f"/app/inventory/replenishment?product={self.product.pk}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Dự báo và thực tế", r.content.decode())
        x = c.get(f"/app/inventory/replenishment?product={self.product.pk}&export=xlsx")
        from io import BytesIO

        from openpyxl import load_workbook
        wb = load_workbook(BytesIO(x.content))
        self.assertEqual(set(wb.sheetnames) >= {"SS_ROP", "NhuCau", "MuaVu"}, True)
        vals = {row[0].value: row[1].value for row in wb["SS_ROP"].iter_rows(min_row=2)}
        self.assertAlmostEqual(vals["SS tự tính"], float(rp.ss), places=2)
        self.assertFalse(any(isinstance(c.value, str) and c.value.startswith("=")
                             for row in wb["SS_ROP"].iter_rows() for c in row))  # chỉ xuất giá trị

    def test_csv_import_director_only(self):
        c = Client()
        c.force_login(self.purchaser)
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("d.csv", b"week_start,qty\n2026-01-05,10\n", content_type="text/csv")
        c.post(f"/app/inventory/replenishment/{self.product.pk}", {"op": "import_csv", "csv": f})
        self.assertFalse(SimulatedDemand.objects.exists())
        c.force_login(self.director)
        f.seek(0)
        c.post(f"/app/inventory/replenishment/{self.product.pk}", {"op": "import_csv", "csv": f})
        self.assertEqual(SimulatedDemand.objects.count(), 1)
        c.post(f"/app/inventory/replenishment/{self.product.pk}", {"op": "create_sim"})
        self.assertEqual(SimulatedDemand.objects.filter(product__code="SIM-PCB40").count(), 104)
