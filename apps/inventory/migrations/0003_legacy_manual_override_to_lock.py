"""SS/ROP nhập tay kiểu cũ (ghi đè không khóa) -> chuyển sang cơ chế Khóa thủ công để giữ nguyên ngưỡng đang áp dụng."""
from django.db import migrations


def forwards(apps, schema_editor):
    ReplenishmentParam = apps.get_model("inventory", "ReplenishmentParam")
    for rp in ReplenishmentParam.objects.filter(manual_lock=False).exclude(manual_ss=None, manual_rop=None):
        rp.manual_ss = rp.manual_ss if rp.manual_ss is not None else rp.ss
        rp.manual_rop = rp.manual_rop if rp.manual_rop is not None else rp.rop
        rp.manual_rop = max(rp.manual_rop, rp.manual_ss)
        rp.manual_lock = True
        rp.lock_reason = "Chuyển từ SS/ROP ghi đè (phiên bản cũ)"
        rp.save(update_fields=["manual_ss", "manual_rop", "manual_lock", "lock_reason"])


class Migration(migrations.Migration):
    dependencies = [("inventory", "0002_replenishmentparam_demand_source_and_more")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
