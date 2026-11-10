"""现有重试状态增加临时故障选项，不新增表、列或改变已有数据。"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('botend', '0270_simc_reference_publication')]

    operations = [migrations.AlterField(
        model_name='wclcombatantfetchstate', name='status',
        field=models.CharField(max_length=20, choices=[
            ('permission_denied', 'Permission denied'), ('missing', 'Missing'),
            ('transient_error', '临时故障')]))]
