"""Cấu hình Django – TRACON 80 Logistics/WMS."""
import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

ON_VERCEL = bool(os.getenv("VERCEL"))  # Vercel tự đặt biến này khi build/chạy

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-insecure-key")
DEBUG = os.getenv("DJANGO_DEBUG", "0" if ON_VERCEL else "1") == "1"
ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()]
if ON_VERCEL:
    ALLOWED_HOSTS += [".vercel.app"]
CSRF_TRUSTED_ORIGINS = ["https://*.vercel.app"] + [
    o.strip() for o in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "rest_framework",
    "apps.core",
    "apps.masterdata",
    "apps.inventory",
    "apps.purchasing",
    "apps.sales",
    "apps.accounting",
    "apps.portal",
    "apps.mobile",
    "apps.reports",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # phục vụ CSS/JS khi DEBUG=0 (Vercel)
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.CurrentUserMiddleware",
    "apps.core.middleware.AreaGuardMiddleware",
]

ROOT_URLCONF = "tracon.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.app_context",
            ],
            "builtins": ["apps.core.templatetags.tracon"],
        },
    },
]

WSGI_APPLICATION = "tracon.wsgi.application"

# ---------- Database: Supabase (PostgreSQL) hoặc SQLite fallback ----------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if os.getenv("USE_SQLITE") == "1":  # ép dùng SQLite (vd. tạo dữ liệu mẫu cục bộ rồi chép lên Supabase)
    DATABASE_URL = ""
if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.parse(
            DATABASE_URL,
            conn_max_age=0 if os.getenv("DB_TRANSACTION_POOLER") == "1" else 60,
            conn_health_checks=True,
            ssl_require=True,  # Supabase bắt buộc SSL
        )
    }
    if os.getenv("DB_TRANSACTION_POOLER") == "1":
        # Supavisor transaction mode không hỗ trợ server-side cursor / prepared statements
        DATABASES["default"]["DISABLE_SERVER_SIDE_CURSORS"] = True
        DATABASES["default"].setdefault("OPTIONS", {})["prepare_threshold"] = None
else:
    if ON_VERCEL:
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured("Chưa đặt DATABASE_URL (Supabase) trong Environment Variables của Vercel.")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.getenv("SQLITE_NAME") or BASE_DIR / "db.sqlite3",
        }
    }

AUTH_USER_MODEL = "core.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 6}},
]
LOGIN_URL = "/login"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login"

LANGUAGE_CODE = "vi"
TIME_ZONE = "Asia/Ho_Chi_Minh"
USE_I18N = True
USE_TZ = True  # lưu UTC, hiển thị giờ VN
DATETIME_FORMAT = "d/m/Y H:i"
DATE_FORMAT = "d/m/Y"
USE_THOUSAND_SEPARATOR = False

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
if not STATIC_ROOT.exists() and "collectstatic" not in " ".join(__import__("sys").argv):
    STATIC_ROOT = None  # chưa collectstatic -> WhiteNoise đọc thẳng static/ (tránh cảnh báo trên Vercel)
MEDIA_URL = "/media/"
MEDIA_ROOT = Path("/tmp/media") if ON_VERCEL else BASE_DIR / "media"  # Vercel: ổ đĩa chỉ ghi được /tmp

# WhiteNoise đọc trực tiếp thư mục static/ (không cần collectstatic khi deploy Vercel)
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = DEBUG

# ---------- Lưu ảnh (mốc hành trình, POD, chữ ký) ----------
# Có SUPABASE_URL + SUPABASE_SERVICE_KEY -> lưu vào Supabase Storage (bắt buộc khi chạy trên Vercel)
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
if SUPABASE_URL:  # chỉ giữ https://<ref>.supabase.co (bỏ /rest/v1 nếu copy nhầm)
    from urllib.parse import urlsplit as _us
    SUPABASE_URL = "{0.scheme}://{0.netloc}".format(_us(SUPABASE_URL))
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "media")
STORAGES = {
    "default": {"BACKEND": "apps.core.storage.SupabaseStorage" if SUPABASE_URL and SUPABASE_SERVICE_KEY
                else "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
CRON_SECRET = os.getenv("CRON_SECRET", "")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
}

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_TIMEZONE = TIME_ZONE

# ---------- Tham số nghiệp vụ (có thể ghi đè trong bảng system_param) ----------
BUSINESS = {
    "PO_APPROVAL_THRESHOLD": 50_000_000,      # PO >= 50 triệu cần Director duyệt
    "ADJ_APPROVAL_THRESHOLD": 5_000_000,      # Điều chỉnh giảm >= 5 triệu cần Director duyệt
    "GRN_VARIANCE_PCT": 2,                    # Lệch GRN > 2% cần duyệt
    "COUNT_VARIANCE_PCT": 1,                  # Lệch kiểm kê > 1% đếm lại
    "OVERDUE_DAYS_BLOCK": 30,                 # Nợ quá hạn > 30 ngày -> chờ duyệt SO
    "MILESTONE_LATE_MIN": 60,                 # Trễ mốc > 60 phút -> cảnh báo
    "SLOT_PROPOSE_HOURS": 4,                  # ETA <= 4h -> đề xuất lịch hạ tải
    "SERVICE_Z": 1.65,                        # Hệ số Z mặc định (95%)
    "VAT_RATE": 8,
    "LOGIN_MAX_FAIL": 5,
    "LOGIN_LOCK_MIN": 15,
}
FORMAT_MODULE_PATH = ["tracon.formats"]
