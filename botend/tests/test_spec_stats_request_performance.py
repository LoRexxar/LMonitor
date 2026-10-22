"""Request-local statistics reuse, fact fidelity, and bounded ORM hydration."""
from copy import deepcopy
from unittest.mock import patch
from django.db import connection
from django.db.models.query import ValuesIterable
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from botend.models import SpecRaidRanking
from botend.services import spec_stats_service as stats
import copy
from botend.models import PlayerSpecTopPlayer

# Request-local reuse and bounded raid hydration contracts (real ORM/imports).




class StatsRequestPerformanceTests(TestCase):
    def test_decoder_reuse_keeps_record_specific_stale_fallback(self):
        from botend.wow.talents.build_code import TalentBuildCodeEncoder
        nodes = [
            {'tree_type': 'class', 'talent_id': 1, 'node_id': 1, 'spell_id': 11, 'max_points': 1,
             'is_choice_node': True, 'choice_options': [
                 {'spell_id': 11, 'name': 'Left'}, {'spell_id': 12, 'name': 'Right'}]},
            {'tree_type': 'spec', 'talent_id': 2, 'node_id': 2, 'spell_id': 22, 'max_points': 3},
        ]
        code = TalentBuildCodeEncoder.encode_node_states(
            'CcEAjLzRlq54bI5v+r8Sr9Xw4jZmZmFzYmZGAAAghphZGmZzMzMzYmxMDAAAAgxyMDsFGLLDsAGwMMBmBbgZGGGMbzsNAzMAYM8AA',
            nodes, [dict(nodes[0], points=1, choice_selection=1)])
        self.assertTrue(code)
        records = [dict(talent_build_code=code, talents_json=[
            dict(nodes[0], points=1, choice_selection=1), dict(nodes[1], points=points)])
            for points in (2, 3, 2)]
        provider = stats.TalentMetadataProvider(usage=stats.TalentVersionResolver.USAGE_STATS)
        by_key = {stats.TalentBuildCodeService._build_node_key(n): n for n in nodes}
        before = deepcopy(records)
        expected = [stats._talent_build_semantic_state(r, provider, 'Warrior', 'Arms', nodes, by_key)
                    for r in records]
        context = {}
        with patch.object(stats.TalentBuildCodeDecoder, 'decode_node_states',
                          wraps=stats.TalentBuildCodeDecoder.decode_node_states) as decode:
            actual = [stats._talent_build_semantic_state(r, provider, 'Warrior', 'Arms', nodes, by_key,
                                                   context=context) for r in records]
        self.assertEqual(actual, expected)
        self.assertNotEqual(actual[0][0], actual[1][0])
        self.assertEqual(actual[0], actual[2])
        self.assertEqual(records, before)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(context[('decoded', code)],
                         stats.TalentBuildCodeDecoder.decode_node_states(code, nodes))

    def test_hero_summary_cache_preserves_nullable_subtrees_and_record_counts(self):
        first_nodes = {None: [object()], 99: [object(), object()]}
        expected = [
            {'subtree_id': None, 'name': '英雄天赋', 'selected_count': 1},
            {'subtree_id': 99, 'name': '英雄天赋 99', 'selected_count': 2},
        ]
        self.assertEqual(stats._build_hero_talent_summary(first_nodes, 'Warrior', 'Arms'), expected)
        context = {}
        first = stats._build_hero_talent_summary(first_nodes, 'Warrior', 'Arms', context=context)
        self.assertEqual(first, expected)
        first[0]['name'] = 'caller mutation'
        second_nodes = {99: [object()], None: [object(), object(), object()]}
        with self.assertNumQueries(0):
            second = stats._build_hero_talent_summary(second_nodes, 'Warrior', 'Arms', context=context)
        self.assertEqual(second, [dict(expected[0], selected_count=3),
                                  dict(expected[1], selected_count=1)])

    def test_build_reuses_observations_without_losing_players_or_overrides(self):
        node = {'node_id': 123, 'spell_id': 456, 'tree_type': 'hero',
                'db2_subtree_id': 60, 'name': 'Original', 'points': 1}
        arms_code = 'CcEAjLzRlq54bI5v+r8Sr9Xw4jZmZmFzYmZGAAAghphZGmZzMzMzYmxMDAAAAgxyMDsFGLLDsAGwMMBmBbgZGGGMbzsNAzMAYM8AA'
        records = [dict(talents_json=[deepcopy(node)], talent_build_code=arms_code,
                        character_name=f'Player{i}', realm='Realm', region='EU', dps=i)
                   for i in range(12)]
        records[-1]['talents_json'][0].update(points=2, name='Override')
        before = deepcopy(records)
        with patch.object(stats, '_normalize_stats_talent_node', wraps=stats._normalize_stats_talent_node) as normalize, \
             patch.object(stats, '_hero_subtree_name_from_table', wraps=stats._hero_subtree_name_from_table) as hero:
            result = stats._compute_talent_build_popularity(records, 'Warrior', 'Arms')
        self.assertEqual(records, before)
        self.assertEqual(result['total'], 12)
        self.assertEqual(sorted(b['count'] for b in result['builds']), [1, 11])
        self.assertEqual(len(result['builds'][0]['top_players']), 5)
        self.assertEqual(normalize.call_count, 2)
        self.assertEqual(hero.call_count, 0)  # Canonical ID 60 supplies the name locally.
        # No cross-request state: a later observation must be resolved again.
        with patch.object(stats, '_normalize_stats_talent_node', wraps=stats._normalize_stats_talent_node) as normalize:
            self.assertEqual(stats._compute_talent_build_popularity(records, 'Warrior', 'Arms'), result)
            self.assertEqual(normalize.call_count, 2)

    def test_raid_top5_stable_order_bounded_json_and_shared_profile_scan(self):
        for i, dps in enumerate([0, -1, 100, 100, 80, 70, 60, 50, 40, 30, 20, 10]):
            SpecRaidRanking.objects.create(season_id=987, boss_id=3379, boss_name='Boss',
                difficulty=5, class_name='Warrior', spec_name='Arms',
                character_name=f'P{i}', realm='Realm', region='EU', dps=dps,
                talents_json=[], gear_json=[{'id': i}], kill_time=60000)
        qs = SpecRaidRanking.objects.filter(season_id=987)
        fields = ('talents_json', 'gear_json', 'faction', 'guild_name',
                  'character_name', 'realm', 'region', 'dps', 'kill_time')
        expected = sorted(qs.values(*fields), key=lambda r: r['dps'] or 0, reverse=True)[:5]
        for row in expected:
            row['kill_time_fmt'] = '1:00'
        consumed = []
        old = ValuesIterable.__iter__
        def tracked(it):
            for row in old(it):
                if it.queryset.model is SpecRaidRanking:
                    consumed.append(tuple(it.queryset.query.values_select))
                yield row
        with patch.object(ValuesIterable, '__iter__', tracked), CaptureQueriesContext(connection) as queries:
            result = stats.SpecStatsService._compute_raid_stats(987, 3379, 'Boss', 'Warrior', 'Arms', full=True)
        self.assertEqual(result['sample_size'], 12)
        self.assertEqual(result['top5'], expected)
        self.assertEqual(sum('guild_name' in cols and 'gear_json' in cols for cols in consumed), 5)
        identity_scans = [q for q in queries if 'wow_spec_top_player' in q['sql'].lower()
                          and 'talent_build_code' not in q['sql'] and 'gear_json' not in q['sql']
                          and 'stats_json' not in q['sql']]
        self.assertEqual(len(identity_scans), 1)
        self.assertFalse(any(
            'wow_spec_top_player' in query['sql'].lower() and 'talent_build_code' in query['sql']
            for query in queries
        ), '团本天赋不能查询或回填当前角色导入码')


# Performance contracts: preserve facts while eliminating repeated work.




class StatsPerformanceTests(TestCase):
    def test_snapshot_reuses_only_identical_observations(self):
        raw = {'node_id': 101, 'spell_id': 201, 'points': 1, 'max_points': 2,
               'name': 'First', 'parents': [], 'choice_selection': 0,
               'choice_options': [{'node_id': 301, 'name': 'Option'}]}
        other = dict(raw, node_id=102, spell_id=202, parents=[101])
        changed = dict(raw, points=2, choice_selection=1, name='Changed')
        records = [{'character_name': f'P{i}', 'talents_json': copy.deepcopy([raw, other])}
                   for i in range(3)]
        records.append({'character_name': 'P3', 'talents_json': [changed, other]})
        original = copy.deepcopy(records)
        with patch.object(stats, '_talent_usage_player_payload', wraps=stats._talent_usage_player_payload) as players, \
             patch.object(stats, '_normalize_stats_talent_node', wraps=stats._normalize_stats_talent_node) as normalize:
            result = stats._build_talent_usage_snapshot(records, 'Warrior', 'Arms')
        self.assertEqual(result['total'], 4)
        node = result['usage_map']['spec:101']
        self.assertEqual(node['point_distribution'], [
            {'points': 1, 'count': 3, 'pct': 75.0}, {'points': 2, 'count': 1, 'pct': 25.0}])
        self.assertEqual(result['canonical_nodes']['spec:101'].choice_selection, 1)
        self.assertEqual(result['parent_edges']['spec:102']['spec:101'], 4)
        self.assertEqual(records, original)
        self.assertEqual(players.call_count, 4)
        self.assertEqual(normalize.call_count, 3)

    def test_snapshot_matches_uncached_normalization_for_all_observation_state(self):
        base = {'node_id': 101, 'spell_id': 201, 'points': 1, 'max_points': 2,
                'name': 'Base', 'row': 0, 'column': 1, 'parents': [],
                'is_choice_node': True, 'choice_selection': 0,
                'choice_options': [{'node_id': 301, 'name': 'Option'}]}
        variants = [base, dict(base, points=2), dict(base, points=0),
                    dict(base, name='Override', icon='override', source='wcl'),
                    dict(base, choice_selection=1, choice_options=[{'node_id': 302}]),
                    dict(base, parents=[102], row=None, column=None),
                    dict(base, tree_type='hero', db2_subtree_id=99),
                    dict(base, tree_type='hero_anchor'),
                    {'nodeID': 102, 'spellID': 202, 'maxPoints': 2,
                     'parents_json': [101], 'treeType': 'class', 'tier': 0},
                    {'node_id': 103}, {}, 'not-a-node', None]
        # Invalid hero identities exclude the entire log, not just that node.
        # Keep each normalization variant as an independent observation so the
        # invalid cases cannot erase all valid nodes from this reuse oracle.
        records = [{'character_name': f'P{i}-{j}', 'dps': i,
                    'talents_json': [copy.deepcopy(node)]}
                   for i in range(3) for j, node in enumerate(variants)]
        before = copy.deepcopy(records)
        # An ignored, non-JSON value naturally exercises the uncached fallback,
        # without mocking normalization, metadata merge or model construction.
        uncached = copy.deepcopy(records)
        for record in uncached:
            for node in record['talents_json']:
                if isinstance(node, dict):
                    node['_ignored'] = {1}
        expected = stats._build_talent_usage_snapshot(uncached, 'Warrior', 'Arms')
        actual = stats._build_talent_usage_snapshot(records, 'Warrior', 'Arms')
        self.assertEqual(actual, expected)
        self.assertEqual(records, before)
        actual['canonical_nodes']['spec:101'].parents.append(999)
        self.assertEqual(stats._build_talent_usage_snapshot(records, 'Warrior', 'Arms'), expected)

    def test_profile_json_hydration_is_selected_and_identity_is_reused(self):
        common = dict(season_id=9, class_name='Warrior', spec_name='Arms', realm='Realm', region='eu')
        PlayerSpecTopPlayer.objects.create(**common, character_name='Selected', gear_json=[{'id': 1}], race='Orc')
        PlayerSpecTopPlayer.objects.create(**common, character_name='Unrelated', gear_json=[{'id': 2}])
        for overrides in ({'season_id': 10}, {'spec_name': 'Fury'}):
            PlayerSpecTopPlayer.objects.create(**dict(common, **overrides),
                                              character_name='Selected', gear_json=[{'id': 999}], race='Human')
        records = [dict(region='EU', realm='realm', character_name='selected', gear_json=[])]
        cache = {}
        gear_field = PlayerSpecTopPlayer._meta.get_field('gear_json')
        stats_field = PlayerSpecTopPlayer._meta.get_field('stats_json')
        with CaptureQueriesContext(connection) as queries, \
             patch.object(gear_field, 'from_db_value', wraps=gear_field.from_db_value) as gear_decode, \
             patch.object(stats_field, 'from_db_value', wraps=stats_field.from_db_value) as stats_decode:
            gear = stats._merge_player_profile_gear(records, 9, 'Warrior', 'Arms', profile_cache=cache)
            fields = stats._merge_player_profile_fields(records, 9, 'Warrior', 'Arms', profile_cache=cache)
        self.assertEqual(gear_decode.call_count, 1)
        self.assertEqual(stats_decode.call_count, 1)
        self.assertEqual(gear[0]['gear_json'], [{'id': 1}])
        self.assertEqual(fields[0]['race'], 'Orc')
        self.assertEqual(len(queries), 3)
        self.assertNotIn('gear_json', queries[0]['sql'])
        self.assertNotIn('stats_json', queries[0]['sql'])
        for query in queries.captured_queries[1:]:
            self.assertIn(' IN ', query['sql'])

    def test_profile_last_match_empty_fallback_and_no_stripping(self):
        common = dict(season_id=9, class_name='Warrior', spec_name='Arms', realm='Realm', region='eu')
        PlayerSpecTopPlayer.objects.create(**common, character_name='Selected',
                                          gear_json=[{'id': 1}], race='Orc')
        PlayerSpecTopPlayer.objects.create(**common, character_name='SELECTED',
                                          gear_json=[], race='')
        PlayerSpecTopPlayer.objects.create(**dict(common, spec_name='Fury'),
                                          character_name='Selected', gear_json=[{'id': 999}])
        records = [dict(region='EU', realm='realm', character_name=name,
                        gear_json=[{'id': 7}], race='Human')
                   for name in ('selected', ' selected ', '')]
        before = copy.deepcopy(records)
        # SQLite's real queryset order provides the same last-match rule as
        # the old full-JSON projection; empty winners must not revive older data.
        cache = {}
        self.assertEqual(stats._merge_player_profile_gear(
            records, 9, 'Warrior', 'Arms', profile_cache=cache), records)
        self.assertEqual(stats._merge_player_profile_fields(
            records, 9, 'Warrior', 'Arms', profile_cache=cache), records)
        self.assertEqual(records, before)
        with patch.object(stats, '_selected_player_profiles', side_effect=RuntimeError('unavailable')):
            self.assertEqual(stats._merge_player_profile_gear(records, 9, 'Warrior', 'Arms'), records)
            self.assertEqual(stats._merge_player_profile_fields(records, 9, 'Warrior', 'Arms'), records)
