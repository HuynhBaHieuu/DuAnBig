"""Chạy tay các tác vụ nền khi chưa có Celery/Redis: python manage.py run_jobs [--rop] [--scan]"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Chạy tác vụ nền: tính SS/ROP + PR tự động (--rop), quét Inbound (--scan). Mặc định chạy cả hai."

    def add_arguments(self, parser):
        parser.add_argument("--rop", action="store_true")
        parser.add_argument("--scan", action="store_true")

    def handle(self, *args, **opts):
        both = not opts["rop"] and not opts["scan"]
        if opts["rop"] or both:
            from apps.inventory.services import run_replenishment
            self.stdout.write(f"SS/ROP: {run_replenishment()}")
        if opts["scan"] or both:
            from apps.purchasing.services import scan_inbound
            self.stdout.write(f"Chuyến trễ mốc: {scan_inbound()}")
