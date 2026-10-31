from celery import shared_task

from .services import scan_inbound as _scan


@shared_task
def scan_inbound():
    """Mỗi 5 phút: quét trễ mốc (MILESTONE_LATE), tính lại ETA, đề xuất lịch hạ tải."""
    return _scan()
