"""Focused SQL-boundary faults; no live source/model calls."""
import copy
import json
from pathlib import Path
from unittest.mock import Mock, patch

from django.db import OperationalError, connection
from django.test import TestCase, SimpleTestCase
from botend.models import SeasonMeta, RaiderIOCharacterActivity
from botend.raiderio_identity import activity_identity_digest
from botend.services.player_observation_sources import _SourceBatch, SCORED_URL
from botend.services.player_observation import _generation_input
from botend.services.player_observation_contract import build_contract, validate_investigation


class ScoredDatabaseBoundaryTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='lock-boundary', season_name='S2',
            rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2)
        self.character = {'identity': {'region': 'us', 'realm_slug': 'illidan', 'character_name': 'boundary'}, 'character_id': 100}
        self.transport = Mock(return_value=Mock(status_code=200, json=lambda: {
            'ui': {'season': 'season-mn-2', 'role': 'all', 'mode': 'scored', 'affixes': 'all', 'date': 'all', 'characterId': 100}, 'dungeons': []}))
        self.batch = _SourceBatch(5, transport=self.transport)
        self.addCleanup(self.batch.close)

    def fault(self, verb, errno):
        table = RaiderIOCharacterActivity._meta.db_table
        def inject(execute, sql, params, many, context):
            if sql.startswith(verb) and table in sql:
                raise OperationalError(errno, 'injected database boundary')
            return execute(sql, params, many, context)
        return connection.execute_wrapper(inject)

    def test_create_timeout_defers_only_target_preserves_prior_http_and_next_target(self):
        with patch('botend.services.player_observation_sources._pace', return_value=True):
            self.batch.get(SCORED_URL)
            with self.fault('INSERT', 1205):
                result = self.batch.scored(self.character, self.season, False)
            self.assertEqual(result['status'], 'busy')
            self.assertEqual(result['phase'], 'ensure_activity')
            self.assertEqual(result['mysql_errno'], 1205)
            self.assertEqual(result['identity_digest'], activity_identity_digest(**self.character['identity']))
            self.assertIs(result['http_sent'], False)
            self.assertEqual(self.batch.requests, 1)
            self.assertEqual(self.transport.call_count, 1)
            self.assertFalse(RaiderIOCharacterActivity.objects.exists())
            self.assertEqual(self.batch.scored(self.character, self.season, False)['status'], 'ready')
            self.assertEqual(self.batch.requests, 2)

    def test_unknown_database_errors_remain_hard_before_and_after_http(self):
        with self.fault('INSERT', 9999), self.assertRaises(OperationalError):
            self.batch.scored(self.character, self.season, False)
        self.transport.assert_not_called()
        with patch('botend.services.player_observation_sources._pace', return_value=True):
            with self.fault('UPDATE', 9999), self.assertRaises(OperationalError):
                self.batch.scored(self.character, self.season, False)
        self.assertEqual(self.transport.call_count, 1)

    def test_write_timeout_reports_sent_http_and_preserves_last_good(self):
        row = RaiderIOCharacterActivity.objects.create(season=self.season,
            identity_digest=activity_identity_digest(**self.character['identity']),
            **self.character['identity'], scored_runs_json={'last_good': True})
        before = RaiderIOCharacterActivity.objects.values().get(pk=row.pk)
        with patch('botend.services.player_observation_sources._pace', return_value=True), self.fault('UPDATE', 1205):
            result = self.batch.scored(self.character, self.season, True)
        self.assertEqual(result['status'], 'busy')
        self.assertEqual(result['phase'], 'save_scored')
        self.assertIs(result['http_sent'], True)
        self.assertEqual(result['mysql_errno'], 1205)
        self.assertEqual(RaiderIOCharacterActivity.objects.values().get(pk=row.pk), before)
        self.assertEqual(self.batch.requests, 1)


class TransportLocalGuideTests(SimpleTestCase):
    def test_local_types_limits_question_menu_and_polarity_preserve_facts(self):
        captures = json.loads((Path(__file__).parent / 'fixtures/player_observation_contract_v8.json').read_text())
        for capture in captures:
            evidence = copy.deepcopy(capture['evidence'])
            evidence['prompt_version'] = 9
            before = copy.deepcopy(evidence)
            contract = build_contract(evidence)
            data = _generation_input(evidence)
            plan = data['metrics']['investigation']
            self.assertEqual(plan['response_contract'], contract)
            schema = plan['schema_version_guide']
            self.assertEqual(schema['type'], 'integer')
            self.assertEqual(schema['value'], 9)
            self.assertIn('无引号', schema['instruction'])
            for guide, menu in zip(plan['selection_guide'], contract['hypotheses']):
                for field in ('support_ids', 'unknown_ids', 'counter_ids'):
                    local = guide[field]
                    self.assertEqual(local['max_items'], contract['limits'][field + '_max'])
                    self.assertEqual(local['candidate_count'], len(menu[field]))
                    self.assertEqual([c['id'] for c in local['candidates']], menu[field])
                self.assertIn('label_reviews', guide['counter_ids']['instruction'])
                self.assertIn('[]', guide['counter_ids']['instruction'])
            questions = {q['id']: q for q in plan['questions']}
            qguide = plan['next_question_guide']
            self.assertEqual(qguide['max_items'], 3)
            self.assertEqual(qguide['candidates'], [{'id': qid, 'reason': questions[qid]['reason']} for qid in contract['next_question_ids']])
            self.assertIn('behavior_review.questions', qguide['instruction'])
            self.assertIn('禁止重组', qguide['instruction'])
            candidate = {'schema_version': 9, 'confidence': contract['confidence'], 'next_question_ids': contract['next_question_ids'][:3],
                'hypotheses': [{'id': h['id'], 'assessment': 'possible', 'support_ids': h['support_ids'][:3],
                    'counter_ids': h['counter_ids'][:], 'unknown_ids': h['unknown_ids'][:3]} for h in contract['hypotheses']]}
            validate_investigation(candidate, evidence)
            self.assertEqual(evidence, before)
            self.assertEqual(data['evidence'], before['evidence'])
