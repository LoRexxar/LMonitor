import hashlib
import json
import os
from unittest.mock import MagicMock, patch

from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

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
    def test_manual_export_without_explicit_stats_does_not_emit_overrides(self):
        profile = SimcProfile.objects.create(
            user_id=1, name='addon', spec='fury', player_config_mode='manual_equipment',
            player_equipment='warrior="Tester"\nhead=,id=1\noff_hand=,id=2',
        )
        payload = _build_profile_payload(profile)
        self.assertIsNone(payload['gear_strength'])
        self.assertIsNone(payload['gear_crit'])
        self.assertIsNone(payload['gear_haste'])
        self.assertIsNone(payload['gear_mastery'])
        self.assertIsNone(payload['gear_versatility'])

    def test_manual_profile_freezes_and_composes_explicit_talent_and_secondary_overrides(self):
        profile = SimcProfile.objects.create(
            user_id=1, name='manual overrides', spec='fury',
            player_config_mode='manual_equipment',
            player_equipment=(
                'warrior="Tester"\nspec=fury\n'
                'talents=STALE_BUILD\nhead=,id=1\n'
                'gear_crit_rating=1\ngear_haste_rating=2\n'
                'gear_mastery_rating=3\ngear_versatility_rating=4'
            ),
            talent='SAVED_BUILD', gear_strength=93330,
            gear_crit=10730, gear_haste=18641,
            gear_mastery=21785, gear_versatility=6757,
        )

        payload = _build_profile_payload(profile)
        self.assertEqual(payload['gear_strength'], 93330)
        self.assertEqual(payload['gear_crit'], 10730)
        self.assertEqual(payload['gear_haste'], 18641)
        self.assertEqual(payload['gear_mastery'], 21785)
        self.assertEqual(payload['gear_versatility'], 6757)

        final, _manifest, error = SimcComposer(1).compose({
            **payload,
            'player_import_mode': payload['player_config_mode'],
            'base_template_content': '{player_config}',
            '_result_file_path': 'simc/result.html',
        })
        self.assertIsNone(error)
        self.assertEqual(final.splitlines().count('talents=SAVED_BUILD'), 1)
        self.assertNotIn('STALE_BUILD', final)
        self.assertEqual(final.splitlines().count('gear_strength=93330'), 1)
        self.assertEqual(final.splitlines().count('gear_crit_rating=10730'), 1)
        self.assertEqual(final.splitlines().count('gear_haste_rating=18641'), 1)
        self.assertEqual(final.splitlines().count('gear_mastery_rating=21785'), 1)
        self.assertEqual(final.splitlines().count('gear_versatility_rating=6757'), 1)
        rendered_lines = set(final.splitlines())
        for stale in ('gear_crit_rating=1', 'gear_haste_rating=2',
                      'gear_mastery_rating=3', 'gear_versatility_rating=4'):
            self.assertNotIn(stale, rendered_lines)

    def test_attribute_composer_emits_explicit_primary_stat_override(self):
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
        self.assertEqual(final.splitlines().count('gear_strength=0'), 1)
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
            name='contract template', spec='fury',
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

    def test_real_run_completion_cannot_resurrect_a_recovered_task(self):
        task = self.make_task()

        def finish_after_recovery(_path, executing_task, _result):
            from botend.models import SimcTask
            SimcTask.objects.filter(pk=executing_task.pk).update(
                current_status=3,
                error_detail='stale execution recovered',
            )
            return True

        with patch.object(SimcMonitor, 'execute_simc_command', side_effect=finish_after_recovery):
            monitor = SimcMonitor(None, task)
            monitor.result_path = '/tmp/simc_contract_results'
            os.makedirs(monitor.result_path, exist_ok=True)
            self.assertFalse(monitor.process_simc_task(task))

        task.refresh_from_db()
        self.assertEqual(task.current_status, 3)
        self.assertEqual(task.error_detail, 'stale execution recovered')

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
        from botend.services.simc_task_service import initialize_task_runs
        initialize_task_runs(task)
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

    def test_attribute_worker_appends_and_drains_search_rounds_without_client_callback(self):
        """One queued attribute Task owns its complete server-side search lifecycle."""
        from botend.dashboard.api import SimcComparisonTaskAPIView

        rows = SimcComparisonTaskAPIView._attribute_variants(
            {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}, 100,
        )
        candidates = [{
            'candidate_key': f'round-1-candidate-{index}',
            'candidate_label': label,
            'round_number': 1,
            'candidate_params': {
                'candidate_type': 'attribute_ratings', 'is_base': is_base,
                'attribute_ratings': ratings, 'search': candidate,
            },
        } for index, (label, ratings, is_base, candidate) in enumerate(rows)]
        task = self.make_task(
            name='automatic attribute search', mode='attribute_sweep', candidates=candidates,
        )
        monitor = SimcMonitor(None, task)

        def complete_run(_task, run):
            # No precision level improves the centre, so the worker drains 100 -> 50 -> 20.
            run.status = 'completed'
            run.result_summary = {
                'dps': 100000,
            }
            run.save(update_fields=['status', 'result_summary'])
            return True

        with patch.object(monitor, 'process_reference_run', side_effect=complete_run):
            self.assertTrue(monitor.process_reference_task(task))

        task.refresh_from_db()
        self.assertEqual(task.simulation_runs.filter(round_number=1).count(), len(rows))
        self.assertEqual(task.simulation_runs.filter(round_number=2).count(), len(rows))
        self.assertEqual(task.simulation_runs.filter(round_number=3).count(), len(rows))
        self.assertFalse(task.simulation_runs.filter(status='pending').exists())
        self.assertTrue(task.analysis_result['attribute_search']['converged'])
        self.assertEqual(
            task.analysis_result['attribute_search']['stop_reason'],
            'local_optimum_20_pairwise',
        )

    def _make_completed_attribute_round(self, task, center, round_number, winner_index=1):
        from botend.services.simc_attribute_search import attribute_variants

        created = []
        sequence = task.simulation_runs.count() + 1
        for index, (label, ratings, is_base, search) in enumerate(
            attribute_variants(center, round_number=round_number)
        ):
            created.append(SimulationRun.objects.create(
                task=task,
                sequence=sequence + index,
                candidate_key=f'round-{round_number}-candidate-{index}',
                candidate_label=label,
                round_number=round_number,
                candidate_params={
                    'candidate_type': 'attribute_ratings',
                    'is_base': is_base,
                    'attribute_ratings': ratings,
                    'search': search,
                },
                status='completed',
                result_summary={'dps': 101500 if index == winner_index else 100000},
            ))
        return created

    def test_attribute_search_stops_when_best_center_was_already_visited(self):
        from botend.services.simc_attribute_search import advance_attribute_search, attribute_variants

        center = {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}
        winner = attribute_variants(center, round_number=2)[1][1]
        task = self.make_task(name='cycle search', mode='attribute_sweep')
        SimulationRun.objects.create(
            task=task,
            sequence=1,
            candidate_key='round-1-base',
            candidate_label='visited center',
            round_number=1,
            candidate_params={
                'candidate_type': 'attribute_ratings',
                'is_base': True,
                'attribute_ratings': winner,
                'search': {'round': 1, 'step': 50},
            },
            status='completed',
            result_summary={'dps': 99000},
        )
        self._make_completed_attribute_round(task, center, round_number=2)
        lease = timezone.now()
        task.current_status = 1
        task.started_at = lease
        task.save(update_fields=['current_status', 'started_at'])

        result = advance_attribute_search(task.id, expected_started_at=lease)

        task.refresh_from_db()
        self.assertTrue(result['converged'])
        self.assertEqual(result['recommendation']['stop_reason'], 'cycle_detected')
        self.assertEqual(task.analysis_result['attribute_search']['stop_reason'], 'cycle_detected')
        self.assertEqual(task.simulation_runs.count(), 14)

    def test_attribute_search_stops_at_max_round_without_appending_runs(self):
        from botend.services.simc_attribute_search import advance_attribute_search

        center = {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}
        task = self.make_task(name='max round search', mode='attribute_sweep')
        self._make_completed_attribute_round(task, center, round_number=20)
        lease = timezone.now()
        task.current_status = 1
        task.started_at = lease
        task.save(update_fields=['current_status', 'started_at'])

        result = advance_attribute_search(task.id, expected_started_at=lease)

        task.refresh_from_db()
        self.assertTrue(result['converged'])
        self.assertEqual(result['recommendation']['stop_reason'], 'max_rounds_reached')
        self.assertEqual(task.analysis_result['attribute_search']['stop_reason'], 'max_rounds_reached')
        self.assertEqual(task.simulation_runs.count(), 13)

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
        from botend.services.simc_task_service import initialize_task_runs
        initialize_task_runs(task)
        self.assertTrue(SimulationRun.objects.filter(task=task, sequence=1).exists())
        with self.assertRaises(IntegrityError):
            with __import__('django.db', fromlist=['transaction']).transaction.atomic():
                SimulationRun.objects.create(task=task, sequence=1)
