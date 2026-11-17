"""Ghi audit_log tự động cho mọi thao tác ghi trên các model nghiệp vụ – kèm giá trị cũ → mới.

- Sửa bản ghi: chỉ ghi các trường thực sự đổi {"Tên trường": [cũ, mới]}; không đổi gì thì không ghi.
- Dòng con của chứng từ (dòng đơn, dòng PO, mốc chuyến, lịch hạ tải...) ghi vào LỊCH SỬ CỦA CHỨNG TỪ CHA
  (vd. "Dòng XM-01 · SL đặt": [10, 15]) để tab "Lịch sử" của chứng từ thấy đủ.
"""
from datetime import date, datetime
from decimal import Decimal

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

AUDITED_APPS = {"masterdata", "inventory", "purchasing", "sales", "accounting", "core"}
SKIP_MODELS = {"AuditLog", "DocSequence", "Alert", "Session", "StockMove", "SalesOrderHistory", "TripEtaLog",
               "ReplenishmentCheckLog", "ReplenishmentJobRun", "ReplenishmentSnapshot", "StockReservation"}
SKIP_FIELDS = {"id", "created_at", "updated_at", "password", "last_login", "failed_login_count", "snapshot",
               "forecast_data"}  # khối JSON tính toán dự báo – không ghi nhật ký (Gà STT 109)

# Dòng con -> tên FK tới chứng từ cha (lịch sử ghi vào chứng từ cha)
DOC_PARENT = {
    "SalesOrderLine": "so", "AllocationLine": "allocation", "PickingLine": "picking", "GoodsIssueLine": "gi",
    "SalesReturnLine": "ret", "PurchaseRequisitionLine": "pr", "PurchaseOrderLine": "po", "InboundTripLine": "trip",
    "TripMilestone": "trip", "TripIncident": "trip", "UnloadingSlot": "trip", "GoodsReceiptLine": "gr",
    "InventoryCountLine": "count", "StockAdjustmentLine": "adjustment", "InvoiceLine": "invoice", "BillLine": "bill",
}


def _should_audit(sender):
    return sender._meta.app_label in AUDITED_APPS and sender.__name__ not in SKIP_MODELS


def _fields(sender):
    return [f for f in sender._meta.concrete_fields if f.name not in SKIP_FIELDS]


def fmt_value(field, value):
    """Giá trị thô -> chuỗi hiển thị (lựa chọn -> nhãn, FK -> tên đối tượng, ngày giờ -> giờ VN)."""
    if value is None or value == "":
        return "—"
    if field.choices:
        return str(dict(field.flatchoices).get(value, value))
    if field.is_relation:
        try:
            return str(field.related_model._default_manager.filter(pk=value).first() or value)
        except Exception:  # noqa: BLE001
            return str(value)
    if isinstance(value, bool):
        return "Có" if value else "Không"
    if isinstance(value, datetime):
        return timezone.localtime(value).strftime("%d/%m/%Y %H:%M") if timezone.is_aware(value) else value.strftime("%d/%m/%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, Decimal):
        return f"{value.normalize():f}" if value == value.to_integral() else f"{value.normalize()}"
    return str(value)[:200]


# Nhãn tiếng Việt cho các trường chưa khai báo verbose_name
FIELD_LABELS = {
    "qty": "Số lượng", "unit_price": "Đơn giá", "unit_cost": "Đơn giá vốn", "amount": "Thành tiền",
    "total_amount": "Tổng tiền", "subtotal": "Tiền hàng", "vat_amount": "Tiền VAT", "vat_rate": "Thuế suất VAT",
    "total": "Tổng tiền", "freight_amount": "Cước vận chuyển", "paid_amount": "Đã thanh toán",
    "status": "Trạng thái", "note": "Ghi chú", "reason": "Lý do", "cancel_reason": "Lý do hủy",
    "reject_reason": "Lý do từ chối", "customer": "Khách hàng", "site": "Công trình", "supplier": "Nhà cung cấp",
    "suggested_supplier": "NCC gợi ý", "carrier": "Nhà vận tải", "product": "Vật tư", "warehouse": "Kho",
    "location": "Vị trí", "route": "Tuyến", "vehicle": "Xe", "driver": "Tài xế", "dock": "Bến",
    "order_date": "Ngày đặt", "requested_date": "Ngày cần giao", "need_date": "Ngày cần hàng",
    "pickup_date": "Ngày lấy hàng", "planned_date": "Ngày kế hoạch", "due_date": "Hạn thanh toán",
    "submitted_at": "Giờ gửi", "confirmed_at": "Giờ xác nhận", "confirmed_by": "Người xác nhận",
    "approved_at": "Giờ duyệt", "approved_by": "Người duyệt", "cancelled_at": "Giờ hủy", "cancelled_by": "Người hủy",
    "rejected_at": "Giờ từ chối", "rejected_by": "Người từ chối", "sent_at": "Giờ gửi NCC", "posted_at": "Giờ ghi sổ",
    "received_at": "Giờ nhận", "arrived_at": "Giờ tới cổng", "departed_at": "Giờ xuất phát",
    "delivered_at": "Giờ giao", "checked_in_at": "Giờ check-in", "done_at": "Giờ hoàn tất",
    "completed_at": "Giờ hoàn tất", "eta": "ETA", "planned_departure": "Giờ xuất phát dự kiến",
    "planned_at": "Kế hoạch gốc", "replanned_at": "Dự kiến lại", "actual_at": "Thực tế", "by_user": "Người cập nhật",
    "photo": "Ảnh", "start_at": "Bắt đầu", "end_at": "Kết thúc", "is_late": "Trễ mốc",
    "reserved_qty": "Đã giữ", "delivered_qty": "Đã giao", "planned_qty": "Đã xếp chuyến", "invoiced_qty": "Đã lập HĐ",
    "atp_status": "Tình trạng hàng", "qty_received": "SL nhận", "qty_expected": "SL dự kiến", "qty_damaged": "SL hỏng",
    "qty_loaded": "SL bốc", "qty_planned": "SL kế hoạch", "qty_on_trips": "SL đã xếp chuyến", "qty_picked": "SL đã soạn",
    "qty_to_pick": "SL cần soạn", "qty_delivered": "SL giao", "qty_billed": "SL đã lập HĐ", "qty_diff": "Chênh lệch",
    "book_qty": "Tồn sổ sách", "count1": "Đếm lần 1", "count2": "Đếm lần 2", "final_qty": "SL chốt",
    "demand_qty": "Nhu cầu", "proposed_qty": "Đề xuất", "approved_qty": "Duyệt", "available_qty": "Khả dụng",
    "priority_rule": "Quy tắc ưu tiên", "urgency": "Mức khẩn", "created_by": "Người lập", "requested_by": "Người đề xuất",
    "receiver_name": "Người nhận", "description": "Mô tả", "delay_minutes": "Trễ (phút)", "incident_type": "Loại sự cố",
    "is_resolved": "Đã xử lý", "approval_reasons": "Lý do cần duyệt", "total_ton": "Tổng tấn", "rank": "Thứ tự",
    "vendor_invoice_no": "Số HĐ NCC", "order_type": "Loại đơn", "source": "Nguồn", "picker": "Người soạn",
}
PRICE_WORDS = ("giá", "tiền", "vat", "cước", "thanh toán", "price", "cost", "amount")


def _label(field):
    v = str(field.verbose_name or field.name)
    if v == field.name.replace("_", " "):
        v = FIELD_LABELS.get(field.name, v)
    return v[:1].upper() + v[1:]


def _line_label(instance):
    name = type(instance).__name__
    if name == "TripMilestone":
        return f"Mốc {instance.name}"
    if name == "UnloadingSlot":
        return "Lịch hạ tải"
    if name == "TripIncident":
        return "Sự cố"
    prod = getattr(instance, "product", None) if getattr(instance, "product_id", None) else None
    return f"Dòng {prod.code}" if prod is not None else "Dòng"


@receiver(pre_save)
def _audit_before(sender, instance, raw=False, **kwargs):
    """Đọc giá trị trong DB trước khi lưu để so sánh cũ → mới."""
    if raw or not _should_audit(sender) or instance.pk is None:
        return
    names = [f.attname for f in _fields(sender)]
    fields = kwargs.get("update_fields")
    if fields:
        names = [f.attname for f in _fields(sender) if f.name in fields or f.attname in fields]
    try:
        instance._audit_old = sender._default_manager.filter(pk=instance.pk).values(*names).first()
    except Exception:  # noqa: BLE001 – audit không được làm hỏng nghiệp vụ
        instance._audit_old = None


def _diff(sender, instance):
    old = getattr(instance, "_audit_old", None)
    if old is None:
        return None
    out = {}
    for f in _fields(sender):
        if f.attname not in old:
            continue
        a, b = old[f.attname], getattr(instance, f.attname)
        if isinstance(a, Decimal) or isinstance(b, Decimal):
            try:
                if Decimal(a if a is not None else "NaN") == Decimal(b if b is not None else "NaN"):
                    continue
            except Exception:  # noqa: BLE001
                pass
        if a == b:
            continue
        out[_label(f)] = [fmt_value(f, a), fmt_value(f, b)]
    return out


@receiver(post_save)
def _audit_save(sender, instance, created, raw=False, **kwargs):
    if raw or not _should_audit(sender):
        return
    from .services import audit
    parent_fk = DOC_PARENT.get(sender.__name__)
    if created:
        if parent_fk:
            parent = getattr(instance, parent_fk, None)
            if parent is not None:
                qty = getattr(instance, "qty", None)
                desc = _line_label(instance) + (f" · SL {fmt_value(sender._meta.get_field('qty'), qty)}" if qty is not None else "")
                audit("UPDATE", parent, {"Thêm": desc})
            return
        audit("CREATE", instance)
        return
    changes = _diff(sender, instance)
    if changes is None:  # không đọc được giá trị cũ -> ghi như trước (danh sách trường)
        fields = kwargs.get("update_fields")
        changes = {"fields": sorted(fields)} if fields else {}
    elif not changes:
        return  # lưu lại nhưng không đổi gì
    if parent_fk:
        parent = getattr(instance, parent_fk, None)
        if parent is not None:
            prefix = _line_label(instance)
            audit("UPDATE", parent, {f"{prefix} · {k}": v for k, v in changes.items()})
            return
    audit("UPDATE", instance, changes)


@receiver(post_delete)
def _audit_delete(sender, instance, **kwargs):
    if not _should_audit(sender):
        return
    from .services import audit
    parent_fk = DOC_PARENT.get(sender.__name__)
    if parent_fk:
        try:
            parent = getattr(instance, parent_fk, None)
        except Exception:  # noqa: BLE001 – chứng từ cha đã bị xóa cùng lúc
            parent = None
        if parent is not None and parent.pk is not None:
            audit("UPDATE", parent, {"Xóa": _line_label(instance)})
            return
    audit("DELETE", instance)
