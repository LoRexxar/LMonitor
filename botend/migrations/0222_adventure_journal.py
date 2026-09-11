# 冒险手册快照模型迁移。

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0221_expand_class_guide_tabs'),
    ]

    operations = [
        migrations.CreateModel(
            name='JournalRelease',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('build', models.CharField(max_length=64)),
                ('locale', models.CharField(default='zhCN', max_length=8)),
                ('status', models.CharField(default='fetching', max_length=20)),
                ('manifest', models.JSONField(default=dict)),
                ('report', models.JSONField(default=dict)),
                ('error', models.TextField(blank=True, default='')),
                ('started_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('completed_at', models.DateTimeField(null=True)),
            ],
            options={
                'ordering': ['-id'],
            },
        ),
        migrations.CreateModel(
            name='JournalInstance',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('journal_id', models.PositiveIntegerField()),
                ('name', models.CharField(max_length=255)),
                ('kind', models.CharField(max_length=16)),
                ('expansion', models.IntegerField(default=0)),
                ('payload', models.JSONField(default=dict)),
                ('release', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='instances', to='botend.journalrelease')),
            ],
        ),
        migrations.CreateModel(
            name='JournalState',
            fields=[
                ('key', models.CharField(default='wow-zhCN', max_length=32, primary_key=True, serialize=False)),
                ('sync_token', models.CharField(blank=True, default='', max_length=36)),
                ('sync_until', models.DateTimeField(null=True)),
                ('active_release', models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT, to='botend.journalrelease')),
            ],
        ),
        migrations.CreateModel(
            name='JournalEncounter',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('journal_id', models.PositiveIntegerField()),
                ('name', models.CharField(max_length=255)),
                ('order', models.IntegerField(default=0)),
                ('payload', models.JSONField(default=dict)),
                ('instance', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='encounters', to='botend.journalinstance')),
            ],
            options={
                'ordering': ['order', 'journal_id'],
                'constraints': [models.UniqueConstraint(fields=('instance', 'journal_id'), name='journal_encounter_instance_id')],
            },
        ),
        migrations.AddConstraint(
            model_name='journalinstance',
            constraint=models.UniqueConstraint(fields=('release', 'journal_id'), name='journal_instance_release_id'),
        ),
    ]
