"""Captured audit-only drift; real SQLite generator and immutable row reads."""
import copy
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db import connection
from django.test import TestCase
from django.utils import timezone
from botend.models import PlayerObservationAnalysis
from botend.services import player_observation as service
from botend.tests import test_player_observation as fixtures


CAPTURES = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures' /
    'player_observation_history_append.json.gz').read_bytes()))


class HistoryAppendTests(TestCase):
    setUp = fixtures.PlayerObservationTests.setUp

    def row(self, capture):
        old = capture['frozen_evidence']
        return SimpleNamespace(prompt_version=old['prompt_version'], evidence_json=old,
            evidence_hash=old['evidence_hash'], result_json=capture['frozen_result'])

    def test_captured_eleven_append_only_and_two_questions(self):
        self.assertEqual(len(CAPTURES), 13)
        self.assertEqual(sum(c['expected_reuse'] for c in CAPTURES), 11)
        for capture in CAPTURES:
            with self.subTest(player=capture['player_id']):
                before = copy.deepcopy(capture)
                row = self.row(capture)
                service._validate_frozen_publication(row)
                self.assertEqual(service._same_publication(row, capture['current_evidence']),
                    capture['expected_reuse'])
                self.assertEqual(capture, before)

    def test_captured_generator_zero_model_and_byte_exact_sqlite_row(self):
        for capture in CAPTURES:
            if not capture['expected_reuse']:
                continue
            with self.subTest(player=capture['player_id']):
                frozen = self.row(capture)
                # Use the captured activity identity in the isolated DB, not production.
                type(self.activity).objects.filter(pk=self.activity.pk).update(
                    id=capture['current_evidence']['activity_id'])
                self.activity.pk = capture['current_evidence']['activity_id']
                row = PlayerObservationAnalysis.objects.create(activity=self.activity,
                    player=self.player, prompt_version=frozen.prompt_version,
                    evidence_json=frozen.evidence_json, evidence_hash=frozen.evidence_hash,
                    result_json=frozen.result_json, status='ready', generated_at=timezone.now())
                def raw_row():
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT * FROM wow_player_observation_analysis WHERE id = %s', [row.pk])
                        return cursor.fetchone()
                before = raw_row()
                engine = Mock()
                with patch.object(service, 'build_evidence', return_value=capture['current_evidence']), \
                        patch.object(service, '_build', return_value=capture['current_evidence']):
                    self.assertEqual(service.generate_observation(self.player, engine=engine)['status'], 'fresh')
                    self.assertEqual(service.project_player_observation(self.player)['status'], 'ready')
                engine.send_message.assert_not_called()
                self.assertEqual(raw_row(), before)
                row.delete()

    def test_append_rejects_changes_failures_unknowns_and_invalid_types(self):
        capture = next(c for c in CAPTURES if c['player_id'] == 166327)
        row = self.row(capture)
        current = capture['current_evidence']
        old_ids = {(v['activity_id'], v['dungeon_id']) for v in row.evidence_json['source_stamps']['histories']}
        added = next(i for i, v in enumerate(current['source_stamps']['histories'])
            if (v['activity_id'], v['dungeon_id']) not in old_ids)
        existing = next(i for i, v in enumerate(current['source_stamps']['histories'])
            if (v['activity_id'], v['dungeon_id']) in old_ids)
        mutations = {
            'delete': lambda e: e['source_stamps']['histories'].pop(existing),
            'changed_digest': lambda e: e['source_stamps']['histories'][existing].update(digest='0' * 64),
            'duplicate': lambda e: e['source_stamps']['histories'].append(copy.deepcopy(e['source_stamps']['histories'][added])),
            'unknown': lambda e: e['source_stamps']['histories'][added].update(unknown={'value': 1}),
            'unknown_clock': lambda e: e['source_stamps']['histories'][added].update(snapshot_as_of='ignored?'),
            'failed': lambda e: e['source_stamps']['histories'][added].update(status='error'),
            'source_failed': lambda e: e.update(source_failed=True),
            'missing_sourcegate': lambda e: e.pop('source_failed'),
            'sourcegate_type': lambda e: e.update(source_failed=0),
            'canonical': lambda e: e['metrics'].update(new_fact=1),
            'facts': lambda e: e['coverage'].update(unique_runs=999),
            'source': lambda e: e['source_stamps'].update(source_updated_at='changed'),
            'unknown_nested': lambda e: e['metrics'].update(unknown={'value': 1}),
            'nested_version': lambda e: e['metrics'].update(prompt_version=9),
            'downgrade': lambda e: e.update(prompt_version=7),
            'future': lambda e: e.update(prompt_version=9),
            'bool_id': lambda e: e['source_stamps']['histories'][added].update(activity_id=True),
            'bad_digest': lambda e: e['source_stamps']['histories'][added].update(digest='bad'),
            'bad_date': lambda e: e['source_stamps']['histories'][added].update(fetched_at='bad'),
            'bad_url': lambda e: e['source_stamps']['histories'][added].update(source_url='https://evil.invalid/'),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                changed = copy.deepcopy(current)
                mutate(changed)
                self.assertFalse(service._same_publication(row, changed))
        corrupt = copy.deepcopy(row)
        corrupt.evidence_json['metrics']['tampered'] = True
        self.assertFalse(service._same_publication(corrupt, current))
        for version in (6, 9, True, '7', 8):
            corrupt = copy.deepcopy(row)
            corrupt.prompt_version = version
            self.assertFalse(service._same_publication(corrupt, current))
        corrupt = copy.deepcopy(row)
        corrupt.result_json['investigation']['schema_version'] = 7
        with self.assertRaises(service.InvalidResult):
            service._validate_frozen_publication(corrupt)

    def test_actual_builder_successful_empty_history_append_is_zero_model(self):
        from urllib.parse import urlencode
        from botend.services.player_observation_history import scope, URL
        from botend.tests.test_player_observation_versions import PublicationVersionTests
        row = PublicationVersionTests.current(self)
        before = PlayerObservationAnalysis.objects.values().get(pk=row.pk)
        self.activity.refresh_from_db()
        ui = scope(self.season.rio_season, 100, 9527)
        self.activity.payload_json['mythic_plus_scored_history'] = {'9527': {
            'ui': ui, 'source_url': URL + '?' + urlencode(ui), 'runs': [],
            'coverage': 'observed_partial', 'status': 'ready',
            'fetched_at': timezone.now().isoformat(), 'last_error': '', 'private': False}}
        self.activity.save()
        current = service.build_evidence(self.player)
        self.assertFalse(current['source_failed'])
        self.assertEqual(len(current['source_stamps']['histories']), 1)
        def differences(a, b, path=''):
            if isinstance(a, dict) and isinstance(b, dict):
                return [p for k in a.keys() | b.keys() for p in differences(a.get(k), b.get(k), path + '.' + k)]
            return [] if a == b else [path]
        self.assertTrue(service._same_publication(row, current), differences(
            service._semantic_evidence(row.evidence_json), service._semantic_evidence(current)))
        engine = Mock()
        self.assertEqual(service.generate_observation(self.player, engine=engine)['status'], 'fresh')
        self.assertEqual(service.project_player_observation(self.player)['status'], 'ready')
        engine.send_message.assert_not_called()
        self.assertEqual(PlayerObservationAnalysis.objects.values().get(pk=row.pk), before)
        # Failure in the same actual source must block despite the valid last-good.
        self.activity.payload_json['mythic_plus_scored_history']['9527']['status'] = 'error'
        self.activity.save()
        self.assertEqual(service.generate_observation(self.player, engine=engine)['status'], 'source_failed')
        engine.send_message.assert_not_called()
        self.assertEqual(PlayerObservationAnalysis.objects.values().get(pk=row.pk), before)

    def test_real_current_source_gate_still_blocks_reuse(self):
        from botend.tests.test_player_observation_versions import PublicationVersionTests
        row = PublicationVersionTests.current(self)
        self.activity.last_error = 'source_error'
        self.activity.save()
        engine = Mock()
        before = PlayerObservationAnalysis.objects.values().get(pk=row.pk)
        self.assertEqual(service.generate_observation(self.player, engine=engine)['status'], 'source_failed')
        engine.send_message.assert_not_called()
        self.assertEqual(PlayerObservationAnalysis.objects.values().get(pk=row.pk), before)

    evidence = fixtures.PlayerObservationTests.evidence
