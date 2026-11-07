"""3. LUỒNG A: MUA HÀNG -> INBOUND (không GPS) -> NHẬP KHO."""
from decimal import Decimal

from django.db import models

from apps.core.models import MONEY, QTY, TimeStamped


class PurchaseRequisition(TimeStamped):
    STATUS = [("DRAFT", "Nháp"), ("SUBMITTED", "Đã gửi"), ("CONVERTED", "Đã lập PO"), ("REJECTED", "Bị từ chối"),
              ("CANCELLED", "Đã hủy")]
    code = models.CharField(max_length=20, unique=True)
    status = models.CharField(max_length=15, choices=STATUS, default="DRAFT")
    source = models.CharField(max_length=10, choices=[("AUTO", "Tự động (ROP)"), ("MANUAL", "Thủ công")],
                              default="MANUAL")
    need_date = models.DateField(null=True, blank=True)
    requested_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    note = models.CharField(max_length=255, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)
    # --- Ghi vết PR tự động (test/logic.docx mục 7.1). PR AUTO: requested_by rỗng = hệ thống tạo ---
    urgency = models.CharField(max_length=10, choices=[("NORMAL", "Thường"), ("URGENT", "Khẩn")], default="NORMAL")
    trigger_event = models.CharField(max_length=30, blank=True)
    trigger_ref_type = models.CharField(max_length=10, blank=True)
    trigger_ref_code = models.CharField(max_length=40, blank=True, db_index=True)
    trigger_user = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    trigger_at = models.DateTimeField(null=True, blank=True)
    snapshot = models.JSONField(default=dict, blank=True, help_text="Số liệu lúc tạo PR (chỉ số lượng, không lưu giá)")
    missing_supplier = models.BooleanField(default=False)
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    rejected_position = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    reject_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "purchase_requisition"
        ordering = ["-id"]

    def __str__(self):
        return self.code


class PurchaseRequisitionLine(models.Model):
    pr = models.ForeignKey(PurchaseRequisition, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT)
    qty = models.DecimalField(**QTY)
    suggested_supplier = models.ForeignKey("masterdata.Supplier", null=True, blank=True, on_delete=models.SET_NULL)
    po_line = models.OneToOneField("PurchaseOrderLine", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="pr_line")

    class Meta:
        db_table = "purchase_requisition_line"


class PurchaseOrder(TimeStamped):
    STATUS = [("DRAFT", "Nháp"), ("PENDING_APPROVAL", "Chờ duyệt"), ("APPROVED", "Đã duyệt"), ("SENT", "Đã gửi NCC"),
              ("PARTIAL", "Nhận một phần"), ("RECEIVED", "Đã nhận đủ"), ("CLOSED", "Đã đóng"), ("CANCELLED", "Đã hủy")]
    code = models.CharField(max_length=20, unique=True)
    supplier = models.ForeignKey("masterdata.Supplier", on_delete=models.PROTECT, related_name="purchase_orders")
    status = models.CharField(max_length=20, choices=STATUS, default="DRAFT")
    order_date = models.DateField()
    pickup_date = models.DateField(null=True, blank=True, help_text="Ngày bốc hẹn tại NCC")
    route = models.ForeignKey("masterdata.Route", null=True, blank=True, on_delete=models.PROTECT)
    warehouse = models.ForeignKey("masterdata.Warehouse", on_delete=models.PROTECT)
    total_amount = models.DecimalField(**MONEY)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "purchase_order"
        ordering = ["-id"]

    def __str__(self):
        return self.code

    def recalc_total(self):
        self.total_amount = sum((l.qty * l.unit_price for l in self.lines.all()), Decimal(0))
        return self.total_amount

    @property
    def vat_amount(self):
        """VAT đơn mua: giá mua lưu chưa VAT, thuế suất = tham số PO_VAT_RATE (mặc định 10%) – giả định G3."""
        from apps.core.services import param
        return (self.total_amount * Decimal(str(param("PO_VAT_RATE"))) / 100).quantize(Decimal(1))

    @property
    def total_with_vat(self):
        return self.total_amount + self.vat_amount

    @property
    def received_pct(self):
        ordered = sum((l.qty for l in self.lines.all()), Decimal(0))
        got = sum((l.qty_received for l in self.lines.all()), Decimal(0))
        return int(got / ordered * 100) if ordered else 0


class PurchaseOrderLine(models.Model):
    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT)
    qty = models.DecimalField(**QTY)
    unit_price = models.DecimalField(**MONEY)
    qty_on_trips = models.DecimalField(**QTY)
    qty_received = models.DecimalField(**QTY)
    qty_billed = models.DecimalField(**QTY)

    class Meta:
        db_table = "purchase_order_line"

    @property
    def amount(self):
        return self.qty * self.unit_price

    @property
    def qty_open(self):
        return max(self.qty - self.qty_received, Decimal(0))


class InboundTrip(TimeStamped):
    STATUS = [("PLANNED", "Chờ bốc"), ("AT_FACTORY", "Tại nhà máy"), ("LOADED", "Đã bốc"),
              ("IN_TRANSIT", "Đang vận chuyển"), ("ARRIVED", "Tới cổng"), ("UNLOADING", "Đang hạ tải"),
              ("RECEIVED", "Đã nhập kho"), ("CANCELLED", "Đã hủy")]
    # 5 cột Kanban của bảng điều phối
    KANBAN = [("Chờ bốc", ["PLANNED", "AT_FACTORY"]), ("Đã bốc", ["LOADED"]), ("Đang đi", ["IN_TRANSIT"]),
              ("Tới cổng / Hạ tải", ["ARRIVED", "UNLOADING"]), ("Đã nhập kho", ["RECEIVED"])]
    code = models.CharField(max_length=20, unique=True)
    po = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="trips")
    route = models.ForeignKey("masterdata.Route", on_delete=models.PROTECT, related_name="inbound_trips")
    carrier = models.ForeignKey("masterdata.Carrier", null=True, blank=True, on_delete=models.PROTECT)
    vehicle = models.ForeignKey("masterdata.Vehicle", on_delete=models.PROTECT, related_name="inbound_trips")
    driver = models.ForeignKey("masterdata.Driver", on_delete=models.PROTECT, related_name="inbound_trips")
    status = models.CharField(max_length=15, choices=STATUS, default="PLANNED")
    planned_departure = models.DateTimeField()
    eta = models.DateTimeField(null=True, blank=True)
    arrived_at = models.DateTimeField(null=True, blank=True)
    total_ton = models.DecimalField(max_digits=10, decimal_places=3, default=0)
    freight_amount = models.DecimalField(**MONEY)
    is_late = models.BooleanField(default=False)
    cancel_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "inbound_trip"
        ordering = ["-id"]

    def __str__(self):
        return self.code

    ACTIVE = ("PLANNED", "AT_FACTORY", "LOADED", "IN_TRANSIT")

    @property
    def late_active(self):
        """Nhãn "Trễ mốc" chỉ có nghĩa khi chuyến còn đang chạy; chuyến đã tới kho / nhập kho chỉ hiện trạng thái vòng đời."""
        return self.is_late and self.status in self.ACTIVE

    @property
    def load_pct(self):
        cap = self.vehicle.capacity_ton
        return float(self.total_ton / cap * 100) if cap else 0

    @property
    def next_milestone(self):
        done = set(self.milestones.exclude(actual_at=None).values_list("seq", flat=True))
        return self.milestones.exclude(seq__in=done).order_by("seq").first()


class InboundTripLine(models.Model):
    trip = models.ForeignKey(InboundTrip, on_delete=models.CASCADE, related_name="lines")
    po_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT, related_name="trip_lines")
    qty_planned = models.DecimalField(**QTY)
    qty_loaded = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)

    class Meta:
        db_table = "inbound_trip_line"


class TripMilestone(models.Model):
    trip = models.ForeignKey(InboundTrip, on_delete=models.CASCADE, related_name="milestones")
    seq = models.PositiveSmallIntegerField()
    code = models.CharField(max_length=15)
    name = models.CharField(max_length=120)
    photo_required = models.BooleanField(default=False)
    planned_at = models.DateTimeField()
    actual_at = models.DateTimeField(null=True, blank=True)
    photo = models.ImageField(upload_to="milestones/%Y/%m/", null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    by_user = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        db_table = "trip_milestone"
        ordering = ["trip", "seq"]

    @property
    def delay_min(self):
        if self.actual_at:
            return int((self.actual_at - self.planned_at).total_seconds() // 60)
        return None


class TripIncident(models.Model):
    TYPES = [("BREAKDOWN", "Hỏng xe"), ("TRAFFIC", "Kẹt xe"), ("ACCIDENT", "Tai nạn"), ("WEATHER", "Thời tiết"),
             ("FACTORY_DELAY", "Chờ bốc lâu"), ("OTHER", "Khác")]
    trip = models.ForeignKey(InboundTrip, on_delete=models.CASCADE, related_name="incidents")
    incident_type = models.CharField(max_length=15, choices=TYPES)
    description = models.TextField(blank=True)
    delay_minutes = models.PositiveIntegerField(default=0)
    photo = models.ImageField(upload_to="incidents/%Y/%m/", null=True, blank=True)
    reported_at = models.DateTimeField(auto_now_add=True)
    reported_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL)
    is_resolved = models.BooleanField(default=False)

    class Meta:
        db_table = "trip_incident"
        ordering = ["-reported_at"]


class TripEtaLog(models.Model):
    trip = models.ForeignKey(InboundTrip, on_delete=models.CASCADE, related_name="eta_logs")
    at = models.DateTimeField(auto_now_add=True)
    old_eta = models.DateTimeField(null=True, blank=True)
    new_eta = models.DateTimeField()
    reason = models.CharField(max_length=255)

    class Meta:
        db_table = "trip_eta_log"
        ordering = ["-at"]


class UnloadingSlot(models.Model):
    STATUS = [("PROPOSED", "Đề xuất"), ("CONFIRMED", "Đã xác nhận"), ("CHECKED_IN", "Đã check-in"),
              ("DONE", "Hoàn tất"), ("CANCELLED", "Đã hủy")]
    trip = models.OneToOneField(InboundTrip, on_delete=models.CASCADE, related_name="slot")
    dock = models.ForeignKey("masterdata.Dock", on_delete=models.PROTECT, related_name="slots")
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    status = models.CharField(max_length=12, choices=STATUS, default="PROPOSED")
    checked_in_at = models.DateTimeField(null=True, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "unloading_slot"
        ordering = ["start_at"]

    @property
    def wait_minutes(self):
        """Thời gian chờ hạ tải: từ lúc tới cổng đến lúc check-in."""
        if self.checked_in_at and self.trip.arrived_at:
            return max(0, int((self.checked_in_at - self.trip.arrived_at).total_seconds() // 60))
        return None


class GoodsReceipt(TimeStamped):
    STATUS = [("DRAFT", "Nháp"), ("PENDING_APPROVAL", "Chờ duyệt chênh lệch"), ("POSTED", "Đã nhập kho"),
              ("CANCELLED", "Đã hủy")]
    code = models.CharField(max_length=20, unique=True)
    trip = models.ForeignKey(InboundTrip, null=True, blank=True, on_delete=models.PROTECT, related_name="receipts")
    po = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="receipts")
    warehouse = models.ForeignKey("masterdata.Warehouse", on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=STATUS, default="DRAFT")
    received_at = models.DateTimeField()
    posted_at = models.DateTimeField(null=True, blank=True)
    lead_time_days = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cancel_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "goods_receipt"
        ordering = ["-id"]

    def __str__(self):
        return self.code

    @property
    def max_variance_pct(self):
        return max((l.variance_pct for l in self.lines.all()), default=0)


class GoodsReceiptLine(models.Model):
    gr = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="lines")
    po_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT, related_name="receipt_lines")
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT)
    location = models.ForeignKey("masterdata.Location", on_delete=models.PROTECT)
    qty_expected = models.DecimalField(**QTY)
    qty_received = models.DecimalField(**QTY)
    qty_damaged = models.DecimalField(**QTY)
    unit_cost = models.DecimalField(**MONEY)

    class Meta:
        db_table = "goods_receipt_line"

    @property
    def qty_good(self):
        return self.qty_received - self.qty_damaged

    @property
    def variance(self):
        return self.qty_good - self.qty_expected

    @property
    def variance_pct(self):
        if not self.qty_expected:
            return 0
        return float(abs(self.variance) / self.qty_expected * 100)


class Claim(TimeStamped):
    TYPES = [("SHORTAGE", "Thiếu hàng"), ("DAMAGE", "Hư hỏng"), ("QUALITY", "Chất lượng")]
    STATUS = [("OPEN", "Đang xử lý"), ("SETTLED", "Đã giải quyết"), ("CANCELLED", "Đã hủy")]
    code = models.CharField(max_length=20, unique=True)
    gr = models.ForeignKey(GoodsReceipt, on_delete=models.PROTECT, related_name="claims")
    claim_type = models.CharField(max_length=10, choices=TYPES)
    against = models.CharField(max_length=10, choices=[("SUPPLIER", "Nhà cung cấp"), ("CARRIER", "Nhà xe")])
    amount = models.DecimalField(**MONEY)
    status = models.CharField(max_length=10, choices=STATUS, default="OPEN")
    description = models.TextField(blank=True)

    class Meta:
        db_table = "claim"
        ordering = ["-id"]
