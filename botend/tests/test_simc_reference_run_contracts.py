import json
import os
from unittest.mock import MagicMock, patch

from django.db import IntegrityError
from django.test import TestCase

from botend.services.simc_task_service import _build_profile_payload

from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.models import SimcApl, SimcContentTemplate, SimcProfile, SimcTaskBatch, SimulationRun
from botend.services.simc_composer import SimcComposer
from botend.services.simc_task_service import create_task


class SimcReferenceRunContractTests(TestCase):
    def test_manual_export_does_not_emit_zero_secondary_stat_overrides(self):
        profile = SimcProfile.objects.create(
            user_id=1, name='addon', spec='fury', player_config_mode='manual_equipment',
            player_equipment='warrior="Tester"\nhead=,id=1\noff_hand=,id=2',
            gear_strength=93330, gear_crit=0, gear_haste=0, gear_mastery=0,
            gear_versatility=0,
        )
        payload = _build_profile_payload(profile)
        self.assertIsNone(payload['gear_crit'])
        self.assertIsNone(payload['gear_haste'])
        self.assertIsNone(payload['gear_mastery'])
        self.assertIsNone(payload['gear_versatility'])

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
        batch = SimcTaskBatch.objects.create(user_id=self.user_id, name='batch', status=1)
        failed = self.make_task(name='failed', batch_id=batch.id)
        pending = self.make_task(name='pending', batch_id=batch.id)
        failed.current_status = 3
        failed.save(update_fields=['current_status'])
        SimcMonitor(None, failed).sync_batch_lifecycle(batch.id)
        batch.refresh_from_db()
        self.assertEqual(batch.status, 1)
        self.assertIsNone(batch.completed_at)
        pending.current_status = 2
        pending.save(update_fields=['current_status'])
        SimcMonitor(None, pending).sync_batch_lifecycle(batch.id)
        batch.refresh_from_db()
        self.assertEqual(batch.status, 3)
        self.assertIsNotNone(batch.completed_at)

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
        SimulationRun.objects.create(task=task, sequence=1)
        with self.assertRaises(IntegrityError):
            with __import__('django.db', fromlist=['transaction']).transaction.atomic():
                SimulationRun.objects.create(task=task, sequence=1)
