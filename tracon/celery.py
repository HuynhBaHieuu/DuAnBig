import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tracon.settings")

app = Celery("tracon")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

app.conf.beat_schedule = {
    # 01:00 hằng đêm: tính SS/ROP + sinh PR tự động
    "nightly-replenishment": {
        "task": "apps.inventory.tasks.nightly_replenishment",
        "schedule": crontab(hour=1, minute=0),
    },
    # Quét trễ mốc / tính ETA / đề xuất lịch hạ tải mỗi 5 phút
    "scan-inbound": {
        "task": "apps.purchasing.tasks.scan_inbound",
        "schedule": crontab(minute="*/5"),
    },
}
