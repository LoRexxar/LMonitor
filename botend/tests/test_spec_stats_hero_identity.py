"""Spec-scoped hero-tree and talent-sample integrity regressions."""
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import RequestFactory, SimpleTestCase

from botend.constants.hero_talents import spec_hero_subtree_ids
from botend.constants.wow import CLASS_SPEC_MAP
from botend.services.spec_stats_service import (
    _build_talent_usage_snapshot, _compute_talent_build_popularity,
    _filter_hero_subtrees_for_spec, _merge_player_profile_fields,
)
from botend.wow.talents.build_code import TalentBuildCodeDecoder


class DungeonHeroIdentityTests(SimpleTestCase):
    PROTECTION_CODE = ('CkEAAAAAAAAAAAAAAAAAAAAAAkBAAGzwMzMzMmNzMLzYMGNzMGWMmZGzwMDAAAAWmZAmxAMwGssY0YGAzSMzGMmZGGbDAmZAAYGwA')
    ARMS_CODE = ('CcEAjLzRlq54bI5v+r8Sr9Xw4jZmZmFzYmZGAAAghphZGmZzMzMzYmxMDAAAAgxyMDsFGLLDsAGwMMBmBbgZGGGMbzsNAzMAYM8AA')

    def test_all_specializations_have_exactly_two_authoritative_hero_ids(self):
        for class_name, specs in CLASS_SPEC_MAP.items():
            for spec_name in specs:
                with self.subTest(class_name=class_name, spec_name=spec_name):
                    self.assertEqual(len(spec_hero_subtree_ids(class_name, spec_name)), 2)
        self.assertEqual(spec_hero_subtree_ids('Warrior', 'Protection'), frozenset({61, 62}))
        self.assertEqual(TalentBuildCodeDecoder.resolve_spec_identity(self.PROTECTION_CODE), ('Warrior', 'Protection'))
        self.assertEqual(TalentBuildCodeDecoder.resolve_spec_identity(self.ARMS_CODE), ('Warrior', 'Arms'))

    @staticmethod
    def _provider(provider_cls):
        provider = provider_cls.return_value
        provider.get_decoder_node_list.return_value = []
        provider.merge_into_node.side_effect = lambda node, class_name='', spec_name='': {
            **node,
            **({'tree_type': 'hero', 'db2_subtree_id': {117381: 60, 61701: 61}[int(node['talent_id'])]}
               if int(node.get('talent_id') or 0) in (117381, 61701) else {}),
        }
        return provider

    @staticmethod
    def _row(name, hero_talent_id, code='', dps=100):
        return {
            'character_name': name, 'realm': 'Realm', 'region': 'CN', 'dps': dps,
            'talent_build_code': code,
            'talents_json': [
                {'tree_type': 'spec', 'talent_id': 5, 'spell_id': 50, 'points': 1, 'name': '专精节点'},
                # WCL marks this as spec; metadata knows its actual hero subtree.
                {'tree_type': 'spec', 'talent_id': hero_talent_id, 'spell_id': hero_talent_id,
                 'points': 1, 'name': '英雄节点'},
            ],
        }

    @patch('botend.services.spec_stats_service.TalentMetadataProvider')
    def test_offspec_wcl_hero_excludes_entire_talent_sample(self, provider_cls):
        self._provider(provider_cls)
        rows = [self._row('valid', 61701), self._row('arms-talents-on-prot-log', 117381)]
        snapshot = _build_talent_usage_snapshot(rows, 'Warrior', 'Protection')
        self.assertEqual(snapshot['total'], 1)
        self.assertEqual(snapshot['hero_subtree_counts'], {61: 1})
        self.assertTrue(all('117381' not in item['node_key'] for item in snapshot['usage_list']))
        # The caller still owns both ranking facts for DPS/gear; this filters
        # only the talent cohort, not the original log table.
        self.assertEqual(len(rows), 2)

    @patch('botend.services.spec_stats_service.WowTalentNodeMetadata.objects.filter')
    @patch('botend.services.spec_stats_service.TalentMetadataProvider')
    def test_profile_code_and_structured_hero_both_must_match_target(self, provider_cls, anchors):
        self._provider(provider_cls)
        anchors.return_value.exclude.return_value.values.return_value = [
            {'db2_subtree_id': 61, 'name': 'Mountain Thane', 'name_zh': '山丘领主'},
        ]
        rows = [
            self._row('valid', 61701, self.PROTECTION_CODE),
            self._row('wrong-hero', 117381, self.PROTECTION_CODE),
            self._row('wrong-code', 61701, self.ARMS_CODE),
            self._row('unverifiable-code', 61701, 'LEGACY_CODE'),
        ]
        result = _compute_talent_build_popularity(rows, 'Warrior', 'Protection')
        self.assertEqual(result['total'], 1)
        self.assertEqual([g['hero_talent_name'] for g in result['hero_groups']], ['山丘领主'])
        self.assertEqual([b['code'] for b in result['builds']], [self.PROTECTION_CODE])

    @patch('botend.services.spec_stats_service.WowTalentNodeMetadata.objects.filter')
    def test_hero_panel_cannot_take_an_offspec_used_or_anchor_tree(self, anchors):
        anchors.return_value.exclude.return_value.values_list.return_value = [60, 62]
        subtrees = {key: [SimpleNamespace(column=key, count=1, usage_pct=1)] for key in (60, 61, 62)}
        result = _filter_hero_subtrees_for_spec(subtrees, 'Warrior', 'Protection', [60, 62])
        self.assertEqual(set(result), {61, 62})

    def test_cached_offspec_hero_panel_and_unknown_code_are_stale(self):
        from botend.portal.spec_detail_views import (
            _talent_tree_matches_spec, _talent_build_popularity_has_builds,
        )
        detail = {'talent_popularity_tree': {'render_model': {'trees': [
            {'tree_type': 'hero', 'subtree_id': 62, 'nodes': [{}]},
            {'tree_type': 'hero', 'subtree_id': 60, 'nodes': [{}]},
        ]}}}
        self.assertFalse(_talent_tree_matches_spec(detail, 'Warrior', 'Protection'))
        detail['talent_popularity_tree']['render_model']['trees'][1]['subtree_id'] = 61
        self.assertTrue(_talent_tree_matches_spec(detail, 'Warrior', 'Protection'))
        detail['talent_build_popularity'] = {
            'semantic_state_version': 2,
            'builds': [{'code': 'LEGACY_CODE', 'top_players': []}],
        }
        self.assertFalse(_talent_build_popularity_has_builds(detail, 'Warrior', 'Protection'))

    @patch('botend.services.spec_stats_service._selected_player_profiles')
    def test_unverifiable_raiderio_code_cannot_overwrite_verified_ranking(self, selected_profiles):
        selected_profiles.return_value = {('cn', 'realm', 'player'): {'talent_build_code': 'INVALID_CODE'}}
        ranking = {'region': 'CN', 'realm': 'Realm', 'character_name': 'Player',
                   'talent_build_code': self.PROTECTION_CODE}
        merged = _merge_player_profile_fields(
            [ranking], 3, 'Warrior', 'Protection', fields=('talent_build_code',),
        )
        self.assertEqual(merged[0]['talent_build_code'], self.PROTECTION_CODE)

    @patch('botend.portal.spec_detail_views.render')
    @patch('botend.portal.spec_detail_views.SpecStatsService.get_dungeon_detail')
    @patch('botend.portal.spec_detail_views._load_json')
    @patch('botend.portal.spec_detail_views._base_context')
    def test_live_dungeon_view_recomputes_only_offspec_cached_hero_panel(
            self, base_context, load_json, get_detail, render):
        from botend.portal.spec_detail_views import SpecDetailDungeonView
        base_context.return_value = {
            'season': SimpleNamespace(id=3, mplus_encounters=[{'id': 61762, 'name': "Kings' Rest"}]),
        }
        detail = {
            'dungeon_id': 61762,
            'talent_popularity_tree': {
                'render_model': {'trees': [
                    {'tree_type': 'hero', 'subtree_id': 60, 'nodes': [{}]},
                    {'tree_type': 'hero', 'subtree_id': 62, 'nodes': [{}]},
                ]},
                'point_statistics_version': 2,
                'usage': [{'selection_pct': 50, 'point_distribution': []}],
            },
            'secondary_stats': [], 'field_sources': {},
            'talent_build_popularity': {
                'semantic_state_version': 2,
                'builds': [{'code': self.PROTECTION_CODE, 'top_players': []}],
            },
        }
        load_json.return_value = {'dungeons': [detail]}
        corrected = {'dungeon_id': 61762, 'sample_size': 100, 'corrected': True}
        get_detail.return_value = corrected
        request = RequestFactory().get('/portal/spec/Warrior/Protection/dungeons/?dungeon_id=61762')
        SpecDetailDungeonView().get(request, 'Warrior', 'Protection')
        get_detail.assert_called_once_with(61762, 'Warrior', 'Protection', 3)
        self.assertIs(render.call_args.args[2]['dungeon_detail'], corrected)

    @patch('botend.management.commands.aggregate_spec_stats.os.makedirs')
    @patch('botend.management.commands.aggregate_spec_stats.Command._aggregate_leaderboard')
    @patch('botend.management.commands.aggregate_spec_stats.Command._aggregate_raid')
    @patch('botend.management.commands.aggregate_spec_stats.Command._aggregate_dungeon')
    @patch('botend.management.commands.aggregate_spec_stats.SeasonMeta.objects.filter')
    def test_dungeon_only_refresh_touches_all_specs_without_raid_or_players(
            self, season_filter, dungeon, raid, players, mkdir):
        season_filter.return_value.first.return_value = SimpleNamespace(id=3, season_key='season-3')
        call_command('aggregate_spec_stats', season=3, dungeon_only=True)
        self.assertEqual(dungeon.call_count, sum(len(specs) for specs in CLASS_SPEC_MAP.values()))
        self.assertTrue(all(args.args[0].id == 3 for args in dungeon.call_args_list))
        raid.assert_not_called()
        players.assert_not_called()
