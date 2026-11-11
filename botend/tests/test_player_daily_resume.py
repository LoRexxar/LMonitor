"""验证按角色恢复、来源退避和已存资料复用，不连接外部服务。"""
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db import DatabaseError
from django.test import TestCase
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.models import (MonitorTask, PlayerDailyCheckpoint, PlayerSpecTopPlayer,
                           RaiderIOProfileCache, RaiderIOSourceBackoff, SeasonMeta)
from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
from botend.services.player_daily_checkpoint import DailyProgress, resume_due_at
from botend.services.player_daily_resume import digest
from botend.services.raiderio_profile_cache import ProfileCache, ranking_signal
from botend.services.character_bindings import refresh_bound_characters


MODULE = 'botend.controller.plugins.portal.SpecDetailPlayerMonitor'


class PlayerDailyResumeTests(TestCase):
    def setUp(self):
        self.now = timezone.localtime().replace(hour=8, minute=0, second=0, microsecond=0)
        self.enterContext(patch('django.utils.timezone.now', return_value=self.now))
        self.enterContext(patch(MODULE + '.CLASS_SPEC_MAP', {'Mage': ['Fire']}))
        self.enterContext(patch('time.sleep'))
        self.enterContext(patch('requests.sessions.Session.request',
                                side_effect=AssertionError('测试不可请求外部服务')))
        self.enterContext(patch('botend.services.character_enrichment.enqueue_battlenet_profiles'))
        self.enterContext(patch('botend.services.character_bindings.refresh_bound_characters', return_value=0))
        self.season = SeasonMeta.objects.create(season_key='人物恢复', season_name='恢复测试',
            is_active=True, rio_season='season-test', mplus_zone_id=1, raid_zone_id=2)
        self.task = MonitorTask.objects.create(name='SpecDetailPlayerMonitor', target='',
                                              last_scan_time=self.now)
        self.plan = {'classes': {'Mage': ['Fire']}, 'ranking_limit': 20,
                    'source_contract': 'top20-full-wcl-statistics-separate-v1'}
        self.key = 'Mage/Fire:profiles'

    def player(self, name, **extra):
        return dict(region='eu', realm='test', name=name, gear=[], talents=[],
                    profile_url=f'https://raider.io/characters/eu/test/{name}',
                    _class_name='Mage', _spec_name='Fire', **extra)

    def monitor(self):
        self.task.refresh_from_db()
        return SpecDetailPlayerMonitor(None, self.task)

    def cache_failure(self, player, delay):
        return RaiderIOProfileCache.objects.create(season=self.season,
            identity_digest=digest(player), next_attempt_at=self.now + delay)

    def partial(self, players):
        monitor = self.monitor()
        with patch.object(monitor, '_fetch_top_players', return_value=players):
            self.assertFalse(monitor.scan(''))
        return PlayerDailyCheckpoint.objects.get(task=self.task)

    def test_nineteen_successes_are_not_read_or_written_during_single_player_resume(self):
        players = [self.player(f'Player{i}') for i in range(20)]
        players[-1]['_profile_refresh_failed'] = True
        row = self.partial(players)
        pending = row.stages[self.key + ':pending']
        self.assertEqual((pending['players'], len(pending['remaining'])), (19, 1))
        self.assertEqual(set(pending['remaining'][0]), {'region', 'realm', 'name', 'profile_url'})
        old = list(PlayerSpecTopPlayer.objects.exclude(character_name='Player19').values())
        monitor = self.monitor()
        seen = []
        def enrich(rows):
            seen.extend(p['name'] for p in rows)
        with patch.object(monitor, '_fetch_top_players', side_effect=AssertionError('恢复不可重读榜单')), \
                patch.object(monitor, '_enrich_talents', side_effect=enrich), \
                patch.object(monitor, '_write_daily_profiles', wraps=monitor._write_daily_profiles) as write:
            self.assertTrue(monitor.scan(''))
        self.assertEqual(seen, ['Player19'])
        self.assertEqual(len(write.call_args.args[3]), 1)
        self.assertEqual(list(PlayerSpecTopPlayer.objects.exclude(character_name='Player19').values()), old)
        row.refresh_from_db()
        self.assertEqual(row.stages[self.key]['players'], 20)
        self.assertNotIn(self.key + ':pending', row.stages)

    def test_backoff_skips_enrichment_and_scheduler_waits_until_real_deadline(self):
        player = self.player('Waiting', _profile_refresh_failed=True)
        self.cache_failure(player, timedelta(hours=2))
        self.partial([player])
        self.task.refresh_from_db()
        self.assertEqual(parse_datetime(json.loads(self.task.flag)['retry_at']), self.now + timedelta(hours=2))
        self.assertIsNone(resume_due_at(self.task, self.now + timedelta(minutes=31)))
        self.assertEqual(resume_due_at(self.task, self.now + timedelta(hours=2)), self.now + timedelta(hours=2))
        monitor = self.monitor()
        with patch.object(monitor, '_fetch_top_players') as ranking, \
                patch.object(monitor, '_enrich_talents') as enrich, \
                patch.object(monitor, '_write_daily_profiles') as write:
            self.assertFalse(monitor.scan(''))
        ranking.assert_not_called()
        enrich.assert_not_called()
        write.assert_not_called()

    def test_ready_player_progresses_while_other_player_is_still_in_backoff(self):
        ready = self.player('Ready', _profile_refresh_failed=True)
        waiting = self.player('Waiting', _profile_refresh_failed=True)
        self.cache_failure(waiting, timedelta(hours=3))
        self.partial([ready, waiting])
        monitor = self.monitor()
        with patch.object(monitor, '_enrich_talents') as enrich:
            self.assertFalse(monitor.scan(''))
        self.assertEqual([p['name'] for p in enrich.call_args.args[0]], ['Ready'])
        pending = PlayerDailyCheckpoint.objects.get().stages[self.key + ':pending']
        self.assertEqual((pending['players'], [p['name'] for p in pending['remaining']]), (1, ['Waiting']))
        self.task.refresh_from_db()
        self.assertEqual(parse_datetime(json.loads(self.task.flag)['retry_at']), self.now + timedelta(hours=3))

    def test_source_backoff_and_bindings_use_same_retry_schedule(self):
        player = self.player('Bound')
        self.cache_failure(player, timedelta(hours=1))
        RaiderIOSourceBackoff.objects.create(key='character', until=self.now + timedelta(hours=4))
        progress = DailyProgress(self.task, self.season, self.plan)
        progress.mark(self.key, {'players': 0, 'identities': []})
        progress.finish(False, failed_bindings=[digest(player)])
        self.assertEqual(parse_datetime(json.loads(self.task.flag)['retry_at']), self.now + timedelta(hours=4))
        progress.finish(True)
        self.assertNotIn('bindings:pending', progress.stages)

    def test_bound_character_in_backoff_is_not_refreshed_when_other_work_wakes_task(self):
        from django.contrib.auth import get_user_model
        from botend.models import UserCharacterBinding
        user = get_user_model().objects.create_user(username='恢复验证')
        ref = self.player('Bound')
        player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu', realm='test',
            character_name='Bound', class_name='Mage', spec_name='Fire', profile_url=ref['profile_url'])
        UserCharacterBinding.objects.create(user=user, player=player, identity_digest=digest(ref),
            region='eu', realm_slug='test', character_name='Bound')
        self.cache_failure(ref, timedelta(hours=2))
        failures = []
        monitor = self.monitor()
        with patch.object(monitor, '_fetch_profile_with_activity', return_value=None) as fetch:
            refresh_bound_characters(monitor, self.season, on_failure=failures.append,
                                     defer_stats=True, respect_backoff=True)
            fetch.assert_not_called()
            self.assertEqual(failures, [digest(ref)])
            with patch('django.utils.timezone.now', return_value=self.now + timedelta(hours=2)):
                refresh_bound_characters(monitor, self.season, on_failure=failures.append,
                                         defer_stats=True, respect_backoff=True)
            fetch.assert_called_once()

    def test_unknown_unfinished_ranking_does_not_wait_for_other_players_long_backoff(self):
        player = self.player('Waiting', _profile_refresh_failed=True)
        self.cache_failure(player, timedelta(hours=12))
        plan = {**self.plan, 'classes': {'Mage': ['Fire', 'Frost']}}
        progress = DailyProgress(self.task, self.season, plan)
        progress.record_profiles(self.key, [player], set())
        progress.finish(False)
        self.assertEqual(json.loads(self.task.flag)['retry_at'], (self.now + timedelta(minutes=30)).isoformat())

    def test_failed_database_transaction_cannot_certify_successful_rows(self):
        monitor = self.monitor()
        original = monitor._save_profile_safely
        def save(profile):
            if profile.character_name == 'Fail':
                raise DatabaseError('模拟数据库失败')
            return original(profile)
        with patch.object(monitor, '_fetch_top_players', return_value=[self.player('Good'), self.player('Fail')]), \
                patch.object(monitor, '_save_profile_safely', side_effect=save), self.assertRaises(DatabaseError):
            monitor.scan('')
        self.assertEqual(PlayerSpecTopPlayer.objects.count(), 0)
        self.assertNotIn(self.key, PlayerDailyCheckpoint.objects.get().stages)

    def test_invalid_pending_state_falls_back_to_reading_ranking(self):
        self.partial([self.player('Failed', _profile_refresh_failed=True)])
        row = PlayerDailyCheckpoint.objects.get()
        row.stages[self.key + ':pending']['remaining'][0]['required_after'] = 'invalid'
        row.save(update_fields=['stages'])
        monitor = self.monitor()
        with patch.object(monitor, '_fetch_top_players', return_value=[self.player('Good')]) as fetch:
            self.assertTrue(monitor.scan(''))
        fetch.assert_called_once()

    def test_retry_cannot_accept_old_cache_after_losing_ranking_signal(self):
        profile = SimpleNamespace(season_id=self.season.pk, region='eu', realm='test',
                                  character_name='Retry', profile_url='https://raider.io/characters/eu/test/Retry')
        payload = {'name': 'Retry', 'region': 'eu', 'realm': 'test', 'profile_url': profile.profile_url}
        old_time = self.now - timedelta(hours=1)
        with patch('django.utils.timezone.now', return_value=old_time):
            ProfileCache().get(profile, self.season, lambda: payload,
                               signal=ranking_signal({'item_level': 100}))
        failure = Mock(return_value=None)
        self.assertIsNone(ProfileCache().get(profile, self.season, failure,
            signal=ranking_signal({'item_level': 200})))
        fetch = Mock(return_value=payload)
        self.assertIsNone(ProfileCache().get(profile, self.season, fetch, required_after=self.now))
        fetch.assert_not_called()
        with patch('django.utils.timezone.now', return_value=self.now + timedelta(hours=1)):
            self.assertEqual(ProfileCache().get(profile, self.season, fetch, required_after=self.now), payload)
            fetch.assert_called_once()
            fetch.reset_mock()
            self.assertEqual(ProfileCache().get(profile, self.season, fetch, required_after=self.now), payload)
            fetch.assert_not_called()

    def test_real_profile_cache_flow_resumes_only_failed_character_after_backoff(self):
        players = [self.player(f'Player{i}', item_level=200) for i in range(20)]
        failed = players[-1]
        profile = SimpleNamespace(season_id=self.season.pk, region='eu', realm='test',
            character_name=failed['name'], profile_url=failed['profile_url'])
        payload = {'name': failed['name'], 'region': 'eu', 'realm': 'test',
                   'profile_url': failed['profile_url']}
        with patch('django.utils.timezone.now', return_value=self.now - timedelta(hours=1)):
            ProfileCache().get(profile, self.season, lambda: payload,
                signal=ranking_signal({'item_level': 100}), signal_scope='Mage/Fire')
        collector = Mock()
        def first_refresh(player, season, **kwargs):
            if player.character_name == 'Player19':
                return {'status': 'http_error'}
            return {'status': 'ready', 'profile_payload': {
                'name': player.character_name, 'region': 'eu', 'realm': 'test',
                'profile_url': player.profile_url}}
        collector.refresh.side_effect = first_refresh
        monitor = self.monitor()
        with patch.object(monitor, '_fetch_world_top_players', return_value=players), \
                patch.object(monitor, '_get_activity_collector', return_value=collector), \
                patch.object(monitor, '_enrich_from_local_rankings'):
            self.assertFalse(monitor.scan(''))
        row = PlayerDailyCheckpoint.objects.get()
        ref = row.stages[self.key + ':pending']['remaining'][0]
        self.assertEqual(ref['required_after'], self.now.isoformat())
        self.assertEqual(row.stages[self.key + ':pending']['players'], 19)
        collector.refresh.reset_mock()
        collector.refresh.side_effect = lambda *a, **k: {'status': 'ready', 'profile_payload': payload}
        monitor = self.monitor()
        with patch('django.utils.timezone.now', return_value=self.now + timedelta(hours=2)), \
                patch.object(monitor, '_fetch_top_players', side_effect=AssertionError('不能重读榜单')), \
                patch.object(monitor, '_get_activity_collector', return_value=collector), \
                patch.object(monitor, '_enrich_from_local_rankings'):
            self.assertTrue(monitor.scan(''))
        collector.refresh.assert_called_once()
        self.assertEqual(collector.refresh.call_args.args[0].character_name, 'Player19')
        self.assertEqual(PlayerDailyCheckpoint.objects.get().stages[self.key]['players'], 20)
