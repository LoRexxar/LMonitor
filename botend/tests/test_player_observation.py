"""Bounded observation contracts against real ORM facts (AI boundary injected)."""
import copy
import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import PlayerSpecTopPlayer, PlayerObservationAnalysis, RaiderIOCharacterActivity, RaiderIORunRoster, SeasonMeta
from botend.services.player_raiderio_activity import player_identity
from botend.tests.test_raiderio_run_roster import details
from botend.tests.test_player_raiderio_activity import payload


def observation_response(data):
    """Schema-only engine substitute; no claim to perform an AI investigation."""
    label = data['eligible_labels'][0]
    result = {'observations': [{'code': label['code'], 'confidence': '低',
        'reason_id': next(iter(label['reason_options'])),
        'limitation_ids': label['required_limitation_ids'],
        'evidence_ids': label['evidence_ids']}], 'conclusion_id': 'public_sample'}
    plan = data.get('metrics', {}).get('investigation', {})
    if plan.get('triggered') and 'response_contract' in plan:
        result['investigation'] = structured_response(plan['response_contract'])
    elif plan.get('triggered'):
        hypotheses = sorted(plan['hypotheses'], key=lambda h: h['id'] != 'external_assistance')[:4]
        result['investigation'] = {'summary': '已有公开线索，仍需比较协作解释。',
            'confidence': '低', 'hypotheses': [{
                'id': h['id'], 'assessment': 'possible',
                'evidence_ids': (h['support_ids'] + h['unknown_ids'])[:3],
                'counter_evidence_ids': h['counter_ids'],
                'explanation': '仅为待核查的解释，不确定组队目的。'} for h in hypotheses],
            'next_question_ids': [q['id'] for q in plan['questions'][:3]]}
    return result


def structured_response(menu):
    """Controlled v8 engine choice from the actual transport contract."""
    return {'schema_version': 8, 'confidence': '低',
        'hypotheses': [{'id': h['id'],
            'assessment': ('unknown' if not h['support_ids'] and not h['counter_ids']
                           and 'unknown' in h['assessment_options'] else h['assessment_options'][0]),
            'support_ids': h['support_ids'][:3], 'counter_ids': h['counter_ids'],
            'unknown_ids': h['unknown_ids'][:3]} for h in menu['hypotheses']],
        'next_question_ids': menu['next_question_ids'][:3]}


class PlayerObservationTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='mn-s2', season_name='S2', rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2)
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu', realm='Test Realm', character_name='Member0', class_name='Priest', spec_name='Holy', rank=1, profile_url='https://raider.io/characters/eu/test-realm/Member0')
        self.activity = RaiderIOCharacterActivity.objects.create(season=self.season, **player_identity(self.player), fetched_at=timezone.now(), status='ready')
        self.runs = []
        for run_id, level, score in ((123, 20, 600), (124, 22, 400), (125, 21, 500)):
            run = copy.deepcopy(payload()['mythic_plus_best_runs'][0])
            run.update(keystone_run_id=run_id, mythic_level=level, score=score, num_keystone_upgrades=1, url=f'https://raider.io/mythic-plus-runs/season-mn-2/{run_id}')
            raw = details(run_id)
            raw.update(mythic_level=level, num_chests=1)
            for i, member in enumerate(raw['roster']):
                member['character']['id'] = 100 + i
            RaiderIORunRoster.objects.create(rio_season=self.season.rio_season, run_id=run_id, payload_json=raw, fetched_at=timezone.now(), roster_visible=True, status='ready')
            self.runs.append(run)
        self.activity.payload_json = {'mythic_plus_best_runs': self.runs, 'mythic_plus_recent_runs': self.runs}
        self.activity.save()
        for i in range(5):
            row, _ = RaiderIOCharacterActivity.objects.get_or_create(season=self.season, region='eu', realm_slug='test-realm', character_name=f'member{i}')
            row.scored_runs_json = {'ui': {'season': self.season.rio_season, 'characterId': 100+i, 'role': 'all', 'mode': 'scored', 'affixes': 'all', 'date': 'all'}, 'dungeons': [{'dungeon': {'id': 1, 'name': "Kings' Rest", 'slug': 'kings-rest'}, 'keystone_run_id': 999+i, 'mythic_level': 22 if i == 0 else 25, 'score': 400 if i == 0 else 450, 'num_chests': 1, 'completed_at': '2026-10-10T12:00:00Z', 'role': 'healer', 'alternate': None}]}
            row.scored_runs_fetched_at = timezone.now()
            row.save()

    def evidence(self):
        from botend.services.player_observation import build_evidence
        return build_evidence(self.player)

    def engine(self, mutate=None):
        def respond(prompt, **kwargs):
            data = json.loads(prompt)
            triggered = data.get('metrics', {}).get('investigation', {}).get('triggered')
            self.assertEqual(kwargs, {'max_tokens': 3600 if triggered else 1800, 'thinking_type': 'disabled'})
            result = observation_response(data)
            if mutate:
                mutate(result)
            return json.dumps(result, ensure_ascii=False)
        return Mock(send_message=Mock(side_effect=respond))

    def test_bounded_highest_timed_and_five_member_comparison(self):
        data = self.evidence()
        self.assertEqual(data['coverage']['unique_runs'], 3)
        self.assertEqual(data['coverage']['comparison_runs'], 3)
        self.assertEqual(data['coverage']['score_characters'], 5)
        self.assertEqual(data['season_counts'], {'total': None, 'timed': None})
        self.assertEqual(data['metrics']['highest_timed_level'], 22)
        labels = {v['code']: v for v in data['eligible_labels']}
        self.assertNotIn('higher_teammates', labels)
        self.assertIn('fixed_team', labels)
        self.assertFalse(data['metrics']['behavior_review']['supported'])
        self.assertNotIn('rotating_core', labels)
        self.assertNotIn('roster', json.dumps(data['evidence']))
        RaiderIOCharacterActivity.objects.filter(character_name='member4').update(scored_runs_fetched_at=timezone.now()-timedelta(hours=49))
        data = self.evidence()
        self.assertEqual(data['coverage']['comparison_runs'], 0)
        self.assertNotIn('higher_teammates', {x['code'] for x in data['eligible_labels']})
        self.assertTrue(data['limitations'])

    def test_shared_cache_readonly_privacy_retraction(self):
        from botend.services.player_observation import generate_observation, project_player_observation
        with CaptureQueriesContext(connection) as queries:
            self.assertIsNone(project_player_observation(self.player))
        self.assertFalse(PlayerObservationAnalysis.objects.exists())
        self.assertFalse(any('wow_raiderio_run_roster' in q['sql'] for q in queries))
        self.assertEqual(generate_observation(self.player, engine=self.engine())['status'], 'ready')
        sibling = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu', realm='Test Realm', character_name='Member0', class_name='Priest', spec_name='Discipline', rank=21, profile_url=self.player.profile_url)
        with patch('requests.get', side_effect=AssertionError('read only')), CaptureQueriesContext(connection) as queries:
            view = project_player_observation(sibling)
        self.assertEqual(view['status'], 'ready')
        self.assertEqual(view['observations'][0]['reason'], '已观测的成绩时序或成员关系形成具体调查线索，仍需比较协作与钥匙解释。')
        self.assertEqual(view['observations'][0]['limitations'], [
            '当前成绩仅为代理，不代表历史当场加分。',
            '协作、练习和后续进步仍可能；无法确认钥匙归属或交易。'])
        self.assertIn('不由当前分差倒推历史', view['conclusion'])
        self.assertTrue(view['has_investigation'])
        self.assertNotIn('reason', PlayerObservationAnalysis.objects.get().result_json['observations'][0])
        self.assertEqual(PlayerObservationAnalysis.objects.count(), 1)
        self.assertLessEqual(len(queries), 7)
        self.assertFalse(any('gear_json' in q['sql'] for q in queries))
        RaiderIORunRoster.objects.filter(run_id=124).update(roster_visible=False)
        view = project_player_observation(sibling)
        self.assertEqual(view['status'], 'pending')
        self.assertEqual(view['observations'], [])

    def test_forged_ai_rejected_busy_and_failure_preserve_good(self):
        from botend.services.player_observation import generate_observation, project_player_observation
        self.assertEqual(generate_observation(self.player, engine=self.engine())['status'], 'ready')
        old = PlayerObservationAnalysis.objects.get().result_json
        forged = self.engine(lambda r: r['observations'][0].update(evidence_ids=['invented']))
        self.assertEqual(generate_observation(self.player, force=True, engine=forged)['status'], 'invalid_evidence')
        row = PlayerObservationAnalysis.objects.get()
        self.assertEqual(row.result_json, old)
        self.assertEqual(row.last_error, 'invalid_evidence')
        self.assertEqual(project_player_observation(self.player)['status'], 'stale')
        self.assertEqual(generate_observation(self.player, force=True, engine=self.engine())['status'], 'backoff')
        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(lease_token='x'*32, lease_expires_at=timezone.now()+timedelta(minutes=1))
        engine = self.engine()
        self.assertEqual(generate_observation(self.player, force=True, engine=engine)['status'], 'busy')
        engine.send_message.assert_not_called()

    def test_revalidation_discards_changed_evidence_and_expired_lease(self):
        from botend.services.player_observation import generate_observation
        def retract(result):
            RaiderIORunRoster.objects.filter(run_id=124).update(roster_visible=False)
        self.assertEqual(generate_observation(self.player, engine=self.engine(retract))['status'], 'evidence_changed')
        self.assertFalse(PlayerObservationAnalysis.objects.get().result_json)
        PlayerObservationAnalysis.objects.update(next_attempt_at=None)
        def expire(result):
            PlayerObservationAnalysis.objects.update(lease_expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(generate_observation(self.player, engine=self.engine(expire))['status'], 'lease_expired')
        self.assertFalse(PlayerObservationAnalysis.objects.get().result_json)

    def test_conflicting_run_id_excluded_unknown_counts_and_strict_ai(self):
        from botend.services.player_observation import generate_observation
        bad = copy.deepcopy(self.runs[0]); bad['mythic_level'] = 30
        self.activity.payload_json['mythic_plus_recent_runs'] = [bad]
        self.activity.save()
        data = self.evidence()
        self.assertEqual(data['coverage']['unique_runs'], 2)
        self.assertEqual(data['season_counts']['timed'], None)
        engine = self.engine(lambda r: r.update(extra='no'))
        self.assertEqual(generate_observation(self.player, engine=engine)['status'], 'ready')
        engine.send_message.assert_not_called()
        self.assertEqual(PlayerObservationAnalysis.objects.get().result_json['metadata']['source_kind'], 'local_deterministic')
        from botend.services.player_observation import InvalidResult, _validate
        with self.assertRaisesRegex(InvalidResult, 'invalid_schema'):
            _validate(json.dumps({**observation_response(data), 'extra': 'no'}), data)

    def test_rotation_specs_and_strict_output_guards(self):
        from botend.services.player_observation import generate_observation
        row = RaiderIORunRoster.objects.get(run_id=125)
        raw = row.payload_json
        character = raw['roster'][4]['character']
        character.update(id=105, name='Member5', path='/characters/eu/test-realm/Member5')
        raw['roster'][0]['character']['spec'] = {'id': 256, 'name': 'Discipline', 'slug': 'discipline', 'role': 'healer'}
        row.payload_json = raw; row.save()
        labels = {label['code']: label for label in self.evidence()['eligible_labels']}
        self.assertIn('multiple_specs', labels)
        self.assertIn('fixed_team', labels)
        self.assertNotIn('rotating_core', labels)
        text = next(item['text'] for item in self.evidence()['evidence'] if item['id'] == 'fixed_team')
        self.assertIn('偶尔替补', text)
        self.assertIn('结构不证明带人', text)
        for mutation, expected in (
                (lambda r: r['observations'][0].update(confidence='绝对'), 'invalid_confidence'),
                (lambda r: r['observations'][0].update(reason='至少99场'), 'invalid_schema'),
                (lambda r: r['observations'][0].update(reason='并非代练'), 'invalid_schema'),
                (lambda r: r['observations'][0].update(reason_id='observed_specs'), 'invalid_reason'),
                (lambda r: r['observations'][0].update(limitation_ids=['no_main_spec']), 'invalid_limitations'),
                (lambda r: r['observations'][0].update(limitation_ids=[]), 'invalid_limitations'),
                (lambda r: r['observations'][0].update(limitation_ids=['sample_scope', 'sample_scope']), 'invalid_limitations'),
                (lambda r: r.update(conclusion_id='invented'), 'invalid_conclusion'),
                (lambda r: r['observations'][0].update(code='invented'), 'invalid_label'),
                (lambda r: r['observations'][0].update(evidence_ids=[]), 'invalid_evidence')):
            with self.subTest(expected=expected):
                PlayerObservationAnalysis.objects.all().delete()
                self.assertEqual(generate_observation(self.player, engine=self.engine(mutation))['status'], expected)

    def test_untimed_best_not_highest_and_engine_failures_are_sanitized(self):
        from botend.services.player_observation import generate_observation
        untimed = copy.deepcopy(self.runs[0])
        untimed.update(keystone_run_id=126, mythic_level=30, num_keystone_upgrades=0, score=999, url='https://raider.io/mythic-plus-runs/season-mn-2/126')
        self.activity.payload_json['mythic_plus_best_runs'].append(untimed)
        self.activity.save()
        self.assertEqual(self.evidence()['metrics']['highest_timed_level'], 22)
        bad = Mock(send_message=Mock(side_effect=RuntimeError('secret API response')))
        self.assertEqual(generate_observation(self.player, engine=bad)['status'], 'engine_error')
        row = PlayerObservationAnalysis.objects.get()
        self.assertEqual(row.last_error, 'engine_error')
        self.assertEqual(row.result_json, {})
        self.assertEqual(row.lease_token, '')

    def test_comparison_requires_timed_record_and_shows_both_times(self):
        row = RaiderIOCharacterActivity.objects.get(character_name='member4')
        row.scored_runs_json['dungeons'][0]['num_chests'] = 0
        row.save()
        self.assertEqual(self.evidence()['coverage']['comparison_runs'], 0)
        alternate = copy.deepcopy(row.scored_runs_json['dungeons'][0])
        alternate.pop('alternate')
        alternate.update(num_chests=1, keystone_run_id=9999, mythic_level=24)
        row.scored_runs_json['dungeons'][0]['alternate'] = alternate
        row.save()
        data = self.evidence()
        self.assertEqual(data['coverage']['comparison_runs'], 3)
        comparison = next(item for item in data['evidence'] if item['id'].startswith('comparison:'))
        self.assertIn('24 层', comparison['text'])
        self.assertIn('通关时间', comparison['text'])
        self.assertIn('成绩采集时间', comparison['text'])

    def test_identifier_contract_requires_evidence_alternatives_and_related_summary(self):
        from botend.services.player_observation import InvalidResult, _validate
        data = self.evidence()
        label = next(x for x in data['eligible_labels'] if x['code'] == 'recent_inquiry')
        result = {'observations': [{'code': label['code'], 'confidence': '低',
            'reason_id': next(iter(label['reason_options'])),
            'limitation_ids': list(label['required_limitation_ids']),
            'evidence_ids': list(label['evidence_ids'])}], 'conclusion_id': 'public_sample'}
        self.assertEqual(_validate(json.dumps(result), data), result)
        for field, value, error in (
                ('evidence_ids', [], 'invalid_evidence'),
                ('confidence', '中', 'invalid_confidence'),
                ('limitation_ids', ['current_not_history'], 'invalid_limitations'),
                ('reason_id', '已经完成九十九场限时，且整个赛季从未失败。', 'invalid_reason'),
                ('limitation_ids', ['他通过陪玩收费获得收入。'], 'invalid_limitations')):
            with self.subTest(field=field, value=value):
                bad = copy.deepcopy(result)
                bad['observations'][0][field] = value
                with self.assertRaisesRegex(InvalidResult, error):
                    _validate(json.dumps(bad), data)
        for conclusion in ('timed_sample', '这次通关使其赛季总分大幅上涨。'):
            result['conclusion_id'] = conclusion
            with self.assertRaisesRegex(InvalidResult, 'invalid_conclusion'):
                _validate(json.dumps(result), data)

    def test_single_json_fence_is_transport_format_not_free_text(self):
        from botend.services.player_observation import generate_observation, InvalidResult, _validate
        data = self.evidence()
        raw = self.engine().send_message(json.dumps(data), max_tokens=3600, thinking_type='disabled')
        wrapped = '```json\n' + raw + '\n```'
        self.assertEqual(_validate(wrapped, data), _validate(raw, data))
        from botend.services.player_observation import _generation_input, _validate_publication
        generated = json.dumps(observation_response(_generation_input(data)))
        generated = '```json\n' + generated + '\n```'
        _validate_publication(generated, data, publication_version=8)
        self.assertEqual(generate_observation(self.player, engine=Mock(send_message=Mock(return_value=generated)))['status'], 'ready')
        for bad in ('说明\n' + wrapped, wrapped + '\n更多文字', '```python\n' + raw + '\n```', wrapped + wrapped):
            with self.subTest(raw=bad), self.assertRaises(InvalidResult):
                _validate(bad, data)

    def test_free_text_fabrications_rejected(self):
        from botend.services.player_observation import InvalidResult, _validate
        data = self.evidence()
        label = data['eligible_labels'][0]
        for text in ('已经完成九十九场限时，且整个赛季从未失败。',
                     '他通过陪玩收费获得收入。', '这次通关使其赛季总分大幅上涨。'):
            for field in ('reason', 'conclusion'):
                with self.subTest(text=text, field=field):
                    result = {'observations': [{'code': label['code'], 'confidence': '低',
                        'reason': '已公开记录支持该观察。', 'limitations': ['仅代表已知样本。'],
                        'evidence_ids': label['evidence_ids']}], 'conclusion': '仅描述公开活动。'}
                    if field == 'reason':
                        result['observations'][0][field] = text
                    else:
                        result[field] = text
                    with self.assertRaises(InvalidResult):
                        _validate(json.dumps(result, ensure_ascii=False), data)

    def test_neutral_structure_does_not_promote_current_score_proxies(self):
        from botend.services.player_observation import generate_observation, project_player_observation
        self.assertEqual(generate_observation(self.player, engine=self.engine())['status'], 'ready')
        view = project_player_observation(self.player)
        decisions = {item['code']: item for item in view['judgements']}
        self.assertEqual(set(decisions), {'rotation'})
        self.assertEqual(decisions['rotation']['verdict'], '近期固定五人／偶尔替补')
        self.assertNotIn('高分队友协助', view['conclusion'])
        self.assertTrue(view['has_investigation'])
        for item in decisions.values():
            self.assertTrue(item['reason'])
        for i in range(1, 5):
            row = RaiderIOCharacterActivity.objects.get(character_name=f'member{i}')
            row.scored_runs_json['dungeons'][0].update(mythic_level=22, score=400)
            row.save()
        self.assertEqual(generate_observation(self.player, force=True, engine=self.engine())['status'], 'ready')
        view = project_player_observation(self.player)
        self.assertEqual([row['code'] for row in view['display_judgements']], ['fixed_team'])
        self.assertNotIn('未见明显被带迹象', view['conclusion'])
        RaiderIOCharacterActivity.objects.filter(character_name='member4').update(scored_runs_fetched_at=None)
        self.assertEqual(generate_observation(self.player, force=True, engine=self.engine())['status'], 'ready')
        view = project_player_observation(self.player)
        self.assertEqual([row['code'] for row in view['display_judgements']], ['fixed_team'])
        self.assertNotIn('未见明显被带迹象', view['conclusion'])

    def test_fixed_five_and_repeated_four_person_rotation_are_distinguished(self):
        data = self.evidence()
        self.assertEqual(data['metrics']['team_pattern']['kind'], 'fixed_five')
        self.assertIn('近期固定五人', data['judgements'][0]['verdict'])
        # One substitution is not enough to infer a carry team.
        row = RaiderIORunRoster.objects.get(run_id=125)
        raw = row.payload_json
        raw['roster'][4]['character'].update(id=105, name='Member5', path='/characters/eu/test-realm/Member5')
        row.payload_json = raw; row.save()
        self.assertEqual(self.evidence()['metrics']['team_pattern']['kind'], 'fixed_five')
        # Same four players, three different fifth players, repeated beyond one swap.
        runs = []
        for index in range(8):
            run = copy.deepcopy(self.runs[0]); rid = 200 + index
            run.update(keystone_run_id=rid, url=f'https://raider.io/mythic-plus-runs/season-mn-2/{rid}')
            raw = details(rid); raw.update(mythic_level=20, num_chests=1)
            member_index = 4 if index < 5 else (5 if index < 7 else 6)
            for i, member in enumerate(raw['roster']):
                n = member_index if i == 4 else i
                member['character'].update(id=100+n, name=f'Member{n}', path=f'/characters/eu/test-realm/Member{n}')
            RaiderIORunRoster.objects.create(rio_season=self.season.rio_season, run_id=rid, payload_json=raw, fetched_at=timezone.now(), roster_visible=True, status='ready')
            runs.append(run)
        self.activity.payload_json = {'mythic_plus_best_runs': runs, 'mythic_plus_recent_runs': runs}
        self.activity.save()
        data = self.evidence()
        pattern = data['metrics']['team_pattern']
        self.assertEqual(pattern['kind'], 'rotating_core')
        structure = data['metrics']['behavior_review']['structure']
        self.assertEqual([lineup['count'] for lineup in structure['lineups']], [5, 2, 1])
        teams = [set(lineup['members']) for lineup in structure['lineups']]
        core = set.intersection(*teams)
        self.assertEqual(len(core), 4)
        self.assertEqual(len(set.union(*teams) - core), 3)
        self.assertEqual(data['judgements'][0]['verdict'], '近期固定组合与轮换成员')
        self.assertNotIn('疑似四人带人队伍', data['conclusion_options']['public_sample']['text'])
        self.assertFalse(data['metrics']['behavior_review']['supported'])
        label = next(x for x in data['eligible_labels'] if x['code'] == 'rotating_core')
        self.assertEqual(label['confidence_max'], '中')

    def test_previous_prompt_is_retained_but_not_published_as_v6(self):
        from botend.services.player_observation import generate_observation, project_player_observation
        self.assertEqual(generate_observation(self.player, engine=self.engine())['status'], 'ready')
        row = PlayerObservationAnalysis.objects.get()
        row.prompt_version = 5
        row.evidence_json['prompt_version'] = 5
        from botend.services.player_observation import _hash
        row.evidence_hash = _hash({k: v for k, v in row.evidence_json.items()
            if k not in ('evidence_hash', 'source_failed')})
        row.evidence_json['evidence_hash'] = row.evidence_hash
        row.save()
        before = PlayerObservationAnalysis.objects.get().generated_at
        frozen = PlayerObservationAnalysis.objects.get().result_json
        self.assertEqual(project_player_observation(self.player)['status'], 'pending')
        self.assertEqual(project_player_observation(self.player)['observations'], [])
        self.assertEqual(PlayerObservationAnalysis.objects.get().result_json, frozen)
        self.assertEqual(PlayerObservationAnalysis.objects.get().generated_at, before)
        RaiderIORunRoster.objects.filter(run_id=124).update(roster_visible=False)
        self.assertEqual(project_player_observation(self.player)['status'], 'pending')

    def test_current_eligibility_rechecked_not_stale_player_rank(self):
        from botend.services.player_observation import build_evidence
        PlayerSpecTopPlayer.objects.filter(pk=self.player.pk).update(rank=21)
        self.assertIsNone(build_evidence(self.player))
