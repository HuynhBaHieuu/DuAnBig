"""4. LUỒNG B & C: ĐƠN BÁN -> FULFILLMENT -> OUTBOUND."""
from decimal import Decimal

from django.db import models

from apps.core.models import MONEY, QTY, TimeStamped


class Quotation(TimeStamped):
    STATUS = [("DRAFT", "Nháp"), ("SENT", "Đã gửi"), ("ACCEPTED", "Đã chốt"), ("REJECTED", "Từ chối"),
              ("EXPIRED", "Hết hạn")]
    code = models.CharField(max_length=20, unique=True)
    customer = models.ForeignKey("masterdata.Customer", on_delete=models.PROTECT, related_name="quotations")
    site = models.ForeignKey("masterdata.Site", null=True, blank=True, on_delete=models.PROTECT)
    status = models.CharField(max_length=10, choices=STATUS, default="DRAFT")
    valid_until = models.DateField(null=True, blank=True)
    total_amount = models.DecimalField(**MONEY)
    created_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "quotation"
        ordering = ["-id"]

    def __str__(self):
        return self.code


class QuotationLine(models.Model):
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT)
    qty = models.DecimalField(**QTY)
    unit_price = models.DecimalField(**MONEY)

    class Meta:
        db_table = "quotation_line"


class SalesOrder(TimeStamped):
    TYPES = [("B2B", "B2B"), ("INTERNAL", "Nội bộ công trình")]
    STATUS = [("DRAFT", "Nháp"), ("PENDING_APPROVAL", "Chờ duyệt"), ("CONFIRMED", "Đã xác nhận"),
              ("PARTIAL", "Giao một phần"), ("DELIVERED", "Đã giao"), ("CLOSED", "Đã đóng"), ("CANCELLED", "Đã hủy")]
    code = models.CharField(max_length=20, unique=True)
    quotation = models.OneToOneField(Quotation, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="sales_order")
    customer = models.ForeignKey("masterdata.Customer", on_delete=models.PROTECT, related_name="sales_orders")
    site = models.ForeignKey("masterdata.Site", on_delete=models.PROTECT, related_name="sales_orders")
    order_type = models.CharField(max_length=10, choices=TYPES, default="B2B")
    source = models.CharField(max_length=10, choices=[("INTERNAL", "Nội bộ"), ("PORTAL", "Cổng KH")],
                              default="INTERNAL")
    is_weekly_plan = models.BooleanField(default=False)
    status = models.CharField(max_length=20, choices=STATUS, default="DRAFT")
    order_date = models.DateField()
    requested_date = models.DateField()
    total_amount = models.DecimalField(**MONEY)
    approval_reasons = models.JSONField(default=list, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "sales_order"
        ordering = ["-id"]

    def __str__(self):
        return self.code

    def recalc_total(self):
        self.total_amount = sum((l.qty * l.unit_price for l in self.lines.all()), Decimal(0))
        return self.total_amount

    @property
    def delivery_status(self):
        """Trạng thái hiển thị cho khách: suy ra từ chuyến giao để Portal luôn khớp app tài xế.

        Trả về (code, nhãn): Đã xếp chuyến khi đơn được gắn vào lệnh điều xe, Đang giao khi xe đã xuất kho."""
        if self.status in ("DRAFT", "PENDING_APPROVAL", "CANCELLED", "DELIVERED", "CLOSED"):
            return self.status, self.get_status_display()
        trips = {s.trip.status for s in self.stops.all() if s.status == "PENDING"}
        if "IN_TRANSIT" in trips:
            return "IN_TRANSIT", "Đang giao"
        if trips & {"PLANNED", "PICKING", "READY"}:
            return "SCHEDULED", "Đã xếp chuyến"
        return self.status, self.get_status_display()

    @property
    def stock_status(self):
        """Đủ / Thiếu / Backorder – tổng hợp từ các dòng."""
        st = set(self.lines.values_list("atp_status", flat=True))
        if "BACKORDER" in st:
            return "BACKORDER"
        if "SHORT" in st:
            return "SHORT"
        return "OK" if st else ""


class SalesOrderLine(models.Model):
    ATP = [("OK", "Đủ hàng"), ("SHORT", "Thiếu"), ("BACKORDER", "Backorder")]
    so = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT)
    qty = models.DecimalField(**QTY)
    unit_price = models.DecimalField(**MONEY)
    atp_status = models.CharField(max_length=10, choices=ATP, blank=True)
    reserved_qty = models.DecimalField(**QTY)
    planned_qty = models.DecimalField(**QTY)
    delivered_qty = models.DecimalField(**QTY)
    invoiced_qty = models.DecimalField(**QTY)

    class Meta:
        db_table = "sales_order_line"

    @property
    def amount(self):
        return self.qty * self.unit_price

    @property
    def qty_to_plan(self):
        """Lượng đã giữ hàng nhưng chưa xếp chuyến."""
        return max(self.reserved_qty - self.planned_qty, Decimal(0))

    @property
    def backorder_qty(self):
        return max(self.qty - self.reserved_qty - self.delivered_qty, Decimal(0))


class StockReservation(models.Model):
    STATUS = [("ACTIVE", "Đang giữ"), ("CONSUMED", "Đã xuất"), ("RELEASED", "Đã nhả")]
    so_line = models.ForeignKey(SalesOrderLine, on_delete=models.CASCADE, related_name="reservations")
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT, related_name="reservations")
    qty = models.DecimalField(**QTY)
    status = models.CharField(max_length=10, choices=STATUS, default="ACTIVE")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "stock_reservation"


class Allocation(TimeStamped):
    STATUS = [("PROPOSED", "Chờ duyệt"), ("APPROVED", "Đã duyệt"), ("REJECTED", "Từ chối")]
    RULES = [("PENALTY", "Mốc phạt"), ("PRIORITY", "Ưu tiên công trình"), ("FIFO", "FIFO")]
    code = models.CharField(max_length=20, unique=True)
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT, related_name="allocations")
    status = models.CharField(max_length=10, choices=STATUS, default="PROPOSED")
    priority_rule = models.CharField(max_length=10, choices=RULES, default="PENALTY")
    available_qty = models.DecimalField(**QTY)
    demand_qty = models.DecimalField(**QTY)
    approved_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "allocation"
        ordering = ["-id"]

    def __str__(self):
        return self.code


class AllocationLine(models.Model):
    allocation = models.ForeignKey(Allocation, on_delete=models.CASCADE, related_name="lines")
    so_line = models.ForeignKey(SalesOrderLine, on_delete=models.PROTECT, related_name="allocation_lines")
    demand_qty = models.DecimalField(**QTY)
    proposed_qty = models.DecimalField(**QTY)
    approved_qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    rank = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "allocation_line"
        ordering = ["rank"]


class DeliveryTrip(TimeStamped):
    STATUS = [("PLANNED", "Đã lập"), ("PICKING", "Đang soạn"), ("READY", "Sẵn sàng xuất"),
              ("IN_TRANSIT", "Đang giao"), ("COMPLETED", "Hoàn tất"), ("CANCELLED", "Đã hủy")]
    code = models.CharField(max_length=20, unique=True)
    vehicle = models.ForeignKey("masterdata.Vehicle", on_delete=models.PROTECT, related_name="delivery_trips")
    driver = models.ForeignKey("masterdata.Driver", on_delete=models.PROTECT, related_name="delivery_trips")
    planned_date = models.DateField()
    status = models.CharField(max_length=12, choices=STATUS, default="PLANNED")
    departed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "delivery_trip"
        ordering = ["-id"]

    def __str__(self):
        return self.code

    @property
    def total_ton(self):
        kg = sum((l.qty_planned * l.so_line.product.unit_weight_kg
                  for s in self.stops.all() for l in s.lines.all()), Decimal(0))
        return kg / 1000


class DeliveryStop(models.Model):
    STATUS = [("PENDING", "Chờ giao"), ("DELIVERED", "Đã giao"), ("PARTIAL", "Giao một phần"), ("FAILED", "Thất bại")]
    trip = models.ForeignKey(DeliveryTrip, on_delete=models.CASCADE, related_name="stops")
    seq = models.PositiveSmallIntegerField(default=1)
    so = models.ForeignKey(SalesOrder, on_delete=models.PROTECT, related_name="stops")
    site = models.ForeignKey("masterdata.Site", on_delete=models.PROTECT)
    status = models.CharField(max_length=10, choices=STATUS, default="PENDING")
    eta = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    receiver_name = models.CharField(max_length=100, blank=True)
    pod_photo = models.ImageField(upload_to="pod/%Y/%m/", null=True, blank=True)
    pod_signature = models.ImageField(upload_to="signatures/%Y/%m/", null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "delivery_stop"
        ordering = ["trip", "seq"]


class DeliveryLine(models.Model):
    stop = models.ForeignKey(DeliveryStop, on_delete=models.CASCADE, related_name="lines")
    so_line = models.ForeignKey(SalesOrderLine, on_delete=models.PROTECT, related_name="delivery_lines")
    qty_planned = models.DecimalField(**QTY)
    qty_delivered = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)

    class Meta:
        db_table = "delivery_line"


class Picking(TimeStamped):
    STATUS = [("DRAFT", "Chờ xử lý"), ("IN_PROGRESS", "Đang soạn"), ("DONE", "Đã soạn")]
    code = models.CharField(max_length=20, unique=True)
    trip = models.OneToOneField(DeliveryTrip, on_delete=models.CASCADE, related_name="picking")
    status = models.CharField(max_length=12, choices=STATUS, default="DRAFT")
    picker = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "picking"
        ordering = ["-id"]

    def __str__(self):
        return self.code


class PickingLine(models.Model):
    picking = models.ForeignKey(Picking, on_delete=models.CASCADE, related_name="lines")
    so_line = models.ForeignKey(SalesOrderLine, on_delete=models.PROTECT)
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT)
    location = models.ForeignKey("masterdata.Location", on_delete=models.PROTECT)
    qty_to_pick = models.DecimalField(**QTY)
    qty_picked = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)

    class Meta:
        db_table = "picking_line"
        ordering = ["location__code"]


class GoodsIssue(TimeStamped):
    STATUS = [("DRAFT", "Nháp"), ("POSTED", "Đã xuất kho"), ("CANCELLED", "Đã hủy")]
    code = models.CharField(max_length=20, unique=True)
    trip = models.OneToOneField(DeliveryTrip, on_delete=models.PROTECT, related_name="goods_issue")
    status = models.CharField(max_length=10, choices=STATUS, default="DRAFT")
    posted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "goods_issue"
        ordering = ["-id"]

    def __str__(self):
        return self.code


class GoodsIssueLine(models.Model):
    gi = models.ForeignKey(GoodsIssue, on_delete=models.CASCADE, related_name="lines")
    so_line = models.ForeignKey(SalesOrderLine, on_delete=models.PROTECT)
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT)
    location = models.ForeignKey("masterdata.Location", on_delete=models.PROTECT)
    qty = models.DecimalField(**QTY)
    unit_cost = models.DecimalField(**MONEY)

    class Meta:
        db_table = "goods_issue_line"


class SalesReturn(TimeStamped):
    STATUS = [("DRAFT", "Nháp"), ("POSTED", "Đã nhập lại"), ("CANCELLED", "Đã hủy")]
    code = models.CharField(max_length=20, unique=True)
    so = models.ForeignKey(SalesOrder, on_delete=models.PROTECT, related_name="returns")
    stop = models.ForeignKey(DeliveryStop, null=True, blank=True, on_delete=models.PROTECT, related_name="returns")
    status = models.CharField(max_length=10, choices=STATUS, default="DRAFT")
    reason = models.CharField(max_length=255)

    class Meta:
        db_table = "sales_return"
        ordering = ["-id"]

    def __str__(self):
        return self.code


class SalesReturnLine(models.Model):
    ret = models.ForeignKey(SalesReturn, on_delete=models.CASCADE, related_name="lines")
    so_line = models.ForeignKey(SalesOrderLine, on_delete=models.PROTECT)
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT)
    location = models.ForeignKey("masterdata.Location", on_delete=models.PROTECT)
    qty = models.DecimalField(**QTY)

    class Meta:
        db_table = "sales_return_line"
