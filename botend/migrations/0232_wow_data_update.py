"""登记按构建更新任务；保留管理员已有开关和周期。"""

import django.utils.timezone
from django.db import migrations, models


def register_monitor(apps, schema_editor):
    apps.get_model('botend', 'MonitorTask').objects.using(schema_editor.connection.alias).get_or_create(
        name='WowDataVersionMonitor', defaults={
            'type': 37, 'target': 'https://wago.tools/builds', 'is_active': True,
            'wait_time': 43200,
            'last_scan_time': django.utils.timezone.now(),
            'notes': '每 12 小时检测装备与天赋；独立执行，下载并发 2，失败保留数据并在下一周期重试。',
        })


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0231_mdt_auto_update'),
    ]

    operations = [
        migrations.CreateModel(
            name='WowDataUpdateRun',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('branch', models.CharField(max_length=16)),
                ('build', models.CharField(blank=True, max_length=32)),
                ('status', models.CharField(default='running', max_length=24)),
                ('report', models.JSONField(blank=True, default=dict)),
                ('error', models.TextField(blank=True)),
                ('started_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'wow_data_update_run',
                'ordering': ['-id'],
            },
        ),
        migrations.CreateModel(
            name='WowDataUpdateState',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('branch', models.CharField(max_length=16, unique=True)),
                ('observed_build', models.CharField(blank=True, max_length=32)),
                ('published_build', models.CharField(blank=True, max_length=32)),
                ('revision', models.PositiveIntegerField(default=0)),
                ('status', models.CharField(default='pending', max_length=24)),
                ('error', models.TextField(blank=True)),
                ('report', models.JSONField(blank=True, default=dict)),
                ('checked_at', models.DateTimeField(blank=True, null=True)),
                ('published_at', models.DateTimeField(blank=True, null=True)),
                ('projections_pending', models.BooleanField(default=False)),
            ],
            options={
                'db_table': 'wow_data_update_state',
            },
        ),
        migrations.RunPython(register_monitor, migrations.RunPython.noop),
    ]
