from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission


def role_required(*roles):
    """Kiểm tra quyền phía backend. ADMIN luôn được phép trong /app/."""
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.user.role not in roles and request.user.role != "ADMIN":
                raise PermissionDenied("Bạn không có quyền truy cập chức năng này.")
            return view(request, *args, **kwargs)
        wrapper.allowed_roles = roles
        return wrapper
    return decorator


class HasRole(BasePermission):
    roles = ()

    def has_permission(self, request, view):
        roles = getattr(view, "allowed_roles", self.roles)
        return request.user.is_authenticated and (request.user.role in roles or request.user.role == "ADMIN")
