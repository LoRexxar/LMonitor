"""Split legacy target lists without changing frozen input or config identity.

The final operation's reverse check runs BEFORE reverse DDL, including on MySQL.
"""
import django.db.models.deletion
from django.db import migrations, models

DEFAULTS = {'fight_style': 'Patchwerk', 'vary_combat_length': 0,
            'raid_buffs': [], 'use_class_raid_buff': False, 'extra_options': []}


def split_scenarios(apps, schema_editor):
    alias = schema_editor.connection.alias
    Settings = apps.get_model('botend', 'SimcReferenceSimulationSettings')
    Scene = apps.get_model('botend', 'SimcReferenceSimulationScenario')
    Result = apps.get_model('botend', 'SimcReferenceSimulation')
    for settings in Settings.objects.using(alias).all().iterator():
        scenes = []
        for order, target in enumerate(settings.targets):
            params = dict(DEFAULTS, desired_targets=target, max_time=settings.duration, iterations=settings.iterations)
            scene = Scene.objects.using(alias).create(settings_id=settings.pk,
                name=f'{target} 目标 · {settings.duration} 秒', simulation_params=params, display_order=order)
            scenes.append(scene)
        for row in Result.objects.using(alias).filter(config__settings_id=settings.pk).iterator():
            matches = [scene for scene in scenes if scene.simulation_params == (row.snapshot or {}).get('simulation_params')]
            if len(matches) == 1:
                Result.objects.using(alias).filter(pk=row.pk).update(scenario_id=matches[0].pk)


def legacy_values(settings, scenes):
    if not scenes:
        raise RuntimeError('公开模拟场景无法无损回滚：场景为空')
    duration, iterations, targets = None, None, []
    for scene in scenes:
        params = scene.simulation_params
        expected = dict(DEFAULTS, desired_targets=params.get('desired_targets'),
                        max_time=params.get('max_time'), iterations=params.get('iterations'))
        if (params != expected or not scene.is_enabled or not isinstance(params.get('max_time'), int)
                or isinstance(params.get('max_time'), bool) or not isinstance(params.get('iterations'), int)
                or isinstance(params.get('iterations'), bool) or type(params.get('desired_targets')) is not int
                or (duration is not None and duration != params['max_time'])
                or (iterations is not None and iterations != params['iterations'])
                or params['desired_targets'] in targets):
            raise RuntimeError('公开模拟场景无法无损回滚：各场景的时长、迭代、启用或扩展参数不能用旧全局条件表达')
        duration, iterations = params['max_time'], params['iterations']
        targets.append(params['desired_targets'])
    return dict(duration=duration, iterations=iterations, targets=targets)


def rollback_preflight(apps, schema_editor):
    alias = schema_editor.connection.alias
    Settings = apps.get_model('botend', 'SimcReferenceSimulationSettings')
    Scene = apps.get_model('botend', 'SimcReferenceSimulationScenario')
    for settings in Settings.objects.using(alias).all().iterator():
        legacy_values(settings, list(Scene.objects.using(alias).filter(settings_id=settings.pk).order_by('display_order', 'pk')))


def restore_legacy(apps, schema_editor):
    alias = schema_editor.connection.alias
    Settings = apps.get_model('botend', 'SimcReferenceSimulationSettings')
    Scene = apps.get_model('botend', 'SimcReferenceSimulationScenario')
    rollback_preflight(apps, schema_editor)
    for settings in Settings.objects.using(alias).all().iterator():
        values = legacy_values(settings, list(Scene.objects.using(alias).filter(settings_id=settings.pk).order_by('display_order', 'pk')))
        Settings.objects.using(alias).filter(pk=settings.pk).update(**values)


class Migration(migrations.Migration):
    dependencies = [('botend', '0271_simc_reference_global_settings')]
    operations = [
        migrations.CreateModel(name='SimcReferenceSimulationScenario', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('name', models.CharField(max_length=200)),
            ('simulation_params', models.JSONField(default=dict)),
            ('is_enabled', models.BooleanField(default=True)),
            ('display_order', models.IntegerField(default=0)),
            ('settings', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='scenarios', to='botend.simcreferencesimulationsettings')),
        ], options={'ordering': ['display_order', 'pk'], 'verbose_name': '公开资料模拟场景', 'verbose_name_plural': '公开资料模拟场景'}),
        migrations.AddField(model_name='simcreferencesimulation', name='scenario', field=models.ForeignKey(
            null=True, blank=True, on_delete=django.db.models.deletion.PROTECT, related_name='simulations', to='botend.simcreferencesimulationscenario')),
        migrations.RunPython(split_scenarios, restore_legacy),
        *[migrations.RemoveField(model_name='simcreferencesimulationsettings', name=name)
          for name in ('duration', 'targets', 'iterations')],
        migrations.RunPython(migrations.RunPython.noop, rollback_preflight),
    ]
