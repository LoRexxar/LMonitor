# 人物日更检查点与资料稳定性复核字段。

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0266_character_enrichment_queue'),
    ]

    operations = [
        migrations.AddField(
            model_name='raiderioprofilecache',
            name='scope_token',
            field=models.CharField(db_default='', default='', max_length=64),
        ),
        migrations.AddField(
            model_name='raiderioprofilecache',
            name='unchanged_count',
            field=models.PositiveIntegerField(db_default=0, default=0),
        ),
        migrations.CreateModel(
            name='PlayerDailyCheckpoint',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('day', models.DateField()),
                ('scope', models.CharField(max_length=64)),
                ('generation', models.CharField(max_length=32)),
                ('stages', models.JSONField(default=dict)),
                ('complete', models.BooleanField(default=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('task', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to='botend.monitortask')),
            ],
        ),
    ]
