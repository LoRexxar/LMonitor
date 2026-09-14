"""Disable automatic Adventure Journal refreshes; updates are manual by design."""
from django.db import migrations


def disable_adventure_journal_monitor(apps, schema_editor):
    MonitorTask = apps.get_model('botend', 'MonitorTask')
    MonitorTask.objects.using(schema_editor.connection.alias).filter(
        name='AdventureJournalMonitor',
        type=35,
    ).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [
        ('botend', '0225_merge_journal_talent_versions'),
    ]

    operations = [
        migrations.RunPython(
            disable_adventure_journal_monitor,
            # 回滚代码不应擅自启用原本就由管理员关闭的任务。
            reverse_code=migrations.RunPython.noop,
        ),
    ]
