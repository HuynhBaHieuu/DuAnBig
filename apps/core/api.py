"""REST API (DRF) – phục vụ tích hợp / app di động sau này. Quyền kiểm tra theo vai trò."""
from django.urls import path
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from apps.core.permissions import HasRole
from apps.inventory.services import stock_summary
from apps.masterdata.models import Product
from apps.purchasing.models import InboundTrip


class StockPerm(HasRole):
    roles = ("DIRECTOR", "PURCHASER", "DISPATCHER", "WAREHOUSE", "ACCOUNTANT")


class InboundPerm(HasRole):
    roles = ("DIRECTOR", "PURCHASER", "WAREHOUSE")


@api_view(["GET"])
@permission_classes([StockPerm])
def stock(request):
    data = []
    for s in stock_summary(list(Product.objects.filter(is_active=True))).values():
        p = s["product"]
        data.append({"code": p.code, "name": p.name, "on_hand": s["on_hand"], "reserved": s["reserved"],
                     "available": s["available"], "incoming": s["incoming"], "projected": s["projected"],
                     "ss": s["ss"], "rop": s["rop"], "level": s["level"]})
    return Response(data)


class TripSerializer(serializers.ModelSerializer):
    supplier = serializers.CharField(source="po.supplier.name")
    vehicle = serializers.CharField(source="vehicle.plate")
    driver = serializers.CharField(source="driver.name")

    class Meta:
        model = InboundTrip
        fields = ["id", "code", "status", "supplier", "vehicle", "driver", "planned_departure", "eta", "is_late", "total_ton"]


@api_view(["GET"])
@permission_classes([InboundPerm])
def inbound_trips(request):
    qs = InboundTrip.objects.exclude(status__in=["RECEIVED", "CANCELLED"]).select_related("po__supplier", "vehicle", "driver")
    return Response(TripSerializer(qs, many=True).data)


urlpatterns = [
    path("stock/", stock),
    path("inbound/trips/", inbound_trips),
]
