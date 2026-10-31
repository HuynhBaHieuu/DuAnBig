from django.db.models import Q

from .models import Alert

ALL_INTERNAL = ("ADMIN", "DIRECTOR", "PURCHASER", "DISPATCHER", "WAREHOUSE", "ACCOUNTANT")

# (nhóm, [(nhãn, url, icon lucide, vai trò được thấy)])
MENU = [
    ("", [
        ("Dashboard", "/app/dashboard", "layout-grid", ALL_INTERNAL),
        ("Cảnh báo", "/app/alerts", "bell", ALL_INTERNAL),
    ]),
    ("Kho hàng", [
        ("Tồn kho", "/app/inventory/stock", "boxes", ALL_INTERNAL),
        ("SS / ROP", "/app/inventory/replenishment", "trending-up", ("DIRECTOR", "PURCHASER")),
        ("Sổ kho", "/app/inventory/moves", "scroll-text", ("DIRECTOR", "WAREHOUSE", "ACCOUNTANT")),
        ("Kiểm kê", "/app/inventory/counts", "clipboard-check", ("DIRECTOR", "WAREHOUSE")),
    ]),
    ("Mua hàng & Inbound", [
        ("Đề xuất mua (PR)", "/app/purchase/requisitions", "file-plus-2", ("DIRECTOR", "PURCHASER")),
        ("Đơn mua (PO)", "/app/purchase/orders", "shopping-cart", ("DIRECTOR", "PURCHASER", "ACCOUNTANT")),
        ("Điều phối Inbound", "/app/inbound/board", "truck", ("DIRECTOR", "PURCHASER", "WAREHOUSE")),
        ("Lịch hạ tải", "/app/warehouse/slots", "calendar-clock", ("DIRECTOR", "PURCHASER", "WAREHOUSE")),
        ("Phiếu nhập (GRN)", "/app/warehouse/receipts", "package-check", ("DIRECTOR", "PURCHASER", "WAREHOUSE")),
    ]),
    ("Bán hàng & Giao hàng", [
        ("Đơn bán (SO)", "/app/sales/orders", "receipt", ("DIRECTOR", "DISPATCHER", "ACCOUNTANT")),
        ("Phân bổ thiếu hàng", "/app/sales/allocations", "split", ("DIRECTOR", "DISPATCHER")),
        ("Kế hoạch giao", "/app/fulfillment/planning", "route", ("DIRECTOR", "DISPATCHER")),
        ("Soạn & xuất kho", "/app/fulfillment/picking", "package-open", ("DIRECTOR", "DISPATCHER", "WAREHOUSE")),
        ("Trả hàng (RT)", "/app/fulfillment/returns", "undo-2", ("DIRECTOR", "DISPATCHER", "WAREHOUSE")),
    ]),
    ("Kế toán", [
        ("Hóa đơn bán", "/app/accounting/invoices", "file-text", ("DIRECTOR", "ACCOUNTANT")),
        ("Hóa đơn mua & 3 bên", "/app/accounting/bills", "file-check-2", ("DIRECTOR", "ACCOUNTANT")),
        ("Thu / Chi", "/app/accounting/payments", "wallet", ("DIRECTOR", "ACCOUNTANT")),
        ("Công nợ & tuổi nợ", "/app/accounting/debts", "hourglass", ("DIRECTOR", "ACCOUNTANT")),
    ]),
    ("Phân tích", [
        ("Báo cáo KPI", "/app/reports/", "bar-chart-3", ("DIRECTOR", "PURCHASER", "ACCOUNTANT", "DISPATCHER")),
    ]),
    ("Danh mục & Hệ thống", [
        ("Danh mục", "/app/master/", "database", ("PURCHASER", "DISPATCHER")),
        ("Tài khoản", "/app/admin/users", "users", ()),
        ("Tham số", "/app/admin/params", "sliders-horizontal", ()),
        ("Nhật ký kiểm toán", "/app/admin/audit", "shield-check", ()),
    ]),
]


def alerts_for(user):
    qs = Alert.objects.filter(is_resolved=False)
    if user.role not in ("ADMIN", "DIRECTOR"):
        qs = qs.filter(Q(target_role=user.role) | Q(target_role=""))
    return qs


def app_context(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not request.path.startswith("/app/"):
        return {}
    menu = []
    for group, items in MENU:
        visible = [
            {"label": l, "url": u, "icon": i, "active": request.path.startswith(u.rstrip("/") if u != "/app/reports/" else u)}
            for l, u, i, roles in items if user.role == "ADMIN" or user.role in roles
        ]
        if visible:
            menu.append({"group": group, "items": visible})
    return {"menu": menu, "alert_count": alerts_for(user).count()}
