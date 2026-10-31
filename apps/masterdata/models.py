"""1. HỆ THỐNG & DANH MỤC CỐT LÕI (MASTER DATA). Không xóa cứng – dùng is_active."""
from django.db import models
from django.utils import timezone

from apps.core.models import MONEY, QTY, TimeStamped


class Master(TimeStamped):
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    class Meta:
        abstract = True
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} – {self.name}"


class Uom(Master):
    class Meta(Master.Meta):
        db_table = "uom"

    def __str__(self):
        return self.code


class ProductCategory(Master):
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="children")

    class Meta(Master.Meta):
        db_table = "product_category"


class Product(Master):
    category = models.ForeignKey(ProductCategory, on_delete=models.PROTECT, related_name="products")
    base_uom = models.ForeignKey(Uom, on_delete=models.PROTECT, related_name="products")
    default_supplier = models.ForeignKey("Supplier", null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name="default_products")
    unit_weight_kg = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    standard_cost = models.DecimalField(**MONEY)
    avg_cost = models.DecimalField(**MONEY, help_text="Moving Average")
    track_rop = models.BooleanField(default=True)

    class Meta(Master.Meta):
        db_table = "product"


class ProductUom(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="conversions")
    uom = models.ForeignKey(Uom, on_delete=models.PROTECT)
    factor = models.DecimalField(max_digits=14, decimal_places=6, help_text="1 đơn vị này = factor × base_uom")

    class Meta:
        db_table = "product_uom"
        unique_together = ("product", "uom")


class Warehouse(Master):
    address = models.CharField(max_length=255, blank=True)

    class Meta(Master.Meta):
        db_table = "warehouse"


class Location(TimeStamped):
    ZONES = [(z, f"Khu {z}") for z in "ABCD"] + [("Q", "Khu cách ly"), ("OUT", "Khu xuất")]
    TYPES = [("STORAGE", "Lưu trữ"), ("QUARANTINE", "Cách ly"), ("STAGING_OUT", "Chờ xuất")]
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="locations")
    code = models.CharField(max_length=30, unique=True)
    zone = models.CharField(max_length=3, choices=ZONES, default="A")
    loc_type = models.CharField(max_length=15, choices=TYPES, default="STORAGE")
    capacity_ton = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "location"
        ordering = ["code"]

    def __str__(self):
        return self.code


class Dock(Master):
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, related_name="docks")

    class Meta(Master.Meta):
        db_table = "dock"


class Route(Master):
    origin = models.CharField(max_length=150)
    destination = models.CharField(max_length=150)
    distance_km = models.DecimalField(max_digits=8, decimal_places=1, default=0)
    std_hours = models.DecimalField(max_digits=6, decimal_places=2, default=0, help_text="Thời gian chuẩn (giờ)")

    class Meta(Master.Meta):
        db_table = "route"


class RouteMilestone(models.Model):
    CODES = [("FACTORY", "Tới nhà máy"), ("LOADED", "Bốc xong"), ("CHECKPOINT", "Qua trạm"), ("GATE", "Tới cổng kho")]
    route = models.ForeignKey(Route, on_delete=models.CASCADE, related_name="milestones")
    seq = models.PositiveSmallIntegerField()
    code = models.CharField(max_length=15, choices=CODES)
    name = models.CharField(max_length=120)
    planned_offset_min = models.PositiveIntegerField(help_text="Số phút kể từ giờ xuất phát dự kiến")
    photo_required = models.BooleanField(default=False)

    class Meta:
        db_table = "route_milestone"
        ordering = ["route", "seq"]
        unique_together = ("route", "seq")

    def __str__(self):
        return f"{self.seq}. {self.name}"


class Supplier(Master):
    tax_code = models.CharField(max_length=20, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.CharField(max_length=255, blank=True)
    default_route = models.ForeignKey(Route, null=True, blank=True, on_delete=models.SET_NULL, related_name="suppliers")
    payment_terms_days = models.PositiveSmallIntegerField(default=30)

    class Meta(Master.Meta):
        db_table = "supplier"


class SupplierPrice(TimeStamped):
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="prices")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="supplier_prices")
    price = models.DecimalField(**MONEY)
    min_qty = models.DecimalField(**QTY)
    valid_from = models.DateField(default=timezone.localdate)
    valid_to = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "supplier_price"
        ordering = ["product", "-valid_from"]


class Carrier(Master):
    tax_code = models.CharField(max_length=20, blank=True)
    phone = models.CharField(max_length=20, blank=True)

    class Meta(Master.Meta):
        db_table = "carrier"


class CarrierContract(Master):
    carrier = models.ForeignKey(Carrier, on_delete=models.PROTECT, related_name="contracts")
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)

    class Meta(Master.Meta):
        db_table = "carrier_contract"


class CarrierRate(models.Model):
    contract = models.ForeignKey(CarrierContract, on_delete=models.CASCADE, related_name="rates")
    route = models.ForeignKey(Route, on_delete=models.PROTECT, related_name="carrier_rates")
    rate_per_trip = models.DecimalField(**MONEY)
    rate_per_ton = models.DecimalField(**MONEY)

    class Meta:
        db_table = "carrier_rate"
        unique_together = ("contract", "route")


class Vehicle(TimeStamped):
    plate = models.CharField(max_length=20, unique=True)
    vehicle_type = models.CharField(max_length=60, blank=True)
    capacity_ton = models.DecimalField(max_digits=8, decimal_places=2)
    carrier = models.ForeignKey(Carrier, null=True, blank=True, on_delete=models.PROTECT, related_name="vehicles",
                                help_text="Để trống = xe nội bộ")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "vehicle"
        ordering = ["plate"]

    def __str__(self):
        return f"{self.plate} ({self.capacity_ton}t)"

    @property
    def is_internal(self):
        return self.carrier_id is None


class Driver(Master):
    phone = models.CharField(max_length=20, blank=True)
    license_no = models.CharField(max_length=30, blank=True)
    license_class = models.CharField(max_length=10, blank=True)
    license_expiry = models.DateField(null=True, blank=True)
    carrier = models.ForeignKey(Carrier, null=True, blank=True, on_delete=models.PROTECT, related_name="drivers",
                                help_text="Để trống = tài xế nội bộ")

    class Meta(Master.Meta):
        db_table = "driver"

    @property
    def license_valid(self):
        return bool(self.license_expiry and self.license_expiry >= timezone.localdate())


class PriceList(Master):
    valid_from = models.DateField(default=timezone.localdate)

    class Meta(Master.Meta):
        db_table = "price_list"


class PriceListLine(models.Model):
    price_list = models.ForeignKey(PriceList, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="price_lines")
    price = models.DecimalField(**MONEY)
    floor_price = models.DecimalField(**MONEY, help_text="Giá sàn – bán dưới cần Director duyệt")

    class Meta:
        db_table = "price_list_line"
        unique_together = ("price_list", "product")


class Customer(Master):
    TYPES = [("B2B", "Khách hàng B2B"), ("INTERNAL", "Công trình nội bộ")]
    customer_type = models.CharField(max_length=10, choices=TYPES, default="B2B")
    tax_code = models.CharField(max_length=20, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    address = models.CharField(max_length=255, blank=True)
    credit_limit = models.DecimalField(**MONEY)
    payment_terms_days = models.PositiveSmallIntegerField(default=30)
    price_list = models.ForeignKey(PriceList, null=True, blank=True, on_delete=models.SET_NULL, related_name="customers")

    class Meta(Master.Meta):
        db_table = "customer"


class Site(Master):
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="sites")
    address = models.CharField(max_length=255, blank=True)
    priority = models.PositiveSmallIntegerField(default=3, help_text="1 = cao nhất")
    penalty_date = models.DateField(null=True, blank=True, help_text="Mốc phạt tiến độ")
    contact_name = models.CharField(max_length=100, blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)

    class Meta(Master.Meta):
        db_table = "site"


class UserSiteAccess(models.Model):
    user = models.ForeignKey("core.User", on_delete=models.CASCADE, related_name="site_access")
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="user_access")

    class Meta:
        db_table = "user_site_access"
        unique_together = ("user", "site")
