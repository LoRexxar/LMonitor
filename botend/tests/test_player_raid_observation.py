"""Raid rule uses mythic first kills, local dates and raid-local boss order."""
from datetime import datetime, timezone

from copy import deepcopy
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.template.loader import render_to_string
from django.utils import timezone as django_timezone

from botend.models import PlayerSpecTopPlayer, PlayerObservationAnalysis, RaiderIOCharacterActivity, SeasonMeta
from botend.services.player_raiderio_activity import player_identity


class RaidObservationProjectionTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='mn-s2', season_name='S2',
            rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2,
            raid_encounters=[{'id':100+i,'index':90+i,'name':f'Boss {i}'} for i in (6,7,8)])
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu',
            realm='test-realm', character_name='Character', class_name='Priest', spec_name='Holy',
            rank=1, profile_url='https://raider.io/characters/eu/test-realm/Character')
        self.activity = RaiderIOCharacterActivity.objects.create(season=self.season,
            **player_identity(self.player), status='ready')
        self.now = django_timezone.now()
        self.state = {'version':1,'status':'ready','visible':True,
            'identity_digest':self.activity.identity_digest,'rio_season':self.season.rio_season,
            'fetched_at':self.now.isoformat(), 'source':{'character_id':10,'tier':'36'},
            'raids':[{'slug':'current-raid','name':'Current Raid','encounters':[
                {'index':i,'wowEncounterId':100+i,'slug':f'boss-{i}', 'numKills':1,
                 'firstDefeated':(self.now-timedelta(days=1)).isoformat() if i in (7,8) else None}
                for i in (6,7,8)]}]}

    def save_state(self, state=None):
        self.activity.payload_json = {'raid_first_kills':self.state if state is None else state}
        self.activity.save(update_fields=['payload_json'])

    def test_real_read_projection_and_template_without_ai_even_after_engine_failure(self):
        from botend.services.player_observation import project_player_observation, build_evidence
        before = build_evidence(self.player)['evidence_hash']
        self.save_state()
        after = build_evidence(self.player)['evidence_hash']
        self.assertEqual(before, after, 'raid-only refresh must preserve M+ evidence hash')
        for failed in (False, True):
            if failed:
                PlayerObservationAnalysis.objects.create(activity=self.activity, status='error',last_error='engine_empty')
            view = project_player_observation(self.player)
            self.assertEqual(view['status'], 'error' if failed else 'pending')
            self.assertTrue(view['raid_judgement']['supported'])
            self.assertEqual(view['raid_judgement']['value'], '团本消费选手')
            html = render_to_string('portal/spec_detail/_player_observation.html', {'observation':view})
            self.assertIn('团本消费选手', html)
            self.assertIn('史诗7、8号', html)
            self.assertIn('首次击杀', html)
            self.assertIn('不代表已确认付费', html)
            self.assertIn('rel="noopener noreferrer"', html)
            self.assertNotIn('<details open', html)
            self.assertEqual(html.count('data-judgement='), 1)
        self.assertIsNone(PlayerObservationAnalysis.objects.get().generated_at)

    def test_activity_fresh_branch_maintains_only_enrolled_raid_pilots(self):
        from botend.services.player_raiderio_activity import ActivityCollector
        self.state['next_attempt_at'] = (self.now+timedelta(hours=24)).isoformat()
        self.save_state()
        self.activity.next_attempt_at = self.now+timedelta(hours=1)
        self.activity.save(update_fields=['next_attempt_at'])
        def no_network(*args, **kwargs):
            self.fail('fresh caches must not trigger requests')
        collector = ActivityCollector(transport=no_network)
        result = collector.refresh(self.player, self.season)
        self.assertEqual(result['status'], 'fresh')
        self.assertEqual(result['raid_first_kills']['status'], 'fresh')
        self.activity.refresh_from_db()
        self.assertEqual(self.activity.payload_json['raid_first_kills'], self.state)
        self.activity.payload_json = {}
        self.activity.save(update_fields=['payload_json'])
        result = collector.refresh(self.player, self.season)
        self.assertNotIn('raid_first_kills', result)
        self.activity.refresh_from_db()
        self.assertNotIn('raid_first_kills', self.activity.payload_json)

    def test_invalid_stale_private_wrong_scope_or_unmatched_only_show_insufficient(self):
        from botend.services.player_raid_observation import project_raid_observation
        variants = []
        for changes in ({'visible':False}, {'rio_season':'old'}, {'identity_digest':'other'},
                        {'fetched_at':(self.now-timedelta(hours=49)).isoformat()},
                        {'fetched_at':(self.now+timedelta(hours=1)).isoformat()}):
            variants.append({**deepcopy(self.state), **changes})
        unknown = deepcopy(self.state)
        unknown['raids'][0]['encounters'][2]['firstDefeated'] = None
        variants.append(unknown)
        different = deepcopy(self.state)
        different['raids'][0]['encounters'][2]['firstDefeated'] = (self.now-timedelta(days=2)).isoformat()
        variants.append(different)
        wrong_boss = deepcopy(self.state)
        wrong_boss['raids'][0]['encounters'][2]['wowEncounterId'] = 999
        variants.append(wrong_boss)
        for state in variants:
            self.save_state(state)
            judgement = project_raid_observation(self.activity, self.season, player_identity(self.player), now=self.now)
            self.assertFalse(judgement['supported'])
            self.assertEqual(judgement['value'], '证据不足')
            self.assertEqual(judgement['reason'], '')
            self.assertEqual(judgement['evidence'], [])
            html = render_to_string('portal/spec_detail/_player_observation.html',
                {'observation':{'status':'pending','raid_judgement':judgement}})
            self.assertEqual(html.count('证据不足'), 1)
            self.assertNotIn('依据：', html)
            self.assertNotIn('<details', html)


class RaidFirstKillRuleTests(SimpleTestCase):
    def test_any_two_tail_bosses_share_beijing_first_kill_date(self):
        from botend.services.player_raid_observation import same_day_first_kills
        fetched = datetime(2026,10,13,tzinfo=timezone.utc)
        for pair in ((6,7),(6,8),(7,8)):
            rows = [{'index':i, 'firstDefeated':None} for i in (6,7,8)]
            rows[pair[0]-6]['firstDefeated'] = '2026-09-20T23:30:00Z'
            rows[pair[1]-6]['firstDefeated'] = '2026-09-21T02:30:00Z'
            with self.subTest(pair=pair):
                matched = same_day_first_kills(list(reversed(rows)), fetched_at=fetched)
                self.assertEqual([r['index'] for r in matched], list(pair))
                self.assertEqual({r['date'] for r in matched}, {'2026-09-21'})

    def test_repeat_kills_missing_naive_future_and_distinct_first_dates_do_not_match(self):
        from botend.services.player_raid_observation import same_day_first_kills
        fetched = datetime(2026,10,13,tzinfo=timezone.utc)
        rows = [{'index':6,'firstDefeated':'2026-09-20T12:00:00Z','lastDefeated':'2026-10-10T12:00:00Z'},
                {'index':7,'firstDefeated':'2026-09-21T12:00:00Z','lastDefeated':'2026-10-10T12:00:00Z'},
                {'index':8,'firstDefeated':None,'lastDefeated':'2026-10-10T12:00:00Z'}]
        self.assertEqual(same_day_first_kills(rows, fetched_at=fetched), [])
        for value in (None, '', '2026-10-12T12:00:00', '2026-10-14T12:00:00Z'):
            rows = [{'index':6,'firstDefeated':value}, {'index':7,'firstDefeated':value}]
            with self.subTest(value=value):
                self.assertEqual(same_day_first_kills(rows, fetched_at=fetched), [])

    def test_other_bosses_and_duplicate_boss_do_not_create_a_pair(self):
        from botend.services.player_raid_observation import same_day_first_kills
        fetched = datetime(2026,10,13,tzinfo=timezone.utc)
        for indices in ((1,2),(5,6),(6,6),(True,7)):
            rows=[{'index':i,'firstDefeated':'2026-10-01T12:00:00Z'} for i in indices]
            with self.subTest(indices=indices):
                self.assertEqual(same_day_first_kills(rows, fetched_at=fetched), [])
