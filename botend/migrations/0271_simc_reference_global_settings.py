"""Consolidate equal legacy conditions, preserving config IDs and frozen history.

Preflight runs before any DDL (important for non-transactional MySQL DDL).
A conflicting branch is deliberately not guessed/overwritten.
"""
import django.db.models.deletion
from django.db import migrations, models
import botend.models

FIELDS = ('backend_id', 'template_id', 'duration', 'targets', 'iterations', 'is_enabled')


def preflight(apps, schema_editor):
    Config = apps.get_model('botend', 'SimcReferenceSimulationConfig')
    baseline = {}
    for row in Config.objects.using(schema_editor.connection.alias).order_by('pk').values('id', 'use_ptr', *FIELDS):
        branch = row['use_ptr']
        values = {field: row[field] for field in FIELDS}
        if branch in baseline and baseline[branch][1] != values:
            raise RuntimeError(
                '公开模拟全局条件迁移拒绝：环境 use_ptr=%s 的配置 #%s 与 #%s 不一致；旧数据未改动，请先明确统一条件。'
                % (branch, baseline[branch][0], row['id'])
            )
        baseline.setdefault(branch, (row['id'], values))


def consolidate(apps, schema_editor):
    alias = schema_editor.connection.alias
    Config = apps.get_model('botend', 'SimcReferenceSimulationConfig')
    Settings = apps.get_model('botend', 'SimcReferenceSimulationSettings')
    Backend = apps.get_model('botend', 'SimcBackendBinary')
    Template = apps.get_model('botend', 'SimcContentTemplate')
    preflight(apps, schema_editor)
    templates = list(Template.objects.using(alias).filter(
        is_active=True, is_selectable=True, owner_user_id__isnull=True,
        spec__in=('default', 'all', '*'),
    ).order_by('pk').values_list('pk', flat=True))
    for branch in (False, True):
        rows = Config.objects.using(alias).filter(use_ptr=branch)
        first = rows.order_by('pk').values(*FIELDS).first()
        if first is None:
            first = dict(backend_id=Backend.objects.using(alias).filter(
                identifier='ptr' if branch else 'production', is_active=True,
            ).values_list('pk', flat=True).first(), template_id=templates[0] if len(templates) == 1 else None,
                duration=300, targets=[1, 5], iterations=10000, is_enabled=True)
        settings = Settings.objects.using(alias).create(use_ptr=branch, **first)
        rows.update(settings_id=settings.pk)


def restore_legacy(apps, schema_editor):
    alias = schema_editor.connection.alias
    Config = apps.get_model('botend', 'SimcReferenceSimulationConfig')
    Settings = apps.get_model('botend', 'SimcReferenceSimulationSettings')
    for row in Settings.objects.using(alias).values('pk', *FIELDS):
        Config.objects.using(alias).filter(settings_id=row['pk']).update(**{field: row[field] for field in FIELDS})


class Migration(migrations.Migration):
    dependencies = [('botend', '0270_simc_reference_publication')]
    operations = [
        migrations.RunPython(preflight, migrations.RunPython.noop),
        migrations.CreateModel(
            name='SimcReferenceSimulationSettings',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('use_ptr', models.BooleanField(default=False, unique=True)),
                ('duration', models.PositiveIntegerField(default=300)),
                ('targets', models.JSONField(default=botend.models.simc_reference_default_targets)),
                ('iterations', models.PositiveIntegerField(default=10000)),
                ('is_enabled', models.BooleanField(default=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('backend', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='botend.simcbackendbinary')),
                ('template', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='botend.simccontenttemplate')),
            ],
            options={'verbose_name': '公开资料全局模拟设置', 'verbose_name_plural': '公开资料全局模拟设置'},
        ),
        migrations.AddField(model_name='simcreferencesimulationconfig', name='settings', field=models.ForeignKey(
            null=True, on_delete=django.db.models.deletion.PROTECT, related_name='configs', to='botend.simcreferencesimulationsettings')),
        migrations.RunPython(consolidate, restore_legacy),
        migrations.AlterField(model_name='simcreferencesimulationconfig', name='settings', field=models.ForeignKey(
            on_delete=django.db.models.deletion.PROTECT, related_name='configs', to='botend.simcreferencesimulationsettings')),
        *[migrations.RemoveField(model_name='simcreferencesimulationconfig', name=field) for field in
          ('backend', 'template', 'duration', 'targets', 'iterations', 'is_enabled', 'updated_at')],
        migrations.AlterModelOptions(name='simcreferencesimulationconfig', options={
            'verbose_name': '公开配置模拟引用', 'verbose_name_plural': '公开配置模拟引用'}),
    ]
