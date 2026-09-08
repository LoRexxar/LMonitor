"""Performance contracts: preserve facts while eliminating repeated work."""
import copy
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import PlayerSpecTopPlayer
from botend.services import spec_stats_service as stats


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
        records = [{'character_name': f'P{i}', 'dps': i,
                    'talents_json': copy.deepcopy(variants)} for i in range(3)]
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
