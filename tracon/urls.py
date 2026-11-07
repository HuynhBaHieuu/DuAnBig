from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from apps.accounting import views as acc
from apps.core import views as core
from apps.inventory import views as inv
from apps.masterdata import views as md
from apps.mobile import views as mob
from apps.portal import views as portal
from apps.purchasing import views as pur
from apps.reports import views as rep
from apps.sales import views as sal

app_patterns = [
    path("dashboard", core.dashboard),                                     # MH-03
    path("alerts", core.alerts),
    path("alerts/<int:pk>/resolve", core.alert_resolve),
    path("search", core.search),
    path("profile", core.profile),
    # Tồn kho
    path("inventory/stock", inv.stock),                                    # MH-10
    path("inventory/stock/<int:pk>/locations", inv.stock_locations),
    path("inventory/transfer", inv.stock_transfer),
    path("inventory/replenishment", inv.replenishment),                    # MH-12
    path("inventory/replenishment/<int:pk>", inv.replenishment_save),
    path("inventory/moves", inv.moves),
    path("inventory/counts", inv.counts),
    path("inventory/counts/<int:pk>", inv.count_detail),
    # Mua hàng & Inbound
    path("purchase/requisitions", pur.pr_list),
    path("purchase/requisitions/new", pur.pr_new),
    path("purchase/requisitions/convert", pur.pr_convert),
    path("purchase/requisitions/<int:pk>", pur.pr_detail),
    path("purchase/orders", pur.po_list),                                  # MH-22
    path("purchase/orders/new", pur.po_form),
    path("purchase/orders/<int:pk>", pur.po_detail),
    path("purchase/orders/<int:pk>/edit", pur.po_form),
    path("inbound/board", pur.inbound_board),                              # MH-24
    path("inbound/trips/new", pur.trip_new),
    path("inbound/trips/<int:pk>", pur.trip_detail),
    path("warehouse/slots", pur.slots),                                    # MH-26
    path("warehouse/slots/<int:pk>", pur.slot_action),
    path("warehouse/receipts", pur.receipts),
    path("warehouse/receipts/<int:pk>", pur.receipt_detail),               # MH-27
    # Bán hàng & Fulfillment
    path("sales/orders", sal.so_list),                                     # MH-31
    path("sales/orders/new", sal.so_form),
    path("sales/orders/<int:pk>", sal.so_detail),
    path("sales/orders/<int:pk>/edit", sal.so_form),
    path("sales/allocations", sal.alloc_list),
    path("sales/allocations/<int:pk>", sal.alloc_detail),
    path("fulfillment/planning", sal.planning),                            # MH-34
    path("fulfillment/picking", sal.picking_list),
    path("fulfillment/picking/<int:pk>", sal.picking_detail),              # MH-35
    path("fulfillment/returns", sal.returns),
    # Kế toán
    path("accounting/invoices", acc.invoices),
    path("accounting/invoices/new", acc.invoice_new),
    path("accounting/invoices/<int:pk>", acc.invoice_detail),
    path("accounting/bills", acc.bills),                                   # MH-42
    path("accounting/bills/new", acc.bill_new),
    path("accounting/bills/<int:pk>", acc.bill_detail),
    path("accounting/payments", acc.payments),
    path("accounting/debts", acc.debts),                                   # MH-44
    # Báo cáo MH-70 → 76
    path("reports/", rep.index),
    path("reports/rop", rep.rop),                                          # MH-71
    path("reports/nxt", rep.nxt),
    path("reports/otif", rep.otif),
    path("reports/leadtime", rep.leadtime),
    path("reports/unloading", rep.unloading),
    # Danh mục & quản trị
    path("master/", md.index),
    path("master/<slug:slug>/", md.entity_list),
    path("master/<slug:slug>/new", md.entity_form),
    path("master/<slug:slug>/<int:pk>", md.entity_form),
    path("admin/users", core.users),
    path("admin/users/new", core.user_edit),
    path("admin/users/<int:pk>", core.user_edit),
    path("admin/params", core.params),
    path("admin/audit", core.audit_log),
]

mobile_patterns = [
    path("trips", mob.trips),                                              # MH-80
    path("inbound/<int:pk>", mob.inbound),                                 # MH-81
    path("outbound/<int:pk>", mob.outbound),                               # MH-82
]

portal_patterns = [
    path("", portal.home),                                                 # MH-90
    path("order", portal.order_new),                                       # MH-91/92
    path("orders", portal.orders),                                         # MH-93
    path("orders/<int:pk>", portal.order_detail),
    path("orders/<int:pk>/status", portal.order_status),
    path("documents", portal.documents),                                   # MH-94
    path("invoices/<int:pk>/pdf", portal.invoice_pdf),
    path("debts", portal.debts),                                           # MH-95
]

urlpatterns = [
    path("", core.home),
    path("login", core.login_view),                                        # MH-01
    path("logout", core.logout_view),
    path("app/", include(app_patterns)),
    path("m/", include(mobile_patterns)),
    path("portal/", include(portal_patterns)),
    path("api/", include("apps.core.api")),
    path("cron/nightly", core.cron_nightly),
    path("django-admin/", admin.site.urls),
]
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
