import threading

from django.shortcuts import redirect

_local = threading.local()


def get_current_request():
    return getattr(_local, "request", None)


def get_current_user():
    req = get_current_request()
    return getattr(req, "user", None) if req else None


class CurrentUserMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        _local.request = request
        try:
            return self.get_response(request)
        finally:
            _local.request = None


class AreaGuardMiddleware:
    """Mỗi vai trò chỉ được vào đúng khu vực: /app/, /m/, /portal/."""
    AREAS = {"/app/": None, "/m/": "DRIVER", "/portal/": "CUSTOMER"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated:
            path = request.path
            for prefix, role in self.AREAS.items():
                if path.startswith(prefix):
                    allowed = (user.role == role) if role else user.role not in ("DRIVER", "CUSTOMER")
                    if not allowed:
                        return redirect(user.home_url)
        return self.get_response(request)
