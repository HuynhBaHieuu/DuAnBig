"""Ghi audit_log tự động cho mọi thao tác ghi trên các model nghiệp vụ."""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .middleware import get_current_user

AUDITED_APPS = {"masterdata", "inventory", "purchasing", "sales", "accounting", "core"}
SKIP_MODELS = {"AuditLog", "DocSequence", "Alert", "Session", "StockMove"}


def _should_audit(sender):
    return sender._meta.app_label in AUDITED_APPS and sender.__name__ not in SKIP_MODELS


@receiver(post_save)
def _audit_save(sender, instance, created, raw=False, **kwargs):
    if raw or not _should_audit(sender):
        return
    if get_current_user() is None:  # seed / task nền vẫn ghi nhận với user rỗng
        pass
    from .services import audit
    fields = kwargs.get("update_fields")
    audit("CREATE" if created else "UPDATE", instance,
          changes={"fields": sorted(fields)} if fields else {})


@receiver(post_delete)
def _audit_delete(sender, instance, **kwargs):
    if not _should_audit(sender):
        return
    from .services import audit
    audit("DELETE", instance)
