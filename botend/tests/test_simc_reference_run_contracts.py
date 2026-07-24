import hashlib
import json
import os
from unittest.mock import MagicMock, patch

from django.db import IntegrityError
from django.test import TestCase

from botend.services.simc_task_service import _build_profile_payload

from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.models import SimcApl, SimcContentTemplate, SimcProfile, SimulationRun
from botend.services.simc_composer import SimcComposer
from botend.services.simc_task_service import create_task


TEST_VALIDATION_IDENTITY = ('test-simc-revision', 'test-game-build')


def mark_apl_valid(apl):
    values = {'validation_status': SimcApl.VALIDATION_VALID,
              'validated_content_hash': hashlib.sha256(apl.content.encode()).hexdigest(),
              'validation_revision': TEST_VALIDATION_IDENTITY[0],
              'validation_game_build': TEST_VALIDATION_IDENTITY[1], 'is_selectable': True}
    SimcApl.objects.filter(pk=apl.pk).update(**values)
    for key, value in values.items(): setattr(apl, key, value)


def setUpModule():
    from django.test import override_settings
    global _validation_settings, _validation_mock
    _validation_settings = override_settings(SIMC_APL_CURRENT_IDENTITY=TEST_VALIDATION_IDENTITY)
    _validation_settings.enable()
    _validation_mock = patch('botend.services.simc_task_service.validate_apl_for_profile', side_effect=lambda _p, apl: {
        'valid': True, 'content_hash': hashlib.sha256(apl.content.encode()).hexdigest(),
        'revision': TEST_VALIDATION_IDENTITY[0], 'game_build': TEST_VALIDATION_IDENTITY[1]})
    _validation_mock.start()


def tearDownModule():
    _validation_mock.stop(); _validation_settings.disable()


class SimcReferenceRunContractTests(TestCase):
    def test_manual_export_does_not_emit_zero_secondary_stat_overrides(self):
        profile = SimcProfile.objects.create(
            user_id=1, name='addon', spec='fury', player_config_mode='manual_equipment',
            player_equipment='warrior="Tester"\nhead=,id=1\noff_hand=,id=2',
            gear_strength=93330, gear_crit=0, gear_haste=0, gear_mastery=0,
            gear_versatility=0,
        )
        payload = _build_profile_payload(profile)
        self.assertIsNone(payload['gear_strength'])
        self.assertIsNone(payload['gear_crit'])
        self.assertIsNone(payload['gear_haste'])
        self.assertIsNone(payload['gear_mastery'])
        self.assertIsNone(payload['gear_versatility'])

    def test_attribute_composer_never_emits_primary_stat_override(self):
        request = {
            'spec': 'fury', 'player_import_mode': 'attribute_only',
            'player_equipment': (
                'warrior="Tester"\nlevel=80\nspec=fury\n'
                'head=,id=1\ngear_strength=0\n'
            ),
            'gear_strength': 0,
            'gear_crit': 1200, 'gear_haste': 1300,
            'gear_mastery': 1400, 'gear_versatility': 1500,
            'base_template_content': '{player_config}',
            '_result_file_path': 'simc/result.html',
        }
        final, _manifest, error = SimcComposer(1).compose(request)
        self.assertIsNone(error)
        self.assertNotRegex(final, r'(?m)^\s*gear_strength\s*=')
        self.assertIn('gear_crit_rating=1200', final)

    def setUp(self):
        self.user_id = 8123
        self.profile = SimcProfile.objects.create(
            user_id=self.user_id, name='contract profile', spec='fury',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Contract"\nspec=fury\nhead=,id=1',
            is_active=True,
        )
        self.template = SimcContentTemplate.objects.create(
            name='contract template', template_type='base_template', spec='fury',
            content='{simulation_options}\n{player_config}\n{action_list}\n{output_options}',
            is_active=True, is_selectable=True,
        )
        self.apl = SimcApl.objects.create(
            name='contract apl', spec='fury', content='actions=/bloodthirst',
            is_system=True, is_active=True, is_selectable=True,
        )
        mark_apl_valid(self.apl)

    def make_task(self, **kwargs):
        values = {
            'user_id': self.user_id, 'name': 'contract task',
            'profile_id': self.profile.id, 'template_id': self.template.id,
            'apl_id': self.apl.id,
        }
        values.update(kwargs)
        return create_task(**values)

    def test_worker_forwards_all_supported_simulation_params_to_composer(self):
        task = self.make_task(simulation_params={
            'iterations': 23456, 'target_error': 0.17, 'fight_style': 'HecticAddCleave',
            'max_time': 421, 'vary_combat_length': 0.13, 'enemy_type': 'Fluffy_Pillow',
            'desired_targets': 4,
        })
        captured = {}
        with patch('botend.controller.plugins.simc.SimcMonitor.SimcComposer') as composer_cls:
            composer = MagicMock()
            composer.compose.side_effect = lambda request: (captured.update(request) or 'warrior="x"', {}, None)
            composer_cls.return_value = composer
            with patch.object(SimcMonitor, 'execute_simc_command', return_value=True):
                monitor = SimcMonitor(None, task)
                monitor.result_path = '/tmp/simc_contract_results'
                os.makedirs(monitor.result_path, exist_ok=True)
                self.assertTrue(monitor.process_simc_task(task))
        for key, expected in task.simulation_params.items():
            mapped = {'max_time': 'time', 'desired_targets': 'target_count'}.get(key, key)
            self.assertEqual(captured[mapped], expected)

    def test_composer_renders_supported_options_and_rejects_invalid_values(self):
        request = {
            'spec': 'fury', 'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Contract"\nspec=fury\nhead=,id=1',
            'override_action_list': 'actions=/bloodthirst',
            'base_template_content': '{simulation_options}\n{player_config}\n{action_list}\n{output_options}',
            'iterations': 23456, 'target_error': 0.17, 'fight_style': 'HecticAddCleave',
            'time': 421, 'vary_combat_length': 0.13, 'enemy_type': 'Fluffy_Pillow',
            'target_count': 4,
        }
        content, _, error = SimcComposer(self.user_id).compose(request)
        self.assertIsNone(error)
        for option in ('iterations=23456', 'target_error=0.17', 'fight_style=HecticAddCleave',
                       'max_time=421', 'vary_combat_length=0.13', 'enemy=Fluffy_Pillow',
                       'desired_targets=4'):
            self.assertIn(option, content)
        invalid = dict(request, iterations=0)
        content, _, error = SimcComposer(self.user_id).compose(invalid)
        self.assertIsNone(content)
        self.assertIn('iterations', error)

    def test_batch_does_not_finish_until_every_member_is_terminal(self):
        """The request Task lifecycle is aggregated from all of its Runs."""
        task = self.make_task(name='multi-run request')
        first = task.simulation_runs.get(sequence=1)
        first.status = 'failed'
        first.error_detail = 'candidate failed'
        first.save(update_fields=['status', 'error_detail'])
        pending = SimulationRun.objects.create(
            task=task, sequence=2, candidate_key='second', status='pending',
        )
        monitor = SimcMonitor(None, task)

        with patch.object(monitor, 'process_reference_run', return_value=False):
            self.assertFalse(monitor.process_reference_task(task))
        task.refresh_from_db()
        self.assertEqual(task.current_status, 0)
        self.assertIsNone(task.completed_at)

        pending.status = 'completed'
        pending.result_summary = {'dps': 42}
        pending.save(update_fields=['status', 'result_summary'])
        self.assertTrue(monitor.process_reference_task(task))
        task.refresh_from_db()
        self.assertEqual(task.current_status, 2)
        self.assertIsNotNone(task.completed_at)
        self.assertEqual(task.analysis_result['total'], 2)

    def test_success_persists_semantic_summary_on_run_and_task(self):
        task = self.make_task()
        summary = {'valid': True, 'dps': 123456.7, 'non_auto_dps': 120000, 'action_row_count': 7}

        def successful_execution(_path, actual_task, _result):
            SimcMonitor.persist_semantic_validation(actual_task, summary)
            return True

        with patch.object(SimcMonitor, 'execute_simc_command', side_effect=successful_execution):
            monitor = SimcMonitor(None, task)
            monitor.result_path = '/tmp/simc_contract_results'
            os.makedirs(monitor.result_path, exist_ok=True)
            self.assertTrue(monitor.process_simc_task(task))
        task.refresh_from_db()
        run = SimulationRun.objects.get(task=task)
        self.assertEqual(run.result_summary['dps'], 123456.7)
        self.assertEqual(json.loads(task.result_summary)['dps'], 123456.7)

    def test_execution_failure_uses_real_error_detail_not_report_filename(self):
        task = self.make_task()
        report_name = task.result_file

        def failed_execution(_path, actual_task, _result):
            actual_task.error_detail = 'SimC execution failed: invalid option enemy'
            actual_task.save(update_fields=['error_detail'])
            return False

        with patch.object(SimcMonitor, 'execute_simc_command', side_effect=failed_execution):
            monitor = SimcMonitor(None, task)
            monitor.result_path = '/tmp/simc_contract_results'
            os.makedirs(monitor.result_path, exist_ok=True)
            self.assertFalse(monitor.process_simc_task(task))
        task.refresh_from_db()
        run = SimulationRun.objects.get(task=task)
        self.assertEqual(task.error_detail, 'SimC execution failed: invalid option enemy')
        self.assertEqual(run.error_detail, task.error_detail)
        self.assertNotEqual(run.error_detail, report_name)

    def test_run_sequence_is_unique_per_task(self):
        task = self.make_task()
        self.assertTrue(SimulationRun.objects.filter(task=task, sequence=1).exists())
        with self.assertRaises(IntegrityError):
            with __import__('django.db', fromlist=['transaction']).transaction.atomic():
                SimulationRun.objects.create(task=task, sequence=1)
