"""日志天赋来源、选项身份、模板和展示分母回归。"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from botend.services.log_talent_analysis import prepare_log_talents
from botend.services.spec_stats_service import (
    _build_talent_usage_snapshot, _compute_talent_build_popularity,
)
from botend.wow.talents.build_code import TalentBuildCodeDecoder


class LogTalentAnalysisTests(SimpleTestCase):
    def setUp(self):
        self.metadata = [
            self.node(101, 1, 'class', '职业功能', max_points=40),
            self.node(201, 2, 'spec', '专精左选项', icon='ability_warrior_charge'),
            self.node(202, 2, 'spec', '专精右选项', icon='ability_warrior_bloodbath'),
            self.node(301, 3, 'hero', '英雄节点', db2_subtree_id=60),
            self.node(401, 4, 'hero_anchor', '英雄入口'),
        ]
        self.decoder = [dict(self.metadata[0]), dict(self.metadata[1]),
                        dict(self.metadata[3]), dict(self.metadata[4])]
        self.decoder[1].update(is_choice_node=True, choice_options=self.metadata[1:3])
        self.provider = Mock()
        self.provider.resolved_version = SimpleNamespace(
            source_dir='测试节点目录', granted_entries_json={}, current_build='测试版本')
        self.provider.get_decoder_node_list.return_value = self.decoder

    @staticmethod
    def node(entry, trait, tree, name, **kwargs):
        node = dict(node_id=entry, talent_id=trait, spell_id=entry + 1000,
                    tree_type=tree, name=name, name_zh=name, max_points=1, db2_subtree_id=0)
        return node | kwargs

    @staticmethod
    def row(name='玩家', choice=201, points=1, hero=301, **kwargs):
        return dict(character_name=name, realm='测试服', region='cn', dps=100,
                    talents_json=[{'talentID': key, 'points': points if key == 101 else 1}
                                  for key in [101, choice, hero, 401]], **kwargs)

    def prepare(self, rows):
        with patch('botend.services.log_talent_analysis.Path.is_file', return_value=True):
            return prepare_log_talents(rows, 'Warrior', 'Arms',
                                       provider=self.provider, metadata=self.metadata)

    def aggregate(self, rows, trees=()):
        with patch('botend.services.spec_stats_service.TalentMetadataProvider', return_value=self.provider):
            return _compute_talent_build_popularity(self.prepare(rows), 'Warrior', 'Arms', trees=trees)

    def test_log_entries_win_over_unrelated_current_character_code(self):
        rows = [self.row(talent_build_code='无关当前角色代码')]
        before = deepcopy(rows)
        prepared = self.prepare(rows)
        self.assertEqual(rows, before)
        self.assertEqual(prepared[0]['_log_talent_status'], 'ok')
        code = prepared[0]['talent_build_code']
        self.assertTrue(TalentBuildCodeDecoder.matches_spec(code, 'Warrior', 'Arms'))
        states = TalentBuildCodeDecoder.decode_node_states(code, self.decoder)
        self.assertEqual(states['spec:201']['choice_selection'], 0)
        self.assertEqual(states['class:101']['points'], 1)

    def test_choice_entry_and_rank_survive_code_round_trip(self):
        prepared = self.prepare([self.row(choice=202, points=2)])
        states = TalentBuildCodeDecoder.decode_node_states(prepared[0]['talent_build_code'], self.decoder)
        self.assertEqual(states['spec:201']['choice_selection'], 1)
        self.assertEqual(states['class:101']['points'], 2)
        node = prepared[0]['_log_talent_state']['nodes']['spec:2']
        self.assertEqual(node['choice_key'], 202)
        self.assertEqual(node['name'], '专精右选项')

    def test_missing_metadata_disables_copy_without_losing_comparable_nodes(self):
        with patch('botend.services.log_talent_analysis.Path.is_file', return_value=False):
            row = prepare_log_talents([self.row()], 'Warrior', 'Arms',
                                     provider=self.provider, metadata=self.metadata)[0]
        self.assertEqual(row['talent_build_code'], '')
        self.assertEqual(row['_log_talent_status'], 'ok')
        with patch('botend.services.spec_stats_service.TalentMetadataProvider', return_value=self.provider):
            result = _compute_talent_build_popularity([row], 'Warrior', 'Arms')
        self.assertEqual(result['hero_groups'][0]['template_count'], 1)
        self.assertEqual(len(result['hero_groups'][0]['template_render_model']['trees']), 3)

    def test_missing_decoder_options_do_not_merge_opposite_choices(self):
        self.provider.get_decoder_node_list.return_value = []
        result = self.aggregate([self.row(choice=201), self.row(choice=202)])
        self.assertEqual(result['total'], 2)
        group = result['hero_groups'][0]
        self.assertEqual(group['build_count'], 2)
        self.assertEqual(group['family_count'], 2)
        self.assertTrue(all(not build['code'] for family in [group['template_family'], *group['other_families']]
                            for build in family['builds']))

    def test_missing_unknown_conflicting_and_partial_samples_are_accounted(self):
        missing = dict(talents_json=[], talent_build_code='角色当前代码')
        unknown = self.row()
        unknown['talents_json'].append({'talentID': 999999, 'points': 1})
        conflict = self.row()
        conflict['talents_json'].append({'talentID': 202, 'points': 1})
        partial = self.row()
        partial['talents_json'] = partial['talents_json'][:2]
        result = self.aggregate([self.row(), missing, unknown, conflict, partial])
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['sample_total'], 5)
        self.assertEqual(result['excluded_count'], 4)
        for kind in ('missing', 'unresolved', 'conflict', 'partial'):
            self.assertEqual(result[kind + '_count'], 1)
        self.assertEqual(result['hero_groups'][0]['template_family']['builds'][0]['pct'], 100)

    def test_malformed_rank_does_not_crash_or_become_a_false_zero(self):
        for value in ('坏数据', None, 1.5, -1):
            with self.subTest(value=value):
                row = self.row()
                row['talents_json'][0]['points'] = value
                result = self.aggregate([row])
                self.assertEqual(result['total'], 0)
                self.assertEqual(result['excluded_count'], 1)

    def test_all_variants_count_once_and_long_tail_is_not_cut_at_twenty(self):
        rows = [self.row(name=f'日志{i}', points=i) for i in range(1, 26)]
        rows += [self.row(name='重复模板', points=1)] * 4
        result = self.aggregate(rows)
        self.assertEqual(result['total'], 29)
        group = result['hero_groups'][0]
        self.assertEqual(group['build_count'], 25)
        self.assertEqual(group['family_count'], 1)
        self.assertEqual(group['template_family']['count'], 29)
        self.assertEqual(group['template_count'], 5)
        self.assertEqual(sum(build['count'] for build in group['template_family']['builds']), 29)
        self.assertEqual(sum(change['count'] for change in group['common_changes']), 24)

    def test_template_and_core_family_have_separate_usage_and_correct_deltas(self):
        rows = [self.row('基准')] * 3 + [self.row('功能调整', points=2)] * 2 + [
            self.row('专精调整', choice=202)]
        result = self.aggregate(rows)
        group = result['hero_groups'][0]
        self.assertEqual(group['template_count'], 3)
        self.assertEqual(group['template_family']['count'], 5)
        self.assertEqual(group['template_family']['builds'][0]['pct'], 50)
        self.assertEqual(group['template_family']['pct'], 83.3)
        changed = next(change for change in group['common_changes'] if change['tree_type'] == 'spec')
        self.assertEqual((changed['before_name'], changed['after_name']),
                         ('专精左选项', '专精右选项'))
        self.assertEqual(changed['count'], 1)
        self.assertEqual(changed['before_icon'], 'ability_warrior_charge')
        self.assertEqual(changed['after_icon'], 'ability_warrior_bloodbath')
        self.assertEqual(len(group['common_changes']), 2)
        prepared = self.prepare(rows)
        with patch('botend.services.spec_stats_service.TalentMetadataProvider', return_value=self.provider):
            usage = _build_talent_usage_snapshot(prepared, 'Warrior', 'Arms')
        self.assertEqual(usage['total'], result['total'])
        right = next(item for item in usage['usage_list'] if item['node_id'] == 202)
        self.assertEqual(right['count'], 1)

    def test_independent_hero_templates_diagrams_and_changes_survive_storage(self):
        from botend.services.stats_storage_codec import pack_detail, unpack_detail
        second = self.node(302, 5, 'hero', '巨神兵节点', db2_subtree_id=62)
        self.metadata.append(second)
        self.decoder.append(second)
        rows = [self.row()] * 3 + [self.row(points=2)] + [
            self.row(hero=302, choice=202)] * 2 + [self.row(hero=302, choice=201)]
        trees = [
            {'tree_type': 'spec', 'nodes': [dict(self.metadata[1], is_choice_node=True, choice_options=self.metadata[2:0:-1]),
                                            self.node(203, 6, 'spec', '未点节点', parents=[201])]},
            {'tree_type': 'hero', 'subtree_id': 60, 'nodes': [self.metadata[3]]},
            {'tree_type': 'hero', 'subtree_id': 62, 'nodes': [second]},
        ]
        result = self.aggregate(rows, trees)
        detail = {'talent_build_popularity': result}
        self.assertEqual(unpack_detail(pack_detail(detail)), detail)
        from botend.portal.spec_detail_views import _talent_build_popularity_has_builds
        self.assertTrue(_talent_build_popularity_has_builds(detail, 'Warrior', 'Arms'))
        slayer, colossus = result['hero_groups']
        self.assertEqual([(g['subtree_id'], g['total'], g['template_count'])
                          for g in (slayer, colossus)], [(60, 4, 3), (62, 3, 2)])
        self.assertNotEqual(slayer['template_code'], colossus['template_code'])
        for group, spec_name, hero_name in [(slayer, '专精左选项', '英雄节点'),
                                            (colossus, '专精右选项', '巨神兵节点')]:
            primary = group['template_family']['builds'][0]
            self.assertTrue(primary['is_template'])
            self.assertEqual(primary['template_diffs'], [])
            self.assertEqual(len(group['common_changes']), 1)
            _, hero, spec = group['template_render_model']['trees']
            self.assertEqual([node['name'] for node in hero['nodes']], [hero_name])
            self.assertEqual([(n['name'], n['points']) for n in spec['nodes']],
                             [(spec_name, 1), ('未点节点', 0)])
            selected = spec['nodes'][0]
            option = selected['choice_options'][selected['selected_choice_index']]
            self.assertEqual((selected['name'], selected['spell_id']), (option['name'], option['spell_id']))
            self.assertEqual(len(spec['paths']), 1)
        self.assertEqual(slayer['common_changes'][0]['pct'], 25)
        change = colossus['common_changes'][0]
        self.assertEqual((change['before_name'], change['after_name'], change['pct']),
                         ('专精右选项', '专精左选项', 33.3))

        from bs4 import BeautifulSoup
        html = render_to_string('portal/spec_detail/_talent_build_analysis.html', {'popularity': result})
        section = BeautifulSoup(html, 'html.parser')
        self.assertEqual(len(section.select('.log-talent-switch:checked')), 1)
        for group in result['hero_groups']:
            switch = section.select_one(f'#select-talent-hero-{group["subtree_id"]}')
            panel = section.find(id=switch['aria-controls'])
            self.assertIn(group['hero_talent_name'], panel.select_one('.log-talent-primary h5').get_text())
            self.assertEqual(len(panel.select('.log-talent-primary .talent-render-stage')), 1)
            self.assertFalse(panel.select('.log-talent-family .talent-render-stage'))

    def test_tied_templates_are_stable_for_each_hero(self):
        rows = [self.row(choice=201), self.row(choice=202)]
        forward = self.aggregate(rows)['hero_groups'][0]
        reverse = self.aggregate(list(reversed(rows)))['hero_groups'][0]
        self.assertEqual(forward['template_code'], reverse['template_code'])
        self.assertEqual(forward['template_render_model']['trees'], reverse['template_render_model']['trees'])

    def test_both_pages_render_template_diffs_coverage_and_log_copy(self):
        result = self.aggregate([self.row()] * 3 + [self.row(choice=202), dict(talents_json=[])])
        for kind, detail_key in [('raid', 'boss_detail'), ('dungeon', 'dungeon_detail')]:
            with self.subTest(kind=kind):
                html = render_to_string(f'portal/spec_detail/{kind}_stats.html', {
                    'class_name': 'Warrior', 'spec_name': 'Arms', 'selected_dungeon_id': '1',
                    detail_key: {'sample_size': 5, 'talent_build_popularity': result},
                })
                self.assertIn('最常用完整模板', html)
                self.assertIn('复制主模板', html)
                self.assertEqual(html.count('class="talent-render-stage"'), 1)
                self.assertNotIn('class="log-talent-switch"', html)
                self.assertIn('日志未提供天赋 1 份', html)
                self.assertIn('专精左选项 1 点</span>', html)
                self.assertIn('专精右选项 1 点</span>', html)
                self.assertIn('width="18" height="18" loading="lazy"', html)
                self.assertNotIn('天赋字符串使用率', html)
                self.assertNotIn('未解析选项', html)

    def test_legacy_snapshot_is_not_presented_as_log_template(self):
        for version in (None, 1, 2):
            html = render_to_string('portal/spec_detail/_talent_build_analysis.html', {
                'popularity': {'total': 48, 'template_code': '旧模板代码', 'hero_groups': [{}],
                               'analysis_version': version},
            })
            self.assertIn('等待后台重新聚合', html)
            self.assertNotIn('旧模板代码', html)

    def test_collection_preserves_entry_identity_without_inventing_spell_ids(self):
        from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
        ranking = SpecDetailBase.parse_wcl_talents([
            {'talentID': 202, 'points': 1}, '损坏条目'])
        combatant = SpecDetailBase.parse_wcl_talent_tree([
            {'id': 202, 'nodeID': 2, 'rank': 1}, None])
        for rows in (ranking, combatant):
            self.assertEqual(rows[0]['entry_id'], 202)
            self.assertIsNone(rows[0]['spell_id'])
            self.assertTrue(rows[1]['invalid'])
        self.assertEqual(combatant[0]['db2_trait_node_id'], 2)


    def test_legacy_entry_alias_is_not_treated_as_conflicting_spell_identity(self):
        row = self.row()
        for raw in row['talents_json']:
            raw['spellID'] = raw['talentID']
            raw['spell_id'] = raw['talentID']
        self.assertEqual(self.prepare([row])[0]['_log_talent_status'], 'ok')

    def test_explicit_old_spell_is_not_counted_as_replacement_talent(self):
        row = self.row()
        row['talents_json'][0]['spellID'] = 999999
        self.assertEqual(self.prepare([row])[0]['_log_talent_status'], 'conflict')
