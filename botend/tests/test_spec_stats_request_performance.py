"""Request-local reuse and bounded raid hydration contracts (real ORM/imports)."""
from copy import deepcopy
from unittest.mock import patch

from django.db import connection
from django.db.models.query import ValuesIterable
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import SpecRaidRanking
from botend.services import spec_stats_service as s


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
        provider = s.TalentMetadataProvider(usage=s.TalentVersionResolver.USAGE_STATS)
        by_key = {s.TalentBuildCodeService._build_node_key(n): n for n in nodes}
        before = deepcopy(records)
        expected = [s._talent_build_semantic_state(r, provider, 'Warrior', 'Arms', nodes, by_key)
                    for r in records]
        context = {}
        with patch.object(s.TalentBuildCodeDecoder, 'decode_node_states',
                          wraps=s.TalentBuildCodeDecoder.decode_node_states) as decode:
            actual = [s._talent_build_semantic_state(r, provider, 'Warrior', 'Arms', nodes, by_key,
                                                   context=context) for r in records]
        self.assertEqual(actual, expected)
        self.assertNotEqual(actual[0][0], actual[1][0])
        self.assertEqual(actual[0], actual[2])
        self.assertEqual(records, before)
        self.assertEqual(decode.call_count, 1)
        self.assertEqual(context[('decoded', code)],
                         s.TalentBuildCodeDecoder.decode_node_states(code, nodes))

    def test_hero_summary_cache_preserves_nullable_subtrees_and_record_counts(self):
        first_nodes = {None: [object()], 99: [object(), object()]}
        expected = [
            {'subtree_id': None, 'name': '英雄天赋', 'selected_count': 1},
            {'subtree_id': 99, 'name': '英雄天赋 99', 'selected_count': 2},
        ]
        self.assertEqual(s._build_hero_talent_summary(first_nodes, 'Warrior', 'Arms'), expected)
        context = {}
        first = s._build_hero_talent_summary(first_nodes, 'Warrior', 'Arms', context=context)
        self.assertEqual(first, expected)
        first[0]['name'] = 'caller mutation'
        second_nodes = {99: [object()], None: [object(), object(), object()]}
        with self.assertNumQueries(0):
            second = s._build_hero_talent_summary(second_nodes, 'Warrior', 'Arms', context=context)
        self.assertEqual(second, [dict(expected[0], selected_count=3),
                                  dict(expected[1], selected_count=1)])

    def test_build_reuses_observations_without_losing_players_or_overrides(self):
        node = {'node_id': 123, 'spell_id': 456, 'tree_type': 'hero',
                'db2_subtree_id': 99, 'name': 'Original', 'points': 1}
        records = [dict(talents_json=[deepcopy(node)], talent_build_code='invalid',
                        character_name=f'Player{i}', realm='Realm', region='EU', dps=i)
                   for i in range(12)]
        records[-1]['talents_json'][0].update(points=2, name='Override')
        before = deepcopy(records)
        with patch.object(s, '_normalize_stats_talent_node', wraps=s._normalize_stats_talent_node) as normalize, \
             patch.object(s, '_hero_subtree_name_from_table', wraps=s._hero_subtree_name_from_table) as hero:
            result = s._compute_talent_build_popularity(records, 'Warrior', 'Arms')
        self.assertEqual(records, before)
        self.assertEqual(result['total'], 12)
        self.assertEqual(sorted(b['count'] for b in result['builds']), [1, 11])
        self.assertEqual(len(result['builds'][0]['top_players']), 5)
        self.assertEqual(normalize.call_count, 2)
        self.assertEqual(hero.call_count, 1)
        # No cross-request state: a later observation must be resolved again.
        with patch.object(s, '_normalize_stats_talent_node', wraps=s._normalize_stats_talent_node) as normalize:
            self.assertEqual(s._compute_talent_build_popularity(records, 'Warrior', 'Arms'), result)
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
            result = s.SpecStatsService._compute_raid_stats(987, 3379, 'Boss', 'Warrior', 'Arms', full=True)
        self.assertEqual(result['sample_size'], 12)
        self.assertEqual(result['top5'], expected)
        self.assertEqual(sum('guild_name' in cols and 'gear_json' in cols for cols in consumed), 5)
        identity_scans = [q for q in queries if 'wow_spec_top_player' in q['sql'].lower()
                          and 'talent_build_code' not in q['sql'] and 'gear_json' not in q['sql']
                          and 'stats_json' not in q['sql']]
        self.assertEqual(len(identity_scans), 1)
