"""Real, composite central captures: not a same-time production closure.

The source hashes and original run-body hashes are in the fixture. Missing peer
activities are not asserted absent. No HTTP/model or production DB is used.
"""
import copy
import gzip
import json
from pathlib import Path
from unittest.mock import patch

from django.apps import apps
from django.db import connection
from django.test import TestCase
from django.utils.dateparse import parse_datetime

from botend.models import PlayerSpecTopPlayer, RaiderIORunRoster
from botend.services import player_observation as service
from botend.services import player_observation_behavior as behavior
from botend.tests import test_player_observation_behavior as pure_fixtures
from botend.tests import test_player_observation as orm_fixtures


class SubjectBestAttributionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.capture = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures/subject_best_attribution.json.gz').read_bytes()))
        for record in cls.capture['records']:
            model = apps.get_model(record['model'])
            values = copy.deepcopy(record['values'])
            for field in model._meta.concrete_fields:
                if field.attname in values:
                    values[field.attname] = field.to_python(values[field.attname])
            model(**values).save_base(raw=True, force_insert=True)

    def build(self, name):
        def readonly(execute, sql, params, many, context):
            self.assertIn(sql.lstrip().split()[0].upper(), ('SELECT', 'PRAGMA'))
            return execute(sql, params, many, context)
        with patch('django.utils.timezone.now', return_value=parse_datetime(self.capture['checked_at'])), \
                patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP forbidden')), \
                connection.execute_wrapper(readonly):
            return service.build_evidence(PlayerSpecTopPlayer.objects.get(character_name=name))

    def test_real_mcsham_absent_best_cannot_be_personal_prestart_proof(self):
        old = next(r for r in self.capture['baseline'] if r['name'] == 'Mcsham')
        bad_ids = {'behavior:prior_higher:23459980', 'behavior:prior_higher:23484522'}
        original = {e['id']: e for e in old['baseline']['evidence']}
        self.assertTrue(bad_ids <= original.keys())
        self.assertIn('23093230', original['behavior:prior_higher:23459980']['text'])
        self.assertIn('23086819', original['behavior:prior_higher:23484522']['text'])
        with patch.object(service, 'build_investigation', wraps=service.build_investigation) as inquiry:
            result = self.build('Mcsham')
        current_best = inquiry.call_args.kwargs['best']
        self.assertEqual({r['keystone_run_id'] for r in current_best}, set(old['source_current_score_association_ids']))
        actual = result['metrics']['behavior_review']
        self.assertFalse(bad_ids & {e['id'] for e in actual['evidence']})
        self.assertEqual(actual['structure'], old['baseline']['structure'])
        self.assertEqual(result['coverage'], old['evidence']['coverage'])
        # Current profile-best comparisons remain intact; only dated personal
        # completion proofs change. This capture contains no accepted score cache.
        own = lambda e: [r for r in e['evidence'] if r['id'].startswith('investigation:own:')]
        self.assertEqual(own(result), own(old['evidence']))
        self.assertEqual(result['metrics']['highest_timed_level'], old['evidence']['metrics']['highest_timed_level'])
        self.assertEqual([e for e in result['evidence'] if e['id'] == 'timed_experience'],
                         [e for e in old['evidence']['evidence'] if e['id'] == 'timed_experience'])
        self.assertEqual(result['source_stamps']['scores'], old['evidence']['source_stamps']['scores'])
        for event in actual['resilience']['events']:
            self.assertEqual(event['members'][actual['resilience']['subject_key']]['state'], 'unknown')
        self.assertEqual(actual['supported'], old['diagnostic_without_absent_best']['supported'])

    def test_real_jinnbei_missing_recent_rosters_stay_unknown(self):
        old = next(r for r in self.capture['baseline'] if r['name'] == 'Jinnbei')
        result = self.build('Jinnbei')
        self.assertEqual(result['metrics']['behavior_review']['structure'], old['baseline']['structure'])
        self.assertEqual(result['metrics']['behavior_review']['structure']['recent_runs'], 3)
        self.assertEqual(result['metrics']['behavior_review']['structure']['roster_runs'], 0)
        self.assertEqual(result['metrics']['behavior_review']['resilience']['events'], [])
        self.assertEqual(result['metrics']['behavior_review']['supported'], {})

    def test_missing_roster_is_not_a_confirmed_absence(self):
        # Explicit coverage variant of the real capture: removing the roster is
        # not historical proof of absence. It must not create an absence stamp.
        RaiderIORunRoster.objects.filter(run_id=23093230).delete()
        result = self.build('Mcsham')
        self.assertNotIn(23093230, [r['run_id'] for r in result['source_stamps'].get('subject_absent_rosters', [])])
        self.assertIn(23086819, [r['run_id'] for r in result['source_stamps'].get('subject_absent_rosters', [])])

    def test_personal_in_roster_dated_proof_and_independent_scores_survive(self):
        # Synthetic boundary controls, kept distinct from the captured RED/GREEN.
        fixture = pure_fixtures.BehaviorReviewTests()
        fixture.setUp()
        fixture.add_run(900, 23, fixture.now - pure_fixtures.timedelta(days=1), fixture.keys[:5], score=530)
        fixture.args['recent'] = fixture.args['recent'][:3]
        fixture.args['best'] = [fixture.args['runs'][900]]
        actual = fixture.build()
        self.assertEqual(sum(e['id'].startswith('behavior:prior_higher:') for e in actual['evidence']), 3)
        # Authenticated independent scored source is not deleted just because its
        # run lacks a usable shared roster. Historical semantics stay unchanged.
        fixture.args['best'] = []
        fixture.args['rosters'].pop(900)
        fixture.records(fixture.keys[0], 23, all_dungeons=True)
        actual = fixture.build()
        self.assertEqual(actual['resilience']['events'][0]['members'][fixture.keys[0]]['state'], 'confirmed_before_start')


class IndependentScoreAttributionTests(TestCase):
    setUp = orm_fixtures.PlayerObservationTests.setUp

    def test_absent_best_does_not_delete_independently_authenticated_current_score(self):
        self.activity.refresh_from_db()
        self.activity.scored_runs_json['dungeons'][0]['keystone_run_id'] = 124
        self.activity.save()
        before = service.build_evidence(self.player)
        self.assertEqual(before['coverage']['score_characters'], 5)
        roster = RaiderIORunRoster.objects.get(run_id=124)
        character = roster.payload_json['roster'][0]['character']
        character.update(id=9999, name='Other', path='/characters/eu/test-realm/Other')
        roster.save()
        after = service.build_evidence(self.player)
        self.assertEqual(after['source_stamps']['scores'], before['source_stamps']['scores'])
        self.assertEqual(after['coverage']['score_characters'], 5)
        self.assertEqual(after['metrics']['comparable_runs'], [123, 125])
        comparisons = lambda e: [r for r in e['evidence'] if r['id'] in ('comparison:123', 'comparison:125')]
        self.assertEqual(comparisons(after), comparisons(before))
        self.assertEqual([r['run_id'] for r in after['source_stamps']['subject_absent_rosters']], [124])
