"""非饰品基准的逐装备对照、冻结结果和原版 SimC 输入回归。"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase as UnitTestCase
from unittest.mock import Mock, patch
import hashlib
import tempfile

from django.test import TestCase

from simc_equipment_control import (
    MARKER, control_key, mark_control_input, parse_equipment_export,
    prepare_control_input, synthetic_item,
)
from botend.models import SimcBenchmarkCandidate
from botend.services.simc_benchmark_config import build_execution_plan
from botend.services.simc_benchmark_execution import (
    reconcile_execution, serialize_incremental_panel_results, serialize_public_execution,
)
from botend.services.simc_task_service import _normalize_candidates, TaskCreationError
from botend.tests import test_simc_benchmark_execution as fixtures


class EquipmentControlInputTests(UnitTestCase):
    def test_native_readback_rejects_stat_drift(self):
        code = mark_control_input('warrior=x\nfinger1=id=123\n', 'finger1')
        def execute(command):
            options = dict(part.split('=', 1) for part in command[2:])
            is_control = '-control.simc' in command[1]
            amount = 101 if is_control else 100
            Path(options['save']).write_text('finger1=x,id=123\n# ilevel=289,quality=epic,stats=100haste\n')
            Path(options['output']).write_text(
                f'0.000 name=x slot=finger1 stats={{ +{amount} Haste }} source=Local')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, '不一致'):
                prepare_control_input(code, 'simc', directory, execute=execute)

    def test_agent_prepares_control_before_actual_simulation_and_renews_lease(self):
        from simc_agent_consumer import SimcAgentConsumer
        consumer = object.__new__(SimcAgentConsumer)
        consumer.config = SimpleNamespace(simc_path='simc', max_run_seconds=120)
        consumer.instance_id, consumer.agent_token = 'test', 'token'
        consumer.transport = Mock()
        consumer.transport.json.return_value = {'lease_expires_at': '2999-01-01T00:00:00+00:00'}
        consumer._lease_heartbeat_loop = Mock()
        consumer._complete = Mock()
        code = mark_control_input('warrior=x\nfinger1=id=123\nhtml=simc_task_1_run_1.html\n', 'finger1')
        executions = []
        def popen(command, **kwargs):
            process = Mock(returncode=0)
            process.poll.return_value = 0
            process.communicate.return_value = (b'', b'')
            options = dict(part.split('=', 1) for part in command[2:])
            if 'save' in options:
                Path(options['save']).write_text('finger1=x,id=123\n# ilevel=289,quality=epic,stats=100haste\n')
                Path(options['output']).write_text('0.000 name=x slot=finger1 stats={ +100 Haste } source=Local')
            else:
                executions.append((Path(kwargs['cwd']) / command[1]).read_text())
                (Path(kwargs['cwd']) / 'simc_task_1_run_1.html').write_text('<html>结果</html>')
            return process
        with patch('simc_agent_consumer.subprocess.Popen', side_effect=popen):
            consumer.execute_job({
                'run_id': 1, 'lease_token': 'test', 'input': code,
                'input_hash': hashlib.sha256(code.encode()).hexdigest(),
                'output_filename': 'simc_task_1_run_1.html', 'timeout_seconds': 120,
                'lease_expires_at': '2999-01-01T00:00:00+00:00',
            })
        self.assertEqual(len(executions), 1)
        self.assertIn('finger1=lmonitor_effect_control,', executions[0])
        self.assertNotIn('id=123', executions[0])
        self.assertNotIn(MARKER, executions[0])
        self.assertEqual(consumer._complete.call_args.args[3], 'completed')
        self.assertEqual(consumer.transport.json.call_count, 3)

    def test_native_export_preserves_resolved_armor_gems_and_weapon(self):
        profile = ('main_hand=real_weapon,id=123,ilevel=300,enchant_id=42\n'
                   '# quality=epic,stats=100str_200haste,weapon=axe2h_3.60speed_1min_2max\n')
        log = ('0.000 name=real_weapon id=123 slot=main_hand quality=epic ilevel=300 '
               'stats={ +100 Str, +200 Haste } damage={ 501.5 - 601.5 } speed=3.600 '
               'enchant={ test_enchant } source=Local')
        item = synthetic_item(parse_equipment_export(profile, log, 'main_hand'))
        self.assertIn('weapon=axe2h_3.600speed_501.5min_601.5max', item)
        self.assertIn('stats=200haste_100str', item)
        self.assertIn('enchant_id=42', item)
        self.assertNotIn('id=123', item)

    def test_marker_is_not_silently_executable_by_old_agents(self):
        code = mark_control_input('warrior=x\nwrists=id=123\n', 'wrists')
        self.assertIn('stats=lmonitor_unprepared', code)
        self.assertIn(MARKER, code)
        with self.assertRaises(ValueError):
            mark_control_input('warrior=x\ntrinket1=id=123', 'trinket1')
        self.assertEqual(prepare_control_input('unchanged', '', ''), 'unchanged')

    def test_control_options_cannot_be_forged_for_trinkets(self):
        with self.assertRaises(TaskCreationError):
            _normalize_candidates([{'candidate_params': {
                'candidate_type': 'gear_swap', 'equipment_effect_control': True,
                'gear_swap': {'slot': 'trinket1'},
            }}])


class EquipmentControlBenchmarkTests(TestCase):
    user_id = 701
    _create = fixtures.SimcBenchmarkExecutionTests._create
    _run = fixtures.SimcBenchmarkExecutionTests._run

    def setUp(self):
        fixtures.SimcBenchmarkExecutionTests.setUp(self)
        self.ring = SimcBenchmarkCandidate.objects.create(
            panel=self.panel, key='ring', label='测试戒指', candidate_type='gear_swap',
            params={'candidate_type': 'gear_swap', 'is_base': False,
                    'gear_swap': {'slot': 'finger1', 'raw_value': ',id=456,ilevel=289',
                                  'item_id': 456, 'item_level': 289, 'source': 'manual'}},
        )

    def finish(self, *, fail_control=False):
        execution = self._create()
        task = execution.cases.get().task
        task.current_status = 3 if fail_control else 2
        task.save(update_fields=['current_status'])
        dps = {'baseline': 1000, 'trinket': 1200, 'ring': 1600, control_key('ring'): 1500}
        for index, candidate in enumerate(task.mode_params['initial_candidates'], 1):
            key = candidate['candidate_key']
            status = 'failed' if fail_control and key == control_key('ring') else 'completed'
            self._run(task, index, status, key, dps=dps[key] if status == 'completed' else None)
        reconcile_execution(execution)
        self.panel.is_public = True
        self.panel.save(update_fields=['is_public'])
        return execution

    def test_slot_aliases_save_and_freeze_controls_without_execution(self):
        import base64
        import json
        from botend.models import SimcBenchmarkExecution, SimcBenchmarkCase, SimcTask
        from botend.services.simc_benchmark_config import replace_panel_config
        from botend.services.simc_player_config import EQUIPMENT_SLOT_ALIASES

        payload = {
            'name': 'Alias controls', 'slug': 'alias-controls',
            'schedule_enabled': False, 'is_public': False,
            'specs': [{'class_name': 'warrior', 'spec_key': 'warrior_fury',
                       'apl_id': self.apl.pk, 'template_id': self.template.pk,
                       'backend_id': self.backend.pk,
                       'profiles': [{'profile_id': self.profile.pk,
                                     'talent_string_id': self.talent.pk}]}],
            'scenarios': [{'key': 'patchwerk', 'name': 'Patchwerk'}],
            'candidates': [
                {'key': slot, 'candidate_type': 'gear_swap',
                 'params': {'slot': slot, 'raw_value': ',id=456,ilevel=321'}}
                for slot in ('wrist', 'wrists', 'shoulder', 'shoulders')
            ],
        }
        panel, plan = replace_panel_config(payload, self.user_id)
        rows = {row['candidate_key']: row for row in
                _normalize_candidates(plan['cases'][0]['candidates'])}
        for supplied_slot in ('wrist', 'wrists', 'shoulder', 'shoulders'):
            with self.subTest(slot=supplied_slot):
                canonical = EQUIPMENT_SLOT_ALIASES.get(supplied_slot, supplied_slot)
                params = rows[control_key(supplied_slot)]['candidate_params']
                self.assertTrue(params['equipment_effect_control'])
                self.assertEqual(params['gear_swap']['slot'], canonical)
                native = next(alias for alias, target in EQUIPMENT_SLOT_ALIASES.items()
                              if target == canonical)
                # Composer may retain either valid spelling in the equipment line.
                for line_slot in (canonical, native):
                    code = mark_control_input(
                        f'warrior=x\n{line_slot}=id=456,ilevel=321\n', canonical)
                    marker = next(line[len(MARKER):] for line in code.splitlines()
                                  if line.startswith(MARKER))
                    self.assertEqual(json.loads(base64.urlsafe_b64decode(marker))['slot'], native)
                # Native saved-profile/log names must resolve to the same slot.
                exported = parse_equipment_export(
                    f'{native}=x,id=456\n# ilevel=321,stats=100haste\n',
                    f'0.000 name=x slot={native} stats={{ +100 Haste }} source=Local',
                    canonical,
                )
                self.assertEqual(exported['stats'], {'haste': 100.0})
        self.assertFalse(panel.schedule_enabled)
        self.assertEqual(SimcBenchmarkExecution.objects.count(), 0)
        self.assertEqual(SimcBenchmarkCase.objects.count(), 0)
        self.assertEqual(SimcTask.objects.count(), 0)

    def test_mixed_panel_keeps_trinket_preset_and_adds_independent_control(self):
        trinket = self.panel.candidates.get(key='trinket')
        trinket.params['benchmark_profile'] = {'kind': 'trinket_standard_reference', 'item_level': 240}
        trinket.save(update_fields=['params'])
        plan = build_execution_plan(self.panel)
        rows = {row['candidate_key']: row for row in plan['cases'][0]['candidates']}
        self.assertEqual(plan['run_count'], 4)
        self.assertEqual(rows['baseline']['candidate_params']['equipment_preset'],
                         rows['trinket']['candidate_params']['equipment_preset'])
        self.assertNotIn('equipment_preset', rows['ring']['candidate_params'])
        self.assertEqual(rows['ring']['candidate_params']['effect_baseline_key'], control_key('ring'))
        self.assertTrue(rows[control_key('ring')]['candidate_params']['equipment_effect_control'])
        self.assertEqual(rows['ring']['candidate_params']['gear_swap'],
                         rows[control_key('ring')]['candidate_params']['gear_swap'])

    def test_two_item_levels_have_separate_controls_but_same_display_group(self):
        from botend.services.simc_benchmark_execution import _candidate_item_variant_key
        params = deepcopy(self.ring.params)
        params['gear_swap'].update(raw_value=',id=456,ilevel=300', item_level=300)
        SimcBenchmarkCandidate.objects.create(
            panel=self.panel, key='ring-300', label='测试戒指 300',
            candidate_type='gear_swap', params=params,
        )
        rows = {row['candidate_key']: row for row in build_execution_plan(self.panel)['cases'][0]['candidates']}
        self.assertNotEqual(rows['ring']['candidate_params']['effect_baseline_key'],
                            rows['ring-300']['candidate_params']['effect_baseline_key'])
        self.assertEqual(_candidate_item_variant_key(rows['ring']), _candidate_item_variant_key(rows['ring-300']))

    def test_composer_marks_only_control_and_local_worker_executes_prepared_input(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        from botend.services.simc_run_control import build_frozen_run_input
        from botend.models import SimulationRun
        self.profile.player_config_mode = 'manual_equipment'
        self.profile.player_equipment = 'warrior="测试"\nlevel=90\nspec=fury\nfinger1=,id=456\ntrinket1=,id=123'
        self.profile.save()
        self.template.content = '{player_config}\n{action_list}\n{simulation_options}\n{output_options}'
        self.template.save()
        execution = self._create()
        task = execution.cases.get().task
        control = next(row for row in task.mode_params['initial_candidates']
                       if row['candidate_key'] == control_key('ring'))
        run = SimulationRun.objects.create(
            task=task, sequence=1, candidate_key=control_key('ring'),
            candidate_params=control['candidate_params'], status='pending',
        )
        code, _ = build_frozen_run_input(task, run)
        self.assertIn(MARKER, code)
        self.assertIn('stats=lmonitor_unprepared', code)
        monitor = object.__new__(SimcMonitor)
        monitor._save_run_for_active_claim = Mock(return_value=True)
        monitor._active_task_claim_is_current = Mock(return_value=True)
        monitor._save_task_fields = Mock(return_value=True)
        monitor.mark_task_failed = Mock()
        prepared = 'warrior="测试"\nfinger1=lmonitor_effect_control,stats=100haste\n'
        executed = []
        def execute(path, *_args):
            executed.append(Path(path).read_text(encoding='utf-8'))
            return True
        monitor.execute_simc_command = execute
        with tempfile.TemporaryDirectory() as directory, patch(
            'simc_equipment_control.prepare_control_input', return_value=prepared,
        ) as prepare:
            monitor.result_path = directory
            self.assertTrue(monitor.process_reference_run(task, run))
            self.assertIn(MARKER, prepare.call_args.args[0])
        self.assertEqual(executed, [prepared])
        monitor.mark_task_failed.assert_not_called()

    def test_live_and_historical_results_use_own_control_and_hide_internal_row(self):
        execution = self.finish()
        live = serialize_incremental_panel_results(self.panel)
        rows = live['coordinates'][0]['candidates']
        self.assertEqual({row['key'] for row in rows}, {'baseline', 'trinket', 'ring'})
        ring = next(row for row in rows if row['key'] == 'ring')
        self.assertEqual(ring['baseline_dps'], 1500)
        self.assertEqual(ring['gain_dps'], 100)
        self.assertAlmostEqual(ring['gain_percent'], 100 / 15)
        historical = serialize_public_execution(execution)
        self.assertEqual(historical['status'], 'ready')
        # 历史投影的冻结定义必须包含对照，且页面不能把对照当作装备。
        self.assertIn(control_key('ring'), [row['key'] for row in execution.config_snapshot['candidates']])
        self.assertNotIn('equipment_effect_control', str(historical))

    def test_missing_control_is_not_compared_with_common_baseline_and_is_supplemented(self):
        self.finish(fail_control=True)
        live = serialize_incremental_panel_results(self.panel)
        self.assertNotIn('ring', [row['key'] for row in live['coordinates'][0]['candidates']])
        supplement = self._create()
        task = supplement.cases.get().task
        self.assertEqual([row['candidate_key'] for row in task.mode_params['initial_candidates']],
                         [control_key('ring')])
        task.current_status = 2
        task.save(update_fields=['current_status'])
        self._run(task, 1, 'completed', control_key('ring'), dps=1500)
        reconcile_execution(supplement)
        rows = serialize_incremental_panel_results(self.panel)['coordinates'][0]['candidates']
        ring = next(row for row in rows if row['key'] == 'ring')
        self.assertEqual(ring['baseline_dps'], 1500)
        self.assertEqual(ring['gain_dps'], 100)
