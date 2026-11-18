"""Tự mở toàn bộ trang của web theo từng vai trò để tìm trang lỗi (500) – chạy sau mỗi lần sửa code.

    python manage.py check_pages            # mỗi loại trang mở tối đa 4 bản ghi
    python manage.py check_pages --per 10   # kỹ hơn

- Đăng nhập lần lượt bằng một tài khoản đang hoạt động của mỗi vai trò, bắt đầu từ trang chủ của vai trò đó rồi bấm
  qua mọi liên kết nội bộ (chỉ MỞ trang – không bấm nút, không gửi form, không xóa gì).
- Trang Kế hoạch giao hàng được mở thêm cho từng ngày trong --days ngày gần đây (trang đổi nội dung theo ngày).
- Báo trang lỗi kèm nguyên nhân; lệnh kết thúc với mã lỗi 1 nếu có trang lỗi.
Nên chạy trên máy với dữ liệu demo (SQLite: load_demo demo.sqlite3 --yes) thay vì trên Supabase.
"""
import re
import traceback
from collections import defaultdict, deque
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.test import Client
from django.utils import timezone

START = {"DIRECTOR": ["/app/dashboard", "/app/master/", "/app/reports/"], "ADMIN": ["/app/dashboard", "/app/admin/audit"],
         "PURCHASER": ["/app/dashboard"], "WAREHOUSE": ["/app/dashboard"], "DISPATCHER": ["/app/dashboard"],
         "ACCOUNTANT": ["/app/dashboard"], "DRIVER": ["/m/trips"], "CUSTOMER": ["/portal/"], "SUPPLIER": ["/factory/"]}
DATED = {"DIRECTOR", "DISPATCHER"}
SKIP = re.compile(r"logout|/static/|/media/|/files/|^/django-admin/|/cron/|delete|cancel|\.(png|jpe?g|pdf|csv|xlsx)$")


class Command(BaseCommand):
    help = "Mở toàn bộ trang theo từng vai trò (chỉ xem), báo trang lỗi 500."

    def add_arguments(self, parser):
        parser.add_argument("--per", type=int, default=4, help="Số bản ghi tối đa mở cho mỗi loại trang")
        parser.add_argument("--days", type=int, default=21, help="Số ngày gần đây mở trang Kế hoạch giao hàng")
        parser.add_argument("--role", action="append", help="Chỉ kiểm tra vai trò này (vd. --role DRIVER)")

    def handle(self, per, days, role, **opts):
        import logging
        import warnings

        from apps.core.models import User
        logging.disable(logging.CRITICAL)  # lỗi được gom lại ở cuối, không in log từng request
        warnings.filterwarnings("ignore")
        today = timezone.localdate()
        dated = [f"/app/fulfillment/planning?date={(today + timedelta(days=d)).isoformat()}" for d in range(-days, 3)]
        errors, total = [], 0
        for r, starts in START.items():
            if role and r not in role:
                continue
            u = User.objects.filter(role=r, is_active=True).order_by("pk").first()
            if not u:
                self.stdout.write(self.style.WARNING(f"{r:<10} bỏ qua – không có tài khoản đang hoạt động"))
                continue
            c = Client(HTTP_HOST="localhost", raise_request_exception=True)
            c.force_login(u)
            queue = deque(starts + (dated if r in DATED else []))
            seen, count = set(), defaultdict(int)
            n = 0
            while queue:
                url = queue.popleft()
                if url in seen:
                    continue
                seen.add(url)
                path, _, qs = url.partition("?")
                kind = re.sub(r"\d+", "#", path) + ("?" + re.sub(r"=[^&]*", "=", qs) if qs else "")
                if count[kind] >= per and url not in starts and url not in dated:
                    continue
                count[kind] += 1
                n += 1
                try:
                    resp = c.get(url)
                except Exception as e:  # noqa: BLE001 – báo mọi lỗi của trang
                    errors.append((u.username, url, self.cause(e)))
                    self.stdout.write(self.style.ERROR(f"  LỖI {u.username} {url}"))
                    continue
                if resp.status_code >= 500:
                    errors.append((u.username, url, f"HTTP {resp.status_code}"))
                    continue
                if resp.status_code != 200 or "text/html" not in resp.get("Content-Type", ""):
                    continue
                for h in re.findall(r'href="(/[^"#]*)"', resp.content.decode(errors="ignore")):
                    h = h.replace("&amp;", "&")
                    if not SKIP.search(h) and h not in seen:
                        queue.append(h)
            total += n
            self.stdout.write(f"{r:<10} {u.username:<12} {n} trang")
        if errors:
            self.stdout.write(self.style.ERROR(f"\n✘ {len(errors)} trang lỗi / {total} trang đã mở:"))
            for un, url, why in errors:
                self.stdout.write(f"  [{un}] {url}\n      {why}")
            raise CommandError("Có trang lỗi – xem danh sách ở trên.")
        self.stdout.write(self.style.SUCCESS(f"\n✔ Đã mở {total} trang, không trang nào lỗi."))

    @staticmethod
    def cause(e):
        """Loại lỗi + dòng code / template gây lỗi (khung cuối thuộc dự án)."""
        frames = [f for f in traceback.extract_tb(e.__traceback__) if "/site-packages/" not in f.filename]
        where = f" @ {frames[-1].filename.split('/apps/')[-1]}:{frames[-1].lineno}" if frames else ""
        tpl = getattr(e, "template_debug", None)
        if tpl:
            where += f" · template {tpl.get('name', '').split('/templates/')[-1]} dòng {tpl.get('line')}"
        return f"{type(e).__name__}: {str(e)[:200]}{where}"
