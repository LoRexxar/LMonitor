"""保留来源授权记录，移除重复配置，统一使用监控任务执行锁。"""
from datetime import timedelta

from django.db import migrations, models
from django.utils import timezone


def ensure_idle(apps, schema_editor):
    alias = schema_editor.connection.alias
    Feed = apps.get_model('botend', 'ClassGuideFeed')
    Lease = apps.get_model('botend', 'MonitorTaskLease')
    now = timezone.now()
    if Feed.objects.using(alias).filter(lease_until__gt=now).exists() or Lease.objects.using(alias).filter(
        task__name='MaxrollClassGuideMonitor', expires_at__gt=now,
    ).exists():
        raise RuntimeError('攻略同步仍持有执行锁，请停止旧版 botend 和手动同步，待当前任务结束或锁过期后重新迁移。')


def migrate_feed(apps, schema_editor):
    alias = schema_editor.connection.alias
    Feed = apps.get_model('botend', 'ClassGuideFeed')
    Task = apps.get_model('botend', 'MonitorTask')
    now = timezone.now()
    feeds = list(Feed.objects.using(alias).order_by('pk'))
    primary = next((feed for feed in feeds if feed.key == 'maxroll'), None)
    task, _ = Task.objects.using(alias).get_or_create(name='MaxrollClassGuideMonitor', defaults={
        'type': 34, 'target': 'https://maxroll.gg/wow/class-guides',
        'is_active': bool(primary and primary.enabled),
        'wait_time': primary.interval_minutes * 60 if primary else 21600,
        'last_scan_time': (primary.last_checked_at if primary else None) or now - timedelta(days=2),
    })
    notes = [task.notes] if task.notes.strip() else []
    for feed in feeds:
        if feed.authorization_note.strip():
            notes.append(feed.authorization_note if feed.key == 'maxroll' else f'来源 {feed.key}：\n{feed.authorization_note}')
    task.notes = '\n\n'.join(notes)
    task.save(using=alias, update_fields=['notes'])


def restore_feed(apps, schema_editor):
    alias = schema_editor.connection.alias
    Task = apps.get_model('botend', 'MonitorTask')
    Feed = apps.get_model('botend', 'ClassGuideFeed')
    task = Task.objects.using(alias).filter(name='MaxrollClassGuideMonitor').first()
    if task:
        Feed.objects.using(alias).update_or_create(key='maxroll', defaults={
            'authorization_note': task.notes, 'enabled': task.is_active,
            'interval_minutes': max(1, task.wait_time // 60), 'last_checked_at': task.last_scan_time,
        })


class Migration(migrations.Migration):
    dependencies = [('botend', '0217_allow_blank_monitor_task_flag')]
    operations = [
        migrations.RunPython(ensure_idle, migrations.RunPython.noop),
        migrations.AddField(model_name='monitortask', name='notes',
            field=models.TextField('任务备注', blank=True, default='')),
        migrations.RunPython(migrate_feed, restore_feed),
        migrations.DeleteModel(name='ClassGuideFeed'),
    ]
