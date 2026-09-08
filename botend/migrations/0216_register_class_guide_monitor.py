"""将攻略监控设置接入现有 MonitorTask 调度，保留原开关和间隔。"""
from datetime import timedelta

from django.db import migrations
from django.utils import timezone


def register_monitor(apps, schema_editor):
    alias = schema_editor.connection.alias
    Task = apps.get_model('botend', 'MonitorTask')
    Feed = apps.get_model('botend', 'ClassGuideFeed')
    if Task.objects.using(alias).filter(name='MaxrollClassGuideMonitor').exists():
        return
    feed = Feed.objects.using(alias).filter(key='maxroll').first()
    Task.objects.using(alias).create(
        name='MaxrollClassGuideMonitor', type=34,
        target='https://maxroll.gg/wow/class-guides',
        is_active=bool(feed and feed.enabled),
        wait_time=(feed.interval_minutes if feed else 360) * 60,
        last_scan_time=feed.last_checked_at if feed and feed.last_checked_at else timezone.now() - timedelta(days=2),
    )


class Migration(migrations.Migration):
    dependencies = [('botend', '0215_merge_guides_and_nga')]
    operations = [migrations.RunPython(register_monitor, migrations.RunPython.noop)]
