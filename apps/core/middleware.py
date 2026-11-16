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
    """Mỗi vai trò chỉ được vào đúng khu vực: /app/ (nội bộ), /m/ (tài xế), /portal/ (khách), /factory/ (nhà máy)."""
    AREAS = {"/app/": None, "/m/": "DRIVER", "/portal/": "CUSTOMER", "/factory/": "SUPPLIER"}
    EXTERNAL = ("DRIVER", "CUSTOMER", "SUPPLIER")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated:
            path = request.path
            for prefix, role in self.AREAS.items():
                if path.startswith(prefix):
                    allowed = (user.role == role) if role else user.role not in self.EXTERNAL
                    if not allowed:
                        return redirect(user.home_url)
        return self.get_response(request)


# Nguồn được phép tải (STT 42): script/style chỉ từ chính trang + CDN đã ghim phiên bản (có SRI).
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://unpkg.com https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com data:",
    "img-src 'self' data: blob: https:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
])


class SecurityHeadersMiddleware:
    """Trang đã đăng nhập không được cache ở proxy / CDN (Cache-Control: private, no-store) + Content-Security-Policy."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        ctype = response.get("Content-Type", "")
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            response["Cache-Control"] = "private, no-store, max-age=0"
            response["Pragma"] = "no-cache"
            response.setdefault("Vary", "Cookie")
        if ctype.startswith("text/html"):
            response.setdefault("Content-Security-Policy", CSP)
            response.setdefault("Referrer-Policy", "same-origin")
            response.setdefault("Permissions-Policy", "camera=(self), geolocation=(), microphone=()")
        return response
