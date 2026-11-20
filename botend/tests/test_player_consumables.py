from datetime import timedelta
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from botend.models import (
    PlayerSpecTopPlayer, RaiderIOCharacterActivity, SeasonMeta, SpecDungeonRanking, WclConsumableObservation,
)
from botend.services.player_consumables import project_player_consumables
from botend.services.player_raiderio_activity import project_player_activity
from botend.services.wcl_combatant_store import persist_events


class PlayerConsumablesTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='s2', season_name='第二赛季',
            rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2)
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk,
            region='eu', realm='Test Realm', character_name='Tester', class_name='DeathKnight', spec_name='Frost',
            rank=1, profile_url='https://raider.io/characters/eu/test-realm/Tester')
        self.now = timezone.now().replace(microsecond=0)

    def ranking(self, day, **overrides):
        fields = dict(season_id=self.season.pk, dungeon_id=123, dungeon_name='Murder Row',
            class_name='DeathKnight', spec_name='Frost', character_name='Tester', realm='Test Realm',
            region='EU', dps=1, keystone_level=20, clear_time=900012, report_code=f'Report{day}', fight_id=1,
            fight_started_at=self.now + timedelta(days=day), last_updated=self.now - timedelta(days=day))
        return SpecDungeonRanking.objects.create(**{**fields, **overrides})

    def observe(self, row, payload, *, actor=8, actor_name='Tester', actor_realm='TestRealm', gear=None):
        spec = {'Blood': 250, 'Frost': 251}[row.spec_name]
        event = dict(sourceID=actor, specID=spec, source=dict(id=actor, name=actor_name, server=actor_realm))
        if gear is not None:
            event['gear'] = gear
        persist_events(row.report_code, {row.fight_id: [event]})
        WclConsumableObservation.objects.create(report_code=row.report_code, fight_id=row.fight_id,
                                               actor_id=actor, payload=payload)

    def rio_run(self, row, *, timed=True):
        completed = row.fight_started_at + timedelta(milliseconds=row.clear_time)
        return dict(dungeon=row.dungeon_name, mythic_level=row.keystone_level, clear_time_ms=row.clear_time,
                    completed_at=completed.isoformat(), num_keystone_upgrades=int(timed),
                    url=f'https://raider.io/mythic-plus-runs/season-mn-2/{row.pk}')

    def project(self, *, best=(), recent=()):
        RaiderIOCharacterActivity.objects.create(season=self.season, region='eu', realm_slug='test-realm',
            character_name='Tester', payload_json={'mythic_plus_best_runs': list(best),
                                                   'mythic_plus_recent_runs': list(recent)})
        with patch('requests.sessions.Session.request', side_effect=AssertionError('角色页应只读本地数据')):
            return project_player_consumables(self.player, project_player_activity(self.player))

    def test_recent_uses_real_fight_time_limits_distinct_runs_and_counts_matched_actor(self):
        rankings = [self.ranking(day, spec_name='Blood' if day == 11 else 'Frost') for day in range(12)]
        self.ranking(11, spec_name='Blood')  # 同一份日志的重复榜单行。
        self.ranking(9, report_code='AnotherUpload')  # 同时间、层数、用时和副本的重复上传。
        self.ranking(20, realm='Other Realm')
        self.ranking(21, region='US')
        self.ranking(22, fight_started_at=None)
        self.ranking(23, season_id=self.season.pk + 1)
        self.observe(rankings[1], {'potion': [1236998]})  # 因实际开打时间掉出最近十场。
        self.observe(rankings[10], {'potion': [1236994, 1236994], 'food': []})
        self.observe(rankings[2], {'potion': []})
        self.observe(rankings[11], {'potion': [], 'flask': [1235108]},
                     gear=[{'id': 12345, 'slot': 15, 'temporaryEnchant': 8051}])
        self.observe(rankings[11], {'potion': [1236998]}, actor=9, actor_realm='OtherRealm')

        preferences = self.project()
        groups = {group['key']: group for group in preferences['recent']['groups']}
        self.assertEqual(preferences['recent']['sample_size'], 10)
        self.assertEqual(groups['potion']['samples'], 3)
        self.assertEqual(groups['potion']['rows'], [{'name': '鲁莽药水', 'count': 1, 'pct': 33.3}])
        self.assertEqual(groups['food']['samples'], 1)
        self.assertEqual(groups['food']['rows'], [])
        self.assertEqual(groups['healing']['samples'], 0)
        self.assertEqual(groups['flask']['rows'][0]['name'], '魔导师合剂')
        self.assertEqual(groups['weapon']['rows'][0]['name'], '萨拉斯凤凰之油')
        html = render_to_string('portal/spec_detail/_player_consumables.html', {'preferences': preferences})
        self.assertIn('最近 10 场', html)
        self.assertIn('1/3 场', html)
        self.assertIn('暂无可统计数据', html)

    def test_highest_matches_same_run_even_when_outside_recent_and_ignores_untimed(self):
        old_high = self.ranking(-22, keystone_level=24)
        best = self.ranking(-20, keystone_level=24)
        self.ranking(-20, keystone_level=24, report_code='WrongDungeon', dungeon_name='Den of Nalorakk')
        self.ranking(-20, keystone_level=24, report_code='WrongDuration', clear_time=best.clear_time + 1)
        self.ranking(-23, keystone_level=24, report_code='WrongStart')
        untimed = self.ranking(-1, keystone_level=28)
        for day in range(10):
            self.ranking(day)
        self.observe(best, {'potion': [1236994]})

        preferences = self.project(best=[self.rio_run(old_high), self.rio_run(untimed, timed=False)],
                                   recent=[self.rio_run(best)])
        highest = preferences['highest']
        self.assertEqual(highest['run']['url'], self.rio_run(best)['url'])
        self.assertEqual(highest['log_url'], 'https://www.warcraftlogs.com/reports/Report-20#fight=1&source=8')
        groups = {group['key']: group for group in highest['groups']}
        self.assertEqual(groups['potion']['rows'][0]['name'], '鲁莽药水')
        self.assertEqual(preferences['recent'], {'sample_size': 10, 'groups': []})
        self.assertIn('密谋小径 +24', render_to_string('portal/spec_detail/_player_consumables.html',
                                                     {'preferences': preferences}))

    def test_ambiguous_highest_never_uses_a_lower_run_or_one_of_multiple_matches(self):
        lower = self.ranking(-1)
        highest = self.ranking(0, keystone_level=25)
        duplicate = self.ranking(0, keystone_level=25, report_code='Ambiguous')
        self.observe(lower, {'potion': [1236994]})
        self.observe(duplicate, {'potion': [1236998]})

        preferences = self.project(best=[self.rio_run(lower), self.rio_run(highest)])
        self.assertEqual(preferences['highest']['run']['level'], 25)
        self.assertEqual(preferences['highest']['log_url'], '')
        self.assertEqual(preferences['highest']['groups'], [])
        self.assertIn('暂无可匹配的 WCL 日志。', render_to_string('portal/spec_detail/_player_consumables.html',
                                                              {'preferences': preferences}))

    def test_player_detail_renders_consumables_from_local_cache(self):
        row = self.ranking(-1)
        self.observe(row, {'potion': [1236994]})
        self.project(best=[self.rio_run(row)])
        url = reverse('spec_detail_player_detail', args=['DeathKnight', 'Frost', self.player.pk])
        with patch('requests.sessions.Session.request', side_effect=AssertionError('角色页应只读本地数据')):
            response = self.client.get(url)
        self.assertContains(response, '最近 1 场')
        self.assertContains(response, '1/1 场')
        self.assertContains(response, '查看 WCL 日志')
