from django.apps import apps
from django.contrib import admin

# Đăng ký toàn bộ model để quản trị viên tra cứu nhanh (nghiệp vụ chính thực hiện ở /app/)
for model in apps.get_app_config("accounting").get_models():
    admin.site.register(model)
