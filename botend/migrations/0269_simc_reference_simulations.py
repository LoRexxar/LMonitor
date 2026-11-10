# 公开配置模拟的独立设置与执行记录，生成于 2026-11-10。

import django.db.models.deletion
import botend.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('botend', '0268_merge_nightly_and_forum'),
    ]

    operations = [
        migrations.CreateModel(
            name='SimcReferenceSimulationConfig',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('spec_key', models.CharField(max_length=100)),
                ('use_ptr', models.BooleanField(default=False)),
                ('duration', models.PositiveIntegerField(default=300)),
                ('targets', models.JSONField(default=botend.models.simc_reference_default_targets)),
                ('iterations', models.PositiveIntegerField(default=10000)),
                ('is_enabled', models.BooleanField(default=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('backend', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='botend.simcbackendbinary')),
                ('template', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='botend.simccontenttemplate')),
            ],
            options={
                'verbose_name': '公开配置模拟设置',
                'verbose_name_plural': '公开配置模拟设置',
            },
        ),
        migrations.CreateModel(
            name='SimcReferenceSimulation',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('requested_by_id', models.PositiveBigIntegerField()),
                ('signature', models.CharField(db_index=True, max_length=64)),
                ('snapshot', models.JSONField(default=dict)),
                ('status', models.CharField(choices=[('queued', '等待派发'), ('preparing', '准备中'), ('submitted', '排队或运行中'), ('completed', '已完成'), ('failed', '失败'), ('cancelled', '已取消')], db_index=True, default='queued', max_length=16)),
                ('dps', models.FloatField(blank=True, null=True)),
                ('error', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('apl', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='botend.simcapl')),
                ('profile', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='botend.simcprofile')),
                ('talent', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='botend.simctalentstring')),
                ('task', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reference_simulation', to='botend.simctask')),
                ('config', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='simulations', to='botend.simcreferencesimulationconfig')),
            ],
            options={
                'verbose_name': '公开配置模拟结果',
                'verbose_name_plural': '公开配置模拟结果',
            },
        ),
        migrations.AddConstraint(
            model_name='simcreferencesimulationconfig',
            constraint=models.UniqueConstraint(fields=('spec_key', 'use_ptr'), name='simc_ref_spec_branch_uniq'),
        ),
        migrations.AddIndex(
            model_name='simcreferencesimulation',
            index=models.Index(fields=['config', 'profile', 'talent'], name='simc_ref_combination_idx'),
        ),
    ]
