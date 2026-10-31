from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Alert, AuditLog, DocSequence, SystemParam, User


@admin.register(User)
class TraconUserAdmin(UserAdmin):
    list_display = ("username", "full_name", "role", "driver", "customer", "is_active")
    list_filter = ("role", "is_active")
    fieldsets = UserAdmin.fieldsets + (("TRACON 80", {"fields": ("role", "full_name", "phone", "driver", "customer",
                                                                 "failed_login_count", "locked_until")}),)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("at", "user", "action", "model", "object_repr")
    list_filter = ("action", "model")


admin.site.register([DocSequence, SystemParam, Alert])
admin.site.site_header = "TRACON 80 – Quản trị dữ liệu"
