"""Lưu file (ảnh mốc hành trình, POD, chữ ký) lên Supabase Storage qua REST API.

Dùng khi chạy serverless (Vercel) – ổ đĩa không lưu lâu dài. Bucket để chế độ PRIVATE (test Gà STT 35):
    python manage.py setup_storage            # tạo bucket private / chuyển bucket đang public sang private
Ảnh không còn link public: storage.url() trả về /files/<đường dẫn> (view core.private_file) – view kiểm tra đăng
nhập + quyền rồi chuyển hướng tới signed URL có hạn (SIGNED_URL_SECONDS) do Supabase cấp.
"""
import json
import logging
import mimetypes
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage
from django.utils.deconstruct import deconstructible
from django.utils.text import slugify

from .services import BusinessError

log = logging.getLogger(__name__)


def ascii_filename(name):
    """'Ảnh chụp màn hình (1).PNG' -> 'anh-chup-man-hinh-1.png'.

    Supabase Storage từ chối key có ký tự ngoài ASCII ("Invalid key") -> upload lỗi 500."""
    root, ext = posixpath.splitext(name.replace("đ", "d").replace("Đ", "D"))
    return (slugify(root)[:80] or "file") + re.sub(r"[^a-z0-9.]", "", ext.lower())


def _headers(extra=None):
    key = settings.SUPABASE_SERVICE_KEY
    h = {"apikey": key}
    if key.startswith("eyJ"):  # khóa service_role dạng JWT (kiểu cũ); khóa sb_secret_ chỉ cần header apikey
        h["Authorization"] = f"Bearer {key}"
    h.update(extra or {})
    return h


def _request(method, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=_headers(headers))
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read()


@deconstructible
class SupabaseStorage(Storage):
    def __init__(self, bucket=None):
        self.bucket = bucket or settings.SUPABASE_BUCKET
        self.base = f"{settings.SUPABASE_URL}/storage/v1"

    def _path(self, name):
        return urllib.parse.quote(name.replace("\\", "/").lstrip("/"))

    def get_valid_name(self, name):
        return ascii_filename(name)

    def get_available_name(self, name, max_length=None):
        # thêm hậu tố ngẫu nhiên thay vì gọi API kiểm tra trùng tên
        root, ext = posixpath.splitext(name.replace("\\", "/"))
        return f"{root}_{uuid.uuid4().hex[:8]}{ext}"

    def _save(self, name, content):
        if hasattr(content, "seek"):
            content.seek(0)
        data = content.read()
        ctype = getattr(content, "content_type", None) or mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            _request("POST", f"{self.base}/object/{self.bucket}/{self._path(name)}", data,
                     {"Content-Type": ctype, "x-upsert": "true"})
        except urllib.error.HTTPError as e:
            log.error("Supabase Storage từ chối upload %s (%s): %r", name, e.code, e.read()[:300])
            raise BusinessError(f"Không lưu được ảnh lên kho lưu trữ (mã {e.code}). Vui lòng chụp/chọn lại ảnh và thử lại.")
        except (urllib.error.URLError, TimeoutError) as e:
            log.error("Không kết nối được Supabase Storage khi upload %s: %s", name, e)
            raise BusinessError("Mất kết nối tới kho lưu trữ ảnh. Vui lòng thử lại sau ít phút.")
        return name

    def _open(self, name, mode="rb"):
        _, body = _request("GET", f"{self.base}/object/{self.bucket}/{self._path(name)}")
        return ContentFile(body, name=name)

    def delete(self, name):
        try:
            _request("DELETE", f"{self.base}/object/{self.bucket}",
                     json.dumps({"prefixes": [name]}).encode(), {"Content-Type": "application/json"})
        except urllib.error.HTTPError:
            pass

    def exists(self, name):
        try:
            status, _ = _request("HEAD", self.url(name))
            return status == 200
        except urllib.error.HTTPError:
            return False

    def url(self, name):
        """Link nội bộ (cần đăng nhập) – không lộ đường dẫn bucket."""
        return f"/files/{self._path(name)}"

    def signed_url(self, name, expires=None):
        """Signed URL có hạn của Supabase cho 1 object trong bucket private."""
        expires = int(expires or getattr(settings, "SIGNED_URL_SECONDS", 300))
        _, body = _request("POST", f"{self.base}/object/sign/{self.bucket}/{self._path(name)}",
                           json.dumps({"expiresIn": expires}).encode(), {"Content-Type": "application/json"})
        signed = json.loads(body).get("signedURL") or json.loads(body).get("signedUrl")
        if not signed:
            raise BusinessError("Không tạo được link ảnh.")
        return f"{self.base}{signed}" if signed.startswith("/") else signed


class PrivateFileSystemStorage(FileSystemStorage):
    """Lưu ảnh trên ổ đĩa (chạy local) nhưng phục vụ qua /files/ (cần đăng nhập), không qua /media/ công khai."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("base_url", "/files/")
        super().__init__(*args, **kwargs)
