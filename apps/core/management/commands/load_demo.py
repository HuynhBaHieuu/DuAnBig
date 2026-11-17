"""Nạp bộ dữ liệu demo (file SQLite do build_demo dựng) lên database hiện tại – thường là Supabase.

    python manage.py load_demo demo.sqlite3 --yes

- XÓA toàn bộ chứng từ và danh mục trên database hiện tại, rồi chép dữ liệu demo sang (bulk insert – vài phút).
- GIỮ tài khoản đang có (mật khẩu, vai trò, trạng thái không đổi). Tài khoản trùng tên đăng nhập với bộ demo được
  gắn lại tài xế / khách hàng / nhà máy theo bộ demo; tài khoản có trong bộ demo mà database chưa có thì được tạo
  (mật khẩu Tracon@123); tài khoản chỉ có trên database thì gắn lại theo mã cũ nếu mã đó còn trong danh mục mới.
- Dời mọi ngày giờ theo số ngày từ hôm dựng tới hôm nay -> chạy lại hôm nào cũng thấy "hôm nay" như lúc dựng.
  Nên chạy sau 07:30 (giờ chốt dữ liệu của build_demo).
- Đưa vài ảnh minh họa (ảnh mốc, POD, chữ ký) lên kho ảnh đang cấu hình (Supabase Storage hoặc thư mục media).
Chạy lại bao nhiêu lần cũng về cùng một trạng thái xuất phát (dùng sau mỗi lần diễn tập).
"""
import time as _time
from datetime import date, timedelta

from django.apps import apps
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connections, models, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

KEEP = {"core.user", "core.user_groups", "core.user_user_permissions", "core.systemparam",
        "contenttypes.contenttype", "auth.permission", "auth.group", "auth.group_permissions",
        "admin.logentry", "sessions.session"}
NO_SHIFT = {"inventory.simulateddemand"}  # tuần mô phỏng cố định (thứ Hai) – không dời
LINKED_ROLES = ("DRIVER", "CUSTOMER", "SUPPLIER")


class Command(BaseCommand):
    help = "Xóa chứng từ + danh mục trên database hiện tại và nạp dữ liệu demo từ file SQLite (giữ tài khoản)."

    def add_arguments(self, parser):
        parser.add_argument("sqlite_path")
        parser.add_argument("--yes", action="store_true", help="Xác nhận xóa dữ liệu hiện có")
        parser.add_argument("--batch", type=int, default=1000)
        parser.add_argument("--no-files", action="store_true", help="Không đưa ảnh minh họa lên kho ảnh")
        parser.add_argument("--recalc", action="store_true", help="Tính lại SS/ROP sau khi nạp (mặc định: khi dời ≥ 7 ngày)")

    def handle(self, sqlite_path, **opts):
        from django.conf import settings
        db = "default"
        target = connections[db]
        if target.vendor == "sqlite" and str(target.settings_dict["NAME"]) == str(sqlite_path):
            raise CommandError("Database đích trùng file nguồn.")
        connections.databases["source"] = {**connections.databases[db], "ENGINE": "django.db.backends.sqlite3",
                                           "NAME": sqlite_path, "OPTIONS": {}, "HOST": "", "PORT": "", "USER": "",
                                           "PASSWORD": "", "CONN_MAX_AGE": 0}
        host = target.settings_dict.get("HOST") or target.settings_dict.get("NAME")
        if not opts["yes"]:
            raise CommandError(f"Lệnh này XÓA toàn bộ chứng từ và danh mục trên {host}, giữ tài khoản. "
                               "Sao lưu trước, rồi chạy lại với --yes.")
        src_mig = set(MigrationRecorder(connections["source"]).applied_migrations())
        dst_mig = set(MigrationRecorder(target).applied_migrations())
        if src_mig - dst_mig:
            raise CommandError("Database đích chưa migrate đủ – chạy: python manage.py migrate")
        mine = {k for k in dst_mig - src_mig if k[0].startswith(("core", "masterdata", "inventory", "purchasing",
                                                                   "sales", "accounting"))}
        if mine:
            raise CommandError(f"Code đã có migration mới hơn file demo ({sorted(mine)[:3]}…) – dựng lại file bằng "
                               "build_demo rồi nạp.")
        from apps.core.models import SystemParam, User
        bd = SystemParam.objects.using("source").filter(key="DEMO_BUILD_DATE").first()
        if not bd:
            raise CommandError("File nguồn không phải file của build_demo (thiếu DEMO_BUILD_DATE).")
        delta = timedelta(days=(timezone.localdate() - date.fromisoformat(bd.value)).days)
        if delta.days < 0:
            raise CommandError("Ngày dựng dữ liệu sau hôm nay – kiểm tra giờ máy.")
        if delta.days > 21:
            self.stdout.write(self.style.WARNING(f"File demo dựng cách đây {delta.days} ngày – mã chứng từ (PO-YYMM-…) sẽ "
                                                 "lệch tháng so với ngày. Nên dựng lại bằng build_demo."))
        all_models = [m for m in apps.get_models(include_auto_created=True)
                      if m._meta.managed and not m._meta.proxy and m._meta.label_lower not in KEEP]
        t0 = _time.time()
        user_fk = {m: [f for f in m._meta.concrete_fields if f.is_relation and f.related_model is User]
                   for m in all_models}
        src_users = list(User.objects.using("source").all())
        from apps.masterdata.models import Customer, Driver, Supplier
        src_codes = {"driver": dict(Driver.objects.using("source").values_list("code", "id")),
                     "customer": dict(Customer.objects.using("source").values_list("code", "id")),
                     "supplier": dict(Supplier.objects.using("source").values_list("code", "id"))}
        live = {u.username: u for u in User.objects.using(db).select_related("driver", "customer", "supplier")}
        old_codes = {u.pk: {k: getattr(getattr(u, k), "code", None) for k in ("driver", "customer", "supplier")}
                     for u in live.values()}
        created, relinked, unlinked = [], [], []
        total = 0
        with transaction.atomic(using=db):
            User.objects.using(db).update(driver=None, customer=None, supplier=None)
            for m in reversed(all_models):
                m._base_manager.using(db).all()._raw_delete(db)
            umap = {}
            for su in src_users:  # tài khoản: giữ mật khẩu / vai trò hiện có, chỉ gắn lại liên kết danh mục
                u = live.get(su.username)
                # ghi bằng bulk_create / update: không phát tín hiệu audit (tránh trùng id với nhật ký sắp chép sang)
                if u is None:
                    u = User(**{f.attname: getattr(su, f.attname) for f in User._meta.concrete_fields if f.name != "id"})
                    User.objects.using(db).bulk_create([u])
                    if u.pk is None:
                        u = User.objects.using(db).get(username=su.username)
                    created.append(u.username)
                elif u.role == su.role:
                    upd = {"driver_id": su.driver_id, "customer_id": su.customer_id, "supplier_id": su.supplier_id}
                    if u.role in LINKED_ROLES:
                        upd["full_name"] = su.full_name
                    User.objects.using(db).filter(pk=u.pk).update(**upd)
                umap[su.pk] = u.pk
            src_names = {su.username for su in src_users}
            fallback = {"supplier": src_codes["supplier"].get("NCC-HM")}
            for u in live.values():
                if u.username in src_names or u.role not in LINKED_ROLES:
                    continue
                kind = u.role.lower()
                code = old_codes[u.pk][kind]
                new_id = src_codes[kind].get(code) or fallback.get(kind)
                User.objects.using(db).filter(pk=u.pk).update(**{f"{kind}_id": new_id})
                (relinked if new_id else unlinked).append(u.username)
            for m in all_models:
                rows = list(m._base_manager.using("source").all())
                if not rows:
                    continue
                shift = m._meta.label_lower not in NO_SHIFT and delta.days
                dfields = [f.attname for f in m._meta.concrete_fields
                           if isinstance(f, (models.DateTimeField, models.DateField))] if shift else []
                ufields = [f.attname for f in user_fk[m]]
                for o in rows:
                    for a in dfields:
                        v = getattr(o, a)
                        if v is not None:
                            setattr(o, a, v + delta)
                    for a in ufields:
                        v = getattr(o, a)
                        if v is not None:
                            setattr(o, a, umap.get(v))
                auto = [f for f in m._meta.concrete_fields if getattr(f, "auto_now", False) or getattr(f, "auto_now_add", False)]
                saved = [(f, f.auto_now, f.auto_now_add) for f in auto]
                try:  # giữ nguyên created_at / updated_at của dữ liệu demo (không để bulk_create đóng dấu giờ hiện tại)
                    for f in auto:
                        f.auto_now = f.auto_now_add = False
                    m._base_manager.using(db).bulk_create(rows, batch_size=opts["batch"])
                finally:
                    for f, a, b in saved:
                        f.auto_now, f.auto_now_add = a, b
                total += len(rows)
            with target.cursor() as cur:
                for sql in target.ops.sequence_reset_sql(no_style(), all_models):
                    cur.execute(sql)
            from apps.masterdata.models import Site, UserSiteAccess
            for u in User.objects.using(db).filter(role="CUSTOMER", customer__isnull=False):
                if not UserSiteAccess.objects.using(db).filter(user=u).exists():
                    UserSiteAccess.objects.using(db).bulk_create(
                        [UserSiteAccess(user=u, site=s) for s in Site.objects.using(db).filter(customer_id=u.customer_id)])
        self.stdout.write(self.style.SUCCESS(f"✔ Đã nạp {total} bản ghi lên {host} trong {_time.time() - t0:.0f}s "
                                             f"(dời ngày +{delta.days})."))
        if created:
            self.stdout.write(f"  Tạo tài khoản mới (mật khẩu Tracon@123): {', '.join(created)}")
        if relinked:
            self.stdout.write(f"  Gắn lại theo mã cũ: {', '.join(relinked)}")
        if unlinked:
            self.stdout.write(self.style.WARNING(f"  Chưa gắn được danh mục (mã cũ không còn): {', '.join(unlinked)} – "
                                                 "gắn lại trong Danh mục > Tài khoản."))
        if not opts["no_files"]:
            self.upload_files(settings)
        if opts["recalc"] or delta.days >= 7:
            from apps.inventory.services import run_replenishment
            res = run_replenishment()
            self.stdout.write(f"  Đã tính lại SS/ROP: {res}")

    def upload_files(self, settings):
        from django.core.files.storage import default_storage

        from .build_demo import demo_file_bytes
        supabase = hasattr(default_storage, "signed_url")
        n = 0
        for name, data in demo_file_bytes().items():
            if supabase:
                default_storage._save(name, ContentFile(data))  # ghi đè đúng tên (x-upsert)
            elif not default_storage.exists(name):
                default_storage.save(name, ContentFile(data))
            n += 1
        self.stdout.write(f"  Ảnh minh họa: {n} file trên {'Supabase Storage' if supabase else settings.MEDIA_ROOT}")
