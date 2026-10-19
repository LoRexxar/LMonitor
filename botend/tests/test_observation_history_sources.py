"""Bounded history contracts at the existing progressive HTTP boundary."""
import copy
from unittest.mock import Mock, patch
from django.test import TestCase
from botend.tests.test_triggered_observation_sources import TriggeredSourcesTests


class HistorySourcesTests(TestCase):
    setUp = TriggeredSourcesTests.setUp
    refresh = TriggeredSourcesTests.refresh
    collect = TriggeredSourcesTests.collect
    contract = TriggeredSourcesTests.contract

    def history_contract(self, count=1):
        data = self.contract(count=count)
        questions = data['metrics']['investigation']['questions']
        data['metrics']['investigation']['questions'] = []
        data['metrics']['investigation']['triggered'] = False
        data['metrics']['behavior_review'] = {'questions': [dict(
            kind='scored_history', member_key=q['identity_digest'], dungeon_id=1,
            run_ids=q['run_ids'], priority=q['priority']) for q in questions]}
        return data

    def transport(self, url, **kwargs):
        if not url.endswith('/mythic-plus-runs'):
            return TriggeredSourcesTests.transport(self, url, **kwargs)
        self.calls.append((url, kwargs))
        scope = dict(kwargs['params'])
        summary = {'season': scope['season'], 'dungeon': {'id': scope['dungeonId']},
                   'keystone_run_id': 321, 'completed_at': self.run['completed_at'],
                   'mythic_level': 10, 'num_chests': 0, 'gear': {'forbidden': True}}
        data = {'ui': scope, 'runs': [{'score': 100.5, 'summary': summary}]}
        if getattr(self, 'bad_history', False):
            data['ui']['characterId'] += 1
        if getattr(self, 'private_history', False):
            data['isPrivate'] = True
        return Mock(status_code=self.http_status, json=Mock(return_value=data), headers={})

    def resilience_contract(self):
        data = self.history_contract()
        q = data['metrics']['behavior_review']['questions'][0]
        q.update(dungeon_id=self.row.payload_json['dungeon']['id'] + 1,
                 purpose='resilience_gap', required_level=self.row.payload_json['mythic_level'] + 1)
        data['metrics']['investigation']['resilience_history_targets'] = [copy.deepcopy(q)]
        return data

    def test_authorized_cross_dungeon_requests_exact_target(self):
        data = self.resilience_contract()
        q = data['metrics']['behavior_review']['questions'][0]
        result = self.collect(data)
        self.assertEqual(result['followup_api_requests'], 1)
        self.assertEqual(self.calls[0][1]['params']['dungeonId'], q['dungeon_id'])
        self.activity.refresh_from_db()
        self.assertIn(str(q['dungeon_id']), self.activity.payload_json['mythic_plus_scored_history'])

    def collect_resolved_history(self, *, replacement=None):
        """Rebuild evidence after the real collector publishes its ORM snapshot."""
        data = self.resilience_contract()
        target = data['metrics']['behavior_review']['questions'][0]
        snapshots = []

        def rebuild(_player):
            self.activity.refresh_from_db()
            entry = self.activity.payload_json.get('mythic_plus_scored_history', {}).get(
                str(target['dungeon_id']))
            evidence = copy.deepcopy(data)
            if entry is not None:
                evidence['metrics']['behavior_review']['questions'] = []
                investigation = evidence['metrics']['investigation']
                investigation['resilience_history_targets'] = []
                if replacement:
                    # Preserve the original generated ID to also test ID reuse.
                    from botend.services.player_observation_sources import _merge_behavior_questions
                    original = _merge_behavior_questions(data, {123})
                    question = copy.deepcopy(original['metrics']['investigation']['questions'][0])
                    question.update(replacement)
                    investigation['questions'] = [question]
            snapshots.append(evidence)
            return evidence

        with patch('botend.services.player_observation.build_evidence', side_effect=rebuild), \
                patch('botend.services.player_observation_sources._pace', return_value=True):
            result = self.refresh(progressive=True, force=True)
        self.assertEqual(len(snapshots), 2)
        self.assertTrue(snapshots[0]['metrics']['investigation']['resilience_history_targets'])
        self.assertEqual(snapshots[1]['metrics']['investigation']['resilience_history_targets'], [])
        self.assertEqual(result['followup_api_requests'], 1)
        self.activity.refresh_from_db()
        return result

    def test_resolved_cross_dungeon_keeps_executed_authorization(self):
        result = self.collect_resolved_history()
        trace = result['investigation']
        self.assertEqual(trace['questions'], [])
        self.assertEqual(trace['outcomes'][0]['request_status'], 'ready')
        self.assertEqual(trace['outcomes'][0]['status'], 'observed_partial')
        self.assertTrue(trace['bounded_complete'])
        self.assertEqual(trace['stop_reason'], 'source_missing')
        self.assertFalse(result['source_failed'])
        entries = self.activity.payload_json['mythic_plus_scored_history'].values()
        self.assertEqual([entry['status'] for entry in entries], ['ready'])
        # The real recheck consumer must accept a successful partial history,
        # without confusing incomplete history coverage with a failed request.
        from botend.services.player_analysis_recheck import require_sources, Blocked
        require_sources(result)
        for field, value in (('kind', 'scored_runs'), ('request_status', 'private'),
                             ('request_status', 'error')):
            bad = copy.deepcopy(result)
            bad['investigation']['outcomes'][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(Blocked):
                require_sources(bad)

    def test_new_question_cannot_inherit_old_target_authorization(self):
        result = self.collect_resolved_history(replacement={'id': 'new_question'})
        outcomes = {q['id']: q for q in result['investigation']['outcomes']}
        self.assertEqual(outcomes.pop('new_question')['status'], 'invalid_question')
        self.assertEqual(next(iter(outcomes.values()))['status'], 'observed_partial')
        self.assertFalse(result['investigation']['bounded_complete'])

    def test_changed_question_id_cannot_inherit_old_authorization(self):
        result = self.collect_resolved_history(replacement={'purpose': 'other'})
        self.assertEqual(result['investigation']['outcomes'][0]['status'], 'invalid_question')
        self.assertFalse(result['investigation']['bounded_complete'])

    def test_disappearing_target_does_not_hide_private_or_failed_response(self):
        for flag in ('private_history', 'bad_history'):
            with self.subTest(flag=flag):
                self.activity.payload_json.pop('mythic_plus_scored_history', None)
                self.activity.save(update_fields=['payload_json'])
                self.private_history = self.bad_history = False
                setattr(self, flag, True)
                result = self.collect_resolved_history()
                outcome = result['investigation']['outcomes'][0]
                self.assertEqual(outcome['status'], 'source_failed')
                self.assertNotIn(outcome['request_status'], ('ready', 'fresh'))
                self.assertFalse(result['investigation']['bounded_complete'])

    def test_cross_dungeon_rejects_untrusted_scope_and_level(self):
        for field, value in [('dungeon_id', 999999), ('purpose', 'other'),
                             ('required_level', 999), ('run_ids', [999999]),
                             ('member_key', 'outsider')]:
            with self.subTest(field=field):
                data = self.resilience_contract()
                data['metrics']['behavior_review']['questions'][0][field] = value
                self.assertEqual(self.collect(data)['followup_api_requests'], 0)
                self.assertEqual(self.calls, [])
        # Even a matching authorization cannot authorize an incorrect L+1.
        data = self.resilience_contract()
        data['metrics']['behavior_review']['questions'][0]['required_level'] = 999
        data['metrics']['investigation']['resilience_history_targets'][0]['required_level'] = 999
        self.assertEqual(self.collect(data)['followup_api_requests'], 0)
        data = self.resilience_contract()
        data['metrics']['investigation']['resilience_history_targets'] = []
        self.assertEqual(self.collect(data)['followup_api_requests'], 0)

    def test_authorization_still_requires_presence_in_every_seed_run(self):
        from botend.models import RaiderIORunRoster
        from botend.raiderio_identity import activity_identity_digest
        data = self.resilience_contract()
        q = data['metrics']['behavior_review']['questions'][0]
        q.update(member_key=activity_identity_digest(**self.chars[1]['identity']), run_ids=[123, 124])
        data['metrics']['investigation']['resilience_history_targets'] = [copy.deepcopy(q)]
        run = copy.deepcopy(self.run)
        run.update(keystone_run_id=124, url='https://raider.io/mythic-plus-runs/season-mn-2/124')
        fact = copy.deepcopy(self.row.payload_json)
        fact['keystone_run_id'] = 124
        for member in fact['roster']:
            c = member['character']
            if c['id'] == self.chars[1]['character_id']:
                c.update(id=999, name='Replacement', path='/characters/eu/test-realm/Replacement')
        RaiderIORunRoster.objects.create(rio_season=self.season.rio_season, run_id=124,
            payload_json=fact, roster_visible=True, fetched_at=self.row.fetched_at, status='ready')
        self.activity.payload_json['mythic_plus_recent_runs'].append(run)
        self.activity.save(update_fields=['payload_json'])
        result = self.collect(data)
        self.assertEqual(result['followup_api_requests'], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(result['rosters_visible'], 2)
        self.assertEqual(result['investigation']['outcomes'][0]['status'], 'invalid_question')

    def test_cross_dungeon_questions_keep_two_request_budget(self):
        data = self.history_contract(4)
        questions = data['metrics']['behavior_review']['questions']
        for q in questions:
            q.update(dungeon_id=2, purpose='resilience_gap',
                     required_level=self.row.payload_json['mythic_level'] + 1)
        data['metrics']['investigation']['resilience_history_targets'] = copy.deepcopy(questions)
        result = self.collect(data)
        self.assertEqual(result['followup_api_requests'], 2)
        self.assertEqual([kwargs['params']['dungeonId'] for _, kwargs in self.calls], [2, 2])

    def test_private_tombstones_and_expired_leases_never_reuse_last_good(self):
        from datetime import timedelta
        from django.utils import timezone
        from botend.services.player_observation_history import _state
        data = self.history_contract()
        did = self.row.payload_json['dungeon']['id']
        data['metrics']['behavior_review']['questions'][0]['dungeon_id'] = did
        self.collect(data)
        self.activity.refresh_from_db()
        original = copy.deepcopy(self.activity.payload_json['mythic_plus_scored_history'][str(did)])
        now = timezone.now()
        for changes, expected in [
                ({'private': True}, 'pending'),
                ({'private': True, 'next_attempt_at': (now + timedelta(minutes=1)).isoformat()}, 'private'),
                ({'status': 'fetching', 'next_attempt_at': (now - timedelta(seconds=1)).isoformat()}, 'pending'),
                ({'status': 'fetching', 'next_attempt_at': (now + timedelta(minutes=1)).isoformat()}, 'busy'),
                ({'status': 'error'}, 'pending'),
                ({'last_error': 'transport_error'}, 'pending')]:
            with self.subTest(changes=changes):
                entry = {**original, **changes}
                self.assertEqual(_state(entry, original['ui'], now), expected)
                self.activity.payload_json['mythic_plus_scored_history'][str(did)] = entry
                self.activity.save(update_fields=['payload_json'])
                self.calls.clear()
                result = self.collect(data)
                self.assertEqual(result['followup_api_requests'], 1 if expected == 'pending' else 0)
                self.activity.refresh_from_db()
                saved = self.activity.payload_json['mythic_plus_scored_history'][str(did)]
                if expected == 'pending':
                    self.assertEqual(saved['status'], 'ready')
                    self.assertNotIn('private', saved)

    def test_history_cached_and_isolated(self):
        data = self.history_contract()
        # Fixture dungeon is the canonical source ID, not WCL encounter ID.
        did = self.row.payload_json['dungeon']['id']
        data['metrics']['behavior_review']['questions'][0]['dungeon_id'] = did
        original = self.activity.fetched_at
        result = self.collect(data)
        self.assertEqual(result['followup_api_requests'], 1)
        self.activity.refresh_from_db()
        cache = self.activity.payload_json['mythic_plus_scored_history'][str(did)]
        self.assertEqual(cache['coverage'], 'observed_partial')
        self.assertEqual(set(cache['runs'][0]), {'run_id', 'dungeon_id', 'level', 'score', 'timed', 'completed_at'})
        self.assertFalse(cache['runs'][0]['timed'])
        self.assertEqual(self.activity.fetched_at, original)
        self.assertEqual(self.activity.scored_runs_json, {})
        self.calls.clear()
        self.assertEqual(self.collect(data)['followup_api_requests'], 0)
        self.activity.refresh_from_db()
        self.assertEqual(cache, self.activity.payload_json['mythic_plus_scored_history'][str(did)])

    def test_budget_authorization_and_errors(self):
        data = self.history_contract(4)
        for q in data['metrics']['behavior_review']['questions']:
            q['dungeon_id'] = self.row.payload_json['dungeon']['id']
        self.bad_history = True
        result = self.collect(data)
        self.assertEqual(result['followup_api_requests'], 2)
        self.assertFalse(result['investigation']['bounded_complete'])
        self.activity.refresh_from_db()
        entry = self.activity.payload_json['mythic_plus_scored_history'][str(self.row.payload_json['dungeon']['id'])]
        self.assertNotIn('runs', entry)
        self.assertEqual(entry['last_error'], 'invalid_payload')
        self.calls.clear()
        data['metrics']['behavior_review']['questions'] = [dict(kind='scored_history', member_key='outsider', dungeon_id=1, run_ids=[123], priority=1)]
        self.collect(data)
        self.assertEqual(self.calls, [])

    def test_private_and_429_are_not_ready(self):
        data = self.history_contract(3)
        for q in data['metrics']['behavior_review']['questions']:
            q['dungeon_id'] = self.row.payload_json['dungeon']['id']
        self.http_status = 429
        result = self.collect(data)
        self.assertEqual(result['followup_api_requests'], 1)
        self.assertEqual(result['status'], 'rate_limited')
        self.assertFalse(result['investigation']['bounded_complete'])

    def test_private_response_is_distinct(self):
        data = self.history_contract()
        data['metrics']['behavior_review']['questions'][0]['dungeon_id'] = self.row.payload_json['dungeon']['id']
        self.private_history = True
        result = self.collect(data)
        self.assertEqual(result['investigation']['outcomes'][0]['request_status'], 'private')
        self.assertFalse(result['investigation']['bounded_complete'])
        self.calls.clear()
        self.assertEqual(self.collect(data)['followup_api_requests'], 0)

    def test_last_good_and_concurrent_module_merge(self):
        from datetime import timedelta
        from django.utils import timezone
        from botend.models import RaiderIOCharacterActivity
        data = self.history_contract()
        did = self.row.payload_json['dungeon']['id']
        data['metrics']['behavior_review']['questions'][0]['dungeon_id'] = did
        self.collect(data)
        self.activity.refresh_from_db()
        entry = self.activity.payload_json['mythic_plus_scored_history'][str(did)]
        entry['fetched_at'] = (timezone.now() - timedelta(days=3)).isoformat()
        self.activity.save(update_fields=['payload_json'])
        old = copy.deepcopy(entry)
        self.bad_history = True
        original = self.transport
        def transport(url, **kwargs):
            row = RaiderIOCharacterActivity.objects.get(pk=self.activity.pk)
            row.payload_json['raid_first_kills'] = {'independent': True}
            row.save(update_fields=['payload_json'])
            return original(url, **kwargs)
        with patch.object(self, 'transport', side_effect=transport):
            self.collect(data)
        self.activity.refresh_from_db()
        current = self.activity.payload_json['mythic_plus_scored_history'][str(did)]
        self.assertEqual(current['runs'], old['runs'])
        self.assertEqual(current['fetched_at'], old['fetched_at'])
        self.assertEqual(self.activity.payload_json['raid_first_kills'], {'independent': True})
        self.calls.clear()
        self.assertEqual(self.collect(data)['followup_api_requests'], 0)

    def test_scope_values_deleted_and_whitelist(self):
        from botend.services.player_observation_history import validate_history, scope
        ui = scope(self.season.rio_season, 104, 1)
        raw = {'ui': ui, 'runs': [{'score': 100, 'summary': {
            'season': self.season.rio_season, 'dungeon': {'id': 1}, 'keystone_run_id': 12,
            'mythic_level': 10, 'num_chests': 1, 'completed_at': self.run['completed_at']}}]}
        for field, value in [('season', 'season-wrong'), ('dungeon', {'id': 2}),
                             ('keystone_run_id', True), ('completed_at', 'not-a-time'),
                             ('num_chests', None), ('mythic_level', 0)]:
            bad = copy.deepcopy(raw)
            bad['runs'][0]['summary'][field] = value
            with self.subTest(field=field), self.assertRaises((ValueError, TypeError)):
                validate_history(bad, self.season.rio_season, 104, 1)
        raw['runs'][0]['summary']['deleted_at'] = '2026-01-01T00:00:00Z'
        clean = validate_history(raw, self.season.rio_season, 104, 1)
        self.assertEqual(clean['runs'], [])
        self.assertEqual(clean['coverage'], 'observed_partial')

    def test_mixed_questions_share_budget_and_bad_seed_never_requests(self):
        data = self.history_contract(3)
        for q in data['metrics']['behavior_review']['questions']:
            q['dungeon_id'] = self.row.payload_json['dungeon']['id']
        data['metrics']['behavior_review']['questions'][0]['kind'] = 'scored_runs'
        result = self.collect(data)
        self.assertEqual(result['followup_api_requests'], 2)
        self.assertEqual(sum(url.endswith('/mythic-plus-runs') for url, _ in self.calls), 1)
        self.calls.clear()
        for q in data['metrics']['behavior_review']['questions']:
            q['run_ids'] = [9999]
        self.collect(data)
        self.assertEqual(self.calls, [])
