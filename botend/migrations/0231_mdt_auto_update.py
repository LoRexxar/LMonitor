"""登记每日 MDT 更新任务及可追溯执行记录，不覆盖已有开关和间隔。"""
from datetime import timedelta
from django.db import migrations, models
from django.utils import timezone


def register_monitor(apps, schema_editor):
    Task = apps.get_model('botend', 'MonitorTask')
    Task.objects.using(schema_editor.connection.alias).get_or_create(
        name='MythicDungeonToolsMonitor', defaults={
            'type': 36, 'target': 'https://github.com/Nnoggie/MythicDungeonTools',
            'is_active': True, 'wait_time': 86400,
            'last_scan_time': timezone.now() - timedelta(days=2),
            'notes': '每日检查 MDT 正式版本；校验和地图上传成功后原位升级，失败保留当前数据。',
        },
    )


class Migration(migrations.Migration):
    dependencies = [('botend', '0230_site_analytics')]
    operations = [
        migrations.CreateModel(
            name='MythicDungeonSyncRun',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('status', models.CharField(default='running', max_length=24, verbose_name='状态')),
                ('source_version', models.CharField(blank=True, max_length=120, verbose_name='原版本')),
                ('target_version', models.CharField(blank=True, max_length=120, verbose_name='目标版本')),
                ('source_commit', models.CharField(blank=True, max_length=40, verbose_name='上游提交')),
                ('summary', models.JSONField(blank=True, default=dict, verbose_name='校验结果')),
                ('error', models.TextField(blank=True, verbose_name='失败原因')),
                ('started_at', models.DateTimeField(default=timezone.now, verbose_name='开始时间')),
                ('finished_at', models.DateTimeField(blank=True, null=True, verbose_name='结束时间')),
            ],
            options={'db_table': 'mythic_dungeon_sync_run', 'ordering': ['-started_at', '-id'], 'verbose_name': 'MDT 自动更新记录'},
        ),
        migrations.RunPython(register_monitor, migrations.RunPython.noop),
    ]
