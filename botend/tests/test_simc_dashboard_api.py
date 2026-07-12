import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase

from botend.dashboard.api import SimcBatchTaskAPIView, SimcRegularCompareAPIView, inspect_raw_simc_code
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.services.simc_player_config import parse_manual_player_config, parse_manual_simc_candidates
from botend.models import SimcContentTemplate, SimcProfile, SimcTask, WowItemSnapshot


class SimcRawInspectTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='simc_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_inspect_raw_simc_code_detects_profile_and_default_apl(self):
        SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_APL,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='hunter_beast_mastery',
            class_name='hunter',
            name='默认APL hunter_beast_mastery',
            content='actions+=/kill_command',
            is_active=True,
            is_selectable=True,
        )
        payload = inspect_raw_simc_code('''
hunter="Bloodmastêr"
level=80
race=orc
role=attack
spec=beast_mastery
''')

        self.assertEqual(payload['character_name'], 'Bloodmastêr')
        self.assertEqual(payload['class'], 'hunter')
        self.assertEqual(payload['spec'], 'beast_mastery')
        self.assertEqual(payload['spec_key'], 'hunter_beast_mastery')
        self.assertTrue(payload['default_apl_available'])
        self.assertEqual(payload['plans'][0]['id'], 'regular')
        self.assertTrue(payload['plans'][0]['enabled'])
        self.assertFalse(payload['plans'][1]['enabled'])

    def test_inspect_raw_endpoint_returns_plans(self):
        response = self.client.post(
            '/api/simc-profile/inspect-raw/',
            data=json.dumps({'raw_simc_code': 'warrior="Foo"\nspec=fury\n'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['data']['class'], 'warrior')
        self.assertEqual(payload['data']['spec'], 'fury')
        self.assertEqual(payload['data']['plans'][0]['task_type'], 1)

    def test_raw_simc_task_create_persists_raw_code_in_ext(self):
        raw_code = 'mage="Arcaneone"\nspec=arcane\n'
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Arcaneone arcane 常规模拟',
                'task_type': 1,
                'simc_profile_id': 0,
                'raw_simc_code': raw_code,
                'regular_time': 300,
                'regular_target_count': 1,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        self.assertEqual(task.simc_profile_id, 0)
        self.assertEqual(task.task_type, 1)
        ext = json.loads(task.ext)
        self.assertEqual(ext['raw_simc_code'], raw_code)
        self.assertEqual(ext['regular_time'], 300)
        self.assertEqual(ext['regular_target_count'], 1)

    def test_raw_simc_attribute_task_is_rejected(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'bad attribute raw',
                'task_type': 2,
                'simc_profile_id': 0,
                'raw_simc_code': 'paladin="Foo"\nspec=retribution\n',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('不支持属性模拟', payload['error'])
        self.assertFalse(SimcTask.objects.exists())


class SimcBatchVariableCompareTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='batch_compare_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_parse_manual_candidates_keeps_equipped_baseline_separate_from_bag_and_loadout_choices(self):
        candidates = parse_manual_simc_candidates('''
warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=212048,ilevel=639
### Gear from Bags
# Bag helm (650)
head=,id=299001,ilevel=650
# Saved Loadout: Cleave
# talents=CLEAVE_BUILD
### Weekly Reward Choices
# Weekly ring (655)
finger1=,id=299002,ilevel=655
''')
        self.assertEqual(candidates['base_talent'], 'ACTIVE_BUILD')
        self.assertEqual(candidates['gear_candidates'][0]['slot'], 'head')
        self.assertEqual(candidates['gear_candidates'][0]['item_id'], 299001)
        self.assertEqual(candidates['gear_candidates'][0]['source'], 'bags')
        self.assertEqual(candidates['gear_candidates'][1]['source'], 'weekly_reward')
        self.assertEqual(candidates['talent_candidates'][0]['talent'], 'CLEAVE_BUILD')
        self.assertEqual(parse_manual_player_config('head=,id=212048\n### Gear from Bags\nhead=,id=299001', 'fury')['equipment'][0]['id'], 212048)

    def test_auto_attribute_batch_creates_complete_50_rating_pairwise_neighborhood(self):
        base = {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}
        rows = SimcBatchTaskAPIView._attribute_variants(base, 50)
        self.assertEqual(len(rows), 13)
        self.assertEqual(sum(is_base for _, _, is_base, _ in rows), 1)
        moves = [candidate['move'] for _, _, is_base, candidate in rows if not is_base]
        self.assertEqual(
            {(move['from'], move['to'], move['transfer']) for move in moves},
            {(source, target, 50) for source in base for target in base if source != target},
        )
        for _, ratings, _, candidate in rows:
            self.assertEqual(sum(ratings.values()), sum(base.values()))
            self.assertTrue(all(value >= 0 for value in ratings.values()))
            if candidate['move'].get('type') != 'baseline':
                self.assertEqual(candidate['move']['transfer'], 50)

    def test_auto_attribute_batch_omits_sub_50_source_without_projecting_non_grid_move(self):
        base = {'crit': 49, 'haste': 50, 'mastery': 100, 'versatility': 0}
        rows = SimcBatchTaskAPIView._attribute_variants(base, 50)
        moves = [candidate['move'] for _, _, is_base, candidate in rows if not is_base]
        self.assertEqual(len(rows), 7)  # centre + (haste/mastery) * 3 valid targets
        self.assertTrue(all(move['from'] != 'crit' for move in moves))
        self.assertTrue(all(move['transfer'] == 50 for move in moves))

    def test_auto_attribute_batch_creates_base_and_limited_variants_with_one_batch_id(self):
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'attribute_variants', 'name': 'Fury 自动属性比较', 'spec': 'fury',
            'player_config_mode': 'attribute_only', 'talent': 'ATTRIBUTE_BUILD',
            'gear_strength': 5000,
            'gear_crit': 1000, 'gear_haste': 2000, 'gear_mastery': 3000, 'gear_versatility': 4000,
            'attribute_step': 50, 'fight_style': 'Patchwerk', 'time': 300, 'target_count': 1,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        # 首轮覆盖四属性之间全部有向 50 绿字转移：中心 + 12 个合法邻居。
        self.assertEqual(payload['data']['accepted'], 13)
        ext_rows = [json.loads(task.ext) for task in SimcTask.objects.order_by('id')]
        self.assertEqual(len(ext_rows), 13)
        self.assertEqual(len({row['batch_compare']['batch_id'] for row in ext_rows}), 1)
        self.assertEqual({row['batch_compare']['kind'] for row in ext_rows}, {'attribute_variants'})
        self.assertEqual(sum(row['batch_compare']['is_base'] for row in ext_rows), 1)
        self.assertEqual({row['player_config_mode'] for row in ext_rows}, {'attribute_only'})
        self.assertEqual({row['talent'] for row in ext_rows}, {'ATTRIBUTE_BUILD'})
        self.assertEqual({row['gear_strength'] for row in ext_rows}, {5000})
        candidates = [row['batch_compare']['candidate'] for row in ext_rows]
        self.assertEqual(candidates[0]['algorithm'], 'four_stat_pairwise_hill_climb')
        self.assertEqual(candidates[0]['algorithm_version'], 2)
        self.assertEqual(candidates[0]['round'], 1)
        base_total = sum((1000, 2000, 3000, 4000))
        gears = [{stat: row[f'gear_{stat}'] for stat in ('crit', 'haste', 'mastery', 'versatility')} for row in ext_rows]
        self.assertTrue(all(sum(gear.values()) == base_total for gear in gears))
        changed_stats = {stat for gear in gears[1:] for stat, value in gear.items() if value != {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}[stat]}
        self.assertEqual(changed_stats, {'crit', 'haste', 'mastery', 'versatility'})

    def test_auto_attribute_batch_projects_anchor_direction_to_boundary_instead_of_dropping_it(self):
        # 50-rating 离散搜索不允许把不足一步的余额投影成 100 等非网格转移。
        base = {'crit': 400, 'haste': 1100, 'mastery': 1140, 'versatility': 100}
        rows = SimcBatchTaskAPIView._attribute_variants(base, 50)
        self.assertEqual(len(rows), 13)
        self.assertTrue(all(sum(ratings.values()) == sum(base.values()) for _, ratings, _, _ in rows))
        self.assertTrue(all(candidate['move'].get('type') == 'baseline' or candidate['move']['transfer'] == 50 for _, _, _, candidate in rows))

        chosen = SimcBatchTaskAPIView._next_attribute_search_center([
            {'ratings': {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}, 'dps': 100000, 'is_center': True},
            {'ratings': {'crit': 950, 'haste': 2050, 'mastery': 3000, 'versatility': 4000}, 'dps': 101500},
        ], step=50, min_step=50)
        self.assertEqual(chosen['ratings'], {'crit': 950, 'haste': 2050, 'mastery': 3000, 'versatility': 4000})
        self.assertEqual(chosen['step'], 50)
        self.assertFalse(chosen['converged'])

        local_optimum = SimcBatchTaskAPIView._next_attribute_search_center([
            {'ratings': {'crit': 950, 'haste': 2050, 'mastery': 3000, 'versatility': 4000}, 'dps': 102000, 'is_center': True},
            {'ratings': {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}, 'dps': 101800},
        ], step=50, min_step=50)
        self.assertTrue(local_optimum['converged'])
        self.assertEqual(local_optimum['stop_reason'], 'local_optimum_50_pairwise')

    def test_next_attribute_round_preserves_budget_and_marks_new_center(self):
        base = {'crit': 1200, 'haste': 2000, 'mastery': 3000, 'versatility': 3800}
        rows = SimcBatchTaskAPIView._attribute_variants(base, 50, round_number=2, mark_base=True)
        self.assertEqual(len(rows), 13)
        self.assertTrue(rows[0][2])
        self.assertEqual(rows[0][3]['round'], 2)
        self.assertTrue(all(sum(ratings.values()) == 10000 for _, ratings, _, _ in rows))

    def test_battlenet_template_selection_accepts_playerless_default_template(self):
        monitor = SimcMonitor(None, None)
        default_template = SimpleNamespace(
            id=1,
            spec='default',
            content='fight_style={fight_style}\n{player_config}\n{action_list}',
        )
        selected = monitor._select_template_from_queryset(
            [default_template], 'blood', player_config_mode='battlenet'
        )
        self.assertIs(selected, default_template)

    def test_template_selection_ignores_non_executable_probe_template(self):
        monitor = SimcMonitor(None, None)
        probe = SimpleNamespace(id=1, spec='default', content='spec={spec}\n{player_config}\n')
        executable = SimpleNamespace(
            id=2,
            spec='default',
            content='warrior="Template"\nspec={spec}\n',
        )
        selected = monitor._select_template_from_queryset([probe, executable], 'fury')
        self.assertIs(selected, executable)

    def test_incomplete_base_template_is_not_executable(self):
        probe = SimpleNamespace(id=1, content='spec=fury\n{player_config}\n')
        self.assertFalse(SimcMonitor._is_executable_base_template(probe))

    def test_simc_error_details_keep_attribute_batch_execution_context(self):
        monitor = SimcMonitor(None, None)
        manifest = {
            'player_config_mode': 'attribute_only',
            'spec': 'fury',
            'talent': 'ATTRIBUTE_BUILD',
            'gear_crit': 1000,
            'gear_haste': 2000,
            'gear_mastery': 3000,
            'gear_versatility': 4000,
            'selected_apl_id': 42,
            'batch_compare': {'batch_id': 'batch-1', 'candidate': {'round': 1}},
        }
        task = SimpleNamespace(ext=json.dumps(manifest), id=99)

        monitor.save_simc_error_details(task, 'SimC未生成预期结果文件', stderr_text='x' * 20000)

        stored = json.loads(task.ext)
        for key, value in manifest.items():
            self.assertEqual(stored[key], value)
        self.assertEqual(stored['simc_error_summary'], 'SimC未生成预期结果文件')
        self.assertIn('simc_error_native', stored)

    def test_attribute_batch_task_renders_its_own_explicit_html_result_file(self):
        monitor = SimcMonitor(None, None)
        rendered = monitor.apply_template(
            'warrior="LMonitor"\n{player_config}\nhtml={result_file}\n{action_list}',
            {
                'player_config_mode': 'attribute_only',
                'talent': 'BUILD',
                'gear_strength': 5000,
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
                'result_file': 'simc_task_42.html',
            },
        )
        self.assertIn('html=simc_task_42.html', rendered)
        self.assertIn('gear_strength=5000', rendered)
        self.assertIn('gear_crit_rating=1000', rendered)
        self.assertIn('gear_haste_rating=2000', rendered)
        self.assertIn('gear_mastery_rating=3000', rendered)
        self.assertIn('gear_versatility_rating=4000', rendered)
        self.assertNotIn('\ncrit_rating=1000', rendered)
        self.assertNotIn('{result_file}', rendered)

    def test_attribute_batch_task_appends_explicit_html_when_base_template_has_no_placeholder(self):
        monitor = SimcMonitor(None, None)
        rendered = monitor.apply_template(
            'warrior="LMonitor"\n{player_config}\n{action_list}',
            {
                'player_config_mode': 'attribute_only',
                'talent': 'BUILD',
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
                'result_file': 'simc_task_43.html',
            },
        )
        self.assertTrue(rendered.endswith('html=simc_task_43.html'))
        self.assertEqual(rendered.count('html='), 1)

    def test_result_file_directive_replaces_existing_html_output(self):
        rendered = SimcMonitor.ensure_result_file_directive(
            'warrior="LMonitor"\nhtml=stale_report.html\n',
            'simc_task_44.html',
        )
        self.assertEqual(rendered.count('html='), 1)
        self.assertTrue(rendered.endswith('html=simc_task_44.html'))
        self.assertNotIn('stale_report.html', rendered)

    def test_attribute_search_stops_when_it_revisits_same_center_and_step(self):
        ratings = {'crit': 1200, 'haste': 2000, 'mastery': 3000, 'versatility': 3800}
        stop = SimcBatchTaskAPIView._attribute_search_stop_reason(
            round_number=4, ratings=ratings, step=200,
            visited_centers={(tuple(ratings[stat] for stat in SimcBatchTaskAPIView.ATTRIBUTE_STATS), 200)},
            max_rounds=20,
        )
        self.assertEqual(stop, 'cycle_detected')

    def test_execute_simc_command_passes_absolute_task_result_path(self):
        from unittest.mock import patch
        import tempfile
        import os
        monitor = object.__new__(SimcMonitor)
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor.simc_path = '/opt/simc'
            monitor.result_path = tmpdir
            task = SimpleNamespace(id=88, result_file='simc_task_88.html', save=lambda **kwargs: None)
            expected = os.path.join(tmpdir, task.result_file)
            with patch('botend.controller.plugins.simc.SimcMonitor.subprocess.run') as run:
                run.return_value = SimpleNamespace(returncode=0, stdout='', stderr='')
                with patch('botend.interface.ossupload.ossUpload', return_value=True):
                    with open(expected, 'w', encoding='utf-8') as report:
                        report.write('<html></html>')
                    self.assertTrue(monitor.execute_simc_command('/tmp/input.simc', task, task.result_file))
            self.assertEqual(run.call_args.args[0], ['/opt/simc', '/tmp/input.simc', f'html={expected}'])

    def test_attribute_search_rejects_any_non_50_step(self):
        results = [
            {'ratings': {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}, 'dps': 100000, 'is_center': True},
            {'ratings': {'crit': 950, 'haste': 2050, 'mastery': 3000, 'versatility': 4000}, 'dps': 100100, 'is_center': False},
        ]
        with self.assertRaisesRegex(ValueError, '固定使用 50'):
            SimcBatchTaskAPIView._next_attribute_search_center(results, step=100, min_step=50)
        bad_response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'attribute_variants', 'name': '错误步长', 'spec': 'fury',
            'player_config_mode': 'attribute_only', 'talent': 'ATTRIBUTE_BUILD',
            'gear_crit': 1000, 'gear_haste': 2000, 'gear_mastery': 3000, 'gear_versatility': 4000,
            'attribute_step': 100,
        }), content_type='application/json')
        self.assertFalse(bad_response.json()['success'])
        self.assertIn('固定使用 50', bad_response.json()['error'])

        stop = SimcBatchTaskAPIView._attribute_search_stop_reason(
            round_number=20, ratings={'crit': 1200, 'haste': 2000, 'mastery': 3000, 'versatility': 3800},
            step=100, visited_centers=set(), max_rounds=20,
        )
        self.assertEqual(stop, 'max_rounds_reached')

    def test_attribute_round_manifest_parser_defaults_invalid_values_to_first_round(self):
        self.assertEqual(SimcBatchTaskAPIView._parse_manifest_round({}), 1)
        self.assertEqual(SimcBatchTaskAPIView._parse_manifest_round({'candidate': {'round': 'bad'}}), 1)
        self.assertEqual(SimcBatchTaskAPIView._parse_manifest_round({'candidate': {'round': 3}}), 3)

    def test_batch_rejects_unsupported_source_and_oversized_candidate_selection(self):
        base = {'name': 'Manual candidate compare', 'spec': 'fury', 'player_config_mode': 'manual_equipment',
                'player_equipment': 'warrior="Batcher"\nspec=fury\ntalents=BASE\nhead=,id=212048'}
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({**base, 'kind': 'gear_candidates', 'candidates': [{'slot': 'head', 'item_id': 1, 'source': 'external'}]}), content_type='application/json')
        self.assertFalse(response.json()['success'])
        self.assertIn('来源', response.json()['error'])
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({**base, 'kind': 'gear_candidates', 'candidates': [{'slot': 'head', 'item_id': 200000 + i, 'source': 'bags'} for i in range(8)]}), content_type='application/json')
        self.assertFalse(response.json()['success'])
        self.assertIn('最多', response.json()['error'])


    def test_legacy_two_stat_scan_uses_adaptive_bounded_points_and_keeps_baseline(self):
        monitor = SimcMonitor(None, None)
        points = monitor.build_attribute_test_points(total_value=4000, base_value=1700, requested_step=50)
        self.assertEqual(points[0], 0)
        self.assertEqual(points[-1], 4000)
        self.assertIn(1700, points)
        self.assertLessEqual(len(points), monitor.MAX_ATTRIBUTE_TEST_POINTS)
        self.assertEqual(points, sorted(set(points)))

    def test_attribute_batch_report_returns_real_dps_rankings_path_and_local_optimum(self):
        batch_id = 'batch-attribute-report'
        base = {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}
        variants = SimcBatchTaskAPIView._attribute_variants(base, 50)
        reports = {}
        for index, (label, ratings, is_base, candidate) in enumerate(variants):
            dps = 100000 if is_base else 99900
            task = SimcTask.objects.create(
                user_id=self.user.id, name=f'attribute {label}', simc_profile_id=0,
                current_status=2, task_type=1, result_file=f'attribute_{index}.html',
                ext=json.dumps({
                    'player_config_mode': 'attribute_only', 'spec': 'fury', 'talent': 'BUILD',
                    **{f'gear_{stat}': ratings[stat] for stat in SimcBatchTaskAPIView.ATTRIBUTE_STATS},
                    'batch_compare': {
                        'version': 2, 'batch_id': batch_id, 'kind': 'attribute_variants',
                        'index': index, 'label': label, 'is_base': is_base, 'candidate': candidate,
                    },
                }),
            )
            reports[task.result_file] = f'<h2>Fury: {dps:,} dps</h2>'

        def result_content(_self, result_file):
            return reports.get(result_file)

        with patch.object(SimcRegularCompareAPIView, '_get_result_file_content', result_content):
            response = self.client.get('/api/simc-regular-compare/?batch_id=' + batch_id)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        report = payload['data']['attribute_report']
        self.assertEqual(report['algorithm'], 'four_stat_pairwise_hill_climb')
        self.assertEqual(report['step'], 50)
        self.assertEqual(report['total_rating'], 10000)
        self.assertEqual(report['rounds_completed'], 1)
        self.assertEqual(report['recommendation']['ratings'], base)
        self.assertEqual(report['stop_reason'], 'local_optimum_50_pairwise')
        self.assertEqual(len(report['candidates']), 13)
        self.assertEqual(report['candidates'][0]['dps'], 100000)
        self.assertTrue(all(row['result_file'] != report['candidates'][0]['result_file'] for row in report['candidates'][1:]))

    def test_batch_compare_query_is_isolated_and_reports_pending_progress(self):
        batch_id, other_id = 'batch-isolated', 'batch-other'
        def create_task(name, bid, index, status=0):
            return SimcTask.objects.create(user_id=self.user.id, name=name, simc_profile_id=0, current_status=status, task_type=1, result_file='', ext=json.dumps({'batch_compare': {'version': 1, 'batch_id': bid, 'kind': 'attribute_variants', 'index': index, 'is_base': index == 0, 'label': name}}))
        create_task('baseline', batch_id, 0)
        create_task('crit +200', batch_id, 1, 1)
        create_task('unrelated', other_id, 0, 2)
        response = self.client.get('/api/simc-regular-compare/?batch_id=' + batch_id)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['data']['batch']['batch_id'], batch_id)
        self.assertEqual(payload['data']['batch']['total'], 2)
        self.assertEqual(payload['data']['batch']['pending'], 1)
        self.assertEqual(payload['data']['batch']['running'], 1)
        self.assertEqual(payload['data']['batch']['succeeded'], 0)


class SimcNewConfigModeTests(TestCase):
    """测试新版工作台任务配置：只输入玩家信息，战斗/APL 由选项控制。"""

    def setUp(self):
        self.user = User.objects.create_user(username='newmode_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_attribute_manifest_task_routes_to_attribute_runner_without_profile_lookup(self):
        task = SimcTask.objects.create(
            user_id=self.user.id,
            name='Manifest attribute snapshot',
            task_type=2,
            simc_profile_id=0,
            ext=json.dumps({
                'player_config_mode': 'attribute_only',
                'spec': 'fury',
                'talent': 'SNAPSHOT_BUILD',
                'selected_attributes': 'crit_haste',
                'attribute_step': 50,
                'gear_strength': 0,
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
            }),
            current_status=0,
            is_active=True,
        )
        monitor = SimcMonitor(None, None)
        with patch.object(monitor, 'process_attribute_simulation', return_value=True) as attribute_runner, \
             patch.object(monitor, 'process_regular_simulation') as regular_runner:
            self.assertTrue(monitor.process_simc_task(task))

        attribute_runner.assert_called_once()
        self.assertIsNone(attribute_runner.call_args.args[1])
        regular_runner.assert_not_called()

    def test_direct_attribute_task_persists_full_manifest_snapshot(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Direct attribute snapshot',
                'task_type': 2,
                'player_import_mode': 'attribute_only',
                'spec': 'fury',
                'talent': 'SNAPSHOT_BUILD',
                'selected_attributes': 'crit_haste',
                'attribute_step': 50,
                'gear_strength': 0,
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
                'fight_style': 'DungeonSlice',
                'time': 180,
                'target_count': 5,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        ext = json.loads(task.ext)
        self.assertEqual(ext['player_config_mode'], 'attribute_only')
        self.assertEqual(ext['spec'], 'fury')
        self.assertEqual(ext['talent'], 'SNAPSHOT_BUILD')
        self.assertEqual(ext['gear_strength'], 0)
        self.assertEqual(ext['gear_crit'], 1000)
        self.assertEqual(ext['gear_versatility'], 4000)
        self.assertEqual(ext['fight_style'], 'DungeonSlice')
        self.assertEqual(ext['time'], 180)
        self.assertEqual(ext['target_count'], 5)

    def test_direct_attribute_task_rejects_non_50_step(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Bad direct attribute step',
                'task_type': 2,
                'player_import_mode': 'attribute_only',
                'spec': 'fury',
                'talent': 'SNAPSHOT_BUILD',
                'selected_attributes': 'crit_haste',
                'attribute_step': 25,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'], payload)
        self.assertIn('50', payload['error'])

    def test_attribute_render_uses_manifest_snapshot_instead_of_changed_profile(self):
        monitor = SimcMonitor(None, None)
        monitor.select_template_by_spec = lambda spec: SimpleNamespace(
            content='warrior="LMonitor"\\nspec={spec}\\n{player_config}\\nhtml={result_file}'
        )
        profile = SimpleNamespace(
            spec='arms', talent='CHANGED_PROFILE_BUILD', player_config_mode='attribute_only',
            player_import_mode='attribute_only', player_equipment='changed=1',
            battlenet_region='us', battlenet_realm='Changed', battlenet_character='Changed',
        )
        rendered = monitor.generate_attribute_simc_code(profile, {
            'gear_strength': 0, 'gear_crit': 1000, 'gear_haste': 2000,
            'gear_mastery': 3000, 'gear_versatility': 4000,
        }, '77_crit_1000_haste_2000.html', {
            'player_config_mode': 'attribute_only', 'spec': 'fury',
            'talent': 'SNAPSHOT_BUILD', 'gear_strength': 0,
        })

        self.assertIn('spec=fury', rendered)
        self.assertIn('talents=SNAPSHOT_BUILD', rendered)
        self.assertNotIn('CHANGED_PROFILE_BUILD', rendered)
        self.assertNotIn('spec=arms', rendered)
        self.assertIn('gear_strength=0', rendered)

    def test_create_task_with_manual_equipment_mode(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Fury Manual Equipment',
                'task_type': 1,
                'player_import_mode': 'manual_equipment',
                'player_equipment': 'talents=TEST\nhead=,id=212048',
                'fight_style': 'Patchwerk',
                'time': 300,
                'target_count': 1,
                'spec': 'fury',
                'talent': 'TEST',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        self.assertEqual(task.result_file, '')
        ext = json.loads(task.ext)
        self.assertEqual(ext['player_config_mode'], 'manual_equipment')
        self.assertEqual(ext['player_import_mode'], 'manual_equipment')
        self.assertEqual(ext['player_equipment'], 'talents=TEST\nhead=,id=212048')
        self.assertEqual(ext['fight_style'], 'Patchwerk')
        self.assertEqual(ext['time'], 300)
        self.assertEqual(ext['target_count'], 1)

    def test_create_task_with_dungeon_preset_values_persists_exact_combat_combination(self):
        """战斗组合预设只是前端预填，任务端必须按选择后的精确值固化。"""
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Fury DungeonSlice 300s 5目标',
                'task_type': 1,
                'player_import_mode': 'attribute_only',
                'spec': 'fury',
                'talent': 'DUNGEON_BUILD',
                'gear_crit': 400,
                'gear_haste': 1100,
                'gear_mastery': 1140,
                'gear_versatility': 100,
                'fight_style': 'DungeonSlice',
                'time': 300,
                'target_count': 5,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        ext = json.loads(task.ext)
        self.assertEqual(ext['fight_style'], 'DungeonSlice')
        self.assertEqual(ext['time'], 300)
        self.assertEqual(ext['target_count'], 5)
        self.assertEqual(ext['spec'], 'fury')
        self.assertEqual(ext['player_config_mode'], 'attribute_only')

    def test_create_task_with_legacy_equipment_alias_maps_to_manual_equipment(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Legacy Equipment Alias',
                'task_type': 1,
                'player_config_mode': 'equipment',
                'player_equipment': 'talents=TEST\nneck=,id=224433',
                'spec': 'fury',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        ext = json.loads(task.ext)
        self.assertEqual(ext['player_config_mode'], 'manual_equipment')
        self.assertEqual(ext['player_import_mode'], 'manual_equipment')

    def test_create_task_with_battlenet_mode(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Fury Battle.net Import',
                'task_type': 1,
                'player_import_mode': 'battlenet',
                'battlenet_region': 'EU',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr',
                'fight_style': 'Patchwerk',
                'time': 300,
                'target_count': 1,
                'spec': 'fury',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        self.assertEqual(task.result_file, '')
        ext = json.loads(task.ext)
        self.assertEqual(ext['player_config_mode'], 'battlenet')
        self.assertEqual(ext['player_import_mode'], 'battlenet')
        self.assertEqual(ext['battlenet_region'], 'eu')
        self.assertEqual(ext['battlenet_realm'], 'Kazzak')
        self.assertEqual(ext['battlenet_character'], 'Bloodmastêr')

    def test_manual_equipment_requires_player_block(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'No Equipment',
                'task_type': 1,
                'player_import_mode': 'manual_equipment',
                'player_equipment': '',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('玩家装备配置不能为空', payload['error'])

    def test_battlenet_requires_region_realm_character(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Bad Battlenet',
                'task_type': 1,
                'player_import_mode': 'battlenet',
                'battlenet_region': 'eu',
                'battlenet_realm': '',
                'battlenet_character': 'Bloodmastêr',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('Battle.net 导入需要提供', payload['error'])

    def test_stats_mode_is_rejected_in_new_workbench(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Stats Not Allowed',
                'task_type': 1,
                'player_config_mode': 'stats',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('玩家信息导入方式必须是', payload['error'])

    def test_apply_template_builds_battlenet_armory_player_block(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        monitor = object.__new__(SimcMonitor)
        rendered = monitor.apply_template(
            'deathknight="LMonitor_Base"\nspec={spec}\nfight_style={fight_style}\n{player_config}\n{action_list}',
            {
                'fight_style': 'Patchwerk',
                'player_import_mode': 'battlenet',
                'battlenet_region': 'eu',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr',
                'spec': 'fury',
                'override_action_list': 'actions=auto_attack',
            },
        )
        self.assertNotIn('Bloodmast_r', rendered)
        self.assertNotIn('deathknight="LMonitor_Base"', rendered)
        self.assertNotIn('\nspec=fury', rendered)
        self.assertIn('armory=eu,Kazzak,Bloodmastêr', rendered)
        self.assertIn('actions=auto_attack', rendered)

    def test_apply_template_inserts_manual_equipment_player_block(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        monitor = object.__new__(SimcMonitor)
        rendered = monitor.apply_template(
            'fight_style={fight_style}\n{player_config}\n{action_list}',
            {
                'fight_style': 'Patchwerk',
                'player_import_mode': 'manual_equipment',
                'player_equipment': 'talents=TEST\nhead=,id=212048',
                'override_action_list': 'actions=auto_attack',
            },
        )
        self.assertIn('talents=TEST', rendered)
        self.assertIn('head=,id=212048', rendered)
        self.assertIn('actions=auto_attack', rendered)
    def test_apply_template_inserts_attribute_only_talents_and_ratings_without_player_block(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        monitor = object.__new__(SimcMonitor)
        rendered = monitor.apply_template(
            'spec={spec}\n{player_config}\n{gear_crit}\n{gear_haste}\n{gear_mastery}\n{gear_versatility}\n{action_list}',
            {
                'spec': 'fury',
                'player_config_mode': 'attribute_only',
                'talent': 'ATTRIBUTE_BUILD',
                'gear_strength': 5000,
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
                'override_action_list': 'actions=auto_attack',
            },
        )
        self.assertIn('talents=ATTRIBUTE_BUILD', rendered)
        self.assertIn('gear_strength=5000', rendered)
        self.assertIn('crit_rating=1000', rendered)
        self.assertIn('haste_rating=2000', rendered)
        self.assertIn('mastery_rating=3000', rendered)
        self.assertIn('versatility_rating=4000', rendered)
        self.assertNotIn('{gear_', rendered)
        self.assertNotIn('armory=', rendered)
        self.assertNotIn('head=,', rendered)
        self.assertIn('actions=auto_attack', rendered)


class SimcPlayerConfigDetailTests(TestCase):
    """玩家详情只解析当前输入与本地快照，不渲染完整 SimC 执行配置。"""

    def setUp(self):
        self.user = User.objects.create_user(username='player_detail_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_player_config_detail_returns_structured_manual_player_detail_with_items_and_stats(self):
        WowItemSnapshot.objects.create(item_id=212048, name='Helm of Tests', name_zh='测试头盔', icon='inv_helmet_01')
        WowItemSnapshot.objects.create(item_id=71543, name='Swift Enchant', name_zh='迅捷附魔')
        WowItemSnapshot.objects.create(item_id=213479, name='Test Gem', name_zh='测试宝石')
        from botend.models import SimcSecondaryStatRule
        SimcSecondaryStatRule.objects.update_or_create(
            class_name='warrior',
            defaults={
                'crit_per_percent': 46, 'haste_per_percent': 44,
                'mastery_per_percent': 46, 'versatility_per_percent': 54,
            },
        )
        response = self.client.post(
            '/api/simc-player-config-detail/',
            data=json.dumps({
                'spec': 'fury',
                'player_config_mode': 'manual_equipment',
                'player_equipment': '\n'.join([
                    'warrior="Previewer"',
                    'level=80',
                    'race=orc',
                    'region=cn',
                    'server=死亡之翼',
                    'spec=fury',
                    'talents=BUILDCODE',
                    'head=,id=212048,ilevel=639,enchant_id=71543,gems=213479/213480',
                    'main_hand=,id=224638,ilevel=646',
                    'crit_rating=10730',
                    'haste_rating=18641',
                    'mastery_rating=21785',
                    'versatility_rating=6757',
                ]),
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        detail = payload['data']
        self.assertEqual(detail['source']['type'], 'manual_equipment')
        self.assertEqual(detail['identity']['name'], 'Previewer')
        self.assertEqual(detail['identity']['race'], 'orc')
        self.assertEqual(detail['identity']['region'], 'cn')
        self.assertEqual(detail['identity']['realm'], '死亡之翼')
        self.assertEqual(detail['talents']['build_code'], 'BUILDCODE')
        self.assertEqual(detail['equipment'][0]['slot'], 'head')
        self.assertEqual(detail['equipment'][0]['display_name'], '测试头盔')
        self.assertEqual(detail['equipment'][0]['item_level'], 639)
        self.assertEqual(detail['equipment'][0]['enchant']['display_name'], '迅捷附魔')
        self.assertEqual(detail['equipment'][0]['gems'][0]['display_name'], '测试宝石')
        self.assertEqual(detail['stats']['secondary']['crit']['rating'], 10730)
        self.assertAlmostEqual(detail['stats']['secondary']['crit']['percent'], 233.26, places=2)
        self.assertEqual(SimcTask.objects.count(), 0)

    def test_player_config_detail_exposes_only_parsed_comparison_candidates(self):
        player_block = '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
trinket1=,id=111,ilevel=639
# Saved Loadout: Cleave
# talents=CLEAVE_BUILD
### Gear from Bags
# Candidate Trinket (645)
trinket1=,id=222,ilevel=645
### Weekly Reward Choices
# Candidate Ring (646)
finger1=,id=333,ilevel=646
'''
        response = self.client.post(
            '/api/simc-player-config-detail/',
            data=json.dumps({
                'spec': 'fury', 'player_config_mode': 'manual_equipment',
                'player_equipment': player_block,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        candidates = payload['data']['comparison_candidates']
        self.assertEqual(candidates['max_selectable'], 7)
        self.assertEqual(
            [(row['slot'], row['item_id'], row['source']) for row in candidates['gear']],
            [('trinket1', 222, 'bags'), ('finger1', 333, 'weekly_reward')],
        )
        self.assertEqual(candidates['talents'], [{'name': 'Cleave', 'talent': 'CLEAVE_BUILD', 'source': 'saved_loadout'}])

    def test_talent_candidate_batch_replaces_player_block_talent_before_execution(self):
        player_block = '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
trinket1=,id=111,ilevel=639
# Saved Loadout: Cleave
# talents=CLEAVE_BUILD
'''
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'talent_candidates', 'name': 'Fury 天赋对比', 'spec': 'fury',
            'player_config_mode': 'manual_equipment', 'player_equipment': player_block,
            'candidates': [{'talent': 'CLEAVE_BUILD'}],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        ext_rows = [json.loads(task.ext) for task in SimcTask.objects.order_by('id')]
        self.assertEqual(len(ext_rows), 2)
        candidate = next(row for row in ext_rows if not row['batch_compare']['is_base'])
        self.assertIn('talents=CLEAVE_BUILD', candidate['player_equipment'])
        self.assertNotIn('talents=ACTIVE_BUILD', candidate['player_equipment'])

    def test_gear_candidate_batch_rejects_slot_not_in_baseline_block(self):
        player_block = '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111,ilevel=639
### Gear from Bags
# Candidate ring (645)
finger1=,id=222,ilevel=645
'''
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'Fury 装备对比', 'spec': 'fury',
            'player_config_mode': 'manual_equipment', 'player_equipment': player_block,
            'candidates': [{'slot': 'finger1', 'item_id': 222, 'source': 'bags'}],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('未包含可替换的装备槽位', response.json()['error'])

    def test_candidate_batch_rejects_duplicate_candidates(self):
        player_block = '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111,ilevel=639
### Gear from Bags
# Candidate helm (645)
head=,id=222,ilevel=645
# Saved Loadout: Cleave
# talents=CLEAVE_BUILD
'''
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'Fury 装备对比', 'spec': 'fury',
            'player_config_mode': 'manual_equipment', 'player_equipment': player_block,
            'candidates': [
                {'slot': 'head', 'item_id': 222, 'source': 'bags'},
                {'slot': 'head', 'item_id': 222, 'source': 'bags'},
            ],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('不可重复选择', response.json()['error'])

    def test_real_simc_export_keeps_main_gear_names_and_excludes_bag_choices(self):
        config = '''# 炎色雷灬 - Fury - 2026-07-10 02:37 - CN/死亡之翼
warrior="炎色雷灬"
level=90
race=orc
region=cn
server=死亡之翼
role=attack
professions=enchanting=100/jewelcrafting=100
spec=fury
talents=ACTIVE_BUILD
# Saved Loadout: 团本屠戮
# talents=SAVED_BUILD
omnium_talents=136817:1/136819:1
# 终夜者的獠牙头盔 (289)
head=,id=249952,enchant_id=8017,gem_id=240892,bonus_id=6652/13534
# 腐沼的孢子之心 (298)
neck=,id=268291,gem_id=240983,bonus_id=6652/13668
# 信徒的流丝罩袍 (285)
back=,id=239656,bonus_id=12214/13667,content_tuning=3615,crafted_stats=32/36,crafting_quality=5
# 旋风虚空裂斧 (298)
main_hand=,id=251117,enchant_id=8041,bonus_id=13440/6652
### Gear from Bags
# 盘绕恶意丝带 (285)
# neck=,id=249337,bonus_id=6652/13668
'''
        detail = parse_manual_player_config(config, 'fury')

        self.assertEqual(detail['identity']['name'], '炎色雷灬')
        self.assertEqual(detail['identity']['region'], 'cn')
        self.assertEqual(detail['identity']['realm'], '死亡之翼')
        self.assertEqual(detail['identity']['role'], 'attack')
        self.assertEqual(detail['identity']['professions'], {'enchanting': 100, 'jewelcrafting': 100})
        self.assertEqual(detail['talents']['build_code'], 'ACTIVE_BUILD')
        self.assertEqual(detail['talents']['saved_loadouts'], [{'name': '团本屠戮', 'build_code': 'SAVED_BUILD'}])
        self.assertEqual(len(detail['equipment']), 4)
        self.assertEqual(detail['equipment'][0]['display_name'], '终夜者的獠牙头盔')
        self.assertEqual(detail['equipment'][0]['item_level'], 289)
        self.assertEqual(detail['equipment'][0]['gems'][0]['id'], 240892)
        self.assertEqual(detail['equipment'][2]['crafted_stats'], ['精通', '全能'])
        self.assertEqual(detail['equipment'][2]['crafting_quality'], 5)
        self.assertEqual(detail['omnium_talents'], [{'id': 136817, 'rank': 1}, {'id': 136819, 'rank': 1}])

    def test_player_config_detail_returns_battlenet_identity_and_explicit_missing_detail(self):
        response = self.client.post(
            '/api/simc-player-config-detail/',
            data=json.dumps({
                'spec': 'fury',
                'player_import_mode': 'battlenet',
                'battlenet_region': 'EU',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        detail = payload['data']
        self.assertEqual(detail['source']['type'], 'battlenet')
        self.assertEqual(detail['identity']['region'], 'eu')
        self.assertEqual(detail['identity']['realm'], 'Kazzak')
        self.assertEqual(detail['identity']['name'], 'Bloodmastêr')
        self.assertEqual(detail['equipment'], [])
        self.assertTrue(detail['missing_fields'])
        self.assertIn('未保存角色装备快照', detail['missing_fields'][0])

    def test_attribute_only_profile_preserves_legacy_data_and_runs_without_player_block(self):
        from botend.models import SimcMasteryCoefficient, SimcSecondaryStatRule

        SimcSecondaryStatRule.objects.update_or_create(
            class_name='warrior',
            defaults={
                'crit_per_percent': 46,
                'haste_per_percent': 44,
                'mastery_per_percent': 46,
                'versatility_per_percent': 54,
            },
        )
        SimcMasteryCoefficient.objects.update_or_create(
            spec='fury', defaults={'mastery_coefficient': 1.4}
        )
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Legacy fury stats',
            spec='fury',
            # 历史记录曾因字段默认值被写成 battlenet，但没有任何角色/装备数据；
            # 读取时必须仍按属性型配置处理。
            player_config_mode='battlenet',
            player_equipment='',
            battlenet_region='',
            battlenet_realm='',
            battlenet_character='',
            talent='LEGACY_BUILD',
            gear_crit=1000,
            gear_haste=2000,
            gear_mastery=3000,
            gear_versatility=4000,
        )

        detail_response = self.client.get(f'/api/simc-profile/{profile.id}/')
        self.assertEqual(detail_response.status_code, 200)
        detail_payload = detail_response.json()
        self.assertTrue(detail_payload['success'], detail_payload)
        self.assertEqual(detail_payload['player_config_mode'], 'attribute_only')
        self.assertEqual(detail_payload['player_equipment'], '')
        self.assertEqual(detail_payload['battlenet_region'], '')

        update_response = self.client.put(
            '/api/simc-profile/',
            data=json.dumps({
                'id': profile.id,
                'name': 'Legacy fury stats updated',
                'spec': 'fury',
                'player_config_mode': 'attribute_only',
                'talent': 'UPDATED_BUILD',
                'gear_crit': 1100,
                'gear_haste': 2200,
                'gear_mastery': 3300,
                'gear_versatility': 4400,
            }),
            content_type='application/json',
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertTrue(update_response.json()['success'], update_response.json())
        profile.refresh_from_db()
        self.assertEqual(profile.player_config_mode, 'attribute_only')
        self.assertEqual(profile.talent, 'UPDATED_BUILD')
        self.assertEqual(profile.player_equipment, '')
        self.assertEqual(profile.battlenet_character, '')

        detail_response = self.client.post(
            '/api/simc-player-config-detail/',
            data=json.dumps({
                'spec': 'fury', 'player_config_mode': 'attribute_only',
                'talent': profile.talent, 'gear_strength': 5000,
                'gear_crit': profile.gear_crit, 'gear_haste': profile.gear_haste,
                'gear_mastery': profile.gear_mastery, 'gear_versatility': profile.gear_versatility,
            }), content_type='application/json',
        )
        self.assertEqual(detail_response.status_code, 200)
        detail_payload = detail_response.json()
        self.assertTrue(detail_payload['success'], detail_payload)
        detail = detail_payload['data']
        self.assertEqual(detail['source']['type'], 'attribute_only')
        self.assertEqual(detail['talents']['build_code'], 'UPDATED_BUILD')
        self.assertEqual(detail['stats']['primary']['strength'], 5000)
        self.assertEqual(detail['stats']['secondary']['crit']['rating'], 1100)
        self.assertAlmostEqual(detail['stats']['secondary']['crit']['percent'], 23.91, places=2)
        self.assertAlmostEqual(detail['stats']['secondary']['mastery']['percent'], 100.43, places=2)
        self.assertEqual(detail['equipment'], [])
        self.assertIn('未提供玩家身份', detail['missing_fields'][0])

        task_response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Legacy fury attributes',
                'task_type': 1,
                'spec': 'fury',
                'player_config_mode': 'attribute_only',
                'talent': profile.talent,
                'gear_crit': profile.gear_crit,
                'gear_haste': profile.gear_haste,
                'gear_mastery': profile.gear_mastery,
                'gear_versatility': profile.gear_versatility,
            }),
            content_type='application/json',
        )
        self.assertEqual(task_response.status_code, 200)
        task_payload = task_response.json()
        self.assertTrue(task_payload['success'], task_payload)
        task = SimcTask.objects.get(id=task_payload['data']['id'])
        task_ext = json.loads(task.ext)
        self.assertEqual(task_ext['player_config_mode'], 'attribute_only')
        self.assertEqual(task_ext['talent'], 'UPDATED_BUILD')
        self.assertEqual(task_ext['gear_versatility'], 4400)

    def test_attribute_only_profile_load_contract_keeps_equipment_empty(self):
        """工作台加载历史属性配置时，属性只能进入专用字段，不能污染隐藏装备框。"""
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Legacy workbench load contract',
            spec='fury',
            player_config_mode='battlenet',  # 新字段迁移时的错误历史默认值。
            player_equipment='',
            battlenet_region='',
            battlenet_realm='',
            battlenet_character='',
            talent='WORKBENCH_BUILD',
            gear_crit=401,
            gear_haste=1100,
            gear_mastery=1140,
            gear_versatility=100,
        )

        response = self.client.get(f'/api/simc-profile/{profile.id}/')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['player_config_mode'], 'attribute_only')
        self.assertEqual(payload['talent'], 'WORKBENCH_BUILD')
        self.assertEqual(payload['player_equipment'], '')
        self.assertFalse(payload['battlenet_region'])
        self.assertFalse(payload['battlenet_realm'])


class SimcBattlenetPreflightTests(TestCase):
    """Battle.net 提交前预检必须真实获取角色信息，而不是只回显 armory 三元组。"""

    def setUp(self):
        self.user = User.objects.create_user(username='battlenet_preflight_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_preflight_returns_fetched_character_and_simc_readiness(self):
        from unittest.mock import patch

        fetched = {
            'identity': {
                'name': 'Bloodmastêr', 'realm': 'Kazzak', 'region': 'eu',
                'class_name': 'warrior', 'level': 80,
            },
            'spec': {'key': 'fury', 'name': 'Fury'},
            'equipment': {'count': 15, 'item_level': 680},
            'stats': {'secondary': {'crit': {'rating': 1000}}},
            'simc_ready': True,
            'warnings': [],
        }
        with patch('botend.services.battlenet_preflight.fetch_battlenet_character_preflight', return_value=fetched) as fetch:
            response = self.client.post('/api/simc-battlenet-preflight/', data=json.dumps({
                'region': 'EU', 'realm': 'Kazzak', 'character': 'Bloodmastêr', 'spec': 'fury',
            }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertTrue(payload['data']['simc_ready'])
        self.assertEqual(payload['data']['identity']['name'], 'Bloodmastêr')
        self.assertEqual(payload['data']['spec']['key'], 'fury')
        fetch.assert_called_once_with(region='eu', realm='Kazzak', character='Bloodmastêr', requested_spec='fury')

    def test_preflight_service_parses_live_stats_and_rejects_missing_talent(self):
        from botend.services.battlenet_preflight import fetch_battlenet_character_preflight

        profile = {
            'name': 'Bloodmastêr', 'level': 80,
            'character_class': {'name': 'Warrior'},
            'active_spec': {'name': 'Fury'},
            'realm': {'name': 'Kazzak'},
        }
        equipment = {'equipped_items': [{'level': {'value': 680}}]}
        stats = {
            'strength': {'effective': 5000},
            'melee_crit': {'rating': 1000, 'value': 20.0},
            'melee_haste': {'rating': 2000, 'value': 15.0},
            'mastery': {'rating': 3000, 'value': 30.0},
            'versatility': {'rating': 4000, 'damageDoneBonus': 10.0},
        }
        with patch('botend.services.battlenet_preflight._token', return_value='token'), patch(
            'botend.services.battlenet_preflight._api_get', side_effect=[profile, equipment, stats]
        ):
            result = fetch_battlenet_character_preflight(
                region='eu', realm='Kazzak', character='Bloodmastêr', requested_spec='fury',
            )

        self.assertTrue(result['simc_ready'], result)
        self.assertEqual(result['stats']['primary']['strength'], 5000)
        self.assertEqual(result['stats']['secondary']['crit']['rating'], 1000)
        self.assertEqual(result['simc_config']['gear_strength'], 5000)
        self.assertEqual(result['simc_config']['gear_versatility'], 4000)
        self.assertEqual(result['simc_config']['talent'], '')
        self.assertEqual(result['warnings'], [])

    def test_preflight_normalizes_spaced_battlenet_class_name(self):
        from botend.services.battlenet_preflight import fetch_battlenet_character_preflight

        profile = {
            'name': 'Bloodmastêr', 'level': 90,
            'character_class': {'name': 'Death Knight'},
            'active_spec': {'name': 'Blood'},
            'realm': {'name': 'Kazzak'},
        }
        equipment = {'equipped_items': [{'level': {'value': 292}}]}
        with patch('botend.services.battlenet_preflight._token', return_value='token'), patch(
            'botend.services.battlenet_preflight._api_get', side_effect=[profile, equipment, {}]
        ):
            result = fetch_battlenet_character_preflight(
                region='eu', realm='Kazzak', character='Bloodmastêr', requested_spec='blood',
            )

        self.assertEqual(result['identity']['class_name'], 'deathknight')
        self.assertTrue(result['simc_ready'], result)
        self.assertEqual(result['warnings'], [])
