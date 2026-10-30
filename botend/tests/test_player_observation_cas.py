"""Opt-in generation CAS, real ORM/writers; no external transport.

SQLite sequential interleavings prove predicates, not InnoDB contention.
"""
import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TransactionTestCase

from botend.models import PlayerObservationAnalysis, RaiderIORunRoster
from botend.services import player_observation as service
from botend.services import player_analysis_management as editorial
from botend.tests import test_player_observation as fixtures


class ObservationCASTests(TransactionTestCase):
    def setUp(self):
        self.enterContext(patch('django.utils.timezone.now', return_value=datetime(
            2026, 10, 11, 12, 0, 0, 123456, tzinfo=timezone.utc)))
        fixtures.PlayerObservationTests.setUp(self)
        self.user = get_user_model().objects.create_user(username='cas-editor')
        self.enterContext(patch('requests.sessions.Session.request',
            side_effect=AssertionError('external HTTP forbidden')))

    def snapshot(self):
        return PlayerObservationAnalysis.objects.values().get()

    def manual(self):
        return editorial.create_analysis({'player_id': self.player.pk,
            'manual': {'tags': [], 'conclusion': '人工结论', 'evidence': []}}, self.user)

    def test_real_writer_between_evidence_and_claim_is_not_adopted(self):
        original = service.build_evidence
        saved = []
        def interleave(player):
            evidence = original(player)
            self.assertTrue(service._has_investigation(evidence))
            if not saved:
                self.manual()
                saved.append(self.snapshot())
            return evidence
        engine = Mock(send_message=Mock(side_effect=RuntimeError('offline engine boundary')))
        # Baseline has no opt-in keyword: exercise its actual old entry, not a
        # TypeError. Candidate MUST expose the keyword and runs the same writer.
        kwargs = {'expected_absent': True} if 'expected_absent' in inspect.signature(service.generate_observation).parameters else {}
        with patch.object(service, 'build_evidence', side_effect=interleave):
            result = service.generate_observation(self.player, engine=engine, skip_uninformative=True, **kwargs)
        self.assertEqual(engine.send_message.call_count, 0, result)
        self.assertEqual(self.snapshot(), saved[0])
        self.assertEqual(result['status'], 'cas_claim_mismatch')

    def local_only(self):
        RaiderIORunRoster.objects.all().delete()
        self.activity.payload_json = {}
        self.activity.save()

    def generate(self, **kwargs):
        return service.generate_observation(self.player, skip_uninformative=True, **kwargs)

    def test_absent_and_existing_no_drift_local_publication_and_fresh(self):
        self.local_only()
        with patch.object(service, 'PlayerObservationGLMClient', side_effect=AssertionError('no AI')):
            result = self.generate(expected_absent=True)
            self.assertEqual(result['status'], 'ready')
            before = self.snapshot()
            self.assertTrue(service._local_empty(before['result_json'], before['evidence_json']))
            self.assertEqual(self.generate(expected_snapshot=before)['status'], 'fresh')
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self.generate(force=True, expected_snapshot=before)['status'], 'ready')
        self.assertEqual(self.snapshot()['player_id'], self.player.pk)

    def test_snapshot_strict_types_microseconds_missing_and_deleted(self):
        self.local_only()
        row = PlayerObservationAnalysis.objects.create(activity=self.activity, recheck_json={'v': False},
            updated_at=datetime(2026, 10, 11, 12, 0, 0, 123456, tzinfo=timezone.utc))
        before = self.snapshot()
        import copy
        variants = []
        changed = copy.deepcopy(before)
        changed['updated_at'] += timedelta(microseconds=1)
        variants.append(changed)
        changed = copy.deepcopy(before)
        changed['recheck_json']['v'] = 0
        variants.append(changed)
        variants.append({k: v for k, v in before.items() if k != 'review_note'})
        for snapshot in variants:
            with self.subTest(snapshot=snapshot):
                self.assertEqual(self.generate(expected_snapshot=snapshot)['status'], 'cas_claim_mismatch')
                self.assertEqual(self.snapshot(), before)
        row.delete()
        self.assertEqual(self.generate(expected_snapshot=before)['status'], 'cas_claim_mismatch')
        self.assertFalse(PlayerObservationAnalysis.objects.exists())

    def test_editorial_states_refused_under_claim_without_any_write(self):
        self.local_only()
        for state in ({'manual_json': {'conclusion': 'manual'}}, {'is_published': False}, {'review_status': 'rejected'}):
            with self.subTest(state=state):
                PlayerObservationAnalysis.objects.all().delete()
                PlayerObservationAnalysis.objects.create(activity=self.activity, **state)
                before = self.snapshot()
                self.assertEqual(self.generate(expected_snapshot=before)['status'], 'cas_editorial_protected')
                self.assertEqual(self.snapshot(), before)

    def test_claim_drift_via_real_editorial_writer(self):
        PlayerObservationAnalysis.objects.create(activity=self.activity, player=self.player)
        expected = self.snapshot()
        original = service.build_evidence
        saved = []
        def build(player):
            evidence = original(player)
            editorial.update_analysis(expected['id'], {'revision': expected['revision'], 'review_note': 'new note'}, self.user)
            saved.append(self.snapshot())
            return evidence
        engine = Mock()
        with patch.object(service, 'build_evidence', side_effect=build):
            result = self.generate(engine=engine, expected_snapshot=expected)
        self.assertEqual(result['status'], 'cas_claim_mismatch')
        self.assertEqual(self.snapshot(), saved[0])
        engine.send_message.assert_not_called()

    def test_engine_exception_clean_failure_and_drift_preservation(self):
        for drift in ('none', 'editorial', 'lease', 'microsecond', 'json_type', 'deleted'):
            with self.subTest(drift=drift):
                PlayerObservationAnalysis.objects.all().delete()
                saved = []
                calls = []
                def send(*args, **kwargs):
                    self.assertTrue(transaction.get_autocommit())
                    calls.append(True)
                    row = PlayerObservationAnalysis.objects.get()
                    if drift == 'editorial':
                        editorial.update_analysis(row.pk, {'revision': row.revision,
                            'manual': {'tags': [], 'conclusion': 'new manual', 'evidence': []}}, self.user)
                    elif drift == 'lease':
                        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(lease_token='other-owner')
                    elif drift == 'microsecond':
                        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(last_attempt_at=row.last_attempt_at + timedelta(microseconds=1))
                    elif drift == 'json_type':
                        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(recheck_json={'v': 0})
                    elif drift == 'deleted':
                        row.delete()
                    saved.append(None if drift == 'deleted' else self.snapshot())
                    raise RuntimeError('offline engine failure')
                result = self.generate(engine=Mock(send_message=send), expected_absent=True)
                self.assertEqual(len(calls), 1)
                self.assertTrue(result['ai_called'])
                if drift == 'none':
                    self.assertEqual(result['status'], 'engine_error')
                    after = self.snapshot()
                    self.assertEqual(after['lease_token'], '')
                    self.assertEqual(after['status'], 'error')
                    self.assertIsNotNone(after['next_attempt_at'])
                else:
                    self.assertEqual(result['status'], 'cas_finalize_mismatch')
                    self.assertEqual(None if drift == 'deleted' else self.snapshot(), saved[0])

    def test_finalize_drift_after_claim_commit_preserves_whole_row(self):
        self.local_only()
        original = service._row_snapshot
        for drift in ('editorial', 'lease', 'microsecond'):
            with self.subTest(drift=drift):
                PlayerObservationAnalysis.objects.all().delete()
                saved, scheduled = [], []
                def mutate():
                    self.assertTrue(transaction.get_autocommit())
                    row = PlayerObservationAnalysis.objects.get()
                    if drift == 'editorial':
                        editorial.update_analysis(row.pk, {'revision': row.revision,
                            'manual': {'tags': [], 'conclusion': 'new manual', 'evidence': []}}, self.user)
                    elif drift == 'lease':
                        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(lease_token='other-owner')
                    else:
                        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(last_attempt_at=row.last_attempt_at + timedelta(microseconds=1))
                    saved.append(self.snapshot())
                def snapshot(row):
                    value = original(row)
                    # Real post-commit writer, before the local-empty finalize.
                    # Snapshot codec still runs unchanged; no ORM is patched.
                    if not scheduled:
                        scheduled.append(True)
                        transaction.on_commit(mutate)
                    return value
                with patch.object(service, '_row_snapshot', side_effect=snapshot):
                    result = self.generate(expected_absent=True)
                self.assertEqual(result['status'], 'cas_finalize_mismatch')
                self.assertFalse(result['ai_called'])
                self.assertEqual(self.snapshot(), saved[0])

    def test_guarded_requires_autocommit_and_explicit_valid_arguments(self):
        with transaction.atomic():
            self.assertEqual(self.generate(expected_absent=True)['status'], 'cas_requires_autocommit')
        for kwargs in ({'expected_absent': 1}, {'expected_absent': None},
                       {'expected_absent': True, 'expected_snapshot': {}}, {'expected_snapshot': {}}):
            self.assertEqual(self.generate(**kwargs)['status'], 'cas_claim_mismatch')
        self.assertFalse(PlayerObservationAnalysis.objects.exists())

    def test_guarded_backoff_and_busy_do_not_bind_or_mutate(self):
        self.local_only()
        now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
        for state, status in (({'next_attempt_at': now + timedelta(hours=1)}, 'backoff'),
                              ({'lease_token': 'other', 'lease_expires_at': now + timedelta(hours=1)}, 'busy')):
            PlayerObservationAnalysis.objects.all().delete()
            PlayerObservationAnalysis.objects.create(activity=self.activity, **state)
            before = self.snapshot()
            self.assertEqual(self.generate(expected_snapshot=before, force=True)['status'], status)
            self.assertEqual(self.snapshot(), before)

    def test_default_manual_recheck_contract_unchanged_and_no_rebind(self):
        self.local_only()
        self.manual()
        before = self.snapshot()
        # Existing UI ordinary calls are intentionally not opted into CAS.
        with transaction.atomic():
            self.assertEqual(self.generate()['status'], 'ready')
        after = self.snapshot()
        for field in ('manual_json', 'review_status', 'is_published', 'player_id'):
            self.assertEqual(after[field], before[field])

    def test_existing_representative_is_never_rebound(self):
        from botend.models import PlayerSpecTopPlayer
        self.local_only()
        old = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu',
            realm='Test Realm', character_name='Member0', class_name='Priest', spec_name='Shadow')
        PlayerObservationAnalysis.objects.create(activity=self.activity, player=old)
        self.assertEqual(self.generate(expected_snapshot=self.snapshot())['status'], 'ready')
        self.assertEqual(self.snapshot()['player_id'], old.pk)
