"""5. KẾ TOÁN CÔNG NỢ."""
from django.db import models

from apps.core.models import MONEY, QTY, TimeStamped


class Invoice(TimeStamped):
    TYPES = [("SALE", "Hóa đơn bán"), ("CREDIT_NOTE", "Giảm trừ (trả hàng)")]
    STATUS = [("DRAFT", "Nháp"), ("POSTED", "Đã ghi sổ"), ("PARTIAL", "Thu một phần"), ("PAID", "Đã thu đủ"),
              ("CANCELLED", "Đã hủy")]
    code = models.CharField(max_length=20, unique=True)
    customer = models.ForeignKey("masterdata.Customer", on_delete=models.PROTECT, related_name="invoices")
    invoice_type = models.CharField(max_length=12, choices=TYPES, default="SALE")
    sales_return = models.OneToOneField("sales.SalesReturn", null=True, blank=True, on_delete=models.PROTECT,
                                        related_name="credit_note")
    status = models.CharField(max_length=10, choices=STATUS, default="DRAFT")
    invoice_date = models.DateField()
    due_date = models.DateField()
    subtotal = models.DecimalField(**MONEY)
    vat_rate = models.DecimalField(max_digits=4, decimal_places=1, default=8)
    vat_amount = models.DecimalField(**MONEY)
    total = models.DecimalField(**MONEY)
    paid_amount = models.DecimalField(**MONEY)
    cancel_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "invoice"
        ordering = ["-id"]

    def __str__(self):
        return self.code

    @property
    def balance(self):
        sign = -1 if self.invoice_type == "CREDIT_NOTE" else 1
        return sign * (self.total - self.paid_amount)


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    delivery_line = models.OneToOneField("sales.DeliveryLine", null=True, blank=True, on_delete=models.PROTECT,
                                         related_name="invoice_line")
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT)
    qty = models.DecimalField(**QTY)
    unit_price = models.DecimalField(**MONEY)
    amount = models.DecimalField(**MONEY)

    class Meta:
        db_table = "invoice_line"


class Bill(TimeStamped):
    TYPES = [("GOODS", "Hóa đơn vật tư"), ("FREIGHT", "Cước vận chuyển")]
    STATUS = [("DRAFT", "Nháp"), ("MATCHED", "Khớp 3 bên"), ("MISMATCH", "Lệch – chờ duyệt"),
              ("ACCEPTED", "Director chấp nhận lệch"), ("POSTED", "Đã ghi sổ"), ("PARTIAL", "Trả một phần"),
              ("PAID", "Đã trả đủ"), ("CANCELLED", "Đã hủy")]
    code = models.CharField(max_length=20, unique=True)
    bill_type = models.CharField(max_length=10, choices=TYPES, default="GOODS")
    supplier = models.ForeignKey("masterdata.Supplier", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="bills")
    carrier = models.ForeignKey("masterdata.Carrier", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="bills")
    vendor_invoice_no = models.CharField(max_length=40, blank=True)
    bill_date = models.DateField()
    due_date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS, default="DRAFT")
    subtotal = models.DecimalField(**MONEY)
    vat_amount = models.DecimalField(**MONEY)
    total = models.DecimalField(**MONEY)
    paid_amount = models.DecimalField(**MONEY)
    accepted_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cancel_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "bill"
        ordering = ["-id"]

    def __str__(self):
        return self.code

    @property
    def partner(self):
        return self.supplier or self.carrier

    @property
    def balance(self):
        return self.total - self.paid_amount


class BillLine(models.Model):
    MATCH = [("MATCH", "Khớp"), ("QTY_DIFF", "Lệch số lượng"), ("PRICE_DIFF", "Lệch đơn giá"), ("NO_GRN", "Chưa nhập kho")]
    bill = models.ForeignKey(Bill, on_delete=models.CASCADE, related_name="lines")
    po_line = models.ForeignKey("purchasing.PurchaseOrderLine", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="bill_lines")
    inbound_trip = models.ForeignKey("purchasing.InboundTrip", null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="bill_lines")
    description = models.CharField(max_length=200, blank=True)
    qty = models.DecimalField(**QTY)
    unit_price = models.DecimalField(**MONEY)
    amount = models.DecimalField(**MONEY)
    match_status = models.CharField(max_length=12, choices=MATCH, blank=True)

    class Meta:
        db_table = "bill_line"


class Payment(TimeStamped):
    DIRECTIONS = [("IN", "Phiếu thu"), ("OUT", "Phiếu chi")]
    METHODS = [("BANK", "Chuyển khoản"), ("CASH", "Tiền mặt")]
    code = models.CharField(max_length=20, unique=True)
    direction = models.CharField(max_length=3, choices=DIRECTIONS)
    customer = models.ForeignKey("masterdata.Customer", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="payments")
    supplier = models.ForeignKey("masterdata.Supplier", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="payments")
    carrier = models.ForeignKey("masterdata.Carrier", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="payments")
    amount = models.DecimalField(**MONEY)
    method = models.CharField(max_length=5, choices=METHODS, default="BANK")
    paid_at = models.DateField()
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "payment"
        ordering = ["-id"]

    def __str__(self):
        return self.code

    @property
    def partner(self):
        return self.customer or self.supplier or self.carrier


class PaymentAllocation(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="allocations")
    invoice = models.ForeignKey(Invoice, null=True, blank=True, on_delete=models.PROTECT, related_name="allocations")
    bill = models.ForeignKey(Bill, null=True, blank=True, on_delete=models.PROTECT, related_name="allocations")
    amount = models.DecimalField(**MONEY)

    class Meta:
        db_table = "payment_allocation"
