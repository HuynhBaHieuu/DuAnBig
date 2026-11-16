"""Cảnh báo chứng từ nằm quá lâu ở một trạng thái (test Gà STT 39, 51, 25, 68).

Quét khi mở Dashboard / Cảnh báo (tối đa 10 phút / lần) và trong tác vụ đêm. Ngưỡng là tham số hệ thống:
  STALE_UNLOADING_HOURS   chuyến Inbound "Tới cổng / Đang hạ tải" (xe bị tính là bận, bến bị chiếm)
  STALE_DELIVERY_HOURS    chuyến giao "Đang giao" (khách thấy mãi ở cổng KH)
  STALE_COUNT_DAYS        phiếu kiểm kê "Đang đếm / Đếm lại" (KPI-2 không có dữ liệu)
  ALLOC_PENDING_DAYS      phiếu phân bổ thiếu hàng (AL) chờ Giám đốc duyệt
  PO_ACK_HOURS            PO đã gửi NCC mà nhà máy chưa bấm "Đã nhận đơn"
Cảnh báo tự đóng khi chứng từ rời trạng thái đó.
"""
from datetime import timedelta

from django.utils import timezone

from .services import param, raise_alert
from .models import Alert


def _dur(minutes):
    from .templatetags.tracon import dur
    return dur(minutes)


def _sync(alert_type, ref_type, stale):
    """stale: {ref_id: (title, message, url, target_role)} – mở cảnh báo mới, đóng cảnh báo của chứng từ đã hết kẹt."""
    for ref_id, (title, msg, url, role) in stale.items():
        raise_alert(alert_type, title, msg, ref_type=ref_type, ref_id=ref_id, url=url, target_role=role)
    Alert.objects.filter(alert_type=alert_type, ref_type=ref_type, is_resolved=False) \
        .exclude(ref_id__in=list(stale)).update(is_resolved=True)
    return len(stale)


def scan_stale(now=None):
    from apps.inventory.models import InventoryCount
    from apps.purchasing.models import InboundTrip, PurchaseOrder
    from apps.sales.models import Allocation, DeliveryTrip

    now = now or timezone.now()
    mins = lambda t: int((now - t).total_seconds() // 60)  # noqa: E731
    out = {}

    h = float(param("STALE_UNLOADING_HOURS") or 12)
    stale = {}
    for t in InboundTrip.objects.filter(status__in=["ARRIVED", "UNLOADING"]).select_related("vehicle"):
        since = t.arrived_at or t.updated_at
        if since and now - since > timedelta(hours=h):
            stale[t.pk] = (f"Chuyến {t.code} kẹt \"{t.get_status_display()}\" {_dur(mins(since))}",
                           f"Xe {t.vehicle.plate} tới cổng {timezone.localtime(since):%d/%m %H:%M} – tài xế bị tính là bận, "
                           "bến bị chiếm. Hoàn tất nhập kho hoặc Giám đốc đóng chuyến thủ công.",
                           f"/app/inbound/trips/{t.pk}", "WAREHOUSE")
    out["inbound"] = _sync("STALE_INBOUND", "inbound_trip", stale)

    h = float(param("STALE_DELIVERY_HOURS") or 48)
    stale = {}
    for t in DeliveryTrip.objects.filter(status="IN_TRANSIT").select_related("vehicle"):
        since = t.departed_at or t.updated_at
        if since and now - since > timedelta(hours=h):
            stale[t.pk] = (f"Chuyến giao {t.code} \"Đang giao\" {_dur(mins(since))}",
                           f"Xe {t.vehicle.plate} xuất kho {timezone.localtime(since):%d/%m %H:%M}, chưa xác nhận POD – "
                           "khách vẫn thấy \"Đang giao\" ở cổng khách hàng.", "/app/fulfillment/planning", "DISPATCHER")
    out["delivery"] = _sync("STALE_DELIVERY", "delivery_trip", stale)

    d = float(param("STALE_COUNT_DAYS") or 3)
    stale = {}
    for ic in InventoryCount.objects.filter(status__in=["COUNTING", "RECOUNT"]):
        if now - ic.created_at > timedelta(days=d):
            stale[ic.pk] = (f"Kiểm kê {ic.code} \"{ic.get_status_display()}\" {_dur(mins(ic.created_at))}",
                            "Phiếu chưa đếm xong nên KPI-2 (độ chính xác tồn kho) không có dữ liệu. Hoàn tất hoặc hủy phiếu.",
                            f"/app/inventory/counts/{ic.pk}", "WAREHOUSE")
    out["count"] = _sync("STALE_COUNT", "inventory_count", stale)

    d = float(param("ALLOC_PENDING_DAYS") or 2)
    stale = {}
    for al in Allocation.objects.filter(status="PROPOSED").select_related("product"):
        if now - al.created_at > timedelta(days=d):
            stale[al.pk] = (f"Phân bổ {al.code} chờ duyệt {_dur(mins(al.created_at))}",
                            f"{al.product.code}: các đơn thiếu hàng đang chờ Giám đốc duyệt phân bổ.",
                            f"/app/sales/allocations/{al.pk}", "DIRECTOR")
    out["allocation"] = _sync("STALE_ALLOCATION", "allocation", stale)

    h = float(param("PO_ACK_HOURS") or 4)
    stale = {}
    for po in PurchaseOrder.objects.filter(status="SENT", supplier_ack_at__isnull=True,
                                           sent_at__lt=now - timedelta(hours=h)).select_related("supplier"):
        stale[po.pk] = (f"{po.code}: nhà máy chưa xác nhận nhận đơn",
                        f"Gửi {po.supplier.name} lúc {timezone.localtime(po.sent_at):%d/%m %H:%M} ({_dur(mins(po.sent_at))}) "
                        "– gọi điện cho nhà máy.", f"/app/purchase/orders/{po.pk}", "PURCHASER")
    out["po_ack"] = _sync("PO_NOT_ACK", "po", stale)
    return out


def maybe_scan_stale(minutes=10):
    from django.core.cache import cache
    if cache.add("scan_stale_throttle", 1, minutes * 60):
        try:
            scan_stale()
        except Exception:  # không để lỗi quét làm hỏng trang
            cache.delete("scan_stale_throttle")
