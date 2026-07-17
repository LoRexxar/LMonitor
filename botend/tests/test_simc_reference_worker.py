"""
TDD tests for SimC Reference-type Worker - strict contract enforcement.

Must pass RED before implementation:
A) SimcComposer.compose() called exactly once, apply_template() forbidden
B) Compose failure creates independent failed Run with error_detail
C) Exception after completed Run must NOT modify old Run
D) Dynamic content from version payload, not live resources
E) Non-complete-reference tasks rejected without calling resolve/compose/execute
F) Retry sequence increments

Run with: DJANGO_SETTINGS_MODULE=LMonitor.settings_test_sqlite python manage.py test botend.tests.test_simc_reference_worker
"""
import json
import hashlib
import os
from unittest.mock import patch, MagicMock, call
from django.test import TestCase
from django.utils import timezone
from botend.models import (
    SimcTask,
    SimcProfile,
    SimcContentTemplate,
    SimcApl,
    SimcResourceVersion,
    SimulationRun,
)


class SimcReferenceWorkerStrictContractTests(TestCase):
    """Strict contract tests - must fail RED until implementation is correct."""

    def setUp(self):
        """Create complete reference task setup."""
        self.user_id = 1001

        # Create live resources
        self.profile = SimcProfile.objects.create(
            user_id=self.user_id,
            name="Fury Warrior",
            spec="fury",
            player_config_mode="manual_equipment",
            player_equipment="warrior=\"TestWarrior\"\nlevel=80\nhead=212345",
            talent="test_talent_string",
            is_active=True,
        )

        self.template = SimcContentTemplate.objects.create(
            name="Base Template",
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec="fury",
            content="fight_style={fight_style}\nmax_time={time}\n{player_config}\n{action_list}",
            is_active=True,
            is_selectable=True,
        )

        self.apl = SimcApl.objects.create(
            name="Fury APL",
            spec="fury",
            content="actions=/auto_attack\nactions+=/bloodthirst",
            is_active=True,
            is_selectable=True,
            is_system=True,
        )

        # Create immutable versions with DIFFERENT content from live
        self.profile_version = SimcResourceVersion.objects.create(
            resource_type='profile',
            resource_id=self.profile.id,
            content_hash='profile_hash_v1',
            payload={
                'name': 'Fury Warrior V1',
                'spec': 'fury',
                'player_config_mode': 'manual_equipment',
                'player_equipment': 'warrior="VersionedWarrior"\nlevel=80\nhead=999999',
                'talent': 'versioned_talent_v1',
            },
        )

        self.template_version = SimcResourceVersion.objects.create(
            resource_type='template',
            resource_id=self.template.id,
            content_hash='template_hash_v1',
            payload={
                'name': 'Base Template V1',
                'template_type': self.template.template_type,
                'content': 'fight_style={fight_style}\nmax_time={time}\n{player_config}\n{action_list}',
            },
        )

        self.apl_version = SimcResourceVersion.objects.create(
            resource_type='apl',
            resource_id=self.apl.id,
            content_hash='apl_hash_v1',
            payload={
                'name': 'Fury APL V1',
                'spec': 'fury',
                'content': 'actions=/auto_attack\nactions+=/rampage_versioned',
                'is_system': True,
            },
        )

    def test_A_composer_called_exactly_once_apply_template_forbidden(self):
        """RED A: Must call SimcComposer.compose() exactly once, apply_template must NOT be called."""
        task = SimcTask.objects.create(
            user_id=self.user_id,
            name="Reference Task",
            simc_profile_id=0,
            task_type=1,
            profile=self.profile,
            template=self.template,
            apl=self.apl,
            profile_version=self.profile_version,
            template_version=self.template_version,
            apl_version=self.apl_version,
            simulation_params={'spec': 'fury', 'fight_style': 'Patchwerk', 'max_time': 300},
            current_status=0,
            is_active=True,
            result_file='test_task.html',
        )

        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        from botend.services.task_resolver import is_reference_task

        fixed_simc_content = "warrior=\"ComposedWarrior\"\nlevel=80\nrace=orc\nfight_style=Patchwerk\nactions=/auto_attack\nhtml=test_task.html"

        with patch('botend.controller.plugins.simc.SimcMonitor.SimcComposer') as MockComposer:
            mock_instance = MagicMock()
            mock_instance.compose.return_value = (fixed_simc_content, {}, None)
            MockComposer.return_value = mock_instance

            executed_content = {}

            def capture_execution(simc_file_path, *_args):
                with open(simc_file_path, 'r', encoding='utf-8') as handle:
                    executed_content['value'] = handle.read()
                return True

            with patch.object(SimcMonitor, 'execute_simc_command', side_effect=capture_execution):
                with patch.object(SimcMonitor, 'apply_template') as mock_apply_template:
                    monitor = SimcMonitor(None, task)
                    monitor.result_path = '/tmp/simc_test_results'
                    os.makedirs(monitor.result_path, exist_ok=True)

                    monitor.process_simc_task(task)

                    MockComposer.assert_called_once_with(self.user_id)
                    self.assertEqual(mock_instance.compose.call_count, 1)
                    compose_call_args = mock_instance.compose.call_args[0][0]
                    self.assertEqual(compose_call_args['spec'], 'fury')
                    mock_apply_template.assert_not_called()
                    self.assertEqual(executed_content['value'], fixed_simc_content)

    def test_B_compose_failure_creates_independent_failed_run(self):
        """RED B: When compose() returns (None, None, error), must create failed Run with error_detail."""
        task = SimcTask.objects.create(
            user_id=self.user_id,
            name="Compose Failure Task",
            simc_profile_id=0,
            task_type=1,
            profile=self.profile,
            template=self.template,
            apl=self.apl,
            profile_version=self.profile_version,
            template_version=self.template_version,
            apl_version=self.apl_version,
            simulation_params={'spec': 'fury'},
            current_status=0,
            is_active=True,
        )

        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor

        compose_error = "Template rendering failed: missing required field"

        with patch('botend.controller.plugins.simc.SimcMonitor.SimcComposer') as MockComposer:
            mock_instance = MagicMock()
            mock_instance.compose.return_value = (None, None, compose_error)
            MockComposer.return_value = mock_instance

            with patch.object(SimcMonitor, 'execute_simc_command') as mock_execute:
                monitor = SimcMonitor(None, task)
                monitor.process_simc_task(task)

                # B1: execute_simc_command MUST NOT be called
                mock_execute.assert_not_called()

        # B2: Task marked as failed
        task.refresh_from_db()
        self.assertEqual(task.current_status, 3)

        # B3: SimulationRun created with status=failed and error_detail
        runs = SimulationRun.objects.filter(task=task)
        self.assertEqual(runs.count(), 1)
        run = runs.first()
        self.assertEqual(run.status, 'failed')
        self.assertIn(compose_error, run.error_detail)
        self.assertEqual(run.input_hash, '')  # Empty string when composition failed

    def test_C_exception_after_completed_run_must_not_modify_old_run(self):
        """RED C: If a completed Run exists, resolver/compose exception must NOT modify it."""
        task = SimcTask.objects.create(
            user_id=self.user_id,
            name="Completed Then Exception",
            simc_profile_id=0,
            task_type=1,
            profile=self.profile,
            template=self.template,
            apl=self.apl,
            profile_version=self.profile_version,
            template_version=self.template_version,
            apl_version=self.apl_version,
            simulation_params={'spec': 'fury'},
            current_status=2,  # Already completed
            is_active=True,
        )

        # Create pre-existing completed Run
        old_run = SimulationRun.objects.create(
            task=task,
            sequence=1,
            status='completed',
            input_hash='old_hash_123',
            resource_manifest={'old': 'manifest'},
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        old_run_id = old_run.id
        old_status = old_run.status
        old_hash = old_run.input_hash
        old_manifest = old_run.resource_manifest

        # Reset task to pending for retry
        task.current_status = 0
        task.save()

        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor

        # Mock the symbol actually used by SimcMonitor.
        with patch('botend.controller.plugins.simc.SimcMonitor.resolve_task') as mock_resolve:
            mock_resolve.side_effect = Exception("Resolver explosion")
            with patch.object(SimcMonitor, 'execute_simc_command') as mock_execute:
                monitor = SimcMonitor(None, task)
                monitor.process_simc_task(task)
            mock_execute.assert_not_called()

        # C1: Old run MUST remain unchanged
        old_run.refresh_from_db()
        self.assertEqual(old_run.id, old_run_id)
        self.assertEqual(old_run.status, old_status)
        self.assertEqual(old_run.input_hash, old_hash)
        self.assertEqual(old_run.resource_manifest, old_manifest)
        runs = list(SimulationRun.objects.filter(task=task).order_by('sequence'))
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0].id, old_run_id)
        self.assertEqual(runs[0].status, 'completed')
        self.assertEqual(runs[1].status, 'failed')
        self.assertIn('Resolver explosion', runs[1].error_detail)

        # C2: Task marked failed
        task.refresh_from_db()
        self.assertEqual(task.current_status, 3)

    def test_D_dynamic_content_from_version_payload_not_live(self):
        """RED D: Composed content must come from version payload, not live resources."""
        # Modify live resources AFTER creating versions
        self.profile.player_equipment = "warrior=\"ModifiedLiveWarrior\"\nlevel=90"
        self.profile.save()

        self.template.content = "warrior=\"ModifiedLiveTemplate\"\nlevel=90\n{player_config}"
        self.template.save()

        self.apl.content = "actions=/modified_live_action"
        self.apl.save()

        task = SimcTask.objects.create(
            user_id=self.user_id,
            name="Version Payload Task",
            simc_profile_id=0,
            task_type=1,
            profile=self.profile,
            template=self.template,
            apl=self.apl,
            profile_version=self.profile_version,  # Points to OLD version
            template_version=self.template_version,
            apl_version=self.apl_version,
            simulation_params={'spec': 'fury'},
            current_status=0,
            is_active=True,
        )

        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor

        # Capture what gets passed to composer
        captured_request = {}

        def capture_compose(request):
            captured_request.update(request)
            return ("warrior=\"ComposedFromVersion\"\nlevel=80\nactions=/rampage_versioned", {}, None)

        with patch('botend.controller.plugins.simc.SimcMonitor.SimcComposer') as MockComposer:
            mock_instance = MagicMock()
            mock_instance.compose.side_effect = capture_compose
            MockComposer.return_value = mock_instance

            with patch.object(SimcMonitor, 'execute_simc_command', return_value=True):
                monitor = SimcMonitor(None, task)
                monitor.result_path = '/tmp/simc_test_results'
                os.makedirs(monitor.result_path, exist_ok=True)
                monitor.process_simc_task(task)

        # D1: Request must contain versioned content, not live content
        self.assertIn('player_equipment', captured_request)
        self.assertIn('VersionedWarrior', captured_request['player_equipment'])
        self.assertNotIn('ModifiedLiveWarrior', captured_request['player_equipment'])

        self.assertIn('base_template_content', captured_request)
        # Template content has placeholders, not literal warrior definitions
        self.assertIn('{player_config}', captured_request['base_template_content'])
        self.assertNotIn('ModifiedLiveTemplate', captured_request['base_template_content'])

        self.assertIn('override_action_list', captured_request)
        self.assertIn('rampage_versioned', captured_request['override_action_list'])
        self.assertNotIn('modified_live_action', captured_request['override_action_list'])
        self.assertEqual(captured_request['player_import_mode'], 'manual_equipment')
        self.assertEqual(captured_request['talent'], 'versioned_talent_v1')

    def test_E_non_complete_reference_rejected_no_resolve_compose_execute(self):
        """RED E: Incomplete reference tasks must fail without calling resolve/compose/execute."""
        # Missing apl and apl_version
        task = SimcTask.objects.create(
            user_id=self.user_id,
            name="Incomplete Reference",
            simc_profile_id=0,
            task_type=1,
            profile=self.profile,
            template=self.template,
            # apl=self.apl,  # MISSING
            profile_version=self.profile_version,
            template_version=self.template_version,
            # apl_version=self.apl_version,  # MISSING
            simulation_params={'spec': 'fury'},
            current_status=0,
            is_active=True,
        )

        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor

        with patch('botend.controller.plugins.simc.SimcMonitor.resolve_task') as mock_resolve:
            with patch('botend.controller.plugins.simc.SimcMonitor.SimcComposer') as MockComposer:
                with patch.object(SimcMonitor, 'execute_simc_command') as mock_execute:
                    monitor = SimcMonitor(None, task)
                    monitor.process_simc_task(task)

                    # E1: resolve_task NOT called
                    mock_resolve.assert_not_called()

                    # E2: SimcComposer NOT instantiated
                    MockComposer.assert_not_called()

                    # E3: execute_simc_command NOT called
                    mock_execute.assert_not_called()

        # E4: Task marked failed
        task.refresh_from_db()
        self.assertEqual(task.current_status, 3)
        self.assertIn('完整引用', task.result_file)

    def test_F_retry_sequence_increments(self):
        """RED F: Each retry creates new Run with sequence+1."""
        task = SimcTask.objects.create(
            user_id=self.user_id,
            name="Retry Task",
            simc_profile_id=0,
            task_type=1,
            profile=self.profile,
            template=self.template,
            apl=self.apl,
            profile_version=self.profile_version,
            template_version=self.template_version,
            apl_version=self.apl_version,
            simulation_params={'spec': 'fury'},
            current_status=0,
            is_active=True,
        )

        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor

        # First execution
        with patch('botend.controller.plugins.simc.SimcMonitor.SimcComposer') as MockComposer:
            mock_instance = MagicMock()
            mock_instance.compose.return_value = ("warrior=\"First\"\nactions=/auto", {}, None)
            MockComposer.return_value = mock_instance

            with patch.object(SimcMonitor, 'execute_simc_command', return_value=True):
                monitor = SimcMonitor(None, task)
                monitor.result_path = '/tmp/simc_test_results'
                os.makedirs(monitor.result_path, exist_ok=True)
                monitor.process_simc_task(task)

        run1 = SimulationRun.objects.get(task=task, sequence=1)
        self.assertEqual(run1.status, 'completed')

        # Reset for retry
        task.current_status = 0
        task.save()

        # Second execution
        with patch('botend.controller.plugins.simc.SimcMonitor.SimcComposer') as MockComposer:
            mock_instance = MagicMock()
            mock_instance.compose.return_value = ("warrior=\"Second\"\nactions=/auto", {}, None)
            MockComposer.return_value = mock_instance

            with patch.object(SimcMonitor, 'execute_simc_command', return_value=True):
                monitor = SimcMonitor(None, task)
                monitor.result_path = '/tmp/simc_test_results'
                monitor.process_simc_task(task)

        # F1: Two runs with sequence 1 and 2
        runs = SimulationRun.objects.filter(task=task).order_by('sequence')
        self.assertEqual(runs.count(), 2)
        self.assertEqual(runs[0].sequence, 1)
        self.assertEqual(runs[1].sequence, 2)

        # F2: First run unchanged
        run1.refresh_from_db()
        self.assertEqual(run1.status, 'completed')

    def _make_reference_candidate(self, name, mode_params=None, candidate_label='candidate'):
        return SimcTask.objects.create(
            user_id=self.user_id,
            name=name,
            simc_profile_id=0,
            task_type=1,
            profile=self.profile,
            template=self.template,
            apl=self.apl,
            profile_version=self.profile_version,
            template_version=self.template_version,
            apl_version=self.apl_version,
            simulation_params={'spec': 'fury'},
            mode='comparison',
            mode_params=mode_params or {'candidate_type': 'base'},
            candidate_label=candidate_label,
            current_status=0,
            is_active=True,
        )

    def test_G_resolver_failure_creates_explicit_failed_run(self):
        """Every reference candidate gets a failed Run even when resolution fails."""
        task = self._make_reference_candidate('Resolver failure', candidate_label='resolver-failure')

        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor

        with patch('botend.controller.plugins.simc.SimcMonitor.resolve_task',
                   side_effect=Exception('resolver exploded')):
            monitor = SimcMonitor(None, task)
            self.assertFalse(monitor.process_simc_task(task))

        run = SimulationRun.objects.get(task=task)
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.candidate_label, 'resolver-failure')
        self.assertIn('resolver exploded', run.error_detail)

    def test_H_candidate_override_failure_creates_failed_run(self):
        """A malformed comparison/attribute candidate must still have its own Run."""
        task = self._make_reference_candidate(
            'Invalid attribute candidate',
            mode_params={
                'candidate_type': 'attribute_ratings',
                'attribute_ratings': {'crit': 100},
            },
            candidate_label='invalid-attributes',
        )

        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor

        with patch.object(SimcMonitor, 'execute_simc_command') as mock_execute:
            monitor = SimcMonitor(None, task)
            self.assertFalse(monitor.process_simc_task(task))
            mock_execute.assert_not_called()

        run = SimulationRun.objects.get(task=task)
        self.assertEqual(run.status, 'failed')
        self.assertEqual(run.candidate_label, 'invalid-attributes')
        self.assertIn('属性候选缺少', run.error_detail)

    def test_I_one_candidate_failure_does_not_change_another_candidate_run(self):
        """Runs are candidate-scoped: one compose failure cannot poison a sibling."""
        failed_task = self._make_reference_candidate('Failed candidate', candidate_label='failed')
        good_task = self._make_reference_candidate('Good candidate', candidate_label='good')

        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor

        with patch('botend.controller.plugins.simc.SimcMonitor.SimcComposer') as MockComposer:
            first = MagicMock()
            first.compose.return_value = (None, None, 'candidate compose failed')
            second = MagicMock()
            second.compose.return_value = ('warrior="good"\\nactions=/auto', {}, None)
            MockComposer.side_effect = [first, second]
            with patch.object(SimcMonitor, 'execute_simc_command', return_value=True):
                monitor = SimcMonitor(None, failed_task)
                self.assertFalse(monitor.process_simc_task(failed_task))
                monitor = SimcMonitor(None, good_task)
                self.assertTrue(monitor.process_simc_task(good_task))

        failed_run = SimulationRun.objects.get(task=failed_task)
        good_run = SimulationRun.objects.get(task=good_task)
        self.assertEqual(failed_run.status, 'failed')
        self.assertIn('candidate compose failed', failed_run.error_detail)
        self.assertEqual(good_run.status, 'completed')
        self.assertEqual(good_run.candidate_label, 'good')
