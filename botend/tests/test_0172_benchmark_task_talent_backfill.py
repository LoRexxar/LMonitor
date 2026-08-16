import importlib

from django.apps import apps as global_apps
from django.test import TestCase

from botend.models import (
    SimcBackendBinary,
    SimcBenchmarkCase,
    SimcBenchmarkExecution,
    SimcBenchmarkPanel,
    SimcResourceVersion,
    SimcTalentString,
    SimcTask,
)
class BenchmarkTaskTalentVersionBackfillTests(TestCase):
    def test_unique_frozen_profile_talent_is_backfilled_as_resource_version(self):
        backend = SimcBackendBinary.objects.create(
            identifier='task-talent-backfill', name='Task Talent Backfill',
            current_version='simc-test',
        )
        panel = SimcBenchmarkPanel.objects.create(
            name='Task Talent Backfill', slug='task-talent-backfill', created_by_id=1,
        )
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_hash='backfill-config', status='success',
        )
        exact_talent = SimcTalentString.objects.create(
            name='Exact Talent', spec='warrior_fury', talent='EXACT',
            hero_talent_names=['斩杀者'],
        )
        SimcTalentString.objects.create(
            name='Duplicate A', spec='warrior_fury', talent='DUPLICATE',
            hero_talent_names=['斩杀者'],
        )
        SimcTalentString.objects.create(
            name='Duplicate B', spec='warrior_fury', talent='DUPLICATE',
            hero_talent_names=['山丘领主'],
        )

        def create_case(profile_key, talent):
            profile_version = SimcResourceVersion.objects.create(
                resource_type='profile', resource_id=len(profile_key),
                content_hash=f'profile-{profile_key}',
                payload={'name': profile_key, 'spec': 'fury', 'talent': talent},
            )
            task = SimcTask.objects.create(
                user_id=1, name=profile_key, simc_profile_id=len(profile_key),
                result_file=f'{profile_key}.html', backend=backend,
                profile_version=profile_version, mode='comparison', current_status=2,
            )
            SimcBenchmarkCase.objects.create(
                execution=execution, task=task, status='success',
                spec_key='warrior_fury', scenario_key='single_target',
                profile_key=profile_key, spec_label='狂怒', scenario_label='单体',
                profile_label=profile_key, coordinate_hash=f'coordinate-{profile_key}',
            )
            return task

        exact_task = create_case('exact', 'EXACT')
        missing_task = create_case('missing', 'MISSING')
        duplicate_task = create_case('duplicate', 'DUPLICATE')

        migration = importlib.import_module(
            'botend.migrations.0172_backfill_benchmark_task_talent_versions'
        )
        migration.backfill_benchmark_task_talent_versions(global_apps, None)

        exact_task.refresh_from_db()
        missing_task.refresh_from_db()
        duplicate_task.refresh_from_db()
        self.assertEqual(exact_task.talent_string_id, exact_talent.id)
        self.assertIsNotNone(exact_task.talent_version_id)
        self.assertEqual(
            exact_task.talent_version.payload['hero_talent_names'], ['斩杀者'],
        )
        exact_talent.hero_talent_names = ['山丘领主']
        exact_talent.save(update_fields=['hero_talent_names'])
        exact_task.talent_version.refresh_from_db()
        self.assertEqual(
            exact_task.talent_version.payload['hero_talent_names'], ['斩杀者'],
        )
        self.assertIsNone(missing_task.talent_version_id)
        self.assertIsNone(duplicate_task.talent_version_id)
