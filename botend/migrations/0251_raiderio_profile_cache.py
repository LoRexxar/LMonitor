# 人物详情独立持久缓存及并发采集租约。

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0250_player_analysis_recheck'),
    ]

    operations = [
        migrations.CreateModel(
            name='RaiderIOProfileCache',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('identity_digest', models.CharField(max_length=64)),
                ('payload', models.JSONField(default=dict)),
                ('ranking_signals', models.JSONField(default=dict)),
                ('fetched_at', models.DateTimeField(blank=True, null=True)),
                ('next_attempt_at', models.DateTimeField(blank=True, null=True)),
                ('failure_count', models.PositiveIntegerField(default=0)),
                ('lease_owner', models.CharField(blank=True, default='', max_length=32)),
                ('lease_expires_at', models.DateTimeField(blank=True, null=True)),
                ('season', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='botend.seasonmeta')),
            ],
            options={
                'db_table': 'wow_raiderio_profile_cache',
                'constraints': [models.UniqueConstraint(fields=('season', 'identity_digest'), name='rio_profile_season_identity')],
            },
        ),
    ]
