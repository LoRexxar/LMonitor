"""A rotation signal must progress through recent core and fifth-seat evidence."""
import copy
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from botend.models import RaiderIOCharacterActivity, RaiderIORunRoster
from botend.tests.test_player_observation import PlayerObservationTests
from botend.tests.test_raiderio_run_roster import details


class ObservationProgressionTests(TestCase):
    evidence = PlayerObservationTests.evidence
    engine = PlayerObservationTests.engine

    def setUp(self):
        PlayerObservationTests.setUp(self)
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

    def test_recent_core_then_low_fifth_support_and_counterevidence(self):
        data = self.evidence()
        progression = data['metrics']['progression']
        self.assertEqual(progression['status'], 'supported')
        self.assertEqual(progression['recent_timed_runs'], 8)
        self.assertEqual(progression['core_comparable_runs'], 8)
        self.assertEqual(progression['below_core_runs'], 8)
        self.assertEqual(progression['fifth_comparable_runs'], 8)
        self.assertEqual(progression['low_fifth_runs'], 8)
        self.assertEqual(progression['distinct_low_fifths'], 3)
        self.assertIn('双信号', data['judgements'][2]['verdict'])
        self.assertTrue(all('不是历史加分' in row['text'] for row in progression['details']))
        # Counterevidence must change the conclusion, not just add a disclaimer.
        for row in RaiderIOCharacterActivity.objects.filter(character_name__in=['member4','member5','member6']):
            row.scored_runs_json['dungeons'][0].update(mythic_level=26, score=550)
            row.save()
        data = self.evidence()
        self.assertEqual(data['metrics']['progression']['status'], 'weakened')
        self.assertEqual(data['metrics']['progression']['low_fifth_runs'], 0)
        self.assertNotIn('疑似四人带人队伍', data['judgements'][2]['verdict'])
        self.assertIn('未支持', data['judgements'][2]['verdict'])

    def test_missing_fifth_does_not_erase_core_evidence_or_become_zero(self):
        RaiderIOCharacterActivity.objects.filter(character_name__in=['member4','member5','member6']).update(scored_runs_fetched_at=None)
        data = self.evidence()
        progression = data['metrics']['progression']
        self.assertEqual(progression['core_comparable_runs'], 8)
        self.assertEqual(progression['below_core_runs'], 8)
        self.assertEqual(progression['fifth_comparable_runs'], 0)
        self.assertEqual(progression['missing_fifth_runs'], 8)
        self.assertIsNone(progression['low_fifth_frequent'])
        self.assertEqual(progression['status'], 'insufficient')

    def test_best_records_never_inflate_recent_frequency_and_alternate_contributes(self):
        self.activity.payload_json['mythic_plus_recent_runs'] = self.runs[:2]
        self.activity.save(update_fields=['payload_json', 'module_fetched_at'])
        data = self.evidence()
        self.assertEqual(data['metrics']['progression']['recent_timed_runs'], 2)
        self.assertEqual(data['metrics']['progression']['status'], 'insufficient')
        self.activity.payload_json['mythic_plus_recent_runs'] = self.runs
        self.activity.save(update_fields=['payload_json', 'module_fetched_at'])
        own = RaiderIOCharacterActivity.objects.get(character_name='member0')
        alt = copy.deepcopy(own.scored_runs_json['dungeons'][0]); alt.pop('alternate')
        alt.update(keystone_run_id=200, mythic_level=20, score=450)
        own.scored_runs_json['dungeons'][0]['alternate'] = alt
        own.save()
        progression = self.evidence()['metrics']['progression']
        self.assertEqual(progression['core_comparable_runs'], 8)
        self.assertEqual(progression['below_core_runs'], 7)
        self.assertNotIn(200, progression['below_core_run_ids'])

    def test_old_recent_snapshot_and_privacy_retraction_never_confirm(self):
        self.activity.module_fetched_at['mythic_plus_recent_runs'] = (self.now-timedelta(days=3)).isoformat()
        self.activity.save(update_fields=['payload_json', 'module_fetched_at'])
        self.assertEqual(self.evidence()['metrics']['progression']['status'], 'insufficient')
        self.activity.module_fetched_at['mythic_plus_recent_runs'] = self.now.isoformat()
        self.activity.save(update_fields=['payload_json', 'module_fetched_at'])
        from botend.services.player_observation import generate_observation, project_player_observation
        from botend.tests.test_player_observation_investigation import InvestigationIntegrationTests
        self.assertEqual(generate_observation(self.player, engine=InvestigationIntegrationTests.engine(self))['status'], 'ready')
        self.assertEqual(project_player_observation(self.player)['progression']['status'], 'supported')
        RaiderIORunRoster.objects.filter(run_id=200).update(roster_visible=False)
        self.assertEqual(project_player_observation(self.player)['status'], 'pending')
