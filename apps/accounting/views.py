from datetime import datetime
from decimal import Decimal

from django.contrib import messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from apps.core.permissions import role_required
from apps.core.services import BusinessError
from apps.core.utils import business_action, excel_response, paginate, pdf_response
from apps.masterdata.models import Carrier, Customer, Supplier
from apps.purchasing.models import InboundTrip, PurchaseOrderLine

from . import services as svc
from .models import Bill, Invoice, Payment

ACC = ("DIRECTOR", "ACCOUNTANT")


def _dec(v, default="0"):
    try:
        return Decimal(str(v).replace(",", "") or default)
    except Exception:
        return Decimal(default)


def _date(v):
    return datetime.strptime(v, "%Y-%m-%d").date() if v else timezone.localdate()


# ------------------------------------------------------------------ Hóa đơn bán
@role_required(*ACC)
def invoices(request):
    qs = Invoice.objects.select_related("customer")
    if request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"])
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(code__icontains=q) | Q(customer__name__icontains=q))
    page, qs_str = paginate(request, qs)
    return render(request, "accounting/invoices.html", {"title": "Hóa đơn bán (INV)", "page_obj": page, "qs": qs_str,
                                                        "statuses": Invoice.STATUS,
                                                        "pending": svc.invoiceable_lines().values("stop").distinct().count()})


@role_required(*ACC)
@business_action
def invoice_new(request):
    if request.method == "POST":
        cust = get_object_or_404(Customer, pk=request.POST["customer"])
        inv = svc.create_invoice(cust, request.POST.getlist("dl"))
        messages.success(request, f"Đã lập hóa đơn nháp {inv.code}.")
        return redirect(f"/app/accounting/invoices/{inv.pk}")
    groups = {}
    for dl in svc.invoiceable_lines().order_by("stop__so__customer__name", "stop__delivered_at"):
        c = dl.stop.so.customer
        g = groups.setdefault(c.pk, {"customer": c, "lines": [], "total": Decimal(0)})
        g["lines"].append(dl)
        g["total"] += dl.qty_delivered * dl.so_line.unit_price
    return render(request, "accounting/invoice_new.html", {"title": "Lập hóa đơn từ điểm giao", "groups": groups.values()})


@role_required(*ACC)
@business_action
def invoice_detail(request, pk):
    inv = get_object_or_404(Invoice.objects.select_related("customer"), pk=pk)
    if request.method == "POST":
        op = request.POST.get("op")
        if op == "post":
            svc.post_invoice(inv)
            messages.success(request, "Đã ghi sổ – phát sinh công nợ phải thu.")
        elif op == "delete" and inv.status == "DRAFT":
            for l in inv.lines.select_related("delivery_line__so_line"):
                if l.delivery_line:
                    sl = l.delivery_line.so_line
                    sl.invoiced_qty -= l.qty
                    sl.save(update_fields=["invoiced_qty"])
            inv.delete()
            return redirect("/app/accounting/invoices")
        elif op == "cancel":
            if inv.paid_amount > 0:
                raise BusinessError("Hóa đơn đã có thu tiền – không hủy được.")
            if not request.POST.get("reason"):
                raise BusinessError("Phải nhập lý do hủy.")
            for l in inv.lines.select_related("delivery_line__so_line"):
                if l.delivery_line:
                    sl = l.delivery_line.so_line
                    sl.invoiced_qty -= l.qty
                    sl.save(update_fields=["invoiced_qty"])
            inv.lines.update(delivery_line=None)
            inv.status, inv.cancel_reason = "CANCELLED", request.POST["reason"]
            inv.save()
        return redirect(request.path)
    if request.GET.get("pdf"):
        return pdf_response(request, "docs/invoice.html", {"inv": inv}, inv.code)
    return render(request, "accounting/invoice_detail.html", {
        "title": inv.code, "inv": inv, "lines": inv.lines.select_related("product", "delivery_line__stop__site", "delivery_line__stop__trip"),
        "allocs": inv.allocations.select_related("payment")})


# ------------------------------------------------------------------ Hóa đơn mua & đối chiếu 3 bên (MH-42)
@role_required(*ACC)
def bills(request):
    qs = Bill.objects.select_related("supplier", "carrier")
    st = request.GET.get("status")
    if st == "MATCHED":
        qs = qs.filter(status__in=["MATCHED", "ACCEPTED"])
    elif st:
        qs = qs.filter(status=st)
    counts = dict(Bill.objects.values_list("status").annotate(n=Count("id")))
    page, qs_str = paginate(request, qs)
    return render(request, "accounting/bills.html", {"title": "Hóa đơn mua & đối chiếu 3 bên", "page_obj": page,
                                                     "qs": qs_str, "statuses": Bill.STATUS, "counts": counts})


@role_required(*ACC)
@business_action
def bill_new(request):
    kind = request.GET.get("type", "GOODS")
    if request.method == "POST":
        bill_date = _date(request.POST.get("bill_date"))
        if request.POST.get("bill_type") == "FREIGHT":
            carrier = get_object_or_404(Carrier, pk=request.POST["carrier"])
            pairs = [(InboundTrip.objects.get(pk=tid), _dec(request.POST.get(f"amt_{tid}")))
                     for tid in request.POST.getlist("trip")]
            if not pairs:
                raise BusinessError("Chọn ít nhất 1 chuyến.")
            bill = svc.create_freight_bill(carrier, request.POST.get("vendor_invoice_no", ""), bill_date, pairs)
        else:
            sup = get_object_or_404(Supplier, pk=request.POST["supplier"])
            lines = []
            for lid in request.POST.getlist("pol"):
                q = _dec(request.POST.get(f"q_{lid}"))
                if q > 0:
                    lines.append((PurchaseOrderLine.objects.get(pk=lid, po__supplier=sup), q, _dec(request.POST.get(f"p_{lid}"))))
            if not lines:
                raise BusinessError("Chưa nhập dòng hóa đơn.")
            bill = svc.create_goods_bill(sup, request.POST.get("vendor_invoice_no", ""), bill_date, lines)
        messages.success(request, f"Đã ghi nhận {bill.code} – kết quả đối chiếu: {bill.get_status_display()}.")
        return redirect(f"/app/accounting/bills/{bill.pk}")
    sup_id = request.GET.get("supplier")
    car_id = request.GET.get("carrier")
    pols = PurchaseOrderLine.objects.filter(po__supplier_id=sup_id, po__status__in=["SENT", "PARTIAL", "RECEIVED"]) \
        .select_related("po", "product__base_uom") if sup_id else []
    pols = [l for l in pols if l.qty_received - l.qty_billed > 0 or l.qty - l.qty_billed > 0]
    # loại None: NOT IN (..., NULL) trong SQL sẽ không trả về dòng nào
    billed_trip_ids = set(Bill.objects.exclude(status="CANCELLED").values_list("lines__inbound_trip_id", flat=True)) - {None}
    trips = InboundTrip.objects.filter(carrier_id=car_id).exclude(status="CANCELLED").exclude(pk__in=billed_trip_ids) \
        .select_related("vehicle", "route") if car_id else []
    return render(request, "accounting/bill_new.html", {
        "title": "Ghi nhận hóa đơn mua", "kind": kind, "suppliers": Supplier.objects.filter(is_active=True),
        "carriers": Carrier.objects.filter(is_active=True), "pols": pols, "trips": trips, "today": timezone.localdate()})


@role_required(*ACC)
@business_action
def bill_detail(request, pk):
    bill = get_object_or_404(Bill.objects.select_related("supplier", "carrier"), pk=pk)
    if request.method == "POST":
        op = request.POST.get("op")
        if op == "rematch":
            svc.three_way_match(bill)
        elif op == "accept":
            if request.user.role not in ("DIRECTOR", "ADMIN"):
                raise BusinessError("Chỉ Director chấp nhận lệch đối chiếu.")
            svc.accept_mismatch(bill, request.user)
            messages.success(request, "Đã chấp nhận chênh lệch – kế toán có thể ghi sổ.")
        elif op == "post":
            svc.post_bill(bill)
            messages.success(request, "Đã ghi sổ – phát sinh công nợ phải trả.")
        elif op == "cancel":
            if bill.status in ("POSTED", "PARTIAL", "PAID"):
                raise BusinessError("Hóa đơn đã ghi sổ – không hủy được.")
            if not request.POST.get("reason"):
                raise BusinessError("Phải nhập lý do hủy.")
            bill.status, bill.cancel_reason = "CANCELLED", request.POST["reason"]
            bill.save()
        return redirect(request.path)
    rows = []
    for bl in bill.lines.select_related("po_line__po", "po_line__product", "inbound_trip"):
        r = {"bl": bl}
        if bl.po_line_id:
            pol = bl.po_line
            r.update(po_qty=pol.qty, po_price=pol.unit_price, grn_qty=pol.qty_received,
                     open_qty=pol.qty_received - pol.qty_billed + (bl.qty if bill.status == "POSTED" else 0))
        elif bl.inbound_trip_id:
            r.update(po_price=bl.inbound_trip.freight_amount, grn_qty=1 if bl.inbound_trip.status == "RECEIVED" else 0)
        rows.append(r)
    return render(request, "accounting/bill_detail.html", {"title": bill.code, "bill": bill, "rows": rows,
                                                           "allocs": bill.allocations.select_related("payment")})


# ------------------------------------------------------------------ Thu / Chi
@role_required(*ACC)
@business_action
def payments(request):
    if request.method == "POST":
        direction = request.POST["direction"]
        ptype = request.POST["partner_type"]
        model = {"customer": Customer, "supplier": Supplier, "carrier": Carrier}[ptype]
        partner = get_object_or_404(model, pk=request.POST.get(ptype))
        pay = svc.record_payment(direction, partner, _dec(request.POST["amount"]), _date(request.POST.get("paid_at")),
                                 request.POST.get("method", "BANK"), request.POST.get("note", ""), request.user)
        n = pay.allocations.count()
        messages.success(request, f"Đã lập {pay.code}, phân bổ FIFO vào {n} chứng từ.")
        return redirect("/app/accounting/payments")
    qs = Payment.objects.select_related("customer", "supplier", "carrier").prefetch_related("allocations__invoice", "allocations__bill")
    if request.GET.get("direction"):
        qs = qs.filter(direction=request.GET["direction"])
    page, qs_str = paginate(request, qs)
    return render(request, "accounting/payments.html", {
        "title": "Phiếu thu / chi", "page_obj": page, "qs": qs_str, "today": timezone.localdate(),
        "customers": Customer.objects.filter(is_active=True, customer_type="B2B"),
        "suppliers": Supplier.objects.filter(is_active=True), "carriers": Carrier.objects.filter(is_active=True)})


# ------------------------------------------------------------------ Công nợ & tuổi nợ (MH-44)
@role_required(*ACC)
def debts(request):
    kind = request.GET.get("kind", "AR")
    rows, totals = svc.aging(kind)
    if request.GET.get("export") == "xlsx":
        head = ["Đối tác", "Số CT", "Trong hạn", "1–30", "31–60", "61–90", ">90", "Tổng"]
        data = [[str(r["partner"]), r["docs"], r["current"], r["d30"], r["d60"], r["d90"], r["over"], r["total"]] for r in rows]
        return excel_response(f"tuoi_no_{kind}", head, data, "Tuổi nợ")
    return render(request, "accounting/debts.html", {"title": "Công nợ & tuổi nợ", "rows": rows, "totals": totals,
                                                     "kind": kind, "buckets": svc.BUCKETS})
