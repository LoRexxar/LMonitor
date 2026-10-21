"""Real ORM integration; the text engine exercises schema, not model quality."""
import json
from unittest.mock import Mock, patch
from django.test import TestCase
from botend.tests.test_player_observation import PlayerObservationTests as Fixtures
from botend.models import PlayerObservationAnalysis, RaiderIOCharacterActivity, RaiderIORunRoster
from django.utils import timezone
from datetime import timedelta
import copy
from botend.services import player_observation as service


class BehaviorIntegrationTests(TestCase):
    setUp = Fixtures.setUp

    def prepare_behavior(self):
        now = timezone.now()
        self.season.mplus_encounters = [{'id': 900+i} for i in range(8)]
        self.season.save()
        self.activity.refresh_from_db()
        for index, run in enumerate(self.runs):
            run.update(mythic_level=22, score=515,
                completed_at=(now-timedelta(hours=index+1)).isoformat())
            roster = RaiderIORunRoster.objects.get(run_id=run['keystone_run_id'])
            roster.payload_json.update(mythic_level=22, completed_at=run['completed_at'])
            roster.save()
        self.activity.payload_json.update(mythic_plus_best_runs=[], mythic_plus_recent_runs=self.runs,
            mythic_plus_dungeon_run_counts=[{'zone_id': i, 'dungeon': f'Dungeon {i}',
                'season_runs_total': 3, 'season_runs_timed': 3} for i in range(1, 9)])
        self.activity.save()
        for row in RaiderIOCharacterActivity.objects.all():
            record = row.scored_runs_json['dungeons'][0]
            record.update(mythic_level=22, score=520,
                completed_at=(now-timedelta(days=2)).isoformat())
            if row.pk == self.activity.pk:
                record.update(mythic_level=23, score=530)
                # The missing eighth scored dungeon must come from history.
                records = []
                for did in range(1, 8):
                    item = copy.deepcopy(record)
                    item['dungeon']['id'] = did
                    if did != 1:
                        item['dungeon'].update(name=f'Dungeon {did}', slug=f'dungeon-{did}')
                    item['keystone_run_id'] = 2000+did
                    records.append(item)
                row.scored_runs_json['dungeons'] = records
                from botend.services.player_observation_history import validate_history, scope
                clean = validate_history({'ui': scope(self.season.rio_season, 100, 8),
                    'runs': [{'score': 530, 'summary': {'season': self.season.rio_season,
                        'dungeon': {'id': 8}, 'keystone_run_id': 2008, 'mythic_level': 23,
                        'num_chests': 1, 'completed_at': (now-timedelta(days=2)).isoformat()}}]},
                    self.season.rio_season, 100, 8)
                row.payload_json['mythic_plus_scored_history'] = {'8': {**clean,
                    'fetched_at': now.isoformat(), 'status': 'ready'}}
            row.save()

    def test_global_rotation_is_not_overridden_by_local_occasional_substitution(self):
        from botend.tests.test_raiderio_run_roster import details
        from django.template.loader import render_to_string
        now = timezone.now()
        runs = []
        for index, lineup in enumerate(('ABCDE', 'ABCDE', 'ABCDF', 'ABCGH', 'ABCGH', 'ABCDE')):
            rid = 300 + index
            run = copy.deepcopy(self.runs[0])
            run.update(keystone_run_id=rid, completed_at=(now-timedelta(hours=6-index)).isoformat(),
                url=f'https://raider.io/mythic-plus-runs/season-mn-2/{rid}')
            raw = details(rid)
            raw.update(completed_at=run['completed_at'], num_chests=1)
            for member, letter in zip(raw['roster'], lineup):
                number = ord(letter) - ord('A')
                member['character'].update(id=100+number, name=f'Member{number}',
                    path=f'/characters/eu/test-realm/Member{number}')
            RaiderIORunRoster.objects.create(rio_season=self.season.rio_season, run_id=rid,
                payload_json=raw, fetched_at=now, roster_visible=True, status='ready')
            runs.append(run)
        self.activity.payload_json = {'mythic_plus_best_runs': [], 'mythic_plus_recent_runs': runs}
        self.activity.save()
        RaiderIOCharacterActivity.objects.all().update(scored_runs_json={})
        evidence = service.build_evidence(self.player)
        structure = evidence['metrics']['behavior_review']['structure']
        self.assertEqual(structure['roster_runs'], 6)
        self.assertEqual(len(structure['lineups']), 3)
        self.assertIn('rotating_core', structure['codes'])
        self.assertIn('occasional_substitution', structure['codes'])
        self.assertEqual(evidence['metrics']['team_pattern']['kind'], 'rotating_core')
        self.assertNotIn('fixed_team', {label['code'] for label in evidence['eligible_labels']})
        engine = Mock(send_message=Mock(side_effect=AssertionError('structure only')))
        outcome = service.generate_observation(self.player, engine=engine, skip_uninformative=True)
        self.assertEqual(outcome['status'], 'ready')
        engine.send_message.assert_not_called()
        view = service.project_player_observation(self.player)
        html = render_to_string('portal/spec_detail/_player_observation.html', {'observation': view})
        self.assertIn('近期固定组合与轮换成员', html)
        self.assertNotIn('近期固定五人／偶尔替补', html)

    def test_current_gap_is_not_behavior_and_old_publication_not_current(self):
        evidence = service.build_evidence(self.player)
        self.assertIn('behavior_review', evidence['metrics'])
        self.assertNotIn('higher_teammates', {x['code'] for x in evidence['eligible_labels']})
        self.assertNotIn('被带迹象', evidence['conclusion_options']['public_sample']['text'])
        PlayerObservationAnalysis.objects.create(activity=self.activity, player=self.player,
            prompt_version=5, evidence_json=evidence, evidence_hash=evidence['evidence_hash'],
            result_json={'observations': [], 'conclusion_id': 'public_sample'}, status='ready')
        view = service.project_player_observation(self.player)
        self.assertNotEqual(view['status'], 'ready')
        self.assertFalse(view['observations'])

    def test_generation_projection_management_and_failure_preserve_publication(self):
        from botend.services.player_analysis_management import get_analysis, list_analyses
        from django.template.loader import render_to_string
        self.prepare_behavior()
        data = service.build_evidence(self.player)
        self.assertIn('carrying', data['metrics']['behavior_review']['supported'])
        self.assertIn('carrying', {x['code'] for x in data['eligible_labels']})
        self.assertEqual(service._season_dungeon_ids(self.season, RaiderIOCharacterActivity.objects.get(pk=self.activity.pk)), set(range(1, 9)))
        def response(prompt, **kwargs):
            data = json.loads(prompt)
            label = next(x for x in data['eligible_labels'] if x['code'] == 'carrying')
            output = {'observations': [{key: value for key, value in {
                'code': label['code'], 'confidence': '低',
                'reason_id': next(iter(label['reason_options'])),
                'limitation_ids': label['required_limitation_ids'],
                'evidence_ids': label['evidence_ids']}.items()}], 'conclusion_id': 'public_sample'}
            plan = data['metrics'].get('investigation')
            if plan:
                from botend.tests.test_player_observation import structured_response
                output['investigation'] = structured_response(plan['response_contract'])
            return json.dumps(output, ensure_ascii=False)
        engine = Mock(send_message=Mock(side_effect=response))
        outcome = service.generate_observation(self.player, engine=engine)
        self.assertEqual(outcome['status'], 'ready')
        row = PlayerObservationAnalysis.objects.get()
        self.assertEqual(row.prompt_version, service.PROMPT_VERSION)
        view = service.project_player_observation(self.player)
        self.assertEqual(view['status'], 'ready')
        html = render_to_string('portal/spec_detail/_player_observation.html', {'observation': view})
        self.assertIn('疑似协助他人上分（低置信）', html)
        self.assertIn('已有线索', html)
        self.assertTrue(get_analysis(row.pk)['ai']['tags'])
        self.assertTrue(list_analyses({})['items'])
        before = row.result_json
        forged = copy.deepcopy(row.evidence_json)
        forged['metrics']['behavior_review']['witnesses']['carrying'] = [123]
        with self.assertRaises(service.InvalidResult):
            service._validate_publication(json.dumps(before, ensure_ascii=False), forged, publication_version=8, require_investigation=True)
        broken = Mock(send_message=Mock(return_value='{"observations": [], "unexpected": true}'))
        self.assertEqual(service.generate_observation(self.player, force=True, engine=broken)['status'], 'invalid_schema')
        row.refresh_from_db()
        self.assertEqual(row.result_json, before)
        self.assertEqual(service.project_player_observation(self.player)['status'], 'stale')

    def test_behavior_only_trigger_uses_actual_leads_and_preserves_existing_trigger(self):
        self.prepare_behavior()
        # Two lower runs suffice for behavior leads, not the older three-run gate.
        self.activity.refresh_from_db()
        self.activity.payload_json['mythic_plus_recent_runs'] = self.runs[:2]
        self.activity.save()
        data = service.build_evidence(self.player)
        behavior = data['metrics']['behavior_review']
        plan = data['metrics']['investigation']
        self.assertTrue(behavior['triggered'])
        self.assertTrue(plan['triggered'])
        self.assertEqual(plan['trigger_reason'], 'behavior_review_signal')
        self.assertEqual(plan['seed_run_ids'], [123, 124])
        self.assertEqual(plan['core_members'], [])
        self.assertNotIn(2001, plan['seed_run_ids'])  # Historical higher record is not a seed.
        self.assertNotIn(125, plan['seed_run_ids'])
        self.assertTrue(any(e['id'] == 'behavior:lead:123' for e in data['evidence']))
        old = {**plan, 'trigger_reason': 'repeated_below_own_current_with_stable_core',
               'seed_run_ids': [124, 125], 'core_members': ['existing-core']}
        before = copy.deepcopy(old)
        merged = service._merge_behavior_investigation(old, behavior)
        self.assertEqual(merged['trigger_reason'], old['trigger_reason'])
        self.assertEqual(merged['seed_run_ids'], [123, 124, 125])
        self.assertEqual(merged['core_members'], ['existing-core'])
        self.assertEqual(old, before)
        # A witness may trigger without a subject-higher lead (e.g. joint progress).
        witnesses_only = {**behavior, 'evidence': [e for e in behavior['evidence']
                                                 if not e['id'].startswith('behavior:lead:')],
                          'witnesses': {'joint_progress': [124]}}
        empty = {**plan, 'triggered': False, 'trigger_reason': 'no_recent_signal', 'seed_run_ids': []}
        merged = service._merge_behavior_investigation(empty, witnesses_only)
        self.assertEqual(merged['trigger_reason'], 'behavior_review_signal')
        self.assertEqual(merged['seed_run_ids'], [124])

    def test_prior_higher_records_counter_later_progress_per_run(self):
        self.prepare_behavior()
        evidence = service.build_evidence(self.player)
        later = next(h for h in evidence['metrics']['investigation']['hypotheses'] if h['id'] == 'later_progress')
        expected = {f"behavior:prior_higher:{r['keystone_run_id']}" for r in self.runs}
        self.assertEqual(set(later['counter_ids']), expected)
        self.assertEqual(later['support_ids'], [])
        self.assertEqual(later['status'], 'weakened')
        self.assertTrue(expected <= {e['id'] for e in evidence['evidence']})
        # A higher record between recent runs only counters the later run;
        # it must not be backdated into earlier runs.
        self.activity.refresh_from_db()
        for record in self.activity.scored_runs_json['dungeons']:
            if record['dungeon']['id'] == 1:
                record['completed_at'] = (timezone.now()-timedelta(minutes=90)).isoformat()
        self.activity.save(update_fields=['scored_runs_json'])
        mixed = service.build_evidence(self.player)
        later = next(h for h in mixed['metrics']['investigation']['hypotheses'] if h['id'] == 'later_progress')
        self.assertEqual(later['counter_ids'], [f"behavior:prior_higher:{self.runs[0]['keystone_run_id']}"])
        self.assertEqual(set(later['support_ids']), {f"investigation:own:{r['keystone_run_id']}" for r in self.runs[1:]})
        self.assertEqual(later['status'], 'compatible')

    def test_structure_only_skips_ai_and_catalog_never_uses_wcl_ids(self):
        self.prepare_behavior()
        for row in RaiderIOCharacterActivity.objects.all():
            row.scored_runs_json = {}
            row.payload_json.pop('mythic_plus_scored_history', None)
            row.save()
        engine = Mock(send_message=Mock(side_effect=AssertionError('no inquiry')))
        out = service.generate_observation(self.player, engine=engine, skip_uninformative=True)
        self.assertEqual(out['status'], 'ready')
        self.assertFalse(out['ai_called'])
        engine.send_message.assert_not_called()
        view = service.project_player_observation(self.player)
        self.assertEqual(view['source_kind'], 'local_deterministic')
        self.assertTrue(view['display_judgements'])
        activity = RaiderIOCharacterActivity.objects.get(pk=self.activity.pk)
        activity.payload_json['mythic_plus_dungeon_run_counts'].pop()
        self.assertIsNone(service._season_dungeon_ids(self.season, activity))
        activity.payload_json['mythic_plus_dungeon_run_counts'] = []
        self.assertIsNone(service._season_dungeon_ids(self.season, activity))

    def test_history_gates_retract_publication_without_erasing_last_good(self):
        self.prepare_behavior()
        initial = service.build_evidence(self.player)
        label = next(x for x in initial['eligible_labels'] if x['code'] == 'carrying')
        result = {'observations': [{'code': 'carrying', 'confidence': '低',
            'reason_id': next(iter(label['reason_options'])),
            'limitation_ids': label['required_limitation_ids'],
            'evidence_ids': label['evidence_ids']}], 'conclusion_id': 'public_sample'}
        from botend.tests.test_player_observation import structured_response
        result['investigation'] = structured_response(
            service._generation_input(initial)['metrics']['investigation']['response_contract'])
        publication = PlayerObservationAnalysis.objects.create(activity=self.activity,
            player=self.player, prompt_version=8, evidence_json=initial,
            evidence_hash=initial['evidence_hash'], result_json=result, status='ready')
        self.assertEqual(service.project_player_observation(self.player)['status'], 'ready')
        self.activity.refresh_from_db()
        good = copy.deepcopy(self.activity.payload_json['mythic_plus_scored_history']['8'])
        for changes in ({'status': 'private'}, {'private': True},
                        {'status': 'error'}, {'last_error': 'transport_error'},
                        {'status': 'fetching', 'lease_token': 'secret'},
                        {'fetched_at': (timezone.now()-timedelta(hours=49)).isoformat()}):
            with self.subTest(changes=changes):
                self.activity.payload_json['mythic_plus_scored_history']['8'] = {**good, **changes}
                self.activity.save()
                current = service.build_evidence(self.player)
                self.assertNotEqual(initial['evidence_hash'], current['evidence_hash'])
                self.assertEqual(current['coverage']['score_characters'], 5)
                behavior = current['metrics']['behavior_review']
                proof = behavior['resilience']['events'][0]['members'][self.activity.identity_digest]
                self.assertEqual(proof['state'], 'unknown')
                self.assertNotIn('8', proof['per_dungeon'])
                if 'fetched_at' not in changes:
                    self.assertTrue(current['source_failed'])
                    engine = Mock(send_message=Mock(side_effect=AssertionError('failed source')))
                    self.assertEqual(service.generate_observation(self.player, force=True, engine=engine)['status'], 'source_failed')
                    engine.send_message.assert_not_called()
                view = service.project_player_observation(self.player)
                self.assertFalse(view['observations'])
                self.assertFalse(view.get('investigation'))
                publication.refresh_from_db()
                self.assertEqual(publication.result_json, result)
                self.assertEqual(publication.evidence_json, initial)
        self.activity.payload_json['mythic_plus_scored_history']['8'] = good
        self.activity.save()
        restored = service.build_evidence(self.player)
        self.assertEqual(restored['evidence_hash'], initial['evidence_hash'])
        stamp = restored['source_stamps']['histories'][0]
        self.assertEqual(stamp['fetched_at'], good['fetched_at'])
        self.assertTrue(stamp['digest'])
        self.activity.payload_json['mythic_plus_scored_history']['8']['lease_token'] = 'unused-lease'
        self.activity.save()
        self.assertEqual(service.build_evidence(self.player)['evidence_hash'], initial['evidence_hash'])

    def test_resilience_targets_derive_only_from_unknown_current_event_proofs(self):
        self.prepare_behavior()
        self.activity.refresh_from_db()
        self.activity.payload_json.pop('mythic_plus_scored_history')
        self.activity.save()
        evidence = service.build_evidence(self.player)
        behavior = evidence['metrics']['behavior_review']
        plan = evidence['metrics']['investigation']
        target = next(t for t in plan['resilience_history_targets']
                      if t['member_key'] == self.activity.identity_digest and t['dungeon_id'] == 8)
        self.assertEqual(target['required_level'], 23)
        self.assertEqual(target['run_ids'], [123, 124, 125])
        question = next(q for q in behavior['questions'] if q.get('purpose') == 'resilience_gap'
                        and q['member_key'] == target['member_key'] and q['dungeon_id'] == 8)
        empty = {'triggered': True, 'evidence': [], 'questions': [], 'hypotheses': []}
        for changes in ({'dungeon_id': 99}, {'dungeon_id': 1}, {'required_level': 22},
                        {'member_key': 'not-in-roster'}, {'run_ids': [999]},
                        {'run_ids': [123, 123]}):
            forged = copy.deepcopy(behavior)
            forged['questions'] = [{**question, **changes}]
            merged = service._merge_behavior_investigation(empty, forged)
            self.assertFalse(merged['resilience_history_targets'], changes)
            self.assertFalse(merged['questions'], changes)
        duplicate = copy.deepcopy(behavior)
        duplicate['questions'] = [question, question]
        self.assertEqual(len(service._merge_behavior_investigation(empty, duplicate)['resilience_history_targets']), 1)

    def test_json_mode_is_player_scoped(self):
        from core.glm import GLMClient
        with patch.object(GLMClient, '_create_completion', return_value='response') as create:
            client = object.__new__(service.PlayerObservationGLMClient)
            self.assertEqual(client._create_completion(messages=[]), 'response')
        self.assertEqual(create.call_args.kwargs['response_format'], {'type': 'json_object'})
