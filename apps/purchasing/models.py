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
    # dòng PO mà dòng PR này đã được GỘP vào (mọi dòng gộp, không chỉ dòng đầu) – để hủy PO trả lại đúng PR
    merged_into = models.ForeignKey("PurchaseOrderLine", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="merged_pr_lines")

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
    # thuế suất VAT của PO – chốt lúc lập PO theo tham số PO_VAT_RATE; hóa đơn mua lấy theo đây (STT 36)
    vat_rate = models.DecimalField(max_digits=4, decimal_places=1, default=10)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    auto_approved = models.BooleanField(default=False)  # tự duyệt vì dưới ngưỡng PO_APPROVAL_THRESHOLD (STT 29)
    sent_at = models.DateTimeField(null=True, blank=True)
    # Nhà máy (tài khoản NCC) xác nhận đã nhận đơn (STT 64–68)
    supplier_ack_at = models.DateTimeField(null=True, blank=True)
    supplier_ack_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    supplier_ack_note = models.CharField(max_length=255, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "purchase_order"
        ordering = ["-id"]

    def __str__(self):
        return self.code

    def recalc_total(self):
        self.total_amount = sum((l.qty * l.unit_price for l in self.lines.all()), Decimal(0))
        return self.total_amount

    def save(self, *args, **kwargs):
        if self._state.adding and not kwargs.get("update_fields"):
            from apps.core.services import param
            self.vat_rate = Decimal(str(param("PO_VAT_RATE") or 10))
        super().save(*args, **kwargs)

    @property
    def vat_amount(self):
        """VAT đơn mua: giá mua lưu chưa VAT, thuế suất = vat_rate của PO (chốt theo PO_VAT_RATE lúc lập) – G3."""
        return (self.total_amount * Decimal(str(self.vat_rate)) / 100).quantize(Decimal(1))

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
    # phần thiếu / hỏng đã chuyển khiếu nại và ĐÓNG (NCC không giao bù) – không còn tính là "Đang về" (STT 61)
    qty_closed = models.DecimalField(**QTY)

    class Meta:
        db_table = "purchase_order_line"

    @property
    def amount(self):
        return self.qty * self.unit_price

    @property
    def qty_open(self):
        return max(self.qty - self.qty_received - self.qty_closed, Decimal(0))


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
    planned_at = models.DateTimeField()  # kế hoạch gốc – giữ nguyên cho báo cáo
    # dự kiến lại khi mốc trước thực tế SỚM hơn kế hoạch > ngưỡng (các mốc sau tính theo mốc thực tế)
    replanned_at = models.DateTimeField(null=True, blank=True)
    actual_at = models.DateTimeField(null=True, blank=True)
    photo = models.ImageField(upload_to="milestones/%Y/%m/", null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    by_user = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        db_table = "trip_milestone"
        ordering = ["trip", "seq"]

    @property
    def expected_at(self):
        """Giờ dự kiến đang hiệu lực: dự kiến lại (nếu có) hoặc kế hoạch gốc."""
        return self.replanned_at or self.planned_at

    @property
    def delay_min(self):
        """Lệch = thực tế − dự kiến (phút, âm = sớm)."""
        if self.actual_at:
            return round((self.actual_at - self.expected_at).total_seconds() / 60)
        return None

    def timing(self, tol=None):
        """('ONTIME' | 'EARLY' | 'LATE', số phút lệch tuyệt đối) – None nếu chưa có thực tế.
        Lệch trong [−tol; +tol] phút là đúng giờ (mặc định tol = MILESTONE_ONTIME_MIN = 30)."""
        d = self.delay_min
        if d is None:
            return None
        if tol is None:
            from apps.core.services import param
            tol = param("MILESTONE_ONTIME_MIN")
        if d < -tol:
            return "EARLY", -d
        if d > tol:
            return "LATE", d
        return "ONTIME", abs(d)


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
    checkin_reason = models.CharField("Lý do check-in lệch slot", max_length=255, blank=True)
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
    # Loại khỏi thống kê lead time (chuyến test / dữ liệu bất thường) – không dùng cho SS/ROP và báo cáo lead time
    exclude_from_stats = models.BooleanField(default=False)
    exclude_reason = models.CharField(max_length=255, blank=True)
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
    STATUS = [("OPEN", "Chờ GĐ duyệt"), ("APPROVED", "Đã duyệt"), ("SENT", "Đã gửi"), ("COMPENSATED", "Đã bồi thường"),
              ("CLOSED", "Đóng"), ("CANCELLED", "Đã hủy")]
    code = models.CharField(max_length=20, unique=True)
    gr = models.ForeignKey(GoodsReceipt, on_delete=models.PROTECT, related_name="claims")
    claim_type = models.CharField(max_length=10, choices=TYPES)
    against = models.CharField(max_length=10, choices=[("SUPPLIER", "Nhà cung cấp"), ("CARRIER", "Nhà xe")])
    product = models.ForeignKey("masterdata.Product", null=True, blank=True, on_delete=models.PROTECT)
    qty = models.DecimalField(**QTY)
    amount = models.DecimalField(**MONEY)
    status = models.CharField(max_length=12, choices=STATUS, default="OPEN")
    description = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    compensated_amount = models.DecimalField(**MONEY)
    compensated_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True)
    # chứng từ giảm trừ công nợ phải trả NCC / nhà xe khi được bồi thường
    credit_bill = models.OneToOneField("accounting.Bill", null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="claim")

    class Meta:
        db_table = "claim"
        ordering = ["-id"]

    def __str__(self):
        return self.code

    @property
    def partner(self):
        """Đối tượng bị khiếu nại: NCC của PO, hoặc nhà xe 3PL của chuyến (None nếu xe nội bộ)."""
        if self.against == "SUPPLIER":
            return self.gr.po.supplier
        return self.gr.trip.carrier if self.gr.trip_id else None
