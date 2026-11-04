"""Luồng B & C: SO -> kiểm tra tín dụng/giá sàn -> ATP & giữ hàng -> phân bổ -> giao Outbound -> POD."""
import base64
import binascii
import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from apps.core.services import BusinessError, audit, next_doc_no, param, raise_alert, resolve_alerts
from apps.inventory.services import default_storage_location, fifo_quants, post_stock_move, stock_summary
from apps.masterdata.models import Location, PriceListLine, Product

from .models import (Allocation, AllocationLine, DeliveryLine, DeliveryStop, DeliveryTrip, GoodsIssue,
                     GoodsIssueLine, Picking, PickingLine, SalesOrder, SalesOrderLine, SalesReturn, SalesReturnLine,
                     StockReservation)

D0 = Decimal(0)


# ------------------------------------------------------------------ Giá & tín dụng
def price_line(customer, product):
    if customer.price_list_id:
        return PriceListLine.objects.filter(price_list=customer.price_list, product=product).first()
    return None


def default_price(customer, product):
    if customer.customer_type == "INTERNAL":
        return product.avg_cost or product.standard_cost  # đơn nội bộ dùng giá vốn
    pl = price_line(customer, product)
    return pl.price if pl else product.standard_cost


def customer_debt(customer):
    """(tổng nợ phải thu, nợ quá hạn > N ngày)"""
    from apps.accounting.models import Invoice

    today = timezone.localdate()
    open_inv = Invoice.objects.filter(customer=customer, status__in=["POSTED", "PARTIAL"])
    total = sum((i.balance for i in open_inv), D0)
    limit_day = today - timedelta(days=int(param("OVERDUE_DAYS_BLOCK")))
    overdue = sum((i.balance for i in open_inv if i.due_date < limit_day and i.invoice_type == "SALE"), D0)
    return total, overdue


def approval_reasons(so):
    """Lý do cần Director duyệt (chỉ áp dụng B2B)."""
    if so.order_type != "B2B":
        return []
    reasons = []
    debt, overdue = customer_debt(so.customer)
    if so.customer.credit_limit and debt + so.total_amount > so.customer.credit_limit:
        reasons.append(f"Vượt hạn mức: nợ {debt:,.0f} + đơn {so.total_amount:,.0f} > hạn mức "
                       f"{so.customer.credit_limit:,.0f}")
    if overdue > 0:
        reasons.append(f"Có nợ quá hạn > {param('OVERDUE_DAYS_BLOCK'):g} ngày: {overdue:,.0f} ₫")
    for line in so.lines.select_related("product"):
        pl = price_line(so.customer, line.product)
        if pl and pl.floor_price and line.unit_price < pl.floor_price:
            reasons.append(f"{line.product.code} bán {line.unit_price:,.0f} < giá sàn {pl.floor_price:,.0f}")
    return reasons


@transaction.atomic
def submit_so(so, user):
    if so.status != "DRAFT":
        raise BusinessError("Chỉ đơn nháp mới gửi được.")
    if not so.lines.exists():
        raise BusinessError("Đơn chưa có dòng hàng.")
    so.recalc_total()
    so.approval_reasons = approval_reasons(so)
    if so.approval_reasons:
        so.status = "PENDING_APPROVAL"
        so.save()
        raise_alert("SO_APPROVAL", f"SO {so.code} chờ duyệt", "; ".join(so.approval_reasons), ref_type="so",
                    ref_id=so.pk, url=f"/app/sales/orders/{so.pk}", target_role="DIRECTOR")
        return False
    confirm_so(so)
    return True


@transaction.atomic
def approve_so(so, user, approve=True, reason=""):
    if so.status != "PENDING_APPROVAL":
        raise BusinessError("Đơn không chờ duyệt.")
    resolve_alerts("SO_APPROVAL", "so", so.pk)
    if not approve:
        so.status, so.cancel_reason = "CANCELLED", reason or "Director từ chối"
        so.save()
        audit("REJECT", so)
        return
    so.approved_by, so.approved_at = user, timezone.now()
    so.save()
    audit("APPROVE", so)
    confirm_so(so)


def confirm_so(so):
    so.status = "CONFIRMED"
    so.save()
    for line in so.lines.select_related("product"):
        reserve_line(line)


# ------------------------------------------------------------------ ATP & giữ hàng
def available_qty(product):
    return stock_summary([product])[product.pk]["available"]


@transaction.atomic
def reserve_line(line):
    """Giữ hàng phần còn thiếu của dòng.

    Đủ hàng -> giữ đủ. Thiếu hàng -> giữ ngay toàn bộ lượng khả dụng, phần chênh lệch là backorder
    (tự giữ tiếp khi có hàng về). Nếu đang có đơn khác chờ phân bổ cùng sản phẩm -> đưa vào đề xuất
    phân bổ cho Director để chia công bằng.
    """
    Product.objects.select_for_update().get(pk=line.product_id)  # tuần tự hóa giữ hàng theo sản phẩm
    need = line.qty - line.reserved_qty - line.delivered_qty
    if need <= 0:
        line.atp_status = "OK"
        line.save(update_fields=["atp_status"])
        return
    competing = SalesOrderLine.objects.filter(product=line.product, atp_status="SHORT",
                                              so__status__in=["CONFIRMED", "PARTIAL"]).exclude(pk=line.pk).exists()
    if competing:
        line.atp_status = "SHORT"
        line.save(update_fields=["atp_status"])
        propose_allocation(line.product)
        return
    take = max(D0, min(available_qty(line.product), need))
    _reserve(line, take)
    line.atp_status = "OK" if take >= need else "BACKORDER"
    line.save(update_fields=["atp_status"])


def _reserve(line, q):
    if q <= 0:
        return
    StockReservation.objects.create(so_line=line, product=line.product, qty=q)
    line.reserved_qty += q
    line.save(update_fields=["reserved_qty"])


def priority_key(line, rule):
    site = line.so.site
    far = timezone.localdate() + timedelta(days=3650)
    fifo = (line.so.order_date, line.so_id)
    if rule == "PENALTY":
        return (site.penalty_date or far, site.priority, *fifo)
    if rule == "PRIORITY":
        return (site.priority, site.penalty_date or far, *fifo)
    return fifo


@transaction.atomic
def propose_allocation(product, rule="PENALTY"):
    """Gom các dòng đang thiếu của 1 sản phẩm, chia lượng khả dụng theo quy tắc ưu tiên."""
    for old in Allocation.objects.filter(product=product, status="PROPOSED"):  # đề xuất cũ chưa duyệt -> làm lại
        resolve_alerts("ALLOCATION", "allocation", old.pk)
        old.delete()
    lines = list(SalesOrderLine.objects.filter(product=product, atp_status="SHORT",
                                               so__status__in=["CONFIRMED", "PARTIAL"])
                 .select_related("so__site"))
    if not lines:
        return None
    avail = max(D0, available_qty(product))
    lines.sort(key=lambda l: priority_key(l, rule))
    demand = sum((l.qty - l.reserved_qty - l.delivered_qty for l in lines), D0)
    alloc = Allocation.objects.create(code=next_doc_no("AL"), product=product, priority_rule=rule,
                                      available_qty=avail, demand_qty=demand)
    remain = avail
    for i, l in enumerate(lines, 1):
        d = l.qty - l.reserved_qty - l.delivered_qty
        p = min(d, remain)
        remain -= p
        AllocationLine.objects.create(allocation=alloc, so_line=l, demand_qty=d, proposed_qty=p, rank=i)
    raise_alert("ALLOCATION", f"Phân bổ thiếu hàng {product.code}", f"Khả dụng {avail:g} / nhu cầu {demand:g}",
                ref_type="allocation", ref_id=alloc.pk, url=f"/app/sales/allocations/{alloc.pk}",
                target_role="DIRECTOR")
    return alloc


@transaction.atomic
def approve_allocation(alloc, user, approved):
    """approved: {allocation_line_id: qty}. Phần thiếu -> backorder."""
    if alloc.status != "PROPOSED":
        raise BusinessError("Phân bổ đã được xử lý.")
    avail = max(D0, available_qty(alloc.product))
    total = sum((Decimal(v or 0) for v in approved.values()), D0)
    if total > avail:
        raise BusinessError(f"Tổng phân bổ {total:g} vượt lượng khả dụng hiện tại {avail:g}.")
    for al in alloc.lines.select_related("so_line"):
        q = Decimal(approved.get(al.pk, al.proposed_qty) or 0)
        q = min(q, al.demand_qty)
        al.approved_qty = q
        al.save(update_fields=["approved_qty"])
        line = al.so_line
        _reserve(line, q)
        line.atp_status = "OK" if q >= al.demand_qty else "BACKORDER"
        line.save(update_fields=["atp_status"])
    alloc.status, alloc.approved_by = "APPROVED", user
    alloc.save()
    resolve_alerts("ALLOCATION", "allocation", alloc.pk)
    audit("APPROVE", alloc)


def retry_backorders(product_id):
    """Khi có hàng về (GRN / trả hàng): thử giữ hàng lại cho các dòng backorder theo FIFO."""
    lines = SalesOrderLine.objects.filter(product_id=product_id, atp_status__in=["BACKORDER", "SHORT"],
                                          so__status__in=["CONFIRMED", "PARTIAL"]).select_related("so", "product")
    product = Product.objects.get(pk=product_id)
    pending = Allocation.objects.filter(product=product, status="PROPOSED").first()
    if pending:
        # đang chờ Director phân bổ -> tính lại đề xuất theo lượng khả dụng mới
        propose_allocation(product, pending.priority_rule)
        return
    for line in sorted(lines, key=lambda l: (l.so.order_date, l.so_id)):
        need = line.qty - line.reserved_qty - line.delivered_qty
        avail = available_qty(product)
        if avail <= 0:
            break
        q = min(need, avail)
        _reserve(line, q)
        line.atp_status = "OK" if q >= need else "BACKORDER"
        line.save(update_fields=["atp_status"])


# ------------------------------------------------------------------ Kế hoạch giao (kéo–thả)
def orders_waiting_delivery():
    lines = SalesOrderLine.objects.filter(so__status__in=["CONFIRMED", "PARTIAL"], reserved_qty__gt=F("planned_qty")) \
        .values_list("so_id", flat=True)
    return SalesOrder.objects.filter(pk__in=set(lines)).select_related("customer", "site").order_by("requested_date")


@transaction.atomic
def add_order_to_trip(trip, so):
    if trip.status != "PLANNED":
        raise BusinessError("Chỉ thêm đơn vào chuyến chưa phát lệnh soạn hàng.")
    lines = [l for l in so.lines.select_related("product") if l.qty_to_plan > 0]
    if not lines:
        raise BusinessError("Đơn không còn hàng đã giữ để xếp chuyến.")
    add_kg = sum((l.qty_to_plan * l.product.unit_weight_kg for l in lines), D0)
    if trip.total_ton + add_kg / 1000 > trip.vehicle.capacity_ton:
        raise BusinessError(f"Vượt tải trọng xe {trip.vehicle.plate}: "
                            f"{trip.total_ton + add_kg / 1000:.2f}/{trip.vehicle.capacity_ton} tấn.")
    stop = trip.stops.filter(so=so).first() or DeliveryStop.objects.create(
        trip=trip, so=so, site=so.site, seq=trip.stops.count() + 1)
    for l in lines:
        q = l.qty_to_plan
        DeliveryLine.objects.create(stop=stop, so_line=l, qty_planned=q)
        l.planned_qty += q
        l.save(update_fields=["planned_qty"])
    return stop


@transaction.atomic
def remove_stop(stop):
    if stop.trip.status != "PLANNED":
        raise BusinessError("Chuyến đã phát lệnh soạn, không gỡ đơn được.")
    for dl in stop.lines.select_related("so_line"):
        dl.so_line.planned_qty -= dl.qty_planned
        dl.so_line.save(update_fields=["planned_qty"])
    trip = stop.trip
    stop.delete()
    for i, s in enumerate(trip.stops.order_by("seq"), 1):
        s.seq = i
        s.save(update_fields=["seq"])


def create_trip(vehicle, driver, planned_date):
    if not driver.license_valid:
        raise BusinessError(f"Bằng lái của {driver.name} đã hết hạn.")
    return DeliveryTrip.objects.create(code=next_doc_no("DL"), vehicle=vehicle, driver=driver,
                                       planned_date=planned_date)


# ------------------------------------------------------------------ Soạn hàng FIFO -> Xuất kho
@transaction.atomic
def release_picking(trip, user):
    if trip.status != "PLANNED":
        raise BusinessError("Chuyến đã phát lệnh soạn.")
    if not trip.stops.exists():
        raise BusinessError("Chuyến chưa có điểm giao.")
    pk = Picking.objects.create(code=next_doc_no("PK"), trip=trip)
    for stop in trip.stops.all():
        for dl in stop.lines.select_related("so_line__product"):
            plan, remain = fifo_quants(dl.so_line.product, dl.qty_planned)
            if remain > 0:
                raise BusinessError(f"Tồn kho vật lý không đủ cho {dl.so_line.product.code} (thiếu {remain:g}).")
            for loc, q in plan:
                PickingLine.objects.create(picking=pk, so_line=dl.so_line, product=dl.so_line.product, location=loc,
                                           qty_to_pick=q)
    trip.status = "PICKING"
    trip.save()
    return pk


@transaction.atomic
def confirm_picking(picking, user, picked):
    """picked: {picking_line_id: qty}. Tạo phiếu xuất GIN nháp."""
    if picking.status == "DONE":
        raise BusinessError("Lệnh soạn đã hoàn tất.")
    for pl in picking.lines.all():
        q = Decimal(picked.get(pl.pk, pl.qty_to_pick) or 0)
        if q > pl.qty_to_pick:
            raise BusinessError("Không soạn vượt số lượng yêu cầu.")
        pl.qty_picked = q
        pl.save(update_fields=["qty_picked"])
    picking.status, picking.picker = "DONE", user
    picking.save()
    gi = GoodsIssue.objects.create(code=next_doc_no("GIN"), trip=picking.trip)
    for pl in picking.lines.filter(qty_picked__gt=0):
        GoodsIssueLine.objects.create(gi=gi, so_line=pl.so_line, product=pl.product, location=pl.location,
                                      qty=pl.qty_picked, unit_cost=pl.product.avg_cost)
    picking.trip.status = "READY"
    picking.trip.save()
    return gi


@transaction.atomic
def post_goods_issue(gi, user):
    if gi.status != "DRAFT":
        raise BusinessError("Phiếu xuất đã ghi sổ.")
    now = timezone.now()
    for line in gi.lines.select_related("so_line__so", "product", "location"):
        mt = "OUT_INTERNAL" if line.so_line.so.order_type == "INTERNAL" else "OUT_SALE"
        post_stock_move(line.product, line.location, -line.qty, mt, "GIN", gi.pk, note=gi.code, at=now)
        line.unit_cost = line.product.avg_cost
        line.save(update_fields=["unit_cost"])
        _consume_reservation(line.so_line, line.qty)
    # soạn thiếu -> giảm lượng kế hoạch của điểm giao tương ứng
    for stop in gi.trip.stops.all():
        for dl in stop.lines.select_related("so_line"):
            issued = gi.lines.filter(so_line=dl.so_line).aggregate(s=Sum("qty"))["s"] or D0
            if issued < dl.qty_planned:
                diff = dl.qty_planned - issued
                dl.qty_planned = issued
                dl.save(update_fields=["qty_planned"])
                dl.so_line.planned_qty -= diff
                dl.so_line.save(update_fields=["planned_qty"])
    gi.status, gi.posted_at = "POSTED", now
    gi.save()
    trip = gi.trip
    trip.status, trip.departed_at = "IN_TRANSIT", now
    trip.save()
    audit("POST", gi)


def _consume_reservation(line, qty):
    remain = Decimal(qty)
    for r in line.reservations.filter(status="ACTIVE").order_by("created_at"):
        if remain <= 0:
            break
        take = min(r.qty, remain)
        if take == r.qty:
            r.status = "CONSUMED"
            r.save(update_fields=["status"])
        else:
            r.qty -= take
            r.save(update_fields=["qty"])
            StockReservation.objects.create(so_line=line, product=line.product, qty=take, status="CONSUMED")
        remain -= take


# ------------------------------------------------------------------ POD (tài xế)
def _decode_signature(data_url):
    """data:image/png;base64,... -> ContentFile. Dữ liệu hỏng -> lỗi nghiệp vụ thay vì lỗi 500."""
    if not data_url or "," not in data_url:
        return None
    header, b64 = data_url.split(",", 1)
    if not header.startswith("data:image/") or ";base64" not in header:
        raise BusinessError("Chữ ký không hợp lệ – vui lòng ký lại.")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise BusinessError("Chữ ký không hợp lệ – vui lòng ký lại.")
    if len(raw) < 100:
        return None
    return ContentFile(raw, name=f"sig_{uuid.uuid4().hex[:10]}.png")


def check_image(f, label="Ảnh"):
    """Chặn file không phải ảnh / quá lớn trước khi lưu."""
    if not f:
        return
    if not (getattr(f, "content_type", "") or "").startswith("image/"):
        raise BusinessError(f"{label} phải là file hình ảnh.")
    if f.size > 15 * 1024 * 1024:
        raise BusinessError(f"{label} quá lớn (tối đa 15 MB).")


@transaction.atomic
def confirm_pod(stop, user, delivered, receiver_name, photo=None, signature_data=None, note="", failed=False):
    if stop.status != "PENDING":
        raise BusinessError("Điểm giao đã được xác nhận.")
    if stop.trip.status != "IN_TRANSIT":
        raise BusinessError("Chuyến chưa xuất kho.")
    sig = _decode_signature(signature_data)
    check_image(photo, "Ảnh giao hàng")
    if not failed and (not photo or not sig):
        raise BusinessError("POD bắt buộc có ảnh giao hàng và chữ ký người nhận.")
    total_plan = total_deliv = D0
    returns = []
    for dl in stop.lines.select_related("so_line__product"):
        q = D0 if failed else Decimal(delivered.get(dl.pk, dl.qty_planned) or 0)
        if q > dl.qty_planned:
            raise BusinessError("Không giao vượt số lượng trên xe.")
        dl.qty_delivered = q
        dl.save(update_fields=["qty_delivered"])
        sl = dl.so_line
        sl.delivered_qty += q
        if q < dl.qty_planned:  # phần không giao -> chờ nhập lại kho, cho phép giữ hàng lại
            diff = dl.qty_planned - q
            sl.planned_qty -= diff
            sl.reserved_qty -= diff
            sl.atp_status = "BACKORDER"
            returns.append((sl, diff))
        sl.save()
        total_plan += dl.qty_planned
        total_deliv += q
    stop.status = "FAILED" if total_deliv == 0 else ("DELIVERED" if total_deliv >= total_plan else "PARTIAL")
    stop.delivered_at, stop.receiver_name, stop.note = timezone.now(), receiver_name, note
    if photo:
        stop.pod_photo = photo
    if sig:
        stop.pod_signature = sig
    stop.save()

    if returns:
        ret = SalesReturn.objects.create(code=next_doc_no("RT"), so=stop.so, stop=stop,
                                         reason=note or ("Giao thất bại" if failed else "Khách nhận thiếu"))
        wh_loc = None
        for sl, q in returns:
            gi_line = GoodsIssueLine.objects.filter(gi__trip=stop.trip, so_line=sl).first()
            wh_loc = gi_line.location if gi_line else Location.objects.filter(loc_type="STORAGE").first()
            SalesReturnLine.objects.create(ret=ret, so_line=sl, product=sl.product, location=wh_loc, qty=q)
    _refresh_so_status(stop.so)
    trip = stop.trip
    if not trip.stops.filter(status="PENDING").exists():
        trip.status, trip.completed_at = "COMPLETED", timezone.now()
        trip.save()
    return stop


def _refresh_so_status(so):
    lines = list(so.lines.all())
    if all(l.delivered_qty >= l.qty for l in lines):
        so.status = "DELIVERED"
    elif any(l.delivered_qty > 0 for l in lines):
        so.status = "PARTIAL"
    so.save(update_fields=["status", "updated_at"])


@transaction.atomic
def post_sales_return(ret, user):
    if ret.status != "DRAFT":
        raise BusinessError("Phiếu trả đã ghi sổ.")
    for line in ret.lines.select_related("product", "location"):
        post_stock_move(line.product, line.location, line.qty, "IN_RETURN", "RT", ret.pk, note=ret.code)
    ret.status = "POSTED"
    ret.save()
    for pid in set(ret.lines.values_list("product_id", flat=True)):
        retry_backorders(pid)
    audit("POST", ret)


def cancel_so(so, reason):
    if so.status in ("DELIVERED", "CLOSED", "CANCELLED"):
        raise BusinessError("Không thể hủy đơn ở trạng thái này.")
    if any(l.planned_qty > 0 or l.delivered_qty > 0 for l in so.lines.all()):
        raise BusinessError("Đơn đã xếp chuyến / đã giao, không hủy được.")
    if not reason:
        raise BusinessError("Phải nhập lý do hủy.")
    with transaction.atomic():
        StockReservation.objects.filter(so_line__so=so, status="ACTIVE").update(status="RELEASED")
        so.lines.update(reserved_qty=0, atp_status="")
        so.status, so.cancel_reason = "CANCELLED", reason
        so.save()
        audit("CANCEL", so, {"reason": reason})
