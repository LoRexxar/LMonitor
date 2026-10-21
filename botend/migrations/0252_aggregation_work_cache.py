# 全职业采集分页及范围计算的可重建中间结果缓存。

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0251_raiderio_profile_cache'),
    ]

    operations = [
        migrations.CreateModel(
            name='SpecAggregationWorkCache',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('scope', models.CharField(max_length=240)),
                ('fingerprint', models.CharField(max_length=64)),
                ('payload', models.JSONField(default=dict)),
                ('payload_hash', models.CharField(max_length=64)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('season', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='botend.seasonmeta')),
            ],
            options={
                'db_table': 'wow_spec_aggregation_work_cache',
                'constraints': [models.UniqueConstraint(fields=('season', 'scope'), name='spec_work_season_scope')],
            },
        ),
    ]
