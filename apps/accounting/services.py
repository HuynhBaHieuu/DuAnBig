"""Kế toán: INV từ điểm giao B2B, BILL + đối chiếu 3 bên PO–GRN–Bill, thu/chi FIFO, tuổi nợ."""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.services import BusinessError, audit, next_doc_no, param, raise_alert, resolve_alerts
from apps.sales.models import DeliveryLine

from .models import Bill, BillLine, Invoice, InvoiceLine, Payment, PaymentAllocation

D0 = Decimal(0)


def _r(x):
    return Decimal(x).quantize(Decimal(1), rounding=ROUND_HALF_UP)


# ------------------------------------------------------------------ Hóa đơn bán
def invoiceable_lines(customer=None):
    qs = DeliveryLine.objects.filter(stop__status__in=["DELIVERED", "PARTIAL"], stop__so__order_type="B2B",
                                     qty_delivered__gt=0, invoice_line__isnull=True) \
        .select_related("stop__so__customer", "stop__trip", "so_line__product")
    if customer:
        qs = qs.filter(stop__so__customer=customer)
    return qs


@transaction.atomic
def create_invoice(customer, delivery_line_ids):
    lines = list(invoiceable_lines(customer).filter(pk__in=delivery_line_ids))
    if not lines:
        raise BusinessError("Không có dòng giao hàng hợp lệ để lập hóa đơn.")
    today = timezone.localdate()
    vat = Decimal(param("VAT_RATE"))
    inv = Invoice.objects.create(code=next_doc_no("INV"), customer=customer, invoice_date=today,
                                 due_date=today + timedelta(days=customer.payment_terms_days), vat_rate=vat)
    subtotal = D0
    for dl in lines:
        amt = _r(dl.qty_delivered * dl.so_line.unit_price)
        InvoiceLine.objects.create(invoice=inv, delivery_line=dl, product=dl.so_line.product, qty=dl.qty_delivered,
                                   unit_price=dl.so_line.unit_price, amount=amt)
        dl.so_line.invoiced_qty += dl.qty_delivered
        dl.so_line.save(update_fields=["invoiced_qty"])
        subtotal += amt
    inv.subtotal = subtotal
    inv.vat_amount = _r(subtotal * vat / 100)
    inv.total = inv.subtotal + inv.vat_amount
    inv.save()
    return inv


def post_invoice(inv):
    if inv.status != "DRAFT":
        raise BusinessError("Hóa đơn đã ghi sổ.")
    inv.status = "POSTED"
    inv.save()
    audit("POST", inv)


# ------------------------------------------------------------------ Hóa đơn mua & đối chiếu 3 bên
def three_way_match(bill):
    """PO (đơn giá) – GRN (SL thực nhận chưa lập bill) – Bill. Cước: so với cước chuyến đã nhận hàng."""
    all_ok = True
    for bl in bill.lines.select_related("po_line", "inbound_trip"):
        status = "MATCH"
        if bl.po_line_id:
            pol = bl.po_line
            unbilled_grn = pol.qty_received - pol.qty_billed
            if pol.qty_received <= 0:
                status = "NO_GRN"
            elif bl.qty > unbilled_grn:
                status = "QTY_DIFF"
            elif bl.unit_price != pol.unit_price:
                status = "PRICE_DIFF"
        elif bl.inbound_trip_id:
            trip = bl.inbound_trip
            if trip.status != "RECEIVED":
                status = "NO_GRN"
            elif bl.amount != trip.freight_amount:
                status = "PRICE_DIFF"
        bl.match_status = status
        bl.save(update_fields=["match_status"])
        all_ok &= status == "MATCH"
    bill.status = "MATCHED" if all_ok else "MISMATCH"
    bill.save(update_fields=["status", "updated_at"])
    if not all_ok:
        raise_alert("BILL_MISMATCH", f"Hóa đơn mua {bill.code} lệch đối chiếu 3 bên", ref_type="bill",
                    ref_id=bill.pk, url=f"/app/accounting/bills/{bill.pk}", target_role="DIRECTOR")
    return all_ok


def recalc_bill(bill):
    bill.subtotal = sum((l.amount for l in bill.lines.all()), D0)
    bill.vat_amount = _r(bill.subtotal * Decimal(param("VAT_RATE")) / 100)
    bill.total = bill.subtotal + bill.vat_amount
    bill.save()


@transaction.atomic
def create_goods_bill(supplier, vendor_invoice_no, bill_date, lines):
    """lines: [(po_line, qty, unit_price)]"""
    bill = Bill.objects.create(code=next_doc_no("BILL"), bill_type="GOODS", supplier=supplier,
                               vendor_invoice_no=vendor_invoice_no, bill_date=bill_date,
                               due_date=bill_date + timedelta(days=supplier.payment_terms_days))
    for pol, q, price in lines:
        BillLine.objects.create(bill=bill, po_line=pol, description=pol.product.name, qty=q, unit_price=price,
                                amount=_r(Decimal(q) * Decimal(price)))
    recalc_bill(bill)
    three_way_match(bill)
    return bill


@transaction.atomic
def create_freight_bill(carrier, vendor_invoice_no, bill_date, trips_amounts):
    bill = Bill.objects.create(code=next_doc_no("BILL"), bill_type="FREIGHT", carrier=carrier,
                               vendor_invoice_no=vendor_invoice_no, bill_date=bill_date,
                               due_date=bill_date + timedelta(days=30))
    for trip, amount in trips_amounts:
        BillLine.objects.create(bill=bill, inbound_trip=trip, description=f"Cước chuyến {trip.code}", qty=1,
                                unit_price=amount, amount=amount)
    recalc_bill(bill)
    three_way_match(bill)
    return bill


def accept_mismatch(bill, user):
    if bill.status != "MISMATCH":
        raise BusinessError("Hóa đơn không ở trạng thái lệch.")
    bill.status, bill.accepted_by = "ACCEPTED", user
    bill.save()
    resolve_alerts("BILL_MISMATCH", "bill", bill.pk)
    audit("APPROVE", bill, {"note": "Chấp nhận lệch đối chiếu 3 bên"})


@transaction.atomic
def post_bill(bill):
    if bill.status == "MISMATCH":
        raise BusinessError("Hóa đơn lệch đối chiếu 3 bên – cần Director chấp nhận trước khi ghi sổ.")
    if bill.status not in ("MATCHED", "ACCEPTED"):
        raise BusinessError("Hóa đơn chưa đối chiếu.")
    for bl in bill.lines.select_related("po_line"):
        if bl.po_line_id:
            bl.po_line.qty_billed += bl.qty
            bl.po_line.save(update_fields=["qty_billed"])
    bill.status = "POSTED"
    bill.save()
    audit("POST", bill)


# ------------------------------------------------------------------ Thu / chi – phân bổ FIFO
@transaction.atomic
def record_payment(direction, partner, amount, paid_at, method="BANK", note="", user=None):
    amount = Decimal(amount)
    if amount <= 0:
        raise BusinessError("Số tiền phải lớn hơn 0.")
    kind = partner._meta.model_name
    pay = Payment.objects.create(code=next_doc_no("PT" if direction == "IN" else "PC"), direction=direction,
                                 amount=amount, paid_at=paid_at, method=method, note=note, created_by=user,
                                 **{kind: partner})
    remain = amount
    if direction == "IN":
        docs = Invoice.objects.select_for_update().filter(customer=partner, invoice_type="SALE",
                                                          status__in=["POSTED", "PARTIAL"]).order_by("due_date", "id")
    else:
        docs = Bill.objects.select_for_update().filter(**{kind: partner}, status__in=["POSTED", "PARTIAL"]) \
            .order_by("due_date", "id")
    for doc in docs:
        if remain <= 0:
            break
        open_amt = doc.total - doc.paid_amount
        take = min(open_amt, remain)
        PaymentAllocation.objects.create(payment=pay, amount=take,
                                         **({"invoice": doc} if direction == "IN" else {"bill": doc}))
        doc.paid_amount += take
        doc.status = "PAID" if doc.paid_amount >= doc.total else "PARTIAL"
        doc.save(update_fields=["paid_amount", "status", "updated_at"])
        remain -= take
    return pay


# ------------------------------------------------------------------ Tuổi nợ
BUCKETS = [("current", "Trong hạn"), ("d30", "1–30 ngày"), ("d60", "31–60 ngày"), ("d90", "61–90 ngày"),
           ("over", "> 90 ngày")]


def _bucket(days_over):
    if days_over <= 0:
        return "current"
    if days_over <= 30:
        return "d30"
    if days_over <= 60:
        return "d60"
    if days_over <= 90:
        return "d90"
    return "over"


def aging(kind="AR"):
    today = timezone.localdate()
    rows = {}
    if kind == "AR":
        docs = Invoice.objects.filter(status__in=["POSTED", "PARTIAL"]).select_related("customer")
        get_partner = lambda d: d.customer  # noqa: E731
    else:
        docs = Bill.objects.filter(status__in=["POSTED", "PARTIAL"]).select_related("supplier", "carrier")
        get_partner = lambda d: d.partner  # noqa: E731
    for d in docs:
        p = get_partner(d)
        key = (p._meta.model_name, p.pk)
        r = rows.setdefault(key, {"partner": p, "total": D0, **{b: D0 for b, _ in BUCKETS}, "docs": 0})
        bal = d.balance
        r[_bucket((today - d.due_date).days)] += bal
        r["total"] += bal
        r["docs"] += 1
    data = sorted(rows.values(), key=lambda r: -r["total"])
    totals = {b: sum((r[b] for r in data), D0) for b, _ in BUCKETS}
    totals["total"] = sum((r["total"] for r in data), D0)
    return data, totals
