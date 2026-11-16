"""/factory/ – tài khoản nhà máy (NCC). Test Gà STT 64–68.

- Mỗi tài khoản vai trò SUPPLIER gắn với đúng 1 nhà cung cấp (User.supplier); đăng nhập vào thẳng /factory/.
- Chỉ thấy PO của chính NCC mình, đã được duyệt (Đã duyệt / Đã gửi / đang nhận / đã nhận / đã hủy sau khi gửi).
  Lọc ở phía máy chủ: đổi ID trên URL sang PO của NCC khác -> 404.
- Đúng 1 nút "Đã nhận đơn" (lưu giờ + người xác nhận + ghi chú không bắt buộc).
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.core.services import audit, resolve_alerts
from apps.core.utils import business_action, pdf_response

from .models import PurchaseOrder

VISIBLE = ("APPROVED", "SENT", "PARTIAL", "RECEIVED", "CLOSED", "CANCELLED")


def _supplier(request):
    u = request.user
    if u.role != "SUPPLIER" or not u.supplier_id:
        raise PermissionDenied("Tài khoản chưa gắn với nhà cung cấp.")
    return u.supplier


def _qs(supplier):
    # PO hủy chỉ hiện nếu nhà máy đã từng được gửi / đã duyệt (không lộ PO nháp bị hủy)
    return PurchaseOrder.objects.filter(supplier=supplier, status__in=VISIBLE) \
        .exclude(Q(status="CANCELLED") & Q(approved_at__isnull=True) & Q(sent_at__isnull=True))


@login_required
def orders(request):
    sup = _supplier(request)
    qs = _qs(sup).prefetch_related("lines__product__base_uom").order_by("-id")
    tab = request.GET.get("tab", "open")
    if tab == "open":
        qs = qs.filter(status__in=["APPROVED", "SENT", "PARTIAL"])
    elif tab == "pending":
        qs = qs.filter(status__in=["APPROVED", "SENT"], supplier_ack_at__isnull=True)
    return render(request, "factory/orders.html", {
        "title": "Đơn mua của nhà máy", "sup": sup, "orders": qs[:200], "tab": tab,
        "pending": _qs(sup).filter(status__in=["APPROVED", "SENT"], supplier_ack_at__isnull=True).count()})


@login_required
@business_action
def order_detail(request, pk):
    sup = _supplier(request)
    po = get_object_or_404(_qs(sup).select_related("warehouse", "route"), pk=pk)  # PO của NCC khác -> 404
    if request.method == "POST":
        if po.status not in ("APPROVED", "SENT", "PARTIAL"):
            messages.error(request, "Đơn này không còn chờ xác nhận.")
        elif po.supplier_ack_at:
            messages.info(request, "Đơn đã được xác nhận trước đó.")
        else:
            po.supplier_ack_at, po.supplier_ack_by = timezone.now(), request.user
            po.supplier_ack_note = request.POST.get("note", "").strip()[:255]
            po.save(update_fields=["supplier_ack_at", "supplier_ack_by", "supplier_ack_note", "updated_at"])
            resolve_alerts("PO_NOT_ACK", "po", po.pk)
            audit("APPROVE", po, {"Nhà máy xác nhận đã nhận đơn": po.supplier_ack_note or "—"})
            messages.success(request, f"Đã xác nhận nhận đơn {po.code}. Cảm ơn quý đối tác!")
        return redirect(request.path)
    if request.GET.get("pdf"):
        return pdf_response(request, "docs/po.html", {"po": po}, po.code)
    return render(request, "factory/order_detail.html", {"title": po.code, "sup": sup, "po": po,
                                                          "lines": po.lines.select_related("product__base_uom")})
