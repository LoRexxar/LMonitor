"""Real central/typed-trace reconstruction, not an original HTTP replay.

The gzip fixture retains source hashes and complete saved ORM rows. Only the
clock and outbound HTTP boundary are replaced; domain, planner and gates are real.
"""
import gzip
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from django.apps import apps
from django.test import TestCase
from django.utils.dateparse import parse_datetime

from botend.models import RaiderIOCharacterActivity, RaiderIORunRoster, SeasonMeta, PlayerSpecTopPlayer
from botend.services import player_observation_sources as src
from botend.services.player_analysis_recheck import require_sources, Blocked


def decode(node):
    tag = node[0]
    if tag == 'none':
        return None
    if tag in ('str', 'bool'):
        return node[1]
    if tag == 'int':
        return int(node[1])
    if tag == 'float':
        return float.fromhex(node[1])
    if tag == 'dict':
        return {decode(k): decode(v) for k, v in node[1]}
    if tag in ('list', 'tuple'):
        return {'list': list, 'tuple': tuple}[tag](decode(v) for v in node[1])
    raise ValueError(tag)


class DynamicExecutionDomainTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fixture_data = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures/dynamic_execution_domain.json.gz').read_bytes()))
        records = deepcopy(cls.fixture_data['snapshot']['records'])
        overlays = {('botend.RaiderIOCharacterActivity', r['id']): r for r in cls.fixture_data['central']['activities']}
        overlays.update({('botend.RaiderIORunRoster', r['id']): r for r in cls.fixture_data['central']['rosters']})
        for record in records:
            model = apps.get_model(record['model'])
            key = record['model'], record['values'][model._meta.pk.attname]
            record['values'] = overlays.pop(key, record['values'])
        records += [{'model': model, 'values': values} for (model, _), values in overlays.items()]
        for record in records:
            model = apps.get_model(record['model'])
            values = deepcopy(record['values'])
            for field in model._meta.concrete_fields:
                if field.attname in values and field.get_internal_type() in ('DateTimeField', 'DateField', 'DecimalField'):
                    values[field.attname] = field.to_python(values[field.attname])
            model(**values).save_base(raw=True, force_insert=True)

    def setUp(self):
        self.clock = patch('django.utils.timezone.now', return_value=parse_datetime(self.fixture_data['central']['captured_at']))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.http = patch('requests.sessions.Session.request', side_effect=AssertionError('No external HTTP permitted'))
        self.transport = self.http.start()
        self.addCleanup(self.http.stop)
        self.args = decode(self.fixture_data['trace']['value'])['args']
        self.plan, self.entries, self.characters, self.states, self.scores, self.rio_season = self.args
        self.result = deepcopy(self.fixture_data['sources'])
        self.activity = RaiderIOCharacterActivity.objects.get(pk=1623)
        self.season = SeasonMeta.objects.get(pk=self.activity.season_id)

    def sync(self):
        return src._sync_execution_domain(self.activity, self.season, self.entries, self.characters,
                                         self.states, self.scores, self.result, 40)

    def test_real_trace_extension_preserves_cap_and_strict_gate(self):
        original = src._question_outcomes(*self.args)
        self.assertEqual(original, self.fixture_data['original_outcomes'])
        self.assertEqual(sum(q['status'] == 'invalid_question' for q in original), 3)
        with self.assertRaises(Blocked):
            require_sources(self.result)
        admitted = set(self.characters)
        self.sync()
        self.assertEqual(len(self.entries), 21)
        self.assertEqual(set(self.characters), admitted)
        self.assertEqual(self.result['characters_discovered'], 41)
        self.assertTrue(self.result['characters_truncated'])
        src._refresh_history_states(self.fixture_data['after'], self.entries, self.characters, self.states, self.season)
        outcomes = src._question_outcomes(self.plan, self.entries, self.characters, self.states, self.scores, self.rio_season)
        added = [q for q in outcomes if q['run_ids'] == [22349632]]
        self.assertEqual(len(added), 3)
        self.assertEqual(sum(q['status'] == 'identity_unresolved' for q in added), 1)
        self.assertFalse(any(q['status'] == 'invalid_question' for q in outcomes))
        attempts = {src._request_key(a['question']): a for a in self.result['investigation']['requests']}
        src._finish_investigation(self.fixture_data['before'], self.fixture_data['after'], self.entries,
                                 self.characters, self.states, self.scores, attempts, self.result, self.rio_season)
        self.result['characters'] = list(self.states.values())
        require_sources(self.result)
        self.assertLessEqual(self.result['followup_api_requests'], 2)
        self.assertEqual(self.transport.call_count, 0)
        for change in ({'invalid_references': 1}, {'identity_conflicts': 1}, {'source_failed': True}):
            with self.subTest(change=change), self.assertRaises(Blocked):
                require_sources({**self.result, **change})
        q = next(q for q in self.plan['questions'] if q['run_ids'] == [22349632] and q['identity_digest'] in self.characters)
        fact = next(f for r, f in self.entries if src.run_reference(r, self.rio_season) == 22349632)
        members = {src.activity_identity_digest(**c['identity']) for c in src._characters([fact], '')[0]}
        wrong_member = next(key for key in self.characters if key not in members)
        for change in ({'run_ids': [999999999]}, {'identity_digest': wrong_member}):
            rejected = src._question_outcomes({**self.plan, 'questions': [{**q, **change}]}, self.entries,
                                             self.characters, self.states, self.scores, self.rio_season)
            self.assertEqual(rejected[0]['status'], 'invalid_question')

    def test_second_real_case_original_rejections_remain_strict(self):
        case = self.fixture_data['second_case']
        files = case['files']
        replayed = []
        for name, value in sorted(files.items()):
            if not name.endswith('-input.json'):
                continue
            args = decode(value['value'])['args']
            actual = src._question_outcomes(*args)
            self.assertEqual(actual, files[name.replace('-input', '-output')])
            replayed.append(name)
            if name in ('trace-questions-05-input.json', 'trace-questions-08-input.json'):
                bad = [q for q in actual if q['status'] == 'invalid_question']
                self.assertTrue(bad)
                self.assertTrue(all(21488728 in q['run_ids'] and q['identity_digest'] in args[2] for q in bad))
                self.assertNotIn(21488728, {src.run_reference(r, args[5]) for r, _ in args[1]})
        self.assertEqual(len(replayed), 8)
        with self.assertRaises(Blocked):
            require_sources(files['sources.json'])
        self.assertEqual(self.transport.call_count, 0)

    def test_cumulative_admission_survives_roster_lease_and_restoration(self):
        from datetime import timedelta
        from django.utils import timezone

        self.sync()
        original = set(self.characters)
        self.assertEqual(len(original), 40)
        row = RaiderIORunRoster.objects.get(run_id=23427664, rio_season=self.rio_season)
        saved = row.status, row.next_attempt_at, row.lease_token
        row.status, row.next_attempt_at, row.lease_token = (
            'fetching', timezone.now() + timedelta(minutes=2), 'offline-regression')
        row.save(update_fields=['status', 'next_attempt_at', 'lease_token'])
        self.sync()
        current = set(self.characters)
        self.assertEqual(len(original - current), 2)
        self.assertEqual(len(original | current), 40)
        self.assertEqual(len(current), 38)
        self.assertTrue(self.result['characters_truncated'])
        self.assertEqual(self.result['characters_discovered'], 39)
        self.assertEqual(next(r['status'] for r in self.result['runs'] if r['run_id'] == row.run_id), 'busy')
        for key in original - current:
            self.assertNotIn(key, self.scores)
            self.assertFalse(any(target == key or isinstance(target, tuple) and target[0] == key
                                 for target in self.states))
        with self.assertRaises(Blocked):
            require_sources(self.result)
        row.status, row.next_attempt_at, row.lease_token = saved
        row.save(update_fields=['status', 'next_attempt_at', 'lease_token'])
        self.sync()
        self.assertEqual(set(self.characters), original)
        self.assertEqual(len(self.result['character_admissions']), 40)
        self.assertEqual(self.result['identity_conflicts'], 0)
        self.assertTrue(self.result['characters_truncated'])
        self.assertLessEqual(self.result['followup_api_requests'], 2)
        json.dumps(self.result)
        self.assertEqual(self.transport.call_count, 0)

    def test_temporarily_absent_admission_cannot_change_uid(self):
        from datetime import timedelta
        from django.utils import timezone

        self.sync()
        original = deepcopy(self.characters)
        row = RaiderIORunRoster.objects.get(run_id=23427664, rio_season=self.rio_season)
        saved = row.status, row.next_attempt_at, row.lease_token
        row.status, row.next_attempt_at, row.lease_token = (
            'fetching', timezone.now() + timedelta(minutes=2), 'offline-identity-regression')
        row.save(update_fields=['status', 'next_attempt_at', 'lease_token'])
        self.sync()
        missing = set(original) - set(self.characters)
        self.assertEqual(len(missing), 2)
        target = next(iter(missing))
        member = next(m for m in row.payload_json['roster']
                      if m['character']['id'] == original[target]['character_id'])
        # Controlled identity mutation of a captured fact, not a new source fact.
        member['character']['id'] = 999999999
        row.status, row.next_attempt_at, row.lease_token = saved
        row.save(update_fields=['status', 'next_attempt_at', 'lease_token', 'payload_json'])
        self.sync()
        self.assertGreater(self.result['identity_conflicts'], 0)
        self.assertNotIn(target, self.characters)
        self.assertEqual(self.result['character_admissions'][target], original[target]['character_id'])
        self.assertLessEqual(len(set(original) | set(self.characters)), 40)
        with self.assertRaises(Blocked):
            require_sources(self.result)
        self.assertLessEqual(self.result['followup_api_requests'], 2)
        json.dumps(self.result)
        self.assertEqual(self.transport.call_count, 0)

    def test_shared_history_key_uses_latest_completion_not_last_question(self):
        self.sync()
        from datetime import timedelta
        target = next(a for a in self.result['investigation']['requests'] if a['character_id'] == 308340455)
        q = target['question']
        key = src._request_key(q)
        rid = q['run_ids'][0]
        run, fact = next((r, f) for r, f in self.entries if src.run_reference(r, self.rio_season) == rid)
        later = parse_datetime(target['fetched_at']) + timedelta(seconds=1)
        late = deepcopy(fact)
        late['completed_at'] = later.isoformat()
        # Controlled temporal mutation, explicitly NOT a new real source fact.
        changed = [(r, late if src.run_reference(r, self.rio_season) == rid else f) for r, f in self.entries]
        other_rid = next(src.run_reference(r, self.rio_season) for r, f in self.entries if parse_datetime(f['completed_at']) < later and src.run_reference(r, self.rio_season) != rid)
        evidence = {'metrics': {'investigation': {'questions': [q, {**q, 'id': 'older', 'run_ids': [other_rid]}]}}}
        src._refresh_history_states(evidence, changed, self.characters, self.states, self.season)
        self.assertEqual(self.states[key]['status'], 'pending')
        self.assertEqual(self.transport.call_count, 0)

    def test_real_collector_current_reconstruction_zero_followup(self):
        player = PlayerSpecTopPlayer.objects.get(pk=148725)
        result = src.refresh_observation_sources(player, progressive=True, max_followup_requests=0, max_basic_roster_requests=0)
        self.assertEqual(result['api_requests'], 0)
        self.assertEqual(result['characters_discovered'], 41)
        self.assertTrue(result['characters_truncated'])
        self.assertEqual(len(result['character_admissions']), 40)
        json.dumps(result)
        self.assertEqual(result['identity_conflicts'], 0)
        require_sources(result)
        self.assertEqual(self.transport.call_count, 0)

    def test_new_identity_conflict_and_malformed_roster_still_block(self):
        self.sync()
        row = RaiderIORunRoster.objects.get(run_id=22349632, rio_season=self.rio_season)
        members = {m['character']['id'] for m in row.payload_json['roster']}
        other = next(c for c in self.characters.values() if c['character_id'] not in members)
        row.payload_json['roster'][0]['character']['id'] = other['character_id']
        row.save(update_fields=['payload_json'])
        self.sync()
        self.assertGreater(self.result['identity_conflicts'], 0)
        with self.assertRaises(Blocked):
            require_sources(self.result)
        row.payload_json = {'malformed': True}
        row.save(update_fields=['payload_json'])
        self.sync()
        self.assertEqual(next(r['status'] for r in self.result['runs'] if r['run_id'] == 22349632), 'invalid_payload')

    def test_changed_question_cannot_borrow_execution_authorization(self):
        self.sync()
        before, after = deepcopy(self.fixture_data['before']), deepcopy(self.fixture_data['after'])
        attempt = deepcopy(self.result['investigation']['requests'][0])
        key = src._request_key(attempt['question'])
        changed = {**attempt['question'], 'run_ids': [999999999]}
        after['metrics']['investigation']['questions'] = [changed]
        src._finish_investigation(before, after, self.entries, self.characters, self.states, self.scores,
                                 {key: attempt}, self.result, self.rio_season,
                                 execution_scopes={key: (before['metrics']['investigation'], list(self.entries), dict(self.characters))})
        outcome = next(q for q in self.result['investigation']['outcomes'] if q['id'] == changed['id'])
        self.assertEqual(outcome['status'], 'invalid_question')
        self.assertEqual(outcome['request_status'], 'not_requested')
        with self.assertRaises(Blocked):
            require_sources(self.result)

    def test_dynamic_error_payload_does_not_become_fresh(self):
        self.sync()
        row = RaiderIORunRoster.objects.get(run_id=22349632, rio_season=self.rio_season)
        row.status, row.last_error = 'error', 'transport_error'
        row.save(update_fields=['status', 'last_error'])
        # Retain its real reference as an already admitted run, then revalidate.
        self.sync()
        self.assertEqual(next(r['status'] for r in self.result['runs'] if r['run_id'] == 22349632), 'backoff')
        row.next_attempt_at = None
        row.save(update_fields=['next_attempt_at'])
        self.sync()
        self.assertEqual(next(r['status'] for r in self.result['runs'] if r['run_id'] == 22349632), 'error')
        self.result['invalid_references'] = 7
        self.sync()
        self.assertEqual(self.result['invalid_references'], 7)
        self.assertEqual(self.transport.call_count, 0)
