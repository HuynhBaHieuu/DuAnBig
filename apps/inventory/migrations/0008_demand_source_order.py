"""Đặc tả dự báo mục 3.1: nguồn nhu cầu mặc định = đơn đã xác nhận (ORDER); các dòng đang là ALL chuyển sang ORDER."""
from django.db import migrations


def forwards(apps, schema_editor):
    apps.get_model("inventory", "ReplenishmentParam").objects.filter(demand_source="ALL").update(demand_source="ORDER")


def backwards(apps, schema_editor):
    apps.get_model("inventory", "ReplenishmentParam").objects.filter(demand_source="ORDER").update(demand_source="ALL")


class Migration(migrations.Migration):
    dependencies = [("inventory", "0007_forecast_model")]
    operations = [migrations.RunPython(forwards, backwards)]
