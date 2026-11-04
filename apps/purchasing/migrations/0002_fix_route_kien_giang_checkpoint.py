"""Sửa master data tuyến Kiên Giang → Kho BD: mốc kiểm soát là cầu Mỹ Thuận (QL1A), không phải 'trạm Cần Thơ'.

Cập nhật luôn các chuyến chưa đi qua mốc này; mốc đã ghi nhận giữ nguyên lịch sử."""
from django.db import migrations

OLD, NEW = "Qua trạm Cần Thơ", "Qua cầu Mỹ Thuận (QL1A)"


def forwards(apps, schema_editor):
    RouteMilestone = apps.get_model("masterdata", "RouteMilestone")
    TripMilestone = apps.get_model("purchasing", "TripMilestone")
    RouteMilestone.objects.filter(route__origin__icontains="Kiên", name=OLD).update(name=NEW)
    RouteMilestone.objects.filter(route__name__icontains="Kiên Giang", name=OLD).update(name=NEW)
    TripMilestone.objects.filter(trip__route__name__icontains="Kiên Giang", name=OLD, actual_at=None).update(name=NEW)


class Migration(migrations.Migration):
    dependencies = [("purchasing", "0001_initial"), ("masterdata", "0001_initial")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
