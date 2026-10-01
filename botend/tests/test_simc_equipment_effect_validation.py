"""Frozen activation contracts, completion reuse and both gain projections."""
from contextlib import ExitStack, nullcontext
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from botend.tests.test_simc_equipment_result_evidence import native_html, PARAMS


def expectation(slot='neck', item_id=987, bonus_ids=None, events=None):
    return {'slot': slot, 'item_id': item_id, 'game_build': '12.0.1.70077',
            'required_bonus_ids': [123, 456] if bonus_ids is None else bonus_ids,
            'driver_spell_ids': [9001], 'event_spell_ids': [9002, 9003] if events is None else events,
            'fact_hash': 'a' * 64}


def frozen_params():
    return {**deepcopy(PARAMS), 'equipment_effect_policy': {'target_slots': ['neck']},
            'equipment_effect_expectation': {'schema_version': 1, 'targets': [expectation()]}}


class EquipmentEffectValidationTests(SimpleTestCase):
    def validate(self, html=None, params=None):
        from botend.services.simc_equipment_effect_validation import validate_equipment_effect_report
        return validate_equipment_effect_report(native_html() if html is None else html,
                                                frozen_params() if params is None else params)

    def test_frozen_ids_required_bonus_readback_and_input_immutability(self):
        params = frozen_params()
        params['gear_swap']['raw_value'] = ',id=987,bonus_id=123'
        before = deepcopy(params)
        valid = self.validate(params=params)
        self.assertEqual(valid['status'], 'valid')
        self.assertEqual(valid['expected_spell_ids']['driver'], [9002, 9003])
        missing_bonus = self.validate(native_html(profile='neck=x,id=987,bonus_id=123'), params)
        self.assertEqual(missing_bonus['status'], 'invalid')
        self.assertIn('target_bonus_mismatch', missing_bonus['reason_codes'])
        self.assertEqual(params, before)
        self.assertEqual(json.loads(json.dumps(valid, allow_nan=False)), valid)

    def test_unknown_legacy_and_incomplete_report_are_unverified(self):
        for params in (None, {}, PARAMS, {**PARAMS, 'equipment_effect_expectation': None}):
            with self.subTest(params=params):
                # None is deliberately passed, not replaced with the default fixture.
                from botend.services.simc_equipment_effect_validation import validate_equipment_effect_report
                result = validate_equipment_effect_report(native_html(), params)
                self.assertEqual(result['status'], 'unverified')
        self.assertEqual(self.validate('<html></html>')['status'], 'unverified')
        self.assertEqual(self.validate(native_html(damage=0, triggers=0, uptime=0))['status'], 'unverified')

    def test_expectation_shape_identity_and_slot_alias_validation(self):
        for mutation in (
            lambda p: p.update(equipment_effect_expectation=[]),
            lambda p: p['equipment_effect_expectation'].update(schema_version=True),
            lambda p: p['equipment_effect_expectation']['targets'][0].update(item_id=986),
            lambda p: p['equipment_effect_expectation']['targets'][0].update(slot='head'),
            lambda p: p['equipment_effect_expectation']['targets'][0].update(game_build='70077'),
            lambda p: p['equipment_effect_expectation']['targets'][0].update(fact_hash='not-a-hash'),
            lambda p: p['equipment_effect_expectation']['targets'][0].update(event_spell_ids=[True]),
            lambda p: p['equipment_effect_expectation']['targets'][0].pop('required_bonus_ids'),
        ):
            params = frozen_params()
            mutation(params)
            with self.subTest(params=params):
                self.assertEqual(self.validate(params=params)['status'], 'invalid')
        params = frozen_params()
        params['gear_swap']['slot'] = 'wrist'
        params['equipment_effect_expectation']['targets'][0]['slot'] = 'wrists'
        self.assertEqual(self.validate(native_html(profile='wrists=x,id=987,bonus_id=123/456'), params)['status'], 'valid')

    def test_every_combination_target_needs_expectation_and_effective_event(self):
        params = frozen_params()
        params['gear_swaps'] = [params.pop('gear_swap'),
                               {'slot': 'head', 'item_id': 986, 'raw_value': ',id=986,bonus_id=789'}]
        profile = 'neck=x,id=987,bonus_id=123/456\nhead=x,id=986,bonus_id=789'
        self.assertEqual(self.validate(native_html(profile=profile), params)['status'], 'unverified')
        params['equipment_effect_expectation']['targets'].append(expectation('head', 986, [789], [7777]))
        self.assertEqual(self.validate(native_html(profile=profile), params)['status'], 'unverified')
        params['equipment_effect_expectation']['targets'][1]['event_spell_ids'] = [9003]
        self.assertEqual(self.validate(native_html(profile=profile), params)['status'], 'valid')
        params['equipment_effect_expectation']['targets'][1]['required_bonus_ids'] = [789, 790]
        self.assertEqual(self.validate(native_html(profile=profile), params)['status'], 'invalid')

    def test_control_background_same_event_is_unknown_not_invalid(self):
        params = frozen_params()
        params['equipment_effect_control'] = True
        shared = self.validate(native_html(profile='neck=custom,stats=10crit\nhead=background,id=986'), params)
        self.assertEqual(shared['status'], 'unverified')
        self.assertIn('control_effect_source_unverified', shared['reason_codes'])
        inactive = self.validate(native_html(profile='neck=custom,stats=10crit', damage=0, triggers=0, uptime=0), params)
        self.assertEqual(inactive['status'], 'valid')
        self.assertEqual(self.validate(params=params)['status'], 'invalid')


class EquipmentEffectCompletionHookTests(SimpleTestCase):
    def test_agent_reuses_downloaded_html_without_affecting_dps_or_terminal_status(self):
        from botend.services import simc_run_control as control
        params = frozen_params()
        task = Mock(pk=7, execution_owner=control.SimcTask.EXECUTION_OWNER_AGENT)
        run = Mock(pk=8, task=task, task_id=7, status='running', candidate_params=params,
                   lease_token_hash='fence')
        metadata = {'status': 'completed', 'lease_token': 'a' * 43, 'instance_id': 'instance',
                    'completion_id': 'completed-1', 'stdout': 'DPS=1234', 'stderr': '',
                    'report': {'object_key': 'bound.html', 'size': 123, 'sha256': 'b' * 64}}
        run_queryset = Mock()
        run_queryset.filter.return_value.first.return_value = run
        run_queryset.get.return_value = run
        with ExitStack() as stack:
            for name, value in (
                ('authenticate_bearer', Mock(pk=1)), ('validate_completion_metadata', metadata),
                ('_validate_fence', None), ('_finalize_task', None), ('reconcile_execution_for_task', None),
            ):
                stack.enter_context(patch.object(control, name, return_value=value))
            stack.enter_context(patch.object(control.transaction, 'atomic', side_effect=nullcontext))
            stack.enter_context(patch.object(control.SimulationRun.objects, 'select_related', return_value=run_queryset))
            stack.enter_context(patch.object(control.SimulationRun.objects, 'select_for_update', return_value=run_queryset))
            task_query = stack.enter_context(patch.object(control.SimcTask.objects, 'select_for_update'))
            task_query.return_value.get.return_value = task
            stack.enter_context(patch.object(control.SimcTaskArtifact.objects, 'update_or_create'))
            for name in ('task_has_active_panel_purge', 'artifact_key_has_active_panel_purge'):
                stack.enter_context(patch('botend.services.simc_benchmark_purge.' + name, return_value=False))
            stack.enter_context(patch('botend.services.simc_agent_oss.object_key_for_run', return_value='bound.html'))
            stack.enter_context(patch('botend.services.simc_agent_oss.public_report_url', return_value='https://example.com/bound.html'))
            stack.enter_context(patch('botend.services.simc_agent_oss.verify_uploaded_report'))
            download = stack.enter_context(patch('botend.services.simc_agent_oss.download_report_html', return_value=(native_html(), 'b' * 64)))
            stack.enter_context(patch('botend.controller.plugins.simc.SimcMonitor.SimcMonitor.validate_simulation_semantics', return_value={'valid': True, 'dps': 1234}))
            result = control.complete_run(8, metadata, 'Bearer token')
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(run.result_summary['dps'], 1234)
            self.assertTrue(run.result_summary['valid'])
            self.assertEqual(run.result_summary['equipment_effect_validation']['status'], 'valid')
            download.assert_called_once()
            frozen = deepcopy(run.result_summary)
            duplicate = control.complete_run(8, metadata, 'Bearer token')
            self.assertTrue(duplicate['idempotent'])
            self.assertEqual(run.result_summary, frozen)
            download.assert_called_once()

    def test_local_reuses_html_and_scopes_validation_to_equipment_effect_runs(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        monitor = object.__new__(SimcMonitor)
        monitor._active_task_claim_is_current = Mock(return_value=True)
        monitor._save_task_fields = Mock(return_value=True)
        backend = SimpleNamespace(simc_path='/fixture/simc', identifier='fixture')
        task = SimpleNamespace(id=7, result_file='report.html', ext='{}', result_summary='',
                               backend=backend, backend_id=1)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            monitor.result_path = directory
            stack.enter_context(patch('botend.services.simc_artifacts.result_filename_for_run', return_value='report.html'))
            stack.enter_context(patch('botend.controller.plugins.simc.SimcMonitor.os.path.isfile', side_effect=lambda p: p == backend.simc_path or Path(p).is_file()))
            stack.enter_context(patch('botend.controller.plugins.simc.SimcMonitor.os.access', return_value=True))
            stack.enter_context(patch('botend.interface.ossupload.ossUpload', return_value=True))
            popen = stack.enter_context(patch('botend.controller.plugins.simc.SimcMonitor.subprocess.Popen'))
            process = popen.return_value
            process.returncode = 0
            def communicate(**kwargs):
                Path(directory, 'report.html').write_text(native_html(), encoding='utf-8')
                return 'Player: Test\nDPS=1234\n  bloodthirst Count=10 pDPS=1234', ''
            process.communicate.side_effect = communicate
            for params, expected_status in ((frozen_params(), 'valid'), (PARAMS, None),
                                            ({**PARAMS, 'effect_baseline_key': 'control'}, 'unverified')):
                with self.subTest(params=params):
                    monitor._active_run = SimpleNamespace(candidate_params=params)
                    self.assertTrue(monitor.execute_simc_command('fixture.simc', task))
                    summary = json.loads(task.result_summary)
                    self.assertTrue(summary['valid'])
                    self.assertEqual(summary['dps'], 1234)
                    if expected_status is None:
                        self.assertNotIn('equipment_effect_validation', summary)
                    else:
                        self.assertEqual(summary['equipment_effect_validation']['status'], expected_status)


class EquipmentEffectProjectionTests(TestCase):
    """Use existing isolated SQLite benchmark fixtures, never live business data."""
    def setUp(self):
        from botend.tests.test_simc_equipment_control import EquipmentControlBenchmarkTests
        self.fixtures = EquipmentControlBenchmarkTests()
        self.fixtures.setUp()

    def test_both_projections_consume_frozen_run_validation_and_unknown_legacy(self):
        from botend.models import SimulationRun
        from botend.services.simc_benchmark_execution import serialize_incremental_panel_results, serialize_public_execution
        execution = self.fixtures.finish()
        run = SimulationRun.objects.get(task=execution.cases.get().task, candidate_key='ring')
        for validation in (None, {'schema_version': 1, 'status': 'unverified', 'valid': None},
                           {'schema_version': 1, 'status': 'invalid', 'valid': False},
                           {'schema_version': 1, 'status': 'valid', 'valid': True}):
            summary = deepcopy(run.result_summary)
            if validation is None:
                summary.pop('equipment_effect_validation', None)
            else:
                summary['equipment_effect_validation'] = validation
            SimulationRun.objects.filter(pk=run.pk).update(result_summary=summary)
            live = serialize_incremental_panel_results(self.fixtures.panel)['coordinates'][0]['candidates']
            public = serialize_public_execution(execution)['execution']['cases'][0]['candidates']
            for rows in (live, public):
                row = next(row for row in rows if row['key'] == 'ring')
                self.assertEqual(row['dps'], 1600)
                self.assertEqual(row['effect_validation']['status'], validation['status'] if validation else 'unverified')
                if validation and validation['status'] == 'valid':
                    self.assertAlmostEqual(row['effect_delta_percent'], 100 / 15)
                else:
                    self.assertNotIn('effect_delta_percent', row)
                for ordinary in (item for item in rows if item['key'] != 'ring'):
                    self.assertNotIn('effect_validation', ordinary)
                    self.assertNotIn('effect_delta_percent', ordinary)
            run.refresh_from_db()
            self.assertEqual(run.status, 'completed')
            self.assertEqual(run.result_summary['dps'], 1600)
