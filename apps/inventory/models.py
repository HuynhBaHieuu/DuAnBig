"""2. LÕI TỒN KHO & SS/ROP."""
from django.db import models
from django.db.models import Q

from apps.core.models import MONEY, QTY, TimeStamped


class StockQuant(models.Model):
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT, related_name="quants")
    location = models.ForeignKey("masterdata.Location", on_delete=models.PROTECT, related_name="quants")
    on_hand = models.DecimalField(**QTY)
    in_date_first = models.DateTimeField(null=True, blank=True, help_text="Ngày nhập sớm nhất – phục vụ FIFO")

    class Meta:
        db_table = "stock_quant"
        unique_together = ("product", "location")
        constraints = [models.CheckConstraint(condition=Q(on_hand__gte=0), name="stock_quant_on_hand_gte_0")]


class StockMove(models.Model):
    MOVE_TYPES = [
        ("IN_PURCHASE", "Nhập mua"), ("IN_RETURN", "Nhập trả lại"), ("OUT_SALE", "Xuất bán"),
        ("OUT_INTERNAL", "Xuất công trình"), ("ADJ", "Điều chỉnh"), ("TRF", "Chuyển vị trí"),
    ]
    REF_TYPES = [("GRN", "Phiếu nhập"), ("GIN", "Phiếu xuất"), ("ADJ", "Điều chỉnh"), ("RT", "Trả hàng"),
                 ("TRF", "Chuyển vị trí")]
    at = models.DateTimeField(db_index=True)
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT, related_name="moves")
    location = models.ForeignKey("masterdata.Location", on_delete=models.PROTECT, related_name="moves")
    qty = models.DecimalField(max_digits=14, decimal_places=3, help_text="+ nhập / - xuất, theo base_uom")
    unit_cost = models.DecimalField(**MONEY)
    move_type = models.CharField(max_length=15, choices=MOVE_TYPES)
    ref_type = models.CharField(max_length=5, choices=REF_TYPES)
    ref_id = models.BigIntegerField()
    balance_after = models.DecimalField(**QTY)
    user = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "stock_move"
        ordering = ["-at", "-id"]


class ReplenishmentParam(models.Model):
    METHODS = [("FULL", "FULL – biến động cầu & lead time"), ("DEMAND", "Chỉ biến động cầu"), ("MANUAL", "Nhập tay")]
    product = models.OneToOneField("masterdata.Product", on_delete=models.CASCADE, related_name="rop_param")
    method = models.CharField(max_length=10, choices=METHODS, default="FULL")
    z = models.DecimalField(max_digits=4, decimal_places=2, default=1.65)
    review_days = models.PositiveSmallIntegerField(default=7, help_text="Chu kỳ xem xét đặt hàng (ngày)")
    history_days = models.PositiveSmallIntegerField(default=90)
    d_avg = models.DecimalField(**QTY, help_text="Nhu cầu TB / ngày")
    d_std = models.DecimalField(**QTY, help_text="Độ lệch chuẩn nhu cầu / ngày")
    lead_time_days = models.DecimalField(max_digits=6, decimal_places=2, default=3)
    lt_std_days = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    ss = models.DecimalField(**QTY)
    rop = models.DecimalField(**QTY)
    suggested_q = models.DecimalField(**QTY)
    manual_ss = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    manual_rop = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    calculated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "replenishment_param"


class ReplenishmentSnapshot(models.Model):
    product = models.ForeignKey("masterdata.Product", on_delete=models.CASCADE, related_name="rop_snapshots")
    date = models.DateField()
    on_hand = models.DecimalField(**QTY)
    reserved = models.DecimalField(**QTY)
    incoming = models.DecimalField(**QTY)
    projected = models.DecimalField(**QTY)
    demand = models.DecimalField(**QTY, help_text="Lượng xuất trong ngày")
    d_avg = models.DecimalField(**QTY)
    ss = models.DecimalField(**QTY)
    rop = models.DecimalField(**QTY)

    class Meta:
        db_table = "replenishment_snapshot"
        unique_together = ("product", "date")
        ordering = ["-date"]


class StockAlert(models.Model):
    TYPES = [("BELOW_ROP", "Dưới điểm đặt hàng"), ("BELOW_SS", "Dưới tồn an toàn"), ("STOCKOUT", "Hết hàng")]
    product = models.ForeignKey("masterdata.Product", on_delete=models.CASCADE, related_name="stock_alerts")
    alert_type = models.CharField(max_length=15, choices=TYPES)
    projected = models.DecimalField(**QTY)
    threshold = models.DecimalField(**QTY)
    created_at = models.DateTimeField(auto_now_add=True)
    is_resolved = models.BooleanField(default=False)

    class Meta:
        db_table = "stock_alert"
        ordering = ["-created_at"]


class InventoryCount(TimeStamped):
    STATUS = [("DRAFT", "Nháp"), ("COUNTING", "Đang đếm"), ("RECOUNT", "Đếm lại"),
              ("PENDING_APPROVAL", "Chờ duyệt"), ("APPROVED", "Đã duyệt"), ("CANCELLED", "Đã hủy")]
    code = models.CharField(max_length=20, unique=True)
    warehouse = models.ForeignKey("masterdata.Warehouse", on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=STATUS, default="DRAFT")
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey("core.User", null=True, on_delete=models.SET_NULL, related_name="+")
    approved_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    cancel_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "inventory_count"
        ordering = ["-id"]

    def __str__(self):
        return self.code


class InventoryCountLine(models.Model):
    count = models.ForeignKey(InventoryCount, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT)
    location = models.ForeignKey("masterdata.Location", on_delete=models.PROTECT)
    book_qty = models.DecimalField(**QTY, help_text="Tồn sổ – ẩn khi đếm mù")
    count1 = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    count2 = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    final_qty = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)

    class Meta:
        db_table = "inventory_count_line"

    @property
    def variance_pct(self):
        if self.final_qty is None:
            return None
        if not self.book_qty:
            return 100 if self.final_qty else 0
        return abs(self.final_qty - self.book_qty) / self.book_qty * 100


class StockAdjustment(TimeStamped):
    STATUS = [("DRAFT", "Nháp"), ("PENDING_APPROVAL", "Chờ duyệt"), ("POSTED", "Đã ghi sổ"), ("CANCELLED", "Đã hủy")]
    code = models.CharField(max_length=20, unique=True)
    count = models.OneToOneField(InventoryCount, null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="adjustment")
    status = models.CharField(max_length=20, choices=STATUS, default="DRAFT")
    reason = models.CharField(max_length=255)
    approved_by = models.ForeignKey("core.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "stock_adjustment"
        ordering = ["-id"]

    def __str__(self):
        return self.code


class StockAdjustmentLine(models.Model):
    adjustment = models.ForeignKey(StockAdjustment, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("masterdata.Product", on_delete=models.PROTECT)
    location = models.ForeignKey("masterdata.Location", on_delete=models.PROTECT)
    qty_diff = models.DecimalField(max_digits=14, decimal_places=3)
    unit_cost = models.DecimalField(**MONEY)

    class Meta:
        db_table = "stock_adjustment_line"
