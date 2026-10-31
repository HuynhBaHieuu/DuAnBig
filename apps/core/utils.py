"""Tiện ích cho view: phân trang, xử lý lỗi nghiệp vụ, xuất Excel, PDF."""
import io
from functools import wraps

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.template.loader import render_to_string

from .services import BusinessError


def paginate(request, qs, per_page=20):
    page = Paginator(qs, per_page).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return page, params.urlencode()


def business_action(view):
    """Bắt BusinessError -> flash message, quay lại trang trước."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except BusinessError as e:
            if request.headers.get("Content-Type", "").startswith("application/json") or \
                    request.headers.get("Accept", "").startswith("application/json"):
                return JsonResponse({"ok": False, "error": str(e)}, status=400)
            messages.error(request, str(e))
            return redirect(request.META.get("HTTP_REFERER") or request.path)
    return wrapper


def excel_response(filename, headers, rows, title=None):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = (title or "Data")[:30]
    ws.append(headers)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="E65F2B")
        c.alignment = Alignment(horizontal="center")
    for r in rows:
        ws.append([float(v) if hasattr(v, "quantize") else v for v in r])
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = max(12, min(45, max(len(str(c.value or "")) for c in col) + 2))
    buf = io.BytesIO()
    wb.save(buf)
    resp = HttpResponse(buf.getvalue(),
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="{filename}.xlsx"'
    return resp


def pdf_response(request, template, context, filename):
    """WeasyPrint nếu có (Linux/production); nếu không -> trang HTML in được (Ctrl+P)."""
    html = render_to_string(template, context, request=request)
    try:
        from weasyprint import HTML  # type: ignore
        pdf = HTML(string=html, base_url=request.build_absolute_uri("/")).write_pdf()
        resp = HttpResponse(pdf, content_type="application/pdf")
        resp["Content-Disposition"] = f'inline; filename="{filename}.pdf"'
        return resp
    except Exception:
        return HttpResponse(html)
