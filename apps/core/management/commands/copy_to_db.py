"""Chép toàn bộ dữ liệu từ một file SQLite sang database hiện tại (Supabase) bằng bulk insert.

Nhanh hơn nhiều so với chạy seed_demo trực tiếp qua mạng (mỗi câu lệnh tới Supabase mất ~0,3s):
    set USE_SQLITE=1 & set SQLITE_NAME=seed.sqlite3 & python manage.py migrate & python manage.py seed_demo
    python manage.py copy_to_db seed.sqlite3
"""
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connections, transaction

SKIP = {"contenttypes.contenttype", "auth.permission", "admin.logentry", "sessions.session"}


class Command(BaseCommand):
    help = "Chép dữ liệu từ file SQLite sang database mặc định (database đích phải trống, đã migrate)."

    def add_arguments(self, parser):
        parser.add_argument("sqlite_path")
        parser.add_argument("--batch", type=int, default=1000)

    def handle(self, sqlite_path, batch, **opts):
        target = connections["default"]
        if target.vendor == "sqlite":
            raise CommandError("Database đích đang là SQLite – kiểm tra DATABASE_URL trong .env (và bỏ USE_SQLITE).")
        connections.databases["source"] = {**connections.databases["default"], "ENGINE": "django.db.backends.sqlite3",
                                           "NAME": sqlite_path, "OPTIONS": {}, "HOST": "", "PORT": "", "USER": "",
                                           "PASSWORD": ""}
        models = [m for m in apps.get_models(include_auto_created=True)
                  if m._meta.label_lower not in SKIP and not m._meta.proxy and m._meta.managed]
        from apps.core.models import User
        if User.objects.using("default").exists():
            raise CommandError("Database đích đã có dữ liệu. Hãy dùng database trống (hoặc chạy: manage.py flush).")

        total = 0
        # Django tạo khóa ngoại PostgreSQL dạng DEFERRABLE INITIALLY DEFERRED -> thứ tự chèn không quan trọng
        with transaction.atomic(using="default"):
            for m in models:
                rows = list(m._base_manager.using("source").all())
                if rows:
                    m._base_manager.using("default").bulk_create(rows, batch_size=batch)
                    total += len(rows)
                    self.stdout.write(f"  {m._meta.label:<40} {len(rows):>6}")
            with target.cursor() as cur:
                for sql in target.ops.sequence_reset_sql(no_style(), models):
                    cur.execute(sql)
        self.stdout.write(self.style.SUCCESS(f"✔ Đã chép {total} bản ghi sang {target.settings_dict['HOST']}."))
