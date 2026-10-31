"""Dịch vụ dùng chung: mã chứng từ, audit log, tham số hệ thống."""
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .middleware import get_current_request, get_current_user
from .models import Alert, AuditLog, DocSequence, SystemParam


class BusinessError(Exception):
    """Lỗi nghiệp vụ hiển thị được cho người dùng."""


def param(key, cast=float):
    """Đọc tham số: bảng system_param ghi đè settings.BUSINESS."""
    row = SystemParam.objects.filter(key=key).first()
    raw = row.value if row else settings.BUSINESS.get(key)
    return cast(raw) if raw is not None else None


@transaction.atomic
def next_doc_no(prefix: str) -> str:
    """PREFIX-YYMM-#### – reset mỗi tháng, khóa dòng chống trùng số."""
    period = timezone.localtime().strftime("%y%m")
    seq, _ = DocSequence.objects.get_or_create(prefix=prefix, period=period)
    seq = DocSequence.objects.select_for_update().get(pk=seq.pk)
    seq.last_no += 1
    seq.save(update_fields=["last_no"])
    return f"{prefix}-{period}-{seq.last_no:04d}"


def audit(action, obj=None, changes=None, model=None, user=None):
    req = get_current_request()
    ip = None
    if req is not None:
        ip = (req.META.get("HTTP_X_FORWARDED_FOR") or req.META.get("REMOTE_ADDR") or "").split(",")[0] or None
    user = user or get_current_user()
    AuditLog.objects.create(
        user=user if (user and user.is_authenticated) else None,
        action=action,
        model=model or (obj._meta.label if obj is not None else ""),
        object_id=str(getattr(obj, "pk", "") or ""),
        object_repr=str(obj)[:200] if obj is not None else "",
        changes=changes or {},
        ip=ip,
    )


def raise_alert(alert_type, title, message="", level="WARN", ref_type="", ref_id=None, url="", target_role=""):
    """Tạo cảnh báo nếu chưa có cảnh báo mở cùng loại/đối tượng."""
    exists = Alert.objects.filter(alert_type=alert_type, ref_type=ref_type, ref_id=ref_id, is_resolved=False).exists()
    if not exists:
        Alert.objects.create(alert_type=alert_type, title=title, message=message, level=level,
                             ref_type=ref_type, ref_id=ref_id, url=url, target_role=target_role)


def resolve_alerts(alert_type, ref_type, ref_id):
    Alert.objects.filter(alert_type=alert_type, ref_type=ref_type, ref_id=ref_id, is_resolved=False) \
        .update(is_resolved=True)
