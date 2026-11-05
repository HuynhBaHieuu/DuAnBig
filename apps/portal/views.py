"""/portal/ – cổng khách hàng. Không hiển thị tồn kho, giá vốn."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.accounting.models import Invoice
from apps.core.services import BusinessError, next_doc_no, raise_alert
from apps.core.utils import business_action, pdf_response
from apps.masterdata.models import PriceListLine, Product, Site
from apps.sales.models import DeliveryStop, SalesOrder
from apps.sales.services import customer_debt
from apps.sales.views import save_so_lines


def _ctx(request):
    u = request.user
    if u.role != "CUSTOMER" or not u.customer_id:
        raise PermissionDenied("Tài khoản chưa gắn với khách hàng.")
    sites = Site.objects.filter(customer=u.customer, is_active=True)
    granted = sites.filter(user_access__user=u)
    return u.customer, (granted if granted.exists() else sites)


@login_required
def home(request):
    cust, sites = _ctx(request)
    orders = SalesOrder.objects.filter(customer=cust, site__in=sites)
    open_orders = orders.exclude(status__in=["DELIVERED", "CLOSED", "CANCELLED"])
    upcoming = DeliveryStop.objects.filter(so__in=orders, status="PENDING").select_related("trip__vehicle", "so", "site") \
        .order_by("trip__planned_date")[:5]
    debt, overdue = customer_debt(cust)
    return render(request, "portal/home.html", {
        "title": "Tổng quan", "cust": cust, "sites": sites, "open_count": open_orders.count(),
        "recent": orders.select_related("site").prefetch_related("stops__trip")[:6], "upcoming": upcoming, "debt": debt, "overdue": overdue,
        "delivered_month": orders.filter(status="DELIVERED", updated_at__date__gte=timezone.localdate().replace(day=1)).count()})


@login_required
@business_action
def order_new(request):
    cust, sites = _ctx(request)
    weekly = cust.customer_type == "INTERNAL" or request.GET.get("weekly") == "1"
    if request.method == "POST":
        site = get_object_or_404(sites, pk=request.POST["site"])
        with transaction.atomic():
            so = SalesOrder.objects.create(
                code=next_doc_no("SO"), customer=cust, site=site, source="PORTAL",
                order_type="INTERNAL" if cust.customer_type == "INTERNAL" else "B2B",
                is_weekly_plan=request.POST.get("weekly") == "1", order_date=timezone.localdate(),
                requested_date=request.POST.get("requested_date") or timezone.localdate(),
                note=request.POST.get("note", ""), created_by=request.user)
            # khách không tự đặt giá: lấy theo bảng giá của khách
            save_so_lines(so, request.POST.getlist("product"), request.POST.getlist("qty"),
                          [None] * len(request.POST.getlist("product")), user_can_price=False)
        raise_alert("PORTAL_ORDER", f"Đơn mới từ cổng KH: {so.code}", f"{cust.name} – {site.name}", level="INFO",
                    ref_type="so", ref_id=so.pk, url=f"/app/sales/orders/{so.pk}", target_role="DISPATCHER")
        messages.success(request, f"Đã gửi {'kế hoạch vật tư' if so.is_weekly_plan else 'đơn hàng'} {so.code}. TRACON 80 sẽ xác nhận sớm.")
        return redirect(f"/portal/orders/{so.pk}")
    if cust.price_list_id:
        prices = {l.product_id: l.price for l in PriceListLine.objects.filter(price_list_id=cust.price_list_id)}
        products = [p for p in Product.objects.filter(is_active=True, pk__in=prices).select_related("base_uom")]
        for p in products:
            p.sell_price = prices[p.pk]
    else:
        products = list(Product.objects.filter(is_active=True).select_related("base_uom"))
    return render(request, "portal/order_new.html", {"title": "Gửi kế hoạch vật tư tuần" if weekly else "Đặt hàng",
                                                     "sites": sites, "products": products, "weekly": weekly,
                                                     "show_price": cust.customer_type == "B2B"})


@login_required
def orders(request):
    cust, sites = _ctx(request)
    qs = SalesOrder.objects.filter(customer=cust, site__in=sites).select_related("site").prefetch_related("stops__trip")
    if request.GET.get("site"):
        qs = qs.filter(site_id=request.GET["site"])
    return render(request, "portal/orders.html", {"title": "Theo dõi đơn hàng", "orders": qs[:100], "sites": sites})


@login_required
def order_detail(request, pk):
    cust, sites = _ctx(request)
    so = get_object_or_404(SalesOrder.objects.select_related("site"), pk=pk, customer=cust, site__in=sites)
    stops = so.stops.select_related("trip__vehicle", "trip__driver").prefetch_related("lines__so_line__product")         .exclude(trip__status="CANCELLED")
    code, label = so.delivery_status
    has_trip = stops.exists()
    steps = [("Đã gửi", True), ("Đã xác nhận", so.status not in ("DRAFT", "PENDING_APPROVAL", "CANCELLED")),
             ("Đã xếp chuyến", has_trip),
             ("Đang giao", stops.filter(trip__status__in=["IN_TRANSIT", "COMPLETED"]).exists()),
             ("Đã giao", so.status in ("DELIVERED", "CLOSED"))]
    return render(request, "portal/order_detail.html", {"title": so.code, "so": so, "stops": stops, "steps": steps,
                                                        "ds_code": code, "ds_label": label,
                                                        "lines": so.lines.select_related("product__base_uom"),
                                                        "show_price": cust.customer_type == "B2B"})


@login_required
def documents(request):
    cust, sites = _ctx(request)
    invoices = Invoice.objects.filter(customer=cust).exclude(status__in=["DRAFT", "CANCELLED"])
    pods = DeliveryStop.objects.filter(so__customer=cust, so__site__in=sites).exclude(delivered_at=None) \
        .select_related("so", "site", "trip").order_by("-delivered_at")[:50]
    return render(request, "portal/documents.html", {"title": "Chứng từ", "invoices": invoices, "pods": pods})


@login_required
def invoice_pdf(request, pk):
    cust, _ = _ctx(request)
    inv = get_object_or_404(Invoice, pk=pk, customer=cust)
    if inv.status in ("DRAFT", "CANCELLED"):
        raise PermissionDenied
    return pdf_response(request, "docs/invoice.html", {"inv": inv}, inv.code)


@login_required
def debts(request):
    cust, _ = _ctx(request)
    debt, overdue = customer_debt(cust)
    today = timezone.localdate()
    invs = Invoice.objects.filter(customer=cust, status__in=["POSTED", "PARTIAL"]).order_by("due_date")
    for i in invs:
        i.days_over = (today - i.due_date).days
    return render(request, "portal/debts.html", {"title": "Công nợ", "cust": cust, "debt": debt, "overdue": overdue,
                                                 "invoices": invs, "payments": cust.payments.all()[:20]})
