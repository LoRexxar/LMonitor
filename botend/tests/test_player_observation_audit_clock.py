"""Narrow audit-clock candidate: full real captures, no model/source transport."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from botend.models import PlayerObservationAnalysis, RaiderIORunRoster
from botend.services import player_observation as service
from botend.tests import test_player_observation as fixtures


FIXTURE = Path(__file__).parent / 'fixtures/player_observation_audit_clock.json.gz'
CAPTURES = json.loads(gzip.decompress(FIXTURE.read_bytes()))


class AuditClockTests(TestCase):
    evidence = fixtures.PlayerObservationTests.evidence

    def setUp(self):
        fixtures.PlayerObservationTests.setUp(self)
        for target in ('requests.sessions.Session.request', 'socket.socket.connect',
                       'botend.services.player_observation.PlayerObservationGLMClient'):
            guard = patch(target, side_effect=AssertionError('No network/model in offline replay'))
            guard.start()
            self.addCleanup(guard.stop)

    @staticmethod
    def row(capture):
        return SimpleNamespace(**copy.deepcopy(capture['actual_row']))

    def persist(self, capture):
        # Only local FK wiring differs; every captured analysis field is loaded,
        # including raw JSON, generated_at, lease, review and revision metadata.
        values = copy.deepcopy(capture['actual_row'])
        self.assertIsNone(values['reviewed_by_id'])
        type(self.activity).objects.filter(pk=self.activity.pk).update(id=values['activity_id'])
        self.activity.pk = values['activity_id']
        values['player_id'] = self.player.pk
        for field in PlayerObservationAnalysis._meta.concrete_fields:
            if field.get_internal_type() == 'DateTimeField':
                values[field.attname] = field.to_python(values[field.attname])
        row = PlayerObservationAnalysis.objects.create(**values)
        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(**values)
        row.refresh_from_db()
        return row

    @staticmethod
    def raw_row(row):
        with connection.cursor() as cursor:
            cursor.execute('SELECT * FROM wow_player_observation_analysis WHERE id = %s', [row.pk])
            return cursor.fetchone()

    def test_full_captures_three_clock_only_and_seventeen_scope_changes(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            '7a6da7233a7686c7d1880e621d580e9edb2168b5a24b64f866aa068ec3b0562d')
        self.assertEqual(len(CAPTURES), 24)
        self.assertEqual({c['player_id'] for c in CAPTURES if c['expected_reuse']},
                         {169986, 131975, 175549})
        for capture in CAPTURES:
            with self.subTest(player=capture['player_id']):
                before = copy.deepcopy(capture)
                row = self.row(capture)
                self.assertEqual(row.evidence_json, capture['frozen_evidence'])
                self.assertEqual(row.result_json, capture['frozen_result'])
                serialized = json.dumps(capture['actual_row'], ensure_ascii=False,
                    sort_keys=True, separators=(',', ':')).encode()
                self.assertEqual(hashlib.sha256(serialized).hexdigest(), capture['row_sha256'])
                service._validate_frozen_publication(row)
                self.assertEqual(service._same_publication(row, capture['current_evidence']),
                                 capture['expected_reuse'])
                self.assertEqual(capture, before)

    def test_captured_generator_reuses_without_any_analysis_row_write(self):
        for capture in CAPTURES:
            if not capture['expected_reuse']:
                continue
            with self.subTest(player=capture['player_id']):
                row = self.persist(capture)
                try:
                    before = self.raw_row(row)
                    engine = Mock(send_message=Mock(side_effect=AssertionError('must not call model')))
                    with patch.object(service, 'build_evidence', return_value=capture['current_evidence']), \
                            patch.object(service, '_build', return_value=capture['current_evidence']):
                        result = service.generate_observation(self.player, engine=engine)
                        self.assertEqual(result['status'], 'fresh')
                        self.assertIs(result['ai_called'], False)
                        self.assertEqual(service.project_player_observation(self.player)['status'], 'ready')
                    engine.send_message.assert_not_called()
                    self.assertEqual(self.raw_row(row), before)
                finally:
                    row.delete()

    def test_adjacent_changes_do_not_hide_behind_clock_normalization(self):
        c = next(c for c in CAPTURES if c['player_id'] == 110189)
        row = self.row(c)
        # Explicit adversarial derivative of a real complete capture, not a
        # claimed captured current: isolate a clock-only positive for an AI row.
        current = copy.deepcopy(row.evidence_json)
        current['prompt_version'] = 8  # 保持原始 v7→v8 审计兼容场景。
        current['source_stamps']['source_updated_at'] = '2026-10-21T01:00:00+00:00'
        self.assertTrue(service._same_publication(row, current))
        mutations = {
            'behavior_fact': lambda e: e['metrics'].update(highest_timed_level=999),
            'member': lambda e: e['metrics']['investigation']['core_members'].append('other'),
            'support': lambda e: e['metrics']['investigation']['hypotheses'][0]['support_ids'].append('forged'),
            'counter': lambda e: e['metrics']['investigation']['hypotheses'][0]['counter_ids'].append('forged'),
            'unknown_evidence': lambda e: e['metrics']['investigation']['hypotheses'][0]['unknown_ids'].append('forged'),
            'only_question': lambda e: e['metrics']['investigation']['questions'].append({'id': 'new'}),
            'sample': lambda e: e['coverage'].update(unique_runs=999),
            'member_scope': lambda e: e['coverage'].update(total_characters=999),
            'score_scope': lambda e: e['coverage'].update(score_characters=999),
            'future_coverage': lambda e: e['coverage'].update(future_scope=1),
            'future_coverage_clock': lambda e: e['coverage'].update(fetched_at='unknown'),
            'unknown_field': lambda e: e.update(future_field={'x': 1}),
            'unknown_clock_field': lambda e: e['metrics'].update(fetched_at='unknown'),
            'unknown_module': lambda e: e['source_stamps']['module_fetched_at'].update(future='2026-10-21T00:00:00Z'),
            'nested_version': lambda e: e['metrics'].update(prompt_version=9),
            'source_failure': lambda e: e.update(source_failed=True),
            'sourcegate_type': lambda e: e.update(source_failed=0),
            'missing_sourcegate': lambda e: e.pop('source_failed'),
            'private': lambda e: e['source_stamps']['scores'][0].update(private=True),
            'source_owner': lambda e: e['source_stamps']['scores'][0].update(activity_id=999999),
            'source_digest': lambda e: e['source_stamps']['scores'][0].update(digest='0'*64),
            'identity': lambda e: e.update(activity_id=999999),
            'season': lambda e: e.update(season='another-season'),
            'bad_clock': lambda e: e['source_stamps'].update(source_updated_at='2026-99-99T00:00:00Z'),
            'missing_clock': lambda e: e['source_stamps'].pop('source_updated_at'),
            'backward_clock': lambda e: e['source_stamps'].update(source_updated_at='2020-01-01T00:00:00Z'),
            'future_version': lambda e: e.update(prompt_version=9),
            'downgrade': lambda e: e.update(prompt_version=7),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                changed = copy.deepcopy(current)
                mutate(changed)
                self.assertFalse(service._same_publication(row, changed))
        for version in (6, 9, True, '7', 8):
            broken = copy.deepcopy(row)
            broken.prompt_version = version
            self.assertFalse(service._same_publication(broken, current))
        broken = copy.deepcopy(row)
        broken.evidence_json['coverage']['unique_runs'] = 999
        self.assertFalse(service._same_publication(broken, current))

    def test_invalid_real_frozen_result_still_cannot_be_fresh(self):
        capture = next(c for c in CAPTURES if c['player_id'] == 98190)
        row = self.persist(capture)
        current = copy.deepcopy(row.evidence_json)
        current['prompt_version'] = 8  # 保持原始 v7→v8 审计兼容场景。
        current['source_stamps']['source_updated_at'] = '2026-10-21T01:00:00+00:00'
        row.result_json['investigation']['summary'] = '确认收费带人'
        row.save(update_fields=['result_json'])
        with self.assertRaises(service.InvalidResult):
            service._validate_frozen_publication(row)
        engine = Mock(send_message=Mock(return_value='{}'))
        with patch.object(service, 'build_evidence', return_value=current), \
                patch.object(service, '_build', return_value=current):
            self.assertEqual(service.project_player_observation(self.player)['status'], 'error')
            self.assertNotEqual(service.generate_observation(self.player, engine=engine)['status'], 'fresh')
        engine.send_message.assert_called_once()  # Controlled local stub only.
        after = PlayerObservationAnalysis.objects.get(pk=row.pk)
        self.assertEqual(after.evidence_hash, row.evidence_hash)
        self.assertEqual(after.evidence_json, row.evidence_json)
        self.assertEqual(after.result_json, row.result_json)
        self.assertEqual(after.generated_at, row.generated_at)

    def test_audit_equivalence_does_not_replace_exact_inflight_cas(self):
        from datetime import timedelta
        from botend.tests.test_player_observation_versions import PublicationVersionTests
        self.activity.source_updated_at = timezone.now() - timedelta(hours=2)
        self.activity.save()
        row = PublicationVersionTests.current(self)

        def advance_audit_clock(result):
            self.activity.source_updated_at += timedelta(hours=1)
            self.activity.save()
            self.assertTrue(service._same_publication(row, self.evidence()))

        engine = fixtures.PlayerObservationTests.engine(self, advance_audit_clock)
        result = service.generate_observation(self.player, force=True, engine=engine)
        self.assertEqual(result['status'], 'evidence_changed')
        engine.send_message.assert_called_once()  # Local protocol stub, not AI.
        after = PlayerObservationAnalysis.objects.get(pk=row.pk)
        self.assertEqual(after.evidence_hash, row.evidence_hash)
        self.assertEqual(after.evidence_json, row.evidence_json)
        self.assertEqual(after.result_json, row.result_json)
        self.assertEqual(after.generated_at, row.generated_at)

    def test_actual_builder_clock_change_retains_source_privacy_and_identity_gates(self):
        from datetime import timedelta
        from botend.tests.test_player_observation_versions import PublicationVersionTests
        self.activity.source_updated_at = timezone.now() - timedelta(hours=2)
        self.activity.save()
        row = PublicationVersionTests.current(self)
        before = self.raw_row(row)
        self.activity.source_updated_at += timedelta(hours=1)
        self.activity.save()
        engine = Mock(send_message=Mock(side_effect=AssertionError('must not call model')))
        self.assertEqual(service.generate_observation(self.player, engine=engine)['status'], 'fresh')
        self.assertEqual(self.raw_row(row), before)
        self.activity.last_error = 'source_error'
        self.activity.save()
        self.assertEqual(service.generate_observation(self.player, engine=engine)['status'], 'source_failed')
        self.assertEqual(self.raw_row(row), before)
        self.activity.last_error = ''
        self.activity.save()
        RaiderIORunRoster.objects.filter(run_id=123).update(roster_visible=False)
        self.assertFalse(service._same_publication(row, service.build_evidence(self.player)))
        self.player.rank = None
        self.player.save()
        self.assertIsNone(service.build_evidence(self.player))
        self.assertEqual(service.generate_observation(self.player, engine=engine)['status'], 'unavailable')
        engine.send_message.assert_not_called()
        self.assertEqual(self.raw_row(row), before)
