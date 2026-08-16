import importlib
from unittest.mock import patch

from django.apps import apps as global_apps
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from botend.models import (
    SimcBackendBinary,
    SimcBenchmarkCase,
    SimcBenchmarkExecution,
    SimcBenchmarkPanel,
    SimcBenchmarkResult,
    SimcResourceVersion,
    SimcTask,
    SimulationRun,
)
from botend.services.simc_benchmark_execution import _result_seal
from botend.services.simc_hero_talents import enrich_manifest_with_actual_hero_talents


class ActualRunHeroTalentAnalysisTests(SimpleTestCase):
    @patch(
        'botend.services.simc_hero_talents._hero_subtree_name_from_table',
        return_value='实际英雄天赋',
    )
    @patch('botend.services.simc_hero_talents.TalentBuildCodeService.build_api_view')
    def test_final_simc_talents_line_is_analyzed_and_frozen(self, build_api_view, _name):
        build_api_view.return_value = {
            'talent_render_model': {
                'trees': [{
                    'tree_type': 'hero',
                    'nodes': [{
                        'tree_type': 'hero', 'db2_subtree_id': 123,
                        'selected': True, 'points': 1,
                    }],
                }],
            },
        }

        manifest = enrich_manifest_with_actual_hero_talents(
            {'profile': 'frozen'},
            'talents=SOURCE_BUILD\ntalents=ACTUAL_BUILD\nactions=/auto_attack\n',
            'warrior_fury',
        )

        self.assertEqual(manifest['hero_talent_names'], ['实际英雄天赋'])
        self.assertEqual(manifest['hero_talent_analysis_error'], '')
        self.assertEqual(
            build_api_view.call_args.kwargs['talent_build_code'], 'ACTUAL_BUILD',
        )


class BenchmarkResultHeroTalentBackfillTests(TestCase):
    @patch('botend.services.simc_run_control.build_frozen_run_input')
    def test_historical_result_is_parsed_from_its_run_and_resealed(self, build_input):
        build_input.return_value = (
            'warrior="Historical"\nspec=fury\ntalents=ACTUAL_BUILD\n',
            {
                'hero_talent_names': ['实际英雄天赋'],
                'hero_talent_analysis_error': '',
            },
        )
        backend = SimcBackendBinary.objects.create(
            identifier='result-hero-backfill', name='Result Hero Backfill',
            current_version='simc-test',
        )
        profile_version = SimcResourceVersion.objects.create(
            resource_type='profile', resource_id=1, content_hash='profile-backfill',
            payload={'spec': 'fury', 'talent': 'SOURCE_BUILD'},
        )
        task = SimcTask.objects.create(
            user_id=1, name='Historical', simc_profile_id=1,
            result_file='historical.html', backend=backend,
            profile_version=profile_version, mode='comparison', current_status=2,
        )
        run = SimulationRun.objects.create(
            task=task, sequence=1, candidate_key='baseline',
            candidate_label='Baseline', status='completed',
            resource_manifest={'hero_talent_names': ['来源资源名称']},
            result_summary={'dps': 12345},
        )
        retry_task = SimcTask.objects.create(
            user_id=1, name='Historical retry', simc_profile_id=1,
            result_file='historical-retry.html', backend=backend,
            profile_version=profile_version, mode='comparison', current_status=3,
            source_task=task,
        )
        failed_retry_run = SimulationRun.objects.create(
            task=retry_task, sequence=1, candidate_key='baseline',
            candidate_label='Baseline', status='failed',
            resource_manifest={'hero_talent_names': ['错误重试天赋']},
        )
        panel = SimcBenchmarkPanel.objects.create(
            name='Result Hero Backfill', slug='result-hero-backfill', created_by_id=1,
        )
        finalized_at = timezone.now()
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_hash='result-hero-backfill', status='success',
            result_hash='legacy-seal', results_finalized_at=finalized_at,
            completed_at=finalized_at,
        )
        case = SimcBenchmarkCase.objects.create(
            execution=execution, task=retry_task, status='success',
            spec_key='warrior_fury', scenario_key='single_target',
            profile_key='historical', spec_label='狂怒', scenario_label='单体',
            profile_label='Historical', coordinate_hash='historical-coordinate',
        )
        result = SimcBenchmarkResult.objects.create(
            case=case, candidate_key='baseline', dps=12345,
            hero_talent_names=['来源资源名称'],
        )

        migration = importlib.import_module(
            'botend.migrations.0173_freeze_benchmark_result_hero_talents'
        )
        migration.backfill_result_hero_talents(global_apps, None)

        result.refresh_from_db()
        run.refresh_from_db()
        failed_retry_run.refresh_from_db()
        execution.refresh_from_db()
        self.assertEqual(build_input.call_args.args[0].pk, task.pk)
        self.assertEqual(build_input.call_args.args[1].pk, run.pk)
        self.assertEqual(result.hero_talent_names, ['实际英雄天赋'])
        self.assertEqual(
            run.resource_manifest['hero_talent_names'], ['实际英雄天赋'],
        )
        self.assertEqual(
            failed_retry_run.resource_manifest['hero_talent_names'], ['错误重试天赋'],
        )
        self.assertEqual(execution.result_hash, _result_seal([{
            'case_id': case.pk,
            'spec_key': 'warrior_fury',
            'scenario_key': 'single_target',
            'profile_key': 'historical',
            'spec_label': '狂怒',
            'scenario_label': '单体',
            'profile_label': 'Historical',
            'status': 'success',
            'candidate_key': 'baseline',
            'dps': 12345.0,
            'hero_talent_names': ['实际英雄天赋'],
        }], execution.completed_at))
