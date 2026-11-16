"""Phục vụ ảnh (mốc hành trình, sự cố, POD, chữ ký) có kiểm tra quyền – test Gà STT 35.

/files/<đường dẫn>: chưa đăng nhập -> trang đăng nhập; không có quyền -> 404 (không lộ là file có tồn tại).
  - Nội bộ (/app/): xem được mọi ảnh nghiệp vụ.
  - Tài xế: chỉ ảnh của chuyến mình chạy.
  - Khách hàng: chỉ ảnh POD / chữ ký của đơn của mình.
  - Nhà máy: không xem ảnh.
Supabase: chuyển hướng tới signed URL có hạn. Local: trả file trực tiếp.
"""
import mimetypes
import posixpath
from urllib.parse import unquote

from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.http import FileResponse, Http404, HttpResponseRedirect

INTERNAL = ("ADMIN", "DIRECTOR", "PURCHASER", "DISPATCHER", "WAREHOUSE", "ACCOUNTANT")


def can_view_file(user, name):
    from apps.purchasing.models import TripIncident, TripMilestone
    from apps.sales.models import DeliveryStop

    if user.role in INTERNAL:
        return True
    if user.role == "DRIVER" and user.driver_id:
        return (TripMilestone.objects.filter(photo=name, trip__driver_id=user.driver_id).exists()
                or TripIncident.objects.filter(photo=name, trip__driver_id=user.driver_id).exists()
                or DeliveryStop.objects.filter(trip__driver_id=user.driver_id).filter(
                    pod_photo=name).exists()
                or DeliveryStop.objects.filter(trip__driver_id=user.driver_id, pod_signature=name).exists())
    if user.role == "CUSTOMER" and user.customer_id:
        return (DeliveryStop.objects.filter(so__customer_id=user.customer_id, pod_photo=name).exists()
                or DeliveryStop.objects.filter(so__customer_id=user.customer_id, pod_signature=name).exists())
    return False


@login_required
def private_file(request, name):
    name = posixpath.normpath(unquote(name)).lstrip("/")
    if name.startswith("..") or "/../" in f"/{name}/":
        raise Http404
    if not can_view_file(request.user, name):
        raise Http404
    storage = default_storage
    if hasattr(storage, "signed_url"):
        resp = HttpResponseRedirect(storage.signed_url(name))
    else:
        if not storage.exists(name):
            raise Http404
        resp = FileResponse(storage.open(name, "rb"), content_type=mimetypes.guess_type(name)[0])
    resp["Cache-Control"] = "private, no-store"
    return resp
