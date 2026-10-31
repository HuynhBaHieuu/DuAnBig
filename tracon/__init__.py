try:
    from .celery import app as celery_app  # noqa: F401
except Exception:  # celery chưa cài / chưa cấu hình
    celery_app = None
