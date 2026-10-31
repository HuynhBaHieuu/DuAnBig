"""CRUD danh mục dùng chung. Không xóa cứng – chỉ bật/tắt is_active."""
from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from apps.core.permissions import role_required
from apps.core.utils import paginate

from . import models as m

P, D = "PURCHASER", "DISPATCHER"
# slug: (model, nhãn, vai trò được sửa, cột hiển thị)
ENTITIES = {
    "products": (m.Product, "Sản phẩm", (P,), ["code", "name", "category", "base_uom", "unit_weight_kg", "default_supplier", "track_rop"]),
    "categories": (m.ProductCategory, "Nhóm hàng", (P,), ["code", "name", "parent"]),
    "uoms": (m.Uom, "Đơn vị tính", (P,), ["code", "name"]),
    "product-uoms": (m.ProductUom, "Quy đổi ĐVT", (P,), ["product", "uom", "factor"]),
    "suppliers": (m.Supplier, "Nhà cung cấp", (P,), ["code", "name", "tax_code", "phone", "default_route", "payment_terms_days"]),
    "supplier-prices": (m.SupplierPrice, "Giá mua", (P,), ["supplier", "product", "price", "min_qty", "valid_from", "valid_to"]),
    "carriers": (m.Carrier, "Nhà xe 3PL", (P,), ["code", "name", "phone"]),
    "carrier-contracts": (m.CarrierContract, "Hợp đồng nhà xe", (P,), ["code", "name", "carrier", "valid_from", "valid_to"]),
    "carrier-rates": (m.CarrierRate, "Bảng cước", (P,), ["contract", "route", "rate_per_trip", "rate_per_ton"]),
    "routes": (m.Route, "Tuyến", (P,), ["code", "name", "origin", "destination", "distance_km", "std_hours"]),
    "route-milestones": (m.RouteMilestone, "Mốc tuyến", (P,), ["route", "seq", "code", "name", "planned_offset_min", "photo_required"]),
    "vehicles": (m.Vehicle, "Xe", (P, D), ["plate", "vehicle_type", "capacity_ton", "carrier"]),
    "drivers": (m.Driver, "Tài xế", (P, D), ["code", "name", "phone", "license_class", "license_expiry", "carrier"]),
    "customers": (m.Customer, "Khách hàng", (D,), ["code", "name", "customer_type", "credit_limit", "payment_terms_days", "price_list"]),
    "sites": (m.Site, "Công trình", (D,), ["code", "name", "customer", "priority", "penalty_date"]),
    "price-lists": (m.PriceList, "Bảng giá bán", (D,), ["code", "name", "valid_from"]),
    "price-list-lines": (m.PriceListLine, "Dòng bảng giá", (D,), ["price_list", "product", "price", "floor_price"]),
    "warehouses": (m.Warehouse, "Kho", (), ["code", "name", "address"]),
    "locations": (m.Location, "Vị trí kho", (), ["code", "warehouse", "zone", "loc_type", "capacity_ton"]),
    "docks": (m.Dock, "Bến hạ tải", (), ["code", "name", "warehouse"]),
}
GROUPS = [
    ("Hàng hóa", ["products", "categories", "uoms", "product-uoms"]),
    ("Mua hàng & vận tải", ["suppliers", "supplier-prices", "carriers", "carrier-contracts", "carrier-rates", "routes", "route-milestones"]),
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


@role_required(P, D)
def index(request):
    groups = [(g, [{"slug": s, "label": ENTITIES[s][1], "count": ENTITIES[s][0].objects.count(),
                    "edit": request.user.role == "ADMIN" or request.user.role in ENTITIES[s][2]} for s in slugs])
              for g, slugs in GROUPS]
    return render(request, "master/index.html", {"title": "Danh mục", "groups": groups})


@role_required(P, D)
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
    page, qs_str = paginate(request, qs, 30)
    headers = [model._meta.get_field(c).verbose_name.capitalize() for c in cols]
    rows = [{"obj": o, "cells": [_display(o, c) for c in cols]} for o in page]
    return render(request, "master/list.html", {"title": label, "slug": slug, "headers": headers, "rows": rows,
                                                "page_obj": page, "qs": qs_str, "can_edit": can_edit,
                                                "has_active": any(f.name == "is_active" for f in model._meta.fields)})


def _display(o, c):
    v = getattr(o, c)
    f = o._meta.get_field(c)
    if f.choices:
        return getattr(o, f"get_{c}_display")()
    if isinstance(v, bool):
        return "✓" if v else "—"
    return "" if v is None else v


@role_required(P, D)
def entity_form(request, slug, pk=None):
    model, label, can_edit, cols = _entity(request, slug)
    if not can_edit:
        raise PermissionDenied("Bạn chỉ có quyền xem danh mục này.")
    obj = get_object_or_404(model, pk=pk) if pk else None
    Form = forms.modelform_factory(model, exclude=["created_at", "updated_at", "is_active"])
    form = Form(request.POST or None, instance=obj)
    for name, f in form.fields.items():
        if isinstance(f.widget, forms.DateInput) or isinstance(f, forms.DateField):
            f.widget = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    if request.method == "POST":
        if request.POST.get("toggle") and obj is not None and hasattr(obj, "is_active"):
            obj.is_active = not obj.is_active
            obj.save()
            messages.success(request, "Đã " + ("kích hoạt" if obj.is_active else "ngưng sử dụng") + f" {obj}.")
            return redirect(f"/app/master/{slug}/")
        if form.is_valid():
            o = form.save()
            messages.success(request, f"Đã lưu {o}.")
            return redirect(f"/app/master/{slug}/")
    return render(request, "master/form.html", {"title": f"{'Sửa' if obj else 'Thêm'} {label.lower()}", "label": label,
                                                "slug": slug, "form": form, "obj": obj,
                                                "has_active": any(f.name == "is_active" for f in model._meta.fields)})
