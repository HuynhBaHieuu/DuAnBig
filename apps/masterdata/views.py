"""CRUD danh mục dùng chung. Không xóa cứng – chỉ bật/tắt is_active."""
from decimal import Decimal

from django import forms
from django.core.validators import MinValueValidator
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from django.db.models import Sum
from django.utils import timezone

from apps.core.permissions import role_required
from apps.core.services import audit, raise_alert, resolve_alerts
from apps.core.utils import paginate

from . import models as m

P, D, DIR, ACC = "PURCHASER", "DISPATCHER", "DIRECTOR", "ACCOUNTANT"
VIEWERS = (P, D, DIR, ACC)
# slug: (model, nhãn, vai trò được sửa, cột hiển thị)
ENTITIES = {
    "products": (m.Product, "Sản phẩm", (P,), ["code", "name", "category", "base_uom", "unit_weight_kg", "default_supplier", "track_rop"]),
    "categories": (m.ProductCategory, "Nhóm hàng", (P,), ["code", "name", "parent"]),
    "uoms": (m.Uom, "Đơn vị tính", (P,), ["code", "name"]),
    "product-uoms": (m.ProductUom, "Quy đổi ĐVT", (P,), ["product", "uom", "factor"]),
    "suppliers": (m.Supplier, "Nhà cung cấp", (P,), ["code", "name", "tax_code", "phone", "default_route", "payment_terms_days"]),
    "supplier-prices": (m.SupplierPrice, "Giá mua", (P, DIR), ["supplier", "product", "price", "min_qty", "valid_from", "valid_to", "status"]),
    "carriers": (m.Carrier, "Nhà xe 3PL", (P,), ["code", "name", "phone"]),
    "carrier-contracts": (m.CarrierContract, "Hợp đồng nhà xe", (P,), ["code", "name", "carrier", "valid_from", "valid_to"]),
    "carrier-rates": (m.CarrierRate, "Bảng cước", (P,), ["contract", "route", "rate_per_trip", "rate_per_ton"]),
    "routes": (m.Route, "Tuyến", (P,), ["code", "name", "origin", "destination", "distance_km", "std_hours"]),
    "route-milestones": (m.RouteMilestone, "Mốc tuyến", (P,), ["route", "seq", "code", "name", "planned_offset_min", "photo_required"]),
    "vehicles": (m.Vehicle, "Xe", (P, D), ["plate", "vehicle_type", "capacity_ton", "carrier"]),
    "drivers": (m.Driver, "Tài xế", (P, D), ["code", "name", "phone", "license_class", "license_expiry", "carrier"]),
    "customers": (m.Customer, "Khách hàng", (D, DIR, ACC), ["code", "name", "customer_type", "credit_limit", "payment_terms_days", "price_list"]),
    "sites": (m.Site, "Công trình", (D,), ["code", "name", "customer", "priority", "penalty_date"]),
    "price-lists": (m.PriceList, "Bảng giá bán", (D,), ["code", "name", "valid_from"]),
    "price-list-lines": (m.PriceListLine, "Dòng bảng giá", (D, DIR), ["price_list", "product", "price", "floor_price"]),
    "warehouses": (m.Warehouse, "Kho", (), ["code", "name", "address"]),
    "locations": (m.Location, "Vị trí kho", (), ["code", "warehouse", "zone", "loc_type", "capacity_ton"]),
    "docks": (m.Dock, "Bến hạ tải", (), ["code", "name", "warehouse"]),
}
# Nhãn tiếng Việt cho form danh mục (model chưa khai báo verbose_name)
LABELS = {
    "code": "Mã", "name": "Tên", "category": "Nhóm hàng", "base_uom": "Đơn vị tính gốc", "parent": "Nhóm cha",
    "default_supplier": "NCC mặc định", "unit_weight_kg": "Trọng lượng / đơn vị (kg)", "standard_cost": "Giá chuẩn (₫)",
    "avg_cost": "Giá vốn bình quân (₫)", "track_rop": "Theo dõi SS/ROP", "supplier": "Nhà cung cấp", "product": "Sản phẩm",
    "price": "Đơn giá (₫)", "min_qty": "SL tối thiểu", "valid_from": "Hiệu lực từ", "valid_to": "Hiệu lực đến",
    "uom": "Đơn vị tính", "factor": "Hệ số quy đổi", "tax_code": "Mã số thuế", "phone": "Điện thoại", "address": "Địa chỉ",
    "default_route": "Tuyến mặc định", "payment_terms_days": "Hạn thanh toán (ngày)", "carrier": "Nhà xe",
    "contract": "Hợp đồng", "route": "Tuyến", "rate_per_trip": "Cước / chuyến (₫)", "rate_per_ton": "Cước / tấn (₫)",
    "origin": "Điểm đi", "destination": "Điểm đến", "distance_km": "Quãng đường (km)", "std_hours": "Thời gian chuẩn (giờ)",
    "seq": "Thứ tự", "planned_offset_min": "Phút từ lúc xuất phát", "photo_required": "Bắt buộc ảnh",
    "plate": "Biển số", "vehicle_type": "Loại xe", "capacity_ton": "Tải trọng (tấn)", "license_no": "Số bằng lái",
    "license_class": "Hạng bằng", "license_expiry": "Hạn bằng lái", "customer_type": "Loại khách hàng", "email": "Email",
    "credit_limit": "Hạn mức tín dụng (₫)", "price_list": "Bảng giá", "customer": "Khách hàng", "priority": "Mức ưu tiên",
    "penalty_date": "Mốc phạt tiến độ", "contact_name": "Người liên hệ", "contact_phone": "SĐT liên hệ",
    "floor_price": "Giá sàn (₫)", "warehouse": "Kho", "zone": "Khu", "loc_type": "Loại vị trí",
}
# Trường số bắt buộc phải > 0 (không chấp nhận giá trị mặc định 0)
POSITIVE = {"products": ["unit_weight_kg", "standard_cost"], "supplier-prices": ["price"],
            "product-uoms": ["factor"], "vehicles": ["capacity_ton"], "price-list-lines": ["price"]}
# Trường hệ thống tự tính – không cho nhập trên form
AUTO_FIELDS = {"products": ["avg_cost"], "supplier-prices": ["status", "approved_by", "approved_at"]}
# Trường nhạy cảm: chỉ vai trò liệt kê (và ADMIN) được sửa; vai trò khác thấy ô bị khóa và máy chủ BỎ QUA giá trị
# gửi lên (field.disabled) – không chỉ ẩn trên giao diện (test Gà STT 33, 43).
#   (slug, field): (vai trò được sửa, chỉ khóa khi sửa bản ghi đã có?)
FIELD_ROLES = {
    ("customers", "credit_limit"): ((DIR, ACC), False),           # hạn mức tín dụng: Giám đốc / Kế toán
    ("price-list-lines", "floor_price"): ((DIR,), False),          # giá sàn: Giám đốc
    ("drivers", "license_no"): ((), False),                        # thông tin bằng lái: chỉ Admin
    ("drivers", "license_class"): ((), False),
    ("drivers", "license_expiry"): ((), False),
    ("products", "standard_cost"): ((DIR,), True),                 # giá chuẩn (giá mua dự phòng) sau khi tạo: Giám đốc
}

GROUPS = [
    ("Hàng hóa", ["products", "categories", "uoms", "product-uoms"]),
    # mốc tuyến không còn dùng: mọi chuyến dùng 2 mốc chuẩn (STT 73)
    ("Mua hàng & vận tải", ["suppliers", "supplier-prices", "carriers", "carrier-contracts", "carrier-rates", "routes"]),
    ("Đội xe", ["vehicles", "drivers"]),
    ("Bán hàng", ["customers", "sites", "price-lists", "price-list-lines"]),
    ("Kho", ["warehouses", "locations", "docks"]),
]


def _entity(request, slug):
    if slug not in ENTITIES:
        raise Http404
    model, label, roles, cols = ENTITIES[slug]
    can_edit = request.user.role == "ADMIN" or request.user.role in roles
    return model, label, can_edit, cols


def _can_field(user, slug, name, obj):
    rule = FIELD_ROLES.get((slug, name))
    if not rule or user.role == "ADMIN":
        return True
    roles, only_existing = rule
    if only_existing and obj is None:
        return True
    return user.role in roles


@role_required(*VIEWERS)
def index(request):
    groups = [(g, [{"slug": s, "label": ENTITIES[s][1], "count": ENTITIES[s][0].objects.count(),
                    "edit": request.user.role == "ADMIN" or request.user.role in ENTITIES[s][2]} for s in slugs])
              for g, slugs in GROUPS]
    return render(request, "master/index.html", {"title": "Danh mục", "groups": groups})


@role_required(*VIEWERS)
def entity_list(request, slug):
    model, label, can_edit, cols = _entity(request, slug)
    qs = model.objects.all()
    q = request.GET.get("q", "").strip()
    if q:
        cond = Q()
        for f in ("code", "name", "plate"):
            if any(x.name == f for x in model._meta.fields):
                cond |= Q(**{f"{f}__icontains": q})
        qs = qs.filter(cond) if cond else qs
    if slug == "supplier-prices" and request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"])
    page, qs_str = paginate(request, qs, 30)
    headers = [LABELS.get(c) or model._meta.get_field(c).verbose_name.capitalize() for c in cols]
    rows = [{"obj": o, "cells": [_display(o, c) for c in cols]} for o in page]
    return render(request, "master/list.html", {"title": label, "slug": slug, "headers": headers, "rows": rows,
                                                "page_obj": page, "qs": qs_str, "can_edit": can_edit,
                                                "pending_prices": m.SupplierPrice.objects.filter(status="PENDING").count()
                                                if slug == "supplier-prices" else 0,
                                                "has_active": any(f.name == "is_active" for f in model._meta.fields)})


def _product_suppliers():
    """{product_id: supplier_id} – NCC mặc định của sản phẩm, nếu chưa có thì NCC của giá mua gần nhất."""
    data = {str(pid): sid for pid, sid in m.SupplierPrice.objects.order_by("valid_from", "id")
            .values_list("product_id", "supplier_id")}
    data.update({str(pid): sid for pid, sid in m.Product.objects.filter(default_supplier__isnull=False)
                 .values_list("id", "default_supplier_id")})
    return data


def _display(o, c):
    v = getattr(o, c)
    f = o._meta.get_field(c)
    if f.choices:
        return getattr(o, f"get_{c}_display")()
    if isinstance(v, bool):
        return "✓" if v else "—"
    return "" if v is None else v


def _stock_of(product):
    from apps.inventory.models import StockQuant
    return StockQuant.objects.filter(product=product).aggregate(s=Sum("on_hand"))["s"] or Decimal(0)


def _price_decision(request, sp, approve):
    """Giám đốc duyệt / từ chối giá mua do Thu mua nhập."""
    if request.user.role not in (DIR, "ADMIN"):
        raise PermissionDenied("Chỉ Giám đốc duyệt giá mua.")
    if sp.status != "PENDING":
        messages.info(request, "Giá này đã được xử lý.")
        return
    sp.status = "APPROVED" if approve else "REJECTED"
    sp.approved_by, sp.approved_at = request.user, timezone.now()
    sp.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    audit("APPROVE" if approve else "REJECT", sp, {"supplier": str(sp.supplier), "product": str(sp.product),
                                                   "price": str(sp.price)})
    if not m.SupplierPrice.objects.filter(status="PENDING").exists():
        resolve_alerts("PRICE_APPROVAL", "supplier_price", 0)
    messages.success(request, f"Đã {'duyệt' if approve else 'từ chối'} giá mua {sp.product} – {sp.supplier}: "
                              f"{sp.price:,.0f} ₫.")


@role_required(*VIEWERS)
def entity_form(request, slug, pk=None):
    model, label, can_edit, cols = _entity(request, slug)
    if not can_edit:
        raise PermissionDenied("Bạn chỉ có quyền xem danh mục này.")
    obj = get_object_or_404(model, pk=pk) if pk else None
    if slug == "supplier-prices" and obj is not None and request.method == "POST" and request.POST.get("decision"):
        _price_decision(request, obj, request.POST["decision"] == "approve")
        return redirect(f"/app/master/{slug}/?status=PENDING")
    Form = forms.modelform_factory(model, exclude=["created_at", "updated_at", "is_active"] + AUTO_FIELDS.get(slug, []))
    form = Form(request.POST or None, instance=obj)
    for name, f in form.fields.items():
        f.label = LABELS.get(name, f.label)
        if not _can_field(request.user, slug, name, obj):
            f.disabled = True  # Django dùng giá trị đang lưu, bỏ qua dữ liệu POST của trường này
            f.help_text = "Bạn không có quyền sửa trường này."
        if isinstance(f.widget, forms.DateInput) or isinstance(f, forms.DateField):
            f.widget = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
        if name in POSITIVE.get(slug, []):
            step = f.widget.attrs.get("step")
            low = step if step not in (None, "any") else "0.001"  # ô tiền step=1 -> min 1 (khớp bước nhập của trình duyệt)
            f.required = True
            f.validators.append(MinValueValidator(Decimal(low), "Giá trị phải lớn hơn 0."))
            f.widget.attrs["min"] = low
            if not obj:
                form.initial[name] = None  # không điền sẵn 0 – người dùng phải nhập
    if slug == "supplier-prices":
        form.fields["supplier"].help_text = "Tự điền theo NCC mặc định / giá mua gần nhất của sản phẩm."
    if request.method == "POST":
        if request.POST.get("toggle") and obj is not None and hasattr(obj, "is_active"):
            if slug == "products" and obj.is_active:
                left = _stock_of(obj)
                if left:  # STT 53: ngừng KD khi còn tồn -> hàng bị ẩn khỏi danh sách tồn, chỉ lộ ra khi kiểm kê
                    messages.error(request, f"Không ngừng kinh doanh được {obj.code}: còn tồn {left:g} "
                                            f"{obj.base_uom.code}. Xuất / điều chỉnh hết tồn trước.")
                    return redirect(request.path)
            obj.is_active = not obj.is_active
            obj.save()
            messages.success(request, "Đã " + ("kích hoạt" if obj.is_active else "ngưng sử dụng") + f" {obj}.")
            return redirect(f"/app/master/{slug}/")
        if form.is_valid():
            o = form.save(commit=False)
            if slug == "products" and not obj:
                o.avg_cost = o.standard_cost  # giá vốn bình quân khởi tạo = giá chuẩn, sau đó cập nhật theo GRN
            pending_price = False
            if slug == "supplier-prices":
                if request.user.role in (DIR, "ADMIN"):
                    o.status, o.approved_by, o.approved_at = "APPROVED", request.user, timezone.now()
                else:  # Thu mua: giá mới / giá sửa chờ Giám đốc duyệt; giá đang dùng giữ nguyên tới khi duyệt
                    if obj is not None and obj.status == "APPROVED":
                        if not form.has_changed():
                            messages.info(request, "Giá không đổi.")
                            return redirect(f"/app/master/{slug}/")
                        o.pk = None
                        o._state.adding = True
                    o.status, o.approved_by, o.approved_at = "PENDING", None, None
                    pending_price = True
            o.save()
            form.save_m2m()
            if slug == "products" and "track_rop" in form.changed_data:  # tick / bỏ tick "Theo dõi SS/ROP"
                from apps.inventory.replenishment import Trigger, fire_checks
                fire_checks([o.pk], Trigger("PARAM_SAVED", "", o.code, request.user))
            if pending_price:
                raise_alert("PRICE_APPROVAL", "Giá mua chờ duyệt",
                            f"{o.product} – {o.supplier}: {o.price:,.0f} ₫ (do {request.user} nhập)",
                            ref_type="supplier_price", ref_id=0, url="/app/master/supplier-prices/?status=PENDING",
                            target_role="DIRECTOR")
                messages.success(request, f"Đã gửi giá mua {o.product} – {o.supplier} chờ Giám đốc duyệt. "
                                          "Giá chỉ được dùng cho PO sau khi duyệt.")
                return redirect(f"/app/master/{slug}/")
            messages.success(request, f"Đã lưu {o}.")
            return redirect(f"/app/master/{slug}/")
    return render(request, "master/form.html", {"title": f"{'Sửa' if obj else 'Thêm'} {label.lower()}", "label": label,
                                                "slug": slug, "form": form, "obj": obj,
                                                "can_approve_price": slug == "supplier-prices" and obj is not None
                                                and obj.status == "PENDING" and request.user.role in (DIR, "ADMIN"),
                                                "product_suppliers": _product_suppliers() if slug == "supplier-prices" else None,
                                                "has_active": any(f.name == "is_active" for f in model._meta.fields)})
