"""Tạo / chuyển bucket ảnh trên Supabase Storage sang chế độ PRIVATE: python manage.py setup_storage

Test Gà STT 35: ảnh POD, chữ ký, ảnh mốc không được tải công khai. Ứng dụng phục vụ ảnh qua /files/<đường dẫn>
(đăng nhập + kiểm tra quyền) rồi chuyển hướng tới signed URL có hạn – nên bucket phải private.
Chỉ chạy SAU KHI đã deploy bản code dùng /files/ (bản cũ dùng link public sẽ không hiện được ảnh).
"""
import json
import urllib.error

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.core.storage import _request


class Command(BaseCommand):
    help = "Tạo bucket (mặc định 'media') ở chế độ PRIVATE, hoặc chuyển bucket đang public sang private."

    def handle(self, *args, **opts):
        if not (settings.SUPABASE_URL and settings.SUPABASE_SERVICE_KEY):
            raise CommandError("Chưa đặt SUPABASE_URL và SUPABASE_SERVICE_KEY trong .env")
        bucket = settings.SUPABASE_BUCKET
        base = f"{settings.SUPABASE_URL}/storage/v1/bucket"
        body = json.dumps({"id": bucket, "name": bucket, "public": False}).encode()
        try:
            _request("POST", base, body, {"Content-Type": "application/json"})
            self.stdout.write(self.style.SUCCESS(f"Đã tạo bucket private '{bucket}'."))
            return
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if not ("already exists" in msg or e.code in (400, 409)):
                raise CommandError(f"Lỗi {e.code}: {msg}")
        try:
            _request("PUT", f"{base}/{bucket}", json.dumps({"public": False}).encode(),
                     {"Content-Type": "application/json"})
        except urllib.error.HTTPError as e:
            raise CommandError(f"Không chuyển được bucket sang private – lỗi {e.code}: {e.read().decode(errors='replace')}")
        _, raw = _request("GET", f"{base}/{bucket}")
        info = json.loads(raw)
        if info.get("public"):
            raise CommandError(f"Bucket '{bucket}' vẫn đang public.")
        self.stdout.write(self.style.SUCCESS(f"Bucket '{bucket}' đã ở chế độ PRIVATE (public=false)."))
