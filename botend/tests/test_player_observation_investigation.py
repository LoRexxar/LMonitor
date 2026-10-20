"""Recent clues initiate inquiry before complete five-member scores exist."""
import copy
import json
from datetime import timedelta
from unittest.mock import Mock

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from botend.models import PlayerObservationAnalysis, RaiderIOCharacterActivity, RaiderIORunRoster
from botend.services import player_observation as observation
from botend.tests import test_player_observation_progression as fixtures


class InvestigationTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.subject = '0' * 64
        self.core = [str(i) * 64 for i in range(4)]
        self.runs, self.rosters, self.scores, self.times = {}, {}, {}, {}
        for i in range(3):
            rid = 200 + i
            self.runs[rid] = {'keystone_run_id': rid, 'url': f'https://raider.io/mythic-plus-runs/season-mn-2/{rid}',
                'dungeon': "Kings' Rest", 'short_name': 'KR', 'mythic_level': 20, 'score': 450,
                'num_keystone_upgrades': 0 if i == 0 else 1,
                'completed_at': (self.now - timedelta(hours=i+1)).isoformat()}
            dungeon = {'id': 1, 'slug': 'kings-rest', 'name': "Kings' Rest", 'short_name': 'KR'}
            team = self.core + [str(i+4) * 64]
            self.rosters[rid] = ({'dungeon': dungeon, 'mythic_level': 20}, [(key, {}) for key in team])
        self.set_score(self.subject, 26)

    def set_score(self, key, level):
        self.scores[key] = [{'dungeon': self.rosters[200][0]['dungeon'], 'mythic_level': level,
            'num_chests': 1, 'score': 400 + level*5, 'keystone_run_id': 900 + int(key[0]), 'alternate': None}]
        self.times[key] = self.now

    def build(self, **overrides):
        from botend.services.player_observation_investigation import build_investigation
        args = dict(subject=self.subject, best=[], recent=list(self.runs.values()), runs=self.runs,
            rosters=self.rosters, scores=self.scores, score_times=self.times, fetched_at=self.now,
            rio_season='season-mn-2', now=self.now)
        return build_investigation(**(args | overrides))

    def test_missing_fifth_triggers_with_untimed_and_targeted_questions(self):
        result = self.build()
        self.assertTrue(result['triggered'])
        self.assertEqual(result['seed_run_ids'], [200, 201, 202])
        self.assertEqual(result['core_members'], self.core)
        self.assertEqual([q['identity_digest'] for q in result['questions'][:3]], [str(i)*64 for i in (4,5,6)])
        assistance = next(h for h in result['hypotheses'] if h['id'] == 'assistance')
        self.assertEqual(assistance['status'], 'unknown')
        self.assertTrue(assistance['unknown_ids'])
        self.assertFalse(assistance['counter_ids'])
        ids = {e['id'] for e in result['evidence']}
        self.assertTrue(all(set(h['support_ids'] + h['counter_ids'] + h['unknown_ids']) <= ids for h in result['hypotheses']))
        self.assertTrue(all(e['run_url'] in {r['url'] for r in self.runs.values()} for e in result['evidence']))

    def test_fixed_five_uses_maximal_core_not_fake_rotation(self):
        team = self.core + ['4'*64]
        rosters = {rid: (clean, [(key, {}) for key in team]) for rid, (clean, _) in self.rosters.items()}
        result = self.build(rosters=rosters)
        self.assertTrue(result['triggered'])
        self.assertEqual(result['core_members'], team)
        self.assertIn('固定', result['scope'])

    def test_comparison_groups_do_not_depend_on_subject_identity(self):
        team = self.core + ['4'*64]
        rosters = {rid: (clean, [(key, {}) for key in team]) for rid, (clean, _) in self.rosters.items()}
        for key in team:
            self.set_score(key, 30 if key == self.subject else 26)
        baseline = self.build(rosters=rosters)
        groups = baseline['comparison_groups']
        self.assertTrue(any(self.subject not in g['core_members'] for g in groups))
        self.assertEqual(len(groups), 5)
        for subject in team:
            with self.subTest(subject=subject):
                result = self.build(rosters=rosters, subject=subject)
                self.assertEqual(result['comparison_groups'], groups)
                self.assertEqual(result['hypotheses'], baseline['hypotheses'])
                self.assertEqual(result['core_members'], team)
                self.assertNotIn('外围席位有变化', result['scope'])

    def test_incomplete_comparisons_never_become_directional_counterevidence(self):
        team = self.core + ['4'*64]
        rosters = {rid: (clean, [(key, {}) for key in team]) for rid, (clean, _) in self.rosters.items()}
        for key in team:
            self.set_score(key, 30 if key == self.subject else 26)
        for score_times in ({self.subject: self.now}, {k: v for k, v in self.times.items() if k != '4'*64}):
            result = self.build(rosters=rosters, score_times=score_times)
            for h in result['hypotheses']:
                if h['id'] in ('assistance', 'external_assistance'):
                    self.assertEqual(h['status'], 'unknown')
                    self.assertFalse(h['counter_ids'])
                    self.assertFalse(h['support_ids'])
                    self.assertTrue(h['unknown_ids'])
        self.assertTrue(result['questions'])
        # Rotating seats do not make missing core references negative facts.
        self.set_score('5'*64, 30)
        self.set_score('6'*64, 30)
        result = self.build(score_times={k: v for k, v in self.times.items() if k != self.core[1]})
        assistance = next(h for h in result['hypotheses'] if h['id'] == 'assistance')
        self.assertFalse(assistance['counter_ids'])

    def test_high_near_low_mixed_and_unknown_are_distinct(self):
        for key in self.core[1:]:
            self.set_score(key, 26)
        for level, direction in ((30, 'high'), (26, 'near'), (22, 'low')):
            for i in (4,5,6):
                self.set_score(str(i)*64, level)
            result = self.build()
            self.assertTrue(result['triggered'])
            h = next(h for h in result['hypotheses'] if h['id'] == 'assistance')
            # Complete scores remove score gaps, not ambiguity about which
            # members form the social core or why they collaborated.
            self.assertFalse(any(':unknown:' in eid or ':core_unknown:' in eid for eid in h['unknown_ids']))
            self.assertTrue(any(':group_unknown:' in eid for eid in h['unknown_ids']))
            self.assertTrue(any(f':{direction}:' in e['id'] for e in result['evidence']))
            self.assertEqual(bool(h['counter_ids']), direction != 'low')
            access = next(h for h in result['hypotheses'] if h['id'] == 'key_access')
            self.assertEqual(access['status'], 'unknown')
        self.set_score(self.core[1], 30)
        self.set_score('4'*64, 28)
        self.assertTrue(any(':mixed:' in e['id'] for e in self.build()['evidence']))

    def test_reverse_direction_is_positive_alternative_not_only_counterevidence(self):
        for key in self.core[1:]:
            self.set_score(key, 26)
        for i in (4, 5, 6):
            self.set_score(str(i)*64, 30)
        plan = self.build()
        by_id = {h['id']: h for h in plan['hypotheses']}
        reverse = by_id['external_assistance']
        self.assertTrue(reverse['support_ids'])
        self.assertTrue(all(':high:' in eid for eid in reverse['support_ids']))
        self.assertEqual(set(reverse['support_ids']), set(by_id['assistance']['counter_ids']))
        self.assertTrue(reverse['unknown_ids'])
        self.assertEqual(by_id['key_access']['status'], 'unknown')
        self.assertFalse(by_id['key_access']['support_ids'])

    def test_stronger_stable_subject_and_real_low_periphery_both_survive(self):
        self.set_score(self.subject, 30)
        for key in self.core[1:]:
            self.set_score(key, 26)
        for i in (4, 5, 6):
            self.set_score(str(i)*64, 22)
        result = self.build()
        self.assertTrue(any(self.subject not in g['core_members'] for g in result['comparison_groups']))
        h = next(h for h in result['hypotheses'] if h['id'] == 'external_assistance')
        self.assertEqual(h['status'], 'possible')
        self.assertTrue(h['support_ids'])
        self.assertTrue(all(':high:' in eid for eid in h['support_ids']))
        self.assertEqual(set(h['counter_ids']), {f'investigation:low:{rid}' for rid in self.runs})
        self.assertTrue(h['unknown_ids'])

    def test_no_signal_best_not_frequency_and_stale_never_trigger(self):
        self.assertFalse(self.build(recent=list(self.runs.values())[:2])['triggered'])
        self.assertFalse(self.build(fetched_at=self.now-timedelta(days=3))['triggered'])
        self.assertFalse(self.build(score_times={self.subject: self.now-timedelta(days=3)})['triggered'])
        self.set_score(self.subject, 20)
        self.assertFalse(self.build()['triggered'])
        self.assertFalse(self.build(subject='invalid')['triggered'])

    def test_profile_best_is_explicit_proxy_not_frequency(self):
        best = dict(self.runs[200], keystone_run_id=999, mythic_level=26,
            url='https://raider.io/mythic-plus-runs/season-mn-2/999', num_keystone_upgrades=1)
        result = self.build(best=[best], runs=self.runs | {999: best}, scores={}, score_times={})
        self.assertTrue(result['triggered'])
        self.assertEqual(result['seed_run_ids'], [200,201,202])
        self.assertTrue(any('profile best' in e['text'] for e in result['evidence']))
        # A fresh actual current score is authoritative over the profile proxy.
        self.set_score(self.subject, 20)
        self.assertFalse(self.build(best=[best], runs=self.runs | {999: best})['triggered'])


class InvestigationIntegrationTests(TestCase):
    setUp = fixtures.ObservationProgressionTests.setUp

    @staticmethod
    def legacy_response(data):
        # Frozen pre-inquiry response shape, independent of the v6 engine fixture.
        label = next(h for h in data['eligible_labels'] if h['code'] == 'timed_experience')
        return json.dumps({'observations': [{
            'code': label['code'], 'confidence': '低',
            'reason_id': next(iter(label['reason_options'])),
            'limitation_ids': label['required_limitation_ids'],
            'evidence_ids': label['evidence_ids'],
        }], 'conclusion_id': 'public_sample'})

    def engine(self, mutate=None):
        def respond(prompt, **kwargs):
            data = json.loads(prompt)
            inquiry = data['metrics']['investigation']
            result = {'observations': [], 'conclusion_id': 'public_sample', 'investigation': {
                'summary': '现有线索允许比较协作与日常活动解释，动机仍待核验。', 'confidence': '低',
                'hypotheses': [{'id': h['id'], 'assessment': h['status'], 'evidence_ids': (h['support_ids'] or h['unknown_ids'])[:3],
                    'counter_evidence_ids': h['counter_ids'], 'explanation': '这是基于已列证据的候选解释，不是事实确认。'}
                    for h in inquiry['hypotheses'][:4]],
                'next_question_ids': [q['id'] for q in inquiry['questions'][:2]]}}
            if 'response_contract' in inquiry:
                from botend.tests.test_player_observation import structured_response
                result['investigation'] = structured_response(inquiry['response_contract'])
            if mutate:
                mutate(result)
            return json.dumps(result, ensure_ascii=False)
        return Mock(send_message=Mock(side_effect=respond))

    def test_fixed_five_high_subject_survives_generation_and_portal_projection(self):
        # Real central ORM facts: three identical teams, not synthetic inquiry
        # JSON. Member0 is the subject, but may be the stronger collaborator.
        self.activity.payload_json['mythic_plus_recent_runs'] = self.runs[:3]
        self.activity.save(update_fields=['payload_json'])
        for person in RaiderIOCharacterActivity.objects.all():
            person.scored_runs_json['dungeons'][0].update(
                mythic_level=30 if person.character_name == 'member0' else 26)
            person.save()
        data = observation.build_evidence(self.player)
        plan = data['metrics']['investigation']
        self.assertTrue(plan['triggered'])
        self.assertEqual(plan['seed_run_ids'], [200, 201, 202])
        self.assertEqual(len(plan['core_members']), 5)
        external = next(h for h in plan['hypotheses'] if h['id'] == 'external_assistance')
        self.assertEqual(external['status'], 'unknown')
        self.assertFalse(external['support_ids'])
        self.assertTrue(any(':high:' in eid for eid in external['unknown_ids']))
        self.assertFalse(external['counter_ids'])
        self.assertNotIn('外围席位有变化', plan['scope'])
        self.assertIn('不能确定', plan['scope'])
        self.assertEqual(len(plan['comparison_groups']), 5)
        self.assertTrue(all(len(g['core_members']) == 4 for g in plan['comparison_groups']))

        outcome = observation.generate_observation(self.player, engine=self.engine(), skip_uninformative=True)
        self.assertEqual(outcome['status'], 'ready')
        self.assertTrue(outcome['ai_called'])
        frozen = PlayerObservationAnalysis.objects.get().evidence_json
        view = observation.project_player_observation(self.player)
        self.assertTrue(view['has_investigation'])
        projected = next(h for h in view['investigation']['hypotheses'] if h['id'] == 'external_assistance')
        self.assertEqual(projected['assessment'], 'unknown')
        self.assertFalse(projected['counter_evidence'])
        refs = {e['id']: e for e in frozen['evidence']}
        self.assertEqual(projected['evidence'], [refs[eid] for eid in external['support_ids'][:3]])
        self.assertTrue(projected['unknown_evidence'])

    def test_missing_then_reverse_explained_not_local_empty_and_manual_preserved(self):
        RaiderIOCharacterActivity.objects.filter(character_name__in=['member4','member5','member6']).update(scored_runs_fetched_at=None)
        data = observation.build_evidence(self.player)
        self.assertTrue(data['metrics']['investigation']['triggered'])
        engine = self.engine()
        result = observation.generate_observation(self.player, engine=engine, skip_uninformative=True)
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(result['ai_called'])
        row = PlayerObservationAnalysis.objects.get()
        manual = {'conclusion': '人工内容', 'tags': [], 'evidence': []}
        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(manual_json=manual, review_status='approved')
        for person in RaiderIOCharacterActivity.objects.filter(character_name__in=['member4','member5','member6']):
            person.scored_runs_json['dungeons'][0].update(mythic_level=30, score=600)
            person.scored_runs_fetched_at = self.now
            person.save()
        result = observation.generate_observation(self.player, engine=engine, skip_uninformative=True)
        self.assertTrue(result['ai_called'])
        self.assertEqual(result['status'], 'ready')
        row.refresh_from_db()
        self.assertEqual(row.manual_json, manual)
        self.assertEqual(observation.project_player_observation(self.player)['conclusion'], '人工内容')

    def test_strict_inquiry_refs_confidence_and_legacy_result(self):
        data = observation.build_evidence(self.player)
        legacy = self.legacy_response(data)
        self.assertNotIn('investigation', observation._validate(legacy, data))
        for mutate in (
            lambda r: r['investigation']['hypotheses'][0].update(evidence_ids=['outside']),
            lambda r: r['investigation'].update(next_question_ids=['outside']),
            lambda r: r['investigation'].update(confidence='高'),
            lambda r: r['investigation'].update(summary='此人已经付款购买钥匙。'),
            lambda r: r['investigation']['hypotheses'][0].update(assessment='confirmed'),
        ):
            with self.subTest(mutation=mutate):
                raw = self.engine(mutate).send_message(json.dumps(data))
                with self.assertRaises(observation.InvalidResult):
                    observation._validate(raw, data)

    def test_semantic_reuse_ignores_only_collection_clocks(self):
        base = {'relative_comparisons': [{'snapshot_as_of': 'old', 'fetched_at': 'old',
            'completed_at': 'original', 'level': 20, 'score': 400}],
            'scope': [1, 2], 'roster_visible': True, 'source_failed': False, 'error': ''}
        refreshed = copy.deepcopy(base)
        refreshed['relative_comparisons'][0].update(snapshot_as_of='new', fetched_at='new')
        self.assertEqual(observation._semantic_evidence(base), observation._semantic_evidence(refreshed))
        for field, value in [('completed_at', 'changed'), ('level', 21), ('score', 401)]:
            changed = copy.deepcopy(refreshed)
            changed['relative_comparisons'][0][field] = value
            with self.subTest(field=field):
                self.assertNotEqual(observation._semantic_evidence(base), observation._semantic_evidence(changed))
        for field, value in [('scope', [1]), ('roster_visible', False), ('source_failed', True), ('error', 'private')]:
            with self.subTest(field=field):
                self.assertNotEqual(observation._semantic_evidence(base),
                    observation._semantic_evidence({**refreshed, field: value}))

    def test_timestamp_only_refresh_reuses_ai_but_privacy_and_expiry_retract(self):
        engine = self.engine()
        self.assertEqual(observation.generate_observation(self.player, engine=engine, skip_uninformative=True)['status'], 'ready')
        RaiderIORunRoster.objects.all().update(fetched_at=timezone.now())
        RaiderIOCharacterActivity.objects.all().update(scored_runs_fetched_at=timezone.now(), fetched_at=timezone.now())
        self.assertEqual(observation.generate_observation(self.player, engine=engine, skip_uninformative=True)['status'], 'fresh')
        self.assertEqual(engine.send_message.call_count, 1)
        row = PlayerObservationAnalysis.objects.get()
        frozen = copy.deepcopy(row.evidence_json)
        self.assertEqual(observation.generate_observation(self.player, engine=engine, skip_uninformative=True)['status'], 'fresh')
        row.refresh_from_db()
        self.assertEqual(row.evidence_json, frozen)
        self.assertEqual(observation.generate_observation(self.player, force=True, engine=engine, skip_uninformative=True)['status'], 'ready')
        self.assertEqual(engine.send_message.call_count, 2)
        RaiderIORunRoster.objects.filter(run_id=200).update(roster_visible=False)
        self.assertEqual(observation.project_player_observation(self.player)['status'], 'pending')

    def test_projection_has_pending_clues_and_frozen_refs_without_final_label(self):
        RaiderIOCharacterActivity.objects.filter(character_name__in=['member4','member5','member6']).update(scored_runs_fetched_at=None)
        self.assertEqual(observation.generate_observation(self.player, engine=self.engine(), skip_uninformative=True)['status'], 'ready')
        row = PlayerObservationAnalysis.objects.get()
        frozen = copy.deepcopy(row.evidence_json)
        view = observation.project_player_observation(self.player)
        self.assertTrue(view['has_supported_judgements'])
        self.assertTrue(view['has_investigation'])
        self.assertEqual(view['investigation']['status'], 'clues_pending')
        self.assertEqual(view['observations'], [])
        refs = {e['id']: e for e in frozen['evidence']}
        for hypothesis in view['investigation']['hypotheses']:
            for key in ('evidence', 'counter_evidence', 'unknown_evidence'):
                self.assertTrue(all(item == refs[item['id']] for item in hypothesis[key]))
        self.assertEqual(view['investigation']['structured_investigation'], row.result_json['investigation'])
        self.assertTrue(view['investigation']['summary'])
        self.assertNotIn('summary', row.result_json['investigation'])
        PlayerObservationAnalysis.objects.update(is_published=False)
        self.assertIsNone(observation.project_player_observation(self.player))

    def test_legacy_pre_inquiry_publication_kept_but_new_analysis_is_increment(self):
        data = observation.build_evidence(self.player)
        previous = copy.deepcopy(data)
        previous['metrics'].pop('investigation')
        previous['evidence'] = [e for e in previous['evidence'] if not e['id'].startswith('investigation:')]
        previous['eligible_labels'] = [e for e in previous['eligible_labels'] if e['code'] != 'recent_inquiry']
        previous['evidence_hash'] = observation._hash({k: v for k, v in previous.items() if k not in ('evidence_hash', 'source_failed')})
        raw = self.legacy_response(previous)
        PlayerObservationAnalysis.objects.create(activity=self.activity, player=self.player, prompt_version=5,
            evidence_json=previous, evidence_hash=previous['evidence_hash'], result_json=json.loads(raw), status='ready')
        self.assertEqual(observation.project_player_observation(self.player)['status'], 'pending')
        stored = PlayerObservationAnalysis.objects.get()
        self.assertEqual(stored.prompt_version, 5)
        self.assertEqual(stored.evidence_json, previous)
        self.assertEqual(stored.result_json, json.loads(raw))
        result = observation.generate_observation(self.player, engine=self.engine(), skip_uninformative=True)
        self.assertTrue(result['ai_called'])
        self.assertEqual(result['status'], 'ready')

    def test_autonomous_comparison_uses_subset_and_unknown_refs_without_copying_status(self):
        data = observation.build_evidence(self.player)
        plan = {h['id']: h for h in data['metrics']['investigation']['hypotheses']}
        raw = json.loads(self.engine().send_message(json.dumps(data)))
        raw['investigation']['summary'] = '日常协作仍相容；可能借钥匙，但不能确认交易或钥匙归属。'
        rows = raw['investigation']['hypotheses'][:2]
        raw['investigation']['hypotheses'] = rows
        rows[0].update(assessment='possible', evidence_ids=plan[rows[0]['id']]['support_ids'][:1])
        rows[1].update(assessment='possible', evidence_ids=plan[rows[1]['id']]['support_ids'][:1]
            + plan[rows[1]['id']]['unknown_ids'][:1])
        validated = observation._validate(json.dumps(raw), data)
        self.assertEqual([h['assessment'] for h in validated['investigation']['hypotheses']], ['possible', 'possible'])
        self.assertEqual(observation._project_investigation(validated['investigation'], data)['hypotheses'][0]['assessment'], 'possible')
        projection = observation._project_investigation(validated['investigation'], data)
        self.assertEqual([e['id'] for e in projection['hypotheses'][1]['evidence']], plan[rows[1]['id']]['support_ids'][:1])
        self.assertTrue(projection['hypotheses'][1]['unknown_evidence'])

    def test_negated_absence_of_transaction_facts_is_not_a_positive_claim(self):
        data = observation.build_evidence(self.player)
        raw = json.loads(self.engine().send_message(json.dumps(data)))
        for text in ('公开记录也无钥匙归属或交易事实，无法确认该方向。',
                     '公开资料并无付款证据，只能保留为候选解释。',
                     '现有资料缺乏交易事实，不能确认组队目的。',
                     '协助假设与低分交易均无法确认。',
                     '公开资料无交易事实，只能继续核查。',
                     '公开记录完全没有钥匙归属、组队目的或交易事实，无法确认归属或转让。'):
            raw['investigation']['summary'] = text
            with self.subTest(text=text):
                observation._validate(json.dumps(raw), data, require_investigation=True)
        for text in ('公开记录也无钥匙归属，但此人已经付款购买钥匙。',
                     '他无数次交易钥匙。',
                     '没有时间所以他付款购买钥匙的事实已确认。',
                     '没有公开记录，但他付款购买了钥匙。'):
            raw['investigation']['summary'] = text
            with self.subTest(text=text), self.assertRaises(observation.InvalidResult):
                observation._validate(json.dumps(raw), data, require_investigation=True)

    def test_clause_scoped_unknowns_and_new_assertion_gate(self):
        data = observation.build_evidence(self.player)
        raw = json.loads(self.engine().send_message(json.dumps(data)))
        legal = (
            '公开通关与当前计分不含钥匙归属、背包库存或交易事实。',
            '钥匙归属、收费与完整坚韧历史未知。',
            '不据此定性交易或带人。',
            '钥匙归属、动机、收费及外围成员历史均未知。',
            '不能确认本人无收益。',
            '不能据此说明这些场次并非为本人当场上分。',
            '不能确认此前场次的差距已由跑前更高成绩覆盖。',
        )
        for text in legal:
            with self.subTest(legal=text):
                raw['investigation']['summary'] = text
                observation._validate(json.dumps(raw), data, require_investigation=True)
        for text in (
            '钥匙归属、收费与完整坚韧历史未知；他已经交易钥匙。',
            '不据此定性交易或带人但他收费。',
            '不含钥匙归属所以他交易钥匙。',
            '收费未知因此他交易钥匙。',
            '没有证据却能确定他收费。',
            '本人无收益。',
            '说明这些场次并非为本人当场上分。',
            '此前两场的差距确实已由跑前更高成绩覆盖。',
            '不能确认动机，但本人没有净增分。',
            '无法确认此前场次的差距，因此此前场次已由跑前更高成绩覆盖。',
        ):
            with self.subTest(illegal=text):
                raw['investigation']['summary'] = text
                with self.assertRaises(observation.InvalidResult):
                    observation._validate(json.dumps(raw), data, require_investigation=True)
        # The added assertion gate is generation-only, not frozen read policy.
        raw['investigation']['summary'] = '本人无收益。'
        observation._validate(json.dumps(raw), data)

    def test_reverse_generation_must_compare_alternative_and_bounded_output(self):
        for person in RaiderIOCharacterActivity.objects.filter(character_name__in=['member4', 'member5', 'member6']):
            person.scored_runs_json['dungeons'][0].update(mythic_level=30, score=600)
            person.save()
        data = observation.build_evidence(self.player)
        raw = json.loads(self.engine().send_message(json.dumps(data)))
        validated = observation._validate(json.dumps(raw), data, require_investigation=True)
        self.assertTrue(next(h for h in validated['investigation']['hypotheses'] if h['id'] == 'external_assistance')['evidence_ids'])
        for mutate in (
            lambda r: r['investigation'].update(hypotheses=r['investigation']['hypotheses'][:1]),
            lambda r: r['investigation'].update(summary='相容' * 111),
            lambda r: r['investigation']['hypotheses'][0].update(explanation='相容' * 111),
        ):
            changed = copy.deepcopy(raw)
            mutate(changed)
            with self.subTest(mutate=mutate), self.assertRaises(observation.InvalidResult):
                observation._validate(json.dumps(changed), data, require_investigation=True)

    def test_legacy_assessments_normalized_without_rewriting_frozen_payload(self):
        data = observation.build_evidence(self.player)
        raw = json.loads(self.engine().send_message(json.dumps(data)))
        raw['investigation']['hypotheses'][0]['assessment'] = 'compatible'
        raw['investigation']['hypotheses'][1]['assessment'] = 'mixed'
        before = copy.deepcopy(raw)
        validated = observation._validate(json.dumps(raw), data)
        self.assertEqual([h['assessment'] for h in validated['investigation']['hypotheses'][:2]], ['possible', 'possible'])
        self.assertEqual(raw, before)
        # Freeze the actual five-entry v5 contract, not the expanded v6 menu.
        data = copy.deepcopy(data)
        data['prompt_version'] = 5
        data['metrics']['investigation']['hypotheses'] = [
            {'id': hid, 'status': status, 'support_ids': refs,
             'counter_ids': [], 'unknown_ids': ['investigation:motive_unknown:200']}
            for hid, status, refs in (
                ('routine_group', 'compatible', ['investigation:team:200']),
                ('assistance', 'mixed', ['investigation:low:200']),
                ('external_assistance', 'unknown', []),
                ('later_progress', 'compatible', ['investigation:own:200']),
                ('key_access', 'unknown', []),
            )]
        raw['investigation']['hypotheses'] = [
            {'id': h['id'], 'assessment': h['status'], 'evidence_ids': h['support_ids'],
             'counter_evidence_ids': h['counter_ids'], 'explanation': '候选解释仍有待核查。'}
            for h in data['metrics']['investigation']['hypotheses']]
        self.assertEqual(len(observation._validate(json.dumps(raw), data)['investigation']['hypotheses']), 5)
        with self.assertRaises(observation.InvalidResult):
            observation._validate(json.dumps(raw), data, require_investigation=True)

    def test_comparison_cannot_promote_unknown_borrow_refs_or_hide_counterevidence(self):
        data = observation.build_evidence(self.player)
        raw = json.loads(self.engine().send_message(json.dumps(data)))
        hypotheses = {h['id']: h for h in data['metrics']['investigation']['hypotheses']}
        for mutation in (
            lambda r: r['investigation'].update(hypotheses=r['investigation']['hypotheses'][:1]),
            lambda r: r['investigation']['hypotheses'][0].update(assessment='supported', evidence_ids=[]),
            lambda r: r['investigation']['hypotheses'][0].update(evidence_ids=hypotheses['assistance']['unknown_ids'][:1]),
            lambda r: next(h for h in r['investigation']['hypotheses'] if h['id'] == 'key_access').update(assessment='supported'),
            lambda r: r['investigation'].update(summary='此人已经付款购买钥匙。'),
            lambda r: r['investigation'].update(summary='钥匙属于本人。'),
            lambda r: r['investigation'].update(summary='<b>候选解释</b>'),
            lambda r: r['investigation'].update(summary='参见www.example.com'),
            lambda r: r['investigation'].update(summary='未知\u007f'),
            lambda r: r['investigation'].update(summary='已完成３场'),
        ):
            changed = copy.deepcopy(raw)
            mutation(changed)
            with self.subTest(mutation=mutation), self.assertRaises(observation.InvalidResult):
                observation._validate(json.dumps(changed), data)
        # Missing protection/beneficiary proof is unknown, not a reverse fact.
        assistance = hypotheses['assistance']
        self.assertFalse(assistance['counter_ids'])
        self.assertTrue(any(eid.startswith('behavior:key_recovery_unknown:')
                            for eid in assistance['unknown_ids']))

    def test_missing_protection_cannot_weaken_assistance_in_strict_validator(self):
        data = observation.build_evidence(self.player)
        raw = json.loads(self.engine().send_message(json.dumps(data)))
        observation._validate(json.dumps(raw), data, require_investigation=True)
        assistance = next(h for h in raw['investigation']['hypotheses'] if h['id'] == 'assistance')
        assistance['assessment'] = 'weakened'
        # Use the actual central evidence classification: no forged plan.
        with self.assertRaises(observation.InvalidResult):
            observation._validate(json.dumps(raw), data, require_investigation=True)
        plan = next(h for h in data['metrics']['investigation']['hypotheses'] if h['id'] == 'assistance')
        gaps = [eid for eid in plan['unknown_ids'] if eid.startswith('behavior:key_recovery_unknown:')]
        self.assertTrue(gaps)
        assistance.update(assessment='possible', evidence_ids=gaps[:1])
        observation._validate(json.dumps(raw), data, require_investigation=True)
        assistance['counter_evidence_ids'] = gaps
        with self.assertRaises(observation.InvalidResult):
            observation._validate(json.dumps(raw), data, require_investigation=True)

    def test_trigger_requires_new_explanation_and_failed_model_keeps_last_good(self):
        engine = self.engine()
        self.assertEqual(observation.generate_observation(self.player, engine=engine)['status'], 'ready')
        row = PlayerObservationAnalysis.objects.get()
        good = copy.deepcopy(row.result_json)
        legacy = self.legacy_response(observation.build_evidence(self.player))
        old_schema = Mock(send_message=Mock(return_value=legacy))
        outcome = observation.generate_observation(self.player, force=True, engine=old_schema)
        self.assertEqual(outcome['status'], 'missing_investigation')
        self.assertTrue(outcome['ai_called'])
        row.refresh_from_db()
        self.assertEqual(row.result_json, good)
        self.assertEqual(engine.send_message.call_args.kwargs['max_tokens'], 3600)

    def test_old_schema_cached_with_trigger_is_not_reused_as_new_explanation(self):
        self.assertEqual(observation.generate_observation(self.player, engine=self.engine())['status'], 'ready')
        data = observation.build_evidence(self.player)
        legacy = self.legacy_response(data)
        PlayerObservationAnalysis.objects.update(result_json=json.loads(legacy))
        engine = self.engine()
        self.assertEqual(observation.generate_observation(self.player, engine=engine)['status'], 'ready')
        engine.send_message.assert_called_once()

    def test_semantic_reuse_retains_source_identity_completion_and_score_facts(self):
        before = observation.build_evidence(self.player)
        for key, value in (('source_updated_at', self.now.isoformat()), ('digest', 'changed')):
            changed = copy.deepcopy(before)
            changed['source_stamps'][key] = value
            self.assertNotEqual(observation._semantic_evidence(before), observation._semantic_evidence(changed))
        engine = self.engine()
        self.assertEqual(observation.generate_observation(self.player, engine=engine)['status'], 'ready')
        person = RaiderIOCharacterActivity.objects.get(character_name='member4')
        person.scored_runs_json['dungeons'][0]['completed_at'] = (self.now-timedelta(hours=2)).isoformat()
        person.save()
        self.assertEqual(observation.generate_observation(self.player, engine=engine)['status'], 'ready')
        self.assertEqual(engine.send_message.call_count, 2)

    def test_expired_sources_and_timestamp_race_still_fence_publication(self):
        engine = self.engine()
        def advance_clock(result):
            RaiderIORunRoster.objects.filter(run_id=200).update(fetched_at=timezone.now())
        result = observation.generate_observation(self.player, engine=self.engine(advance_clock), skip_uninformative=True)
        self.assertEqual(result['status'], 'evidence_changed')
        self.assertFalse(PlayerObservationAnalysis.objects.get().result_json)
        PlayerObservationAnalysis.objects.update(next_attempt_at=None)
        self.assertEqual(observation.generate_observation(self.player, engine=engine, skip_uninformative=True)['status'], 'ready')
        RaiderIOCharacterActivity.objects.all().update(scored_runs_fetched_at=self.now-timedelta(days=3), fetched_at=self.now-timedelta(days=3))
        self.activity.refresh_from_db()
        self.activity.module_fetched_at = {'mythic_plus_recent_runs': (self.now-timedelta(days=3)).isoformat()}
        self.activity.save(update_fields=['module_fetched_at'])
        self.assertEqual(observation.project_player_observation(self.player)['status'], 'pending')
