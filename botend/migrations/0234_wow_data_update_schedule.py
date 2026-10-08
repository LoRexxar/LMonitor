"""降低旧默认检查频率，保留管理员设置的其他周期和开关。"""
from django.db import migrations
from django.utils import timezone


def adjust_schedule(apps, schema_editor):
    apps.get_model('botend', 'MonitorTask').objects.using(schema_editor.connection.alias).filter(
        name='WowDataVersionMonitor', wait_time__in=(600, 3600),
    ).update(wait_time=43200, last_scan_time=timezone.now(),
             notes='每 12 小时检测装备与天赋；独立执行，下载并发 2，失败保留数据并在下一周期重试。')


class Migration(migrations.Migration):
    dependencies = [('botend', '0233_item_data_branch')]
    operations = [migrations.RunPython(adjust_schedule, migrations.RunPython.noop)]
