"""Sửa dữ liệu cũ theo test sheet "Gà" (STT 23, 29, 47, 59)."""
from decimal import Decimal

from django.db import migrations


def forwards(apps, schema_editor):
    Claim = apps.get_model("purchasing", "Claim")
    GoodsReceipt = apps.get_model("purchasing", "GoodsReceipt")
    PurchaseOrder = apps.get_model("purchasing", "PurchaseOrder")
    PRLine = apps.get_model("purchasing", "PurchaseRequisitionLine")
    PR = apps.get_model("purchasing", "PurchaseRequisition")

    # STT 59: trạng thái khiếu nại mới
    Claim.objects.filter(status="SETTLED").update(status="COMPENSATED")

    # STT 23: GRN lead time ngắn bất thường (chuyến test, vd. GRN-0009 = 0,01 ngày) -> loại khỏi thống kê
    for gr in GoodsReceipt.objects.filter(lead_time_days__lt=Decimal("0.1"), exclude_from_stats=False):
        gr.exclude_from_stats = True
        gr.exclude_reason = f"Tự động: lead time {gr.lead_time_days} ngày < tối thiểu 0,1 ngày (dữ liệu test)"
        gr.save(update_fields=["exclude_from_stats", "exclude_reason"])

    # STT 29: PO tự duyệt dưới ngưỡng (có giờ duyệt, không có người duyệt)
    PurchaseOrder.objects.filter(approved_at__isnull=False, approved_by__isnull=True).update(auto_approved=True)

    # STT 47: dòng PR đã gắn PO -> ghi merged_into; PR kẹt "Đã lập PO" vì PO đã hủy -> trả về "Đã gửi"
    for l in PRLine.objects.filter(po_line__isnull=False, merged_into__isnull=True):
        l.merged_into_id = l.po_line_id
        l.save(update_fields=["merged_into"])
    for pr in PR.objects.filter(status="CONVERTED"):
        lines = list(PRLine.objects.filter(pr=pr).select_related("po_line__po"))
        linked = [l for l in lines if l.po_line_id]
        if linked and all(l.po_line.po.status == "CANCELLED" for l in linked):
            for l in linked:
                l.po_line_id = None
                l.merged_into_id = None
                l.save(update_fields=["po_line", "merged_into"])
            pr.status = "SUBMITTED"
            pr.note = ("PO đã hủy – PR trả về chờ lập PO (sửa dữ liệu). " + (pr.note or ""))[:255]
            pr.save(update_fields=["status", "note"])


class Migration(migrations.Migration):
    dependencies = [("purchasing", "0006_ga_sheet_2")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
