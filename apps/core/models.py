from django.contrib.auth.models import AbstractUser
from django.db import models

MONEY = dict(max_digits=16, decimal_places=0, default=0)   # VND NUMERIC(16,0)
QTY = dict(max_digits=14, decimal_places=3, default=0)     # NUMERIC(14,3) theo base_uom


class Role(models.TextChoices):
    ADMIN = "ADMIN", "Quản trị hệ thống"
    DIRECTOR = "DIRECTOR", "Giám đốc"
    PURCHASER = "PURCHASER", "Mua hàng"
    DISPATCHER = "DISPATCHER", "Điều phối bán hàng"
    WAREHOUSE = "WAREHOUSE", "Thủ kho"
    ACCOUNTANT = "ACCOUNTANT", "Kế toán"
    DRIVER = "DRIVER", "Tài xế"
    CUSTOMER = "CUSTOMER", "Khách hàng"


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class User(AbstractUser):
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.WAREHOUSE)
    full_name = models.CharField(max_length=150, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    driver = models.ForeignKey("masterdata.Driver", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="users")
    customer = models.ForeignKey("masterdata.Customer", null=True, blank=True, on_delete=models.PROTECT,
                                 related_name="portal_users")
    failed_login_count = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "user"

    def __str__(self):
        return self.full_name or self.username

    @property
    def display_name(self):
        return self.full_name or self.get_full_name() or self.username

    @property
    def initials(self):
        parts = self.display_name.split()
        return (parts[-1][0] if parts else "U").upper()

    @property
    def can_see_price(self):
        """WAREHOUSE và DRIVER tuyệt đối không thấy giá."""
        return self.role not in (Role.WAREHOUSE, Role.DRIVER)

    @property
    def can_see_cost(self):
        """Giá vốn chỉ dành cho nội bộ quản lý."""
        return self.role in (Role.ADMIN, Role.DIRECTOR, Role.ACCOUNTANT, Role.PURCHASER)

    @property
    def home_url(self):
        if self.role == Role.DRIVER:
            return "/m/trips"
        if self.role == Role.CUSTOMER:
            return "/portal/"
        return "/app/dashboard"


class AuditLog(models.Model):
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=30)  # CREATE|UPDATE|DELETE|LOGIN|LOGIN_FAIL|APPROVE|CANCEL|POST
    model = models.CharField(max_length=80)
    object_id = models.CharField(max_length=40, blank=True)
    object_repr = models.CharField(max_length=200, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        db_table = "audit_log"
        ordering = ["-at"]


class DocSequence(models.Model):
    """Sinh mã PREFIX-YYMM-####, reset mỗi tháng (khóa dòng SELECT ... FOR UPDATE)."""
    prefix = models.CharField(max_length=10)
    period = models.CharField(max_length=4)  # YYMM
    last_no = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "doc_sequence"
        unique_together = ("prefix", "period")


class SystemParam(models.Model):
    key = models.CharField(max_length=60, unique=True)
    value = models.CharField(max_length=200)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "system_param"

    def __str__(self):
        return self.key


class Alert(models.Model):
    """Cảnh báo hệ thống (trễ mốc, dưới ROP, chờ duyệt...)."""
    LEVELS = [("INFO", "Thông tin"), ("WARN", "Cảnh báo"), ("CRIT", "Nghiêm trọng")]
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    alert_type = models.CharField(max_length=40)  # MILESTONE_LATE|BELOW_ROP|STOCKOUT|...
    level = models.CharField(max_length=5, choices=LEVELS, default="WARN")
    title = models.CharField(max_length=200)
    message = models.TextField(blank=True)
    ref_type = models.CharField(max_length=20, blank=True)
    ref_id = models.BigIntegerField(null=True, blank=True)
    url = models.CharField(max_length=200, blank=True)
    target_role = models.CharField(max_length=20, blank=True)
    is_resolved = models.BooleanField(default=False)

    class Meta:
        db_table = "alert"
        ordering = ["-created_at"]
