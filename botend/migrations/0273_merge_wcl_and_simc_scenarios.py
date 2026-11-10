"""合并WCL故障恢复与公开模拟场景的迁移分支，不改变表结构或数据。"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0271_wcl_transient_report_state'),
        ('botend', '0272_simc_reference_scenarios'),
    ]

    operations = []
