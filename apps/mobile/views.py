"""/m/ – giao diện di động cho tài xế. Tuyệt đối không hiển thị giá."""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.core.utils import business_action
from apps.purchasing import services as psvc
from apps.purchasing.models import InboundTrip, TripIncident
from apps.sales import services as ssvc
from apps.sales.models import DeliveryStop, DeliveryTrip


def _driver(request):
    if request.user.role != "DRIVER" or not request.user.driver_id:
        raise PermissionDenied("Tài khoản chưa gắn với tài xế.")
    return request.user.driver


@login_required
def trips(request):
    d = _driver(request)
    tab = request.GET.get("tab", "active")
    ib = InboundTrip.objects.filter(driver=d).select_related("po__supplier", "route", "vehicle")
    ob = DeliveryTrip.objects.filter(driver=d).select_related("vehicle").prefetch_related("stops")
    if tab == "done":
        ib = ib.filter(status__in=["RECEIVED", "CANCELLED"])[:20]
        ob = ob.filter(status__in=["COMPLETED", "CANCELLED"])[:20]
    else:
        ib = ib.exclude(status__in=["RECEIVED", "CANCELLED"])
        ob = ob.exclude(status__in=["COMPLETED", "CANCELLED"])
    return render(request, "mobile/trips.html", {"title": "Chuyến của tôi", "ib": ib, "ob": ob, "tab": tab, "driver": d})


@login_required
@business_action
def inbound(request, pk):
    d = _driver(request)
    trip = get_object_or_404(InboundTrip.objects.select_related("po__supplier", "route", "vehicle"), pk=pk, driver=d)
    if request.method == "POST":
        op = request.POST.get("op")
        if op == "milestone":
            ms = get_object_or_404(trip.milestones, pk=request.POST["milestone"])
            loaded = {l.pk: request.POST.get(f"l_{l.pk}") for l in trip.lines.all()}
            psvc.record_milestone(trip, ms, request.user, photo=request.FILES.get("photo"),
                                  note=request.POST.get("note", ""), loaded_qty=loaded)
            messages.success(request, f"Đã cập nhật: {ms.name}")
        elif op == "incident":
            psvc.report_incident(trip, request.user, request.POST["incident_type"], request.POST.get("description", ""),
                                 int(request.POST.get("delay_minutes") or 0), request.FILES.get("photo"))
            messages.success(request, "Đã báo sự cố cho điều phối.")
        return redirect(request.path)
    return render(request, "mobile/inbound.html", {
        "title": trip.code, "trip": trip, "milestones": trip.milestones.all(), "next_ms": trip.next_milestone,
        "lines": trip.lines.select_related("po_line__product__base_uom"), "incident_types": TripIncident.TYPES})


@login_required
@business_action
def outbound(request, pk):
    d = _driver(request)
    trip = get_object_or_404(DeliveryTrip.objects.select_related("vehicle"), pk=pk, driver=d)
    stop = None
    if request.GET.get("stop") or request.POST.get("stop"):
        stop = get_object_or_404(trip.stops.select_related("so__customer", "site"), pk=request.GET.get("stop") or request.POST.get("stop"))
    if request.method == "POST" and stop:
        failed = request.POST.get("failed") == "1"
        delivered = {l.pk: request.POST.get(f"q_{l.pk}") for l in stop.lines.all()}
        ssvc.confirm_pod(stop, request.user, delivered, request.POST.get("receiver_name", ""),
                         photo=request.FILES.get("photo"), signature_data=request.POST.get("signature"),
                         note=request.POST.get("note", ""), failed=failed)
        messages.success(request, "Đã xác nhận giao hàng (POD)." if not failed else "Đã ghi nhận giao thất bại.")
        return redirect(f"/m/outbound/{trip.pk}")
    return render(request, "mobile/outbound.html", {
        "title": trip.code, "trip": trip, "stops": trip.stops.select_related("so__customer", "site").prefetch_related("lines__so_line__product__base_uom"),
        "stop": stop, "stop_lines": stop.lines.select_related("so_line__product__base_uom") if stop else []})
