from celery import shared_task

from .services import run_replenishment


@shared_task
def nightly_replenishment():
    """01:00 hằng đêm: tính SS/ROP, snapshot, cảnh báo, sinh PR tự động."""
    return run_replenishment()
