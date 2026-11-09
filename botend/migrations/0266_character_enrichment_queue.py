"""新增独立富化队列与跨工作者来源冷却，不迁移或重抓历史人物。"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0265_rio_statistics_chinese_name'),
    ]

    operations = [
        migrations.CreateModel(
            name='RaiderIOSourceBackoff',
            fields=[
                ('key', models.CharField(max_length=32, primary_key=True, serialize=False)),
                ('until', models.DateTimeField()),
            ],
        ),
        migrations.CreateModel(
            name='CharacterEnrichmentTask',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(max_length=16)),
                ('key', models.CharField(max_length=64)),
                ('payload', models.JSONField(default=dict)),
                ('revision', models.CharField(max_length=64)),
                ('state', models.CharField(default='pending', max_length=16)),
                ('due_at', models.DateTimeField()),
                ('expires_at', models.DateTimeField()),
                ('lease_owner', models.CharField(blank=True, default='', max_length=32)),
                ('lease_expires_at', models.DateTimeField(blank=True, null=True)),
                ('failure_count', models.PositiveIntegerField(default=0)),
                ('last_error', models.CharField(blank=True, default='', max_length=80)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('season', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='botend.seasonmeta')),
            ],
            options={
                'indexes': [models.Index(fields=['season', 'state', 'due_at'], name='idx_enrichment_due'),
                            models.Index(fields=['season', 'state', 'expires_at'], name='idx_enrichment_expiry')],
                'constraints': [models.UniqueConstraint(fields=('season', 'kind', 'key'), name='uniq_character_enrichment')],
            },
        ),
    ]
