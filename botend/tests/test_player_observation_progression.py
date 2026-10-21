"""A rotation signal must progress through recent core and fifth-seat evidence."""
import copy
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from botend.models import RaiderIOCharacterActivity, RaiderIORunRoster
from botend.tests import test_player_observation as _fixture_module
from botend.tests.test_raiderio_run_roster import details


class ObservationProgressionTests(TestCase):
    evidence = _fixture_module.PlayerObservationTests.evidence
    engine = _fixture_module.PlayerObservationTests.engine

    def setUp(self):
        _fixture_module.PlayerObservationTests.setUp(self)
        self.now = timezone.now()
        runs = []
        for index in range(8):
            rid = 200 + index
            run = copy.deepcopy(self.runs[0])
            run.update(keystone_run_id=rid, mythic_level=20, score=450,
                       completed_at=(self.now-timedelta(hours=index+1)).isoformat(),
                       url=f'https://raider.io/mythic-plus-runs/season-mn-2/{rid}')
            raw = details(rid)
            raw.update(mythic_level=20, num_chests=1, completed_at=run['completed_at'])
            fifth = 4 if index < 5 else (5 if index < 7 else 6)
            for i, member in enumerate(raw['roster']):
                n = fifth if i == 4 else i
                member['character'].update(id=100+n, name=f'Member{n}', path=f'/characters/eu/test-realm/Member{n}')
            RaiderIORunRoster.objects.create(rio_season=self.season.rio_season, run_id=rid,
                payload_json=raw, fetched_at=self.now, roster_visible=True, status='ready')
            runs.append(run)
        self.runs = runs
        self.activity.payload_json = {'mythic_plus_best_runs': runs, 'mythic_plus_recent_runs': runs}
        self.activity.module_fetched_at = {'mythic_plus_recent_runs': self.now.isoformat()}
        self.activity.save(update_fields=['payload_json', 'module_fetched_at'])
        base = RaiderIOCharacterActivity.objects.get(character_name='member0').scored_runs_json
        for i in range(7):
            row, _ = RaiderIOCharacterActivity.objects.get_or_create(season=self.season,
                region='eu', realm_slug='test-realm', character_name=f'member{i}')
            row.scored_runs_json = copy.deepcopy(base)
            row.scored_runs_json['ui']['characterId'] = 100+i
            row.scored_runs_json['dungeons'][0].update(keystone_run_id=999+i,
                mythic_level=26 if i < 4 else 22, score=550 if i < 4 else 480,
                completed_at=(self.now-timedelta(days=1)).isoformat())
            row.scored_runs_fetched_at = self.now
            row.save()

    def test_recent_core_then_low_fifth_comparison_and_counterevidence(self):
        data = self.evidence()
        review = data['metrics']['behavior_review']
        structure = review['structure']
        self.assertEqual(structure['recent_runs'], 8)
        self.assertEqual(structure['roster_runs'], 8)
        self.assertEqual(structure['codes'], ['rotating_core'])
        self.assertEqual(structure['observed_config_switches'], 2)
        core = sorted(RaiderIOCharacterActivity.objects.filter(
            character_name__in=['member0', 'member1', 'member2', 'member3']
        ).values_list('identity_digest', flat=True))
        group = next(g for g in structure['comparison_groups'] if g['members'] == core)
        self.assertEqual(set(group['run_ids']), set(range(200, 208)))
        self.assertEqual(group['non_dominant_appearances'], 3)
        self.assertEqual(data['coverage']['comparison_runs'], 8)
        self.assertEqual({c['run_id'] for c in data['metrics']['comparisons']}, set(range(200, 208)))
        for comparison in data['metrics']['comparisons']:
            self.assertEqual(comparison['run_level'], 20)
            self.assertEqual(comparison['own_level'], 26)
            self.assertEqual(sorted(comparison['teammate_level_gaps']), [-4, 0, 0, 0])
        events = review['resilience']['events']
        self.assertEqual({e['run_id'] for e in events}, set(range(200, 208)))
        self.assertTrue(all(len(e['relative_comparisons']['lower_peers']) == 1 for e in events))
        comparisons = [e['relative_comparisons']['lower_peers'][0] for e in events]
        self.assertEqual(len({c['member_key'] for c in comparisons}), 3)
        for comparison in comparisons:
            self.assertEqual(comparison['snapshot']['level'], 22)
            self.assertEqual(comparison['leader_prior']['level'], 26)
            self.assertEqual(comparison['snapshot_source'], 'scored_runs')
            self.assertEqual(comparison['snapshot_as_of'], self.now.isoformat())
        self.assertTrue(all('不是历史加分' in e['text'] for e in data['evidence']
                            if e['id'].startswith('comparison:')))
        # v6+ retains the comparison but does not infer carrying from a low
        # fifth alone: this fixture has no verified eight-dungeon protection.
        self.assertTrue(review['triggered'])
        self.assertNotIn('carrying', review['supported'])
        self.assertTrue(all(e['members'][self.activity.identity_digest]['state'] == 'unknown'
                            for e in events))
        self.assertEqual({e['id'] for e in data['evidence'] if e['id'].startswith('investigation:low:')},
                         {f'investigation:low:{rid}' for rid in range(200, 208)})
        # Equal-score fifths must remove the lower-peer fact, not merely add a disclaimer.
        for row in RaiderIOCharacterActivity.objects.filter(character_name__in=['member4','member5','member6']):
            row.scored_runs_json['dungeons'][0].update(mythic_level=26, score=550)
            row.save()
        changed = self.evidence()
        review = changed['metrics']['behavior_review']
        self.assertEqual(review['structure'], structure)
        self.assertTrue(all(e['relative_comparisons']['lower_peers'] == []
                            for e in review['resilience']['events']))
        self.assertFalse(any(e['id'].startswith('investigation:low:') for e in changed['evidence']))
        self.assertEqual({e['id'] for e in changed['evidence'] if e['id'] in
                          {f'investigation:near:{rid}' for rid in range(200, 208)}},
                         {f'investigation:near:{rid}' for rid in range(200, 208)})
        self.assertNotIn('carrying', {label['code'] for label in changed['eligible_labels']})
        self.assertTrue(changed['metrics']['investigation']['triggered'])

    def test_missing_fifth_does_not_erase_core_evidence_or_become_zero(self):
        RaiderIOCharacterActivity.objects.filter(character_name__in=['member4','member5','member6']).update(scored_runs_fetched_at=None)
        data = self.evidence()
        review = data['metrics']['behavior_review']
        plan = data['metrics']['investigation']
        self.assertEqual(review['structure']['recent_runs'], 8)
        self.assertEqual(review['structure']['roster_runs'], 8)
        self.assertEqual(plan['seed_run_ids'], list(range(200, 208)))
        self.assertEqual(len(plan['core_members']), 4)
        self.assertFalse(any(e['id'].startswith('investigation:core_unknown:') for e in data['evidence']))
        self.assertEqual(data['coverage']['comparison_runs'], 0)
        self.assertEqual({e['id'] for e in review['evidence'] if e['id'].startswith('behavior:prior_higher:')},
                         {f'behavior:prior_higher:{rid}' for rid in range(200, 208)})
        self.assertTrue(all(e['relative_comparisons']['lower_peers'] == []
                            for e in review['resilience']['events']))
        missing = {f'investigation:unknown:{rid}' for rid in range(200, 208)}
        facts = {e['id']: e for e in data['evidence']}
        self.assertTrue(missing <= facts.keys())
        self.assertTrue(all('不能判为反证或零分' in facts[eid]['text'] for eid in missing))
        assistance = next(h for h in plan['hypotheses'] if h['id'] == 'assistance')
        self.assertEqual(assistance['status'], 'unknown')
        self.assertEqual(assistance['support_ids'], [])
        self.assertEqual(assistance['counter_ids'], [])
        self.assertTrue(missing <= set(assistance['unknown_ids']))
        # Each guest is queried for its earliest qualifying event, not all of
        # its appearances. Runs are stored newest-first in this fixture.
        expected = {row.identity_digest: [204] if row.character_name == 'member4'
                    else [206] if row.character_name == 'member5' else [207]
                    for row in RaiderIOCharacterActivity.objects.filter(
                        character_name__in=['member4', 'member5', 'member6'])}
        self.assertEqual({q['identity_digest']: q['run_ids'] for q in plan['questions']}, expected)
        for question in plan['questions']:
            self.assertEqual(question['kind'], 'scored_history')
            self.assertEqual(question['purpose'], 'verify_guest_gain')
            self.assertEqual(question['dungeon_id'], 1)
            self.assertEqual(set(question['core_members']), set(plan['core_members']))
            self.assertEqual(set(question['prior_run_ids']), set(question['core_members']))
            self.assertEqual(set(question['prior_run_ids'].values()), {999, 1000, 1001, 1002})
            self.assertNotIn(question['identity_digest'], question['core_members'])

    def test_best_records_never_inflate_recent_frequency_and_alternate_contributes(self):
        self.activity.payload_json['mythic_plus_recent_runs'] = self.runs[:2]
        self.activity.save(update_fields=['payload_json', 'module_fetched_at'])
        data = self.evidence()
        review = data['metrics']['behavior_review']
        self.assertEqual(review['structure']['recent_runs'], 2)
        self.assertEqual(review['structure']['roster_runs'], 2)
        self.assertEqual({e['run_id'] for e in review['resilience']['events']}, {200, 201})
        self.assertEqual(review['structure']['codes'], [])
        # Two real leads may trigger inquiry, but best records cannot turn
        # them into eight recent events or a supported rotation label.
        self.assertEqual(data['metrics']['investigation']['seed_run_ids'], [200, 201])
        self.assertNotIn('rotating_core', {label['code'] for label in data['eligible_labels']})
        self.activity.payload_json['mythic_plus_recent_runs'] = self.runs
        self.activity.save(update_fields=['payload_json', 'module_fetched_at'])
        own = RaiderIOCharacterActivity.objects.get(character_name='member0')
        prior = copy.deepcopy(own.scored_runs_json['dungeons'][0])
        prior.pop('alternate')
        primary = own.scored_runs_json['dungeons'][0]
        primary.update(keystone_run_id=200, mythic_level=20, score=450,
                       completed_at=self.runs[0]['completed_at'], alternate=None)
        own.save()
        self.assertFalse(any(e['id'].startswith('behavior:prior_higher:')
                             for e in self.evidence()['metrics']['behavior_review']['evidence']))
        primary['alternate'] = prior
        own.save()
        review = self.evidence()['metrics']['behavior_review']
        self.assertEqual(review['structure']['recent_runs'], 8)
        self.assertEqual({e['id'] for e in review['evidence'] if e['id'].startswith('behavior:prior_higher:')},
                         {f'behavior:prior_higher:{rid}' for rid in range(200, 208)})
        self.assertTrue(all(e['relative_comparisons']['lower_peers'][0]['leader_prior']['run_id'] == 999
                            for e in review['resilience']['events']))

    def test_old_recent_snapshot_and_privacy_retraction_never_confirm(self):
        self.activity.module_fetched_at['mythic_plus_recent_runs'] = (self.now-timedelta(days=3)).isoformat()
        self.activity.save(update_fields=['payload_json', 'module_fetched_at'])
        data = self.evidence()
        review = data['metrics']['behavior_review']
        self.assertEqual(review['structure']['recent_runs'], 0)
        self.assertEqual(review['structure']['codes'], [])
        self.assertFalse(review['triggered'])
        self.assertEqual(review['supported'], {})
        self.assertFalse(data['metrics'].get('investigation', {}).get('triggered'))
        self.activity.module_fetched_at['mythic_plus_recent_runs'] = self.now.isoformat()
        self.activity.save(update_fields=['payload_json', 'module_fetched_at'])
        from botend.services.player_observation import generate_observation, project_player_observation
        from botend.tests.test_player_observation_investigation import InvestigationIntegrationTests
        self.assertEqual(generate_observation(self.player, engine=InvestigationIntegrationTests.engine(self))['status'], 'ready')
        view = project_player_observation(self.player)
        self.assertEqual(view['status'], 'ready')
        self.assertTrue(view['has_investigation'])
        self.assertEqual(view['judgements'][0]['verdict'], '近期固定组合与轮换成员')
        RaiderIORunRoster.objects.filter(run_id=200).update(roster_visible=False)
        view = project_player_observation(self.player)
        self.assertEqual(view['status'], 'pending')
        self.assertEqual(view['observations'], [])
