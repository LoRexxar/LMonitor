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
    prepare_control_input, synthetic_item, mark_equipment_input, equipment_rules,
    embellishment_count,
)
from botend.models import SimcBenchmarkCandidate, WowItemSnapshot
from botend.services.simc_benchmark_config import build_execution_plan, _normalize_candidate_params
from botend.services.simc_benchmark_execution import (
    reconcile_execution, serialize_incremental_panel_results, serialize_public_execution,
)
from botend.services.simc_task_service import _normalize_candidates, TaskCreationError
from botend.tests import test_simc_benchmark_execution as fixtures


class EquipmentControlInputTests(UnitTestCase):
    def combination_probe(self, command):
        import re
        options = dict(part.split('=', 1) for part in command[2:])
        profile, records = [], []
        for line in Path(command[1]).read_text(encoding='utf-8').splitlines():
            slot, sep, value = line.partition('=')
            if not sep or slot not in ('wrists', 'back', 'feet', 'finger1', 'trinket2'):
                continue
            item_id = re.search(r'\bid=(\d+)', value)
            effect = ' proc_spells={ proc=OnEquip/1283697 }' if 'embellishment=arcanoweave_lining' in value else ''
            profile.extend([line, '# ilevel=289,quality=epic,stats=100haste'])
            records.append(f'0.000 name=x slot={slot} stats={{ +100 Haste }} source=Local{effect}')
        Path(options['save']).write_text('\n'.join(profile), encoding='utf-8')
        Path(options['output']).write_text('\n'.join(records), encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout='', stderr='')

    def test_combination_clears_two_background_embellishments_in_both_groups(self):
        code = ('warrior=x\nwrists=,id=123,ilevel=289\n'
                'back=,id=456,ilevel=289,embellishment=arcanoweave_lining\n'
                'feet=,id=789,ilevel=289,embellishment=arcanoweave_lining\n'
                'finger1=,id=111,ilevel=289,embellishment=arcanoweave_lining\n')
        results = []
        with tempfile.TemporaryDirectory() as directory:
            for control in (False, True):
                marked = mark_equipment_input(code, ['wrists', 'finger1'], control=control, rules=equipment_rules())
                result = prepare_control_input(marked, 'simc', directory, execute=self.combination_probe)
                results.append(result)
                self.assertIn('back=lmonitor_effect_control', result)
                self.assertIn('feet=lmonitor_effect_control', result)
                self.assertNotIn(MARKER, result)
        self.assertEqual(results[0].count('embellishment='), 1)
        self.assertEqual(results[1].count('embellishment='), 0)
        self.assertIn('wrists=lmonitor_effect_control', results[1])

    def test_three_target_embellishments_and_stacked_sources_are_rejected(self):
        code = 'warrior=x\n' + '\n'.join(f'{slot}=,id={100+i},ilevel=289,embellishment=arcanoweave_lining'
                                          for i, slot in enumerate(('wrists', 'back', 'feet'))) + '\n'
        with tempfile.TemporaryDirectory() as directory:
            marked = mark_equipment_input(code, ['wrists', 'back', 'feet'], control=False, rules=equipment_rules())
            with self.assertRaisesRegex(ValueError, '超过两件美化'):
                prepare_control_input(marked, 'simc', directory, execute=self.combination_probe)
        item = parse_equipment_export(
            'wrists=x,id=123,bonus_id=12384,embellishment=arcanoweave_lining\n# stats=100haste',
            '0.000 name=x slot=wrists stats={ +100 Haste } source=Local', 'wrists')
        self.assertEqual(embellishment_count(item, equipment_rules()), 2)

    def test_intrinsic_and_unknown_embellishment_identity(self):
        item = parse_equipment_export('wrists=x,id=123\n# stats=100haste',
            '0.000 name=x slot=wrists stats={ +100 Haste } source=Local proc_spells={ proc=OnEquip/1251815 }', 'wrists')
        self.assertEqual(embellishment_count(item, equipment_rules()), 1)
        item['record'] = '0.000 name=x slot=wrists stats={ +100 Haste } source=Local'
        item['options']['id'] = '239660'
        self.assertEqual(embellishment_count(item, equipment_rules()), 1)
        item['options']['embellishment'] = 'unknown_future_effect'
        with self.assertRaisesRegex(ValueError, '缺少已核实规则'):
            embellishment_count(item, equipment_rules())

    def test_target_set_is_disabled_but_class_set_override_is_preserved(self):
        rules = deepcopy(equipment_rules())
        rules['sets'] = [{'name': 'arcanoweave_trappings', 'pieces': 2, 'items': [123, 456]}]
        code = 'warrior=x\nwrists=,id=123,ilevel=289\nback=,id=456,ilevel=289\nset_bonus=midnight_season_1_4pc=1\n'
        marked = mark_equipment_input(code, ['wrists', 'back'], control=True, rules=rules)
        with tempfile.TemporaryDirectory() as directory:
            result = prepare_control_input(marked, 'simc', directory, execute=self.combination_probe)
        self.assertIn('set_bonus=name=arcanoweave_trappings,pc=2,enable=0', result)
        self.assertIn('set_bonus=midnight_season_1_4pc=1', result)

    def test_removing_third_crafted_piece_keeps_selected_two_piece_set_active(self):
        code = 'warrior=x\nwrists=,id=239660,ilevel=289\nback=,id=239661,ilevel=289\nfeet=,id=239662,ilevel=289\n'
        marked = mark_equipment_input(code, ['wrists', 'back'], control=False, rules=equipment_rules())
        with tempfile.TemporaryDirectory() as directory:
            result = prepare_control_input(marked, 'simc', directory, execute=self.combination_probe)
        self.assertIn('feet=lmonitor_effect_control', result)
        self.assertNotIn('set_bonus=name=arcanoweave_trappings', result)

    def test_intrinsic_crafted_trinket_is_removed_from_background(self):
        code = 'warrior=x\nwrists=,id=123,ilevel=289\ntrinket2=,id=241340,ilevel=289\n'
        marked = mark_equipment_input(code, ['wrists'], control=False, rules=equipment_rules())
        with tempfile.TemporaryDirectory() as directory:
            result = prepare_control_input(marked, 'simc', directory, execute=self.combination_probe)
        self.assertIn('trinket2=lmonitor_effect_control', result)

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
        code = mark_equipment_input('warrior=x\nfinger1=id=123\nhtml=simc_task_1_run_1.html\n',
                                    ['finger1'], control=True, rules=equipment_rules())
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
        for item_id, inventory, subclass in ((123, 12, 0), (456, 11, 0), (239660, 9, 4), (239661, 16, 1)):
            WowItemSnapshot.objects.create(item_id=item_id, item_class_id=4,
                inventory_type=inventory, item_subclass_id=subclass,
                metadata={'primary_stat_options': ['strength', 'agility', 'intellect']})
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

    def test_multiline_candidate_is_safe_and_canonical_roundtrip_preserves_all_items(self):
        code = 'wrists=,id=239660,ilevel=289\nback=,id=239661,ilevel=289,embellishment=arcanoweave_lining'
        params = _normalize_candidate_params('gear_swap', code)
        self.assertEqual(_normalize_candidate_params('gear_swap', params), params)
        self.assertEqual({row['slot'] for row in params['gear_swaps']}, {'wrist', 'back'})
        for invalid in ('wrists=,id=123,ilevel=289\nwrists=,id=456,ilevel=289',
                        'wrists=,id=123,ilevel=289\ntrinket1=,id=456,ilevel=289',
                        'wrists=,id=123,ilevel=289\nback=,id=456,ilevel=289,output=secret'):
            from django.core.exceptions import ValidationError
            with self.assertRaises(ValidationError):
                _normalize_candidate_params('gear_swap', invalid)

    def test_combination_levels_group_only_when_all_members_share_one_level(self):
        from botend.services.simc_benchmark_execution import _candidate_item_variant_key
        def identity(first, second):
            return _candidate_item_variant_key({'candidate_type': 'gear_swap', 'candidate_params':
                _normalize_candidate_params('gear_swap',
                    f'wrists=,id=239660,ilevel={first}\nback=,id=239661,ilevel={second}')})
        self.assertEqual(identity(289, 289), identity(300, 300))
        self.assertNotEqual(identity(289, 300), identity(300, 289))
        self.assertNotEqual(identity(289, 300), identity(300, 300))

    def test_combination_freezes_all_slots_and_reuses_display_and_supplement(self):
        self.ring.params = _normalize_candidate_params('gear_swap',
            'wrists=,id=239660,ilevel=289\nback=,id=239661,ilevel=289')
        self.ring.label = '测试两件套'
        self.ring.save()
        plan = build_execution_plan(self.panel)
        candidate = next(row for row in plan['cases'][0]['candidates'] if row['candidate_key'] == 'ring')
        self.assertEqual(candidate['candidate_params']['equipment_effect_policy']['target_slots'], ['back', 'wrists'])
        execution = self.finish()
        live = serialize_incremental_panel_results(self.panel)
        row = next(row for row in live['coordinates'][0]['candidates'] if row['key'] == 'ring')
        self.assertEqual(row['baseline_dps'], 1500)
        self.assertEqual(len(row['equipment_items']), 2)
        self.assertTrue(row['equipment_group_key'])
        self.assertEqual(len(serialize_public_execution(execution)['execution']['cases'][0]['candidates']), 3)
        self.assertEqual(build_execution_plan(self.panel)['run_count'], 4)
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        request = SimcMonitor.apply_candidate_overrides(
            {'player_equipment': 'warrior=x\nwrists=,id=1\nback=,id=2'}, candidate['candidate_params'])
        self.assertIn('back=,id=239661', request['player_equipment'])
        self.assertIn('wrists=,id=239660', request['player_equipment'])

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

    def test_combination_failed_control_is_supplemented_without_rerunning_successful_items(self):
        self.ring.params = _normalize_candidate_params('gear_swap',
            'wrists=,id=239660,ilevel=289,crafted_stats=crit/haste\n'
            'back=,id=239661,ilevel=289,crafted_stats=crit/haste')
        self.ring.save()
        self.test_missing_control_is_not_compared_with_common_baseline_and_is_supplemented()
        row = next(row for row in serialize_incremental_panel_results(self.panel)
                   ['coordinates'][0]['candidates'] if row['key'] == 'ring')
        self.assertEqual(len(row['equipment_items']), 2)
