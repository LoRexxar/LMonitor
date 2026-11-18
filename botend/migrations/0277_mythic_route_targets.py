"""登记每日路线采集与三个默认目标。"""
from datetime import timedelta
from django.utils import timezone

import django.db.models.deletion
from django.db import migrations, models


def register(apps, schema_editor):
    db = schema_editor.connection.alias
    targets = apps.get_model('botend', 'MythicRouteTarget').objects.using(db)
    for level in (12, 18, 0):
        targets.get_or_create(level=level, spec_id=0)
    apps.get_model('botend', 'MonitorTask').objects.using(db).get_or_create(
        name='MythicRouteMonitor', defaults=dict(type=41, target='https://raider.io', is_active=True,
        wait_time=86400, last_scan_time=timezone.now() - timedelta(days=2),
        notes='每日按赛季副本、层数与队伍专精更新公开 MDT 路线。'))


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0276_simc_ranking_talent_display_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='mythicdungeondefaultroute',
            name='source_code',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='mythicdungeondefaultroute',
            name='source_dungeon_slug',
            field=models.SlugField(blank=True, default='', max_length=100),
        ),
        migrations.CreateModel(
            name='MythicRouteTarget',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('level', models.PositiveSmallIntegerField(default=0, verbose_name='指定层数')),
                ('spec_id', models.PositiveSmallIntegerField(default=0, verbose_name='必含专精')),
                ('is_active', models.BooleanField(default=True, verbose_name='启用')),
            ],
            options={
                'verbose_name': 'MDT 路线采集目标',
                'constraints': [models.UniqueConstraint(fields=('level', 'spec_id'), name='mdt_target_level_spec')],
            },
        ),
        migrations.AddField(
            model_name='mythicdungeondefaultroute',
            name='collection_target',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='botend.mythicroutetarget'),
        ),
        migrations.AddConstraint(
            model_name='mythicdungeondefaultroute',
            constraint=models.UniqueConstraint(fields=('collection_target', 'source_dungeon_slug'), name='mdt_target_dungeon'),
        ),
        migrations.RunPython(register, migrations.RunPython.noop),
    ]
