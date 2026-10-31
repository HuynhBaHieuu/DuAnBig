"""Tạo bucket public trên Supabase Storage để lưu ảnh: python manage.py setup_storage"""
import json
import urllib.error

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.storage import _request


class Command(BaseCommand):
    help = "Tạo bucket (mặc định 'media', chế độ public) trên Supabase Storage."

    def handle(self, *args, **opts):
        if not (settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY):
            raise CommandError("Chưa đặt SUPABASE_URL và SUPABASE_SERVICE_KEY trong .env")
        bucket = settings.SUPABASE_BUCKET
        body = json.dumps({"id": bucket, "name": bucket, "public": True}).encode()
        try:
            _request("POST", f"{settings.SUPABASE_URL}/storage/v1/bucket", body, {"Content-Type": "application/json"})
            self.stdout.write(self.style.SUCCESS(f"Đã tạo bucket public '{bucket}'."))
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if "already exists" in msg or e.code == 409:
                self.stdout.write(f"Bucket '{bucket}' đã tồn tại – OK.")
            else:
                raise CommandError(f"Lỗi {e.code}: {msg}")
