"""One confirmed character-label collection, independent of evidence source."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, SimpleTestCase
from django.test.utils import CaptureQueriesContext

from botend.models import PlayerObservationAnalysis, PlayerSpecTopPlayer, RaiderIOCharacterActivity
from botend.services import player_observation as observation
from botend.services.player_character_labels import (confirmed_character_judgements,
    confirmed_character_badges)
from botend.services.player_observation_badges import (attach_badge_snapshots,
    public_badge_players, high_confidence_badges)
from botend.services.player_raiderio_activity import player_identity
from botend.tests import test_player_raid_observation as raid_fixtures
from botend.tests import test_player_observation_badges as badge_fixtures


def snapshot(player):
    return {'id': player.pk, 'rank': player.rank, **{field: getattr(player, field)
        for field in ('region', 'realm', 'character_name', 'profile_url')}}


def codes(badges):
    return [item['code'] for item in badges]


class ConfirmedCollectionTests(SimpleTestCase):
    def item(self, code='confirmed', **changes):
        return {'code': code, 'value': code + ' · 强证据', 'supported': True,
            'reason': '理由', 'evidence': [{'text': '证据'}], 'note': '限定',
            'evidence_strength': {'level': 'strong', 'confidence': '高'}, **changes}

    def test_one_predicate_dedup_and_preserves_all_evidence_without_mutation(self):
        raid = self.item('independent_rule')  # No source/code allowlist.
        view = {'status': 'ready', 'display_judgements': [self.item(),
            self.item('partial', evidence_strength={'level': 'partial', 'confidence': '高'}),
            self.item('medium', evidence_strength={'level': 'strong', 'confidence': '中'}),
            self.item('unsupported', supported=False), raid], 'raid_judgement': raid}
        original = deepcopy(view)
        result = confirmed_character_judgements(view)
        self.assertEqual(codes(result), ['confirmed', 'independent_rule'])
        self.assertEqual(result[-1], raid)
        result[-1]['evidence'][0]['text'] = 'changed copy'
        self.assertEqual(view, original)
        self.assertEqual(codes(confirmed_character_badges(view)), ['confirmed', 'independent_rule'])

    def test_mplus_status_is_not_an_independent_source_gate(self):
        view = {'display_judgements': [self.item()], 'raid_judgement': self.item('rule')}
        for status in ('pending', 'error'):
            with self.subTest(status=status):
                self.assertEqual(codes(confirmed_character_judgements({**view, 'status': status})), ['rule'])
        stale = {'status': 'stale', 'display_judgements': [self.item()]}
        self.assertEqual(codes(confirmed_character_judgements(stale)), ['confirmed'])
        self.assertEqual(confirmed_character_judgements(stale, include_stale=False), [])
        self.assertEqual(high_confidence_badges(stale), [])

    def test_editorial_withdrawal_and_manual_own_tags_only(self):
        view = {'status': 'ready', 'display_judgements': [self.item()], 'raid_judgement': self.item('rule')}
        for changes in ({'status': 'hidden'}, {'status': 'archived'}, {'review_status': 'rejected'},
                        {'is_published': False}, {'manual': {'tags': []}}):
            with self.subTest(changes=changes):
                self.assertEqual(confirmed_character_judgements({**view, **changes}), [])
        manual = {**view, 'source_kind': 'manual'}
        self.assertEqual(confirmed_character_judgements(manual), [])
        own = {'label': '审核标签', 'value': '人工自己的值', 'reason': '审核理由', 'evidence': []}
        manual['manual'] = {'tags': [own]}
        manual['review_status'] = 'pending'
        self.assertEqual(confirmed_character_judgements(manual), [])
        manual['review_status'] = 'approved'
        self.assertEqual(confirmed_character_judgements(manual),
            [{**own, 'code': 'manual:0', 'supported': True}])
        self.assertEqual(high_confidence_badges(manual), [])


class RuleOnlyCharacterLabelsTests(TestCase):
    setUp = raid_fixtures.RaidObservationProjectionTests.setUp
    save_state = raid_fixtures.RaidObservationProjectionTests.save_state

    def read(self, players=None):
        return public_badge_players(players or [snapshot(self.player)],
            self.player.class_name, self.player.spec_name)

    def assert_matches_detail(self):
        view = observation.project_player_observation(self.player)
        self.assertEqual(self.read()[0].get('observation_badges', []), confirmed_character_badges(view))
        return view

    def test_rule_only_public_list_and_snapshot_without_analysis(self):
        self.save_state()
        result = self.read()[0]
        self.assertEqual(codes(result.get('observation_badges', [])), ['raid_consumer'])
        self.assertFalse(PlayerObservationAnalysis.objects.exists())
        self.assert_matches_detail()
        players = [snapshot(self.player)]
        attach_badge_snapshots(players)
        self.assertEqual(players[0]['observation_badges'], result['observation_badges'])
        self.assertEqual(players[0]['_observation_guard']['season_id'], self.season.pk)
        self.assertEqual(self.read(players)[0], result)

    def test_mplus_source_and_engine_failure_do_not_hide_raid(self):
        self.save_state()
        PlayerObservationAnalysis.objects.create(activity=self.activity, status='error', last_error='engine_empty')
        RaiderIOCharacterActivity.objects.filter(pk=self.activity.pk).update(status='error',
            last_error='mplus_failed')
        view = self.assert_matches_detail()
        self.assertEqual(view['status'], 'error')
        self.assertEqual(codes(confirmed_character_judgements(view)), ['raid_consumer'])
        self.assertIsNone(PlayerObservationAnalysis.objects.get().generated_at)

    def test_rule_only_editorial_hide_reject_and_manual_draft_retract_everywhere(self):
        self.save_state()
        row = PlayerObservationAnalysis.objects.create(activity=self.activity)
        for changes in ({'is_published': False}, {'review_status': 'rejected'},
                        {'manual_json': {'tags': []}},
                        {'manual_json': {'tags': [{'label': '草稿', 'value': '草稿值'}]}}):
            with self.subTest(changes=changes):
                PlayerObservationAnalysis.objects.filter(pk=row.pk).update(
                    is_published=True, review_status='pending', manual_json={})
                PlayerObservationAnalysis.objects.filter(pk=row.pk).update(**changes)
                self.assertNotIn('observation_badges', self.read()[0])
                self.assertIsNone(observation.project_player_observation(self.player))

    def test_rule_source_scope_privacy_clock_and_character_id_gates(self):
        variants = [{'visible': False}, {'rio_season': 'old'}, {'identity_digest': 'other'},
                    {'fetched_at': (self.now-timedelta(hours=49)).isoformat()},
                    {'fetched_at': (self.now+timedelta(hours=1)).isoformat()},
                    {'version': 999}, {'source': {'character_id': 0}}, {'raids': []}]
        for changes in variants:
            with self.subTest(changes=changes):
                self.save_state({**deepcopy(self.state), **changes})
                self.assertNotIn('observation_badges', self.read()[0])
        self.save_state()
        RaiderIOCharacterActivity.objects.filter(pk=self.activity.pk).update(
            scored_runs_json={'ui': {'characterId': 999}})
        self.assertNotIn('observation_badges', self.read()[0])

    def test_anonymous_identity_matches_the_detail_refusal(self):
        from botend.raiderio_identity import activity_identity_digest
        for name in ('Anonymous', 'ANONYMOUS', 'anonymous'):
            with self.subTest(name=name):
                self.player.character_name = name
                self.player.profile_url = f'https://raider.io/characters/eu/test-realm/{name}'
                self.player.save(update_fields=['character_name', 'profile_url'])
                identity = player_identity(self.player)
                digest = activity_identity_digest(**identity)
                state = {**deepcopy(self.state), 'identity_digest': digest}
                RaiderIOCharacterActivity.objects.filter(pk=self.activity.pk).update(
                    **identity, identity_digest=digest, payload_json={'raid_first_kills': state})
                self.assertIsNone(observation.project_player_observation(self.player))
                self.assertNotIn('observation_badges', self.read()[0])

    def test_missing_rio_season_matches_the_detail_refusal(self):
        self.season.rio_season = None
        self.season.save(update_fields=['rio_season'])
        self.save_state({**deepcopy(self.state), 'rio_season': None})
        self.assertIsNone(observation.project_player_observation(self.player))
        self.assertNotIn('observation_badges', self.read()[0])

    def test_rank_identity_season_and_old_snapshot_guards_fail_closed(self):
        self.save_state()
        players = [snapshot(self.player)]
        attach_badge_snapshots(players)
        for field, value in (('character_name', 'Other'), ('region', 'us'), ('realm', 'other'),
                             ('spec_name', 'Shadow'), ('rank', 21), ('season_id', self.season.pk+1)):
            with self.subTest(field=field):
                original = getattr(self.player, field)
                PlayerSpecTopPlayer.objects.filter(pk=self.player.pk).update(**{field: value})
                self.assertNotIn('observation_badges', self.read(players)[0])
                PlayerSpecTopPlayer.objects.filter(pk=self.player.pk).update(**{field: original})
        players[0]['_observation_guard']['season_id'] = self.season.pk+1
        self.assertNotIn('observation_badges', self.read(players)[0])
        RaiderIOCharacterActivity.objects.filter(pk=self.activity.pk).update(character_name='corrupted')
        self.assertNotIn('observation_badges', self.read()[0])

    def test_canonical_identity_shared_by_another_ranking_without_analysis_player_fk(self):
        self.save_state()
        second = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region=self.player.region,
            realm=self.player.realm, character_name=self.player.character_name, class_name='Priest',
            spec_name='Shadow', rank=2, profile_url=self.player.profile_url)
        PlayerObservationAnalysis.objects.create(activity=self.activity, player=self.player, status='error')
        result = public_badge_players([snapshot(second)], second.class_name, second.spec_name)
        self.assertEqual(codes(result[0]['observation_badges']), ['raid_consumer'])

    def test_twenty_distinct_players_are_light_bounded_readonly_without_hydration(self):
        self.save_state()
        players = [snapshot(self.player)]
        for rank in range(2, 21):
            name = f'Character{rank}'
            player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu',
                realm='test-realm', character_name=name, class_name='Priest', spec_name='Holy',
                rank=rank, profile_url=f'https://raider.io/characters/eu/test-realm/{name}')
            activity = RaiderIOCharacterActivity.objects.create(season=self.season, **player_identity(player))
            state = {**deepcopy(self.state), 'identity_digest': activity.identity_digest}
            RaiderIOCharacterActivity.objects.filter(pk=activity.pk).update(payload_json={
                'raid_first_kills': state, 'unrelated_big_fact': 'not selected'})
            players.append(snapshot(player))
        original = deepcopy(players)
        with patch.object(observation, 'project_player_observation', side_effect=AssertionError('no projection')), \
                patch.object(observation, '_build', side_effect=AssertionError('no evidence build')), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('no network')), \
                CaptureQueriesContext(connection) as queries:
            one = self.read(players[:1])
        one_count = len(queries)
        with patch.object(observation, 'project_player_observation', side_effect=AssertionError('no projection')), \
                patch.object(observation, '_build', side_effect=AssertionError('no evidence build')), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('no network')), \
                CaptureQueriesContext(connection) as queries:
            result = self.read(players)
        self.assertEqual(len(queries), one_count)
        self.assertLessEqual(len(queries), 6)
        self.assertEqual(len(result), 20)
        self.assertTrue(all(codes(row.get('observation_badges', [])) == ['raid_consumer'] for row in result))
        self.assertEqual(one[0], result[0])
        self.assertEqual(players, original)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        import re
        for document in ('payload_json', 'scored_runs_json', 'evidence_json', 'result_json', 'manual_json'):
            self.assertFalse(any(re.search(r'(?:SELECT |, )"\w+"\."' + document + r'"(?:,| FROM)',
                q['sql']) for q in queries), document)
        source_queries = [q['sql'] for q in queries if ' AS "raid_state"' in q['sql']]
        self.assertEqual(len(source_queries), 1)
        self.assertIn('raid_first_kills', source_queries[0])
        self.assertIn('characterId', source_queries[0])
        self.assertIn('LEFT OUTER JOIN', source_queries[0])
        self.assertNotIn('_observation_guard', result[0])


class FrozenAndRuleCharacterLabelsTests(TestCase):
    # Reuse the established parser/ORM/validator fixture and its existing model
    # boundary. No new mocked AI result is used to demonstrate raid publication.
    transport = badge_fixtures.ObservationBadgeTests.transport
    refresh = badge_fixtures.ObservationBadgeTests.refresh
    read = badge_fixtures.ObservationBadgeTests.read
    save_state = raid_fixtures.RaidObservationProjectionTests.save_state

    def setUp(self):
        badge_fixtures.ObservationBadgeTests.setUp(self)
        self.season.raid_encounters = [{'id':100+i, 'name':f'Boss {i}'} for i in (6,7,8)]
        self.season.save(update_fields=['raid_encounters'])
        cid = self.activity.scored_runs_json['ui']['characterId']
        self.state = {'version':1, 'status':'ready', 'visible':True,
            'identity_digest':self.activity.identity_digest, 'rio_season':self.season.rio_season,
            'fetched_at':self.now.isoformat(), 'source':{'character_id':cid, 'tier':'36'},
            'raids':[{'slug':'current-raid', 'name':'Current Raid', 'encounters':[
                {'index':i, 'wowEncounterId':100+i, 'slug':f'boss-{i}', 'numKills':1,
                 'firstDefeated':(self.now-timedelta(days=1)).isoformat() if i in (7,8) else None}
                for i in (6,7,8)]}]}
        self.activity.payload_json['raid_first_kills'] = deepcopy(self.state)
        self.activity.save(update_fields=['payload_json'])

    def test_real_frozen_mplus_and_rule_match_detail_collection(self):
        view = observation.project_player_observation(self.player)
        self.assertEqual(codes(confirmed_character_judgements(view)), ['rotating_core', 'carrying', 'raid_consumer'])
        self.assertEqual(self.read()['observation_badges'], confirmed_character_badges(view))
        attach_badge_snapshots(self.players)
        self.assertEqual(self.players[0]['observation_badges'], self.read()['observation_badges'])

    def test_raid_withdrawal_expiry_and_unsupported_do_not_retract_mplus_last_good(self):
        for changes in ({'visible': False}, {'raids': []}, {'rio_season': 'other'},
                        {'fetched_at': (self.now-timedelta(hours=49)).isoformat()},
                        {'fetched_at': (self.now+timedelta(hours=1)).isoformat()}):
            with self.subTest(changes=changes):
                self.activity.payload_json['raid_first_kills'] = {**deepcopy(self.state), **changes}
                self.activity.status = 'error'
                self.activity.last_error = 'mplus_source_failed'
                self.activity.save(update_fields=['payload_json', 'status', 'last_error'])
                self.assertEqual(codes(self.read()['observation_badges']), ['rotating_core', 'carrying'])

    def test_mplus_privacy_withdrawal_does_not_retract_independent_raid(self):
        from botend.models import RaiderIORunRoster
        RaiderIORunRoster.objects.update(roster_visible=False, status='error')
        self.assertEqual(codes(self.read()['observation_badges']), ['raid_consumer'])
        view = observation.project_player_observation(self.player)
        self.assertEqual(codes(confirmed_character_judgements(view)), ['raid_consumer'])

    def test_approved_manual_publishes_only_reviewed_own_tags_no_old_ai_or_rule(self):
        own = {'label': '人工审核', 'value': '已审核角色标签', 'reason': '人工依据', 'evidence': []}
        PlayerObservationAnalysis.objects.filter(activity=self.activity).update(
            manual_json={'tags': [own]}, review_status='approved')
        view = observation.project_player_observation(self.player)
        self.assertEqual(codes(confirmed_character_judgements(view)), ['manual:0'])
        self.assertEqual(self.read()['observation_badges'], confirmed_character_badges(view))
        self.assertEqual(self.read()['observation_badges'][0]['label'], own['value'])
        PlayerObservationAnalysis.objects.filter(activity=self.activity).update(manual_json={'tags': []})
        self.assertNotIn('observation_badges', self.read())
        self.assertEqual(confirmed_character_judgements(observation.project_player_observation(self.player)), [])

    def test_frozen_mplus_remains_six_bounded_selects_with_independent_metadata(self):
        with patch.object(observation, 'project_player_observation', side_effect=AssertionError('no projection')), \
                patch.object(observation, '_build', side_effect=AssertionError('no evidence build')), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('no network')), \
                CaptureQueriesContext(connection) as queries:
            result = public_badge_players(self.players * 20, self.player.class_name, self.player.spec_name)
        self.assertEqual(len(queries), 6)
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertTrue(all(codes(row['observation_badges']) == ['rotating_core', 'carrying', 'raid_consumer'] for row in result))
