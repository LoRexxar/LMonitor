"""真实检查点恢复、缓存复核和独立补采预算回归。"""
from datetime import timedelta
import json
from unittest.mock import Mock, patch

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from botend.models import (MonitorTask, MonitorTaskLease, MonitorTaskLeaseLost, PlayerDailyCheckpoint,
    SeasonMeta, PlayerSpecTopPlayer, CharacterEnrichmentTask, RaiderIOSourceBackoff)
from botend.services.player_daily_checkpoint import DailyProgress
from botend.plugin_sync import portal_data_task_due_at, claim_next_monitor_task, monitor_task_due_at
from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
from botend.services.character_enrichment import enqueue_battlenet_profiles, consume_enrichment, EnrichmentBudget

MODULE = 'botend.controller.plugins.portal.SpecDetailPlayerMonitor'


class DailyCheckpointTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='断点验证', season_name='测试',
            is_active=True, rio_season='season-test', mplus_zone_id=1, raid_zone_id=2)
        MonitorTask.objects.update(is_active=False)
        self.task = MonitorTask.objects.create(name='SpecDetailPlayerMonitor', target='', is_active=True,
                                              last_scan_time=timezone.now()-timedelta(days=1))

    def test_partial_scan_resumes_only_failed_specialization_and_completion_is_idempotent(self):
        player = {'region': 'eu', 'realm': 'test', 'name': 'Tester', 'gear': [], 'talents': []}
        with patch(MODULE + '.CLASS_SPEC_MAP', {'Mage': ['Fire', 'Frost']}), patch('time.sleep'), \
                patch.object(SpecDetailPlayerMonitor, '_backfill_ranking_sample_profiles') as samples:
            first = SpecDetailPlayerMonitor(None, self.task)
            with patch.object(first, '_fetch_top_players', side_effect=[[player], []]):
                self.assertFalse(first.scan(''))
            row = PlayerDailyCheckpoint.objects.get()
            self.assertIn('Mage/Fire:profiles', row.stages)
            self.assertNotIn('Mage/Frost:profiles', row.stages)
            samples.assert_not_called()
            second = SpecDetailPlayerMonitor(None, MonitorTask.objects.get(pk=self.task.pk))
            with patch.object(second, '_fetch_top_players', return_value=[dict(player, name='Other')]) as fetch:
                self.assertTrue(second.scan(''))
                self.assertTrue(second.scan(''))
            fetch.assert_called_once_with('Mage', 'Frost', self.season.rio_season)
            samples.assert_not_called()
            self.assertTrue(PlayerDailyCheckpoint.objects.get().complete)

    def test_scope_and_day_changes_reset_checkpoints(self):
        first = DailyProgress(self.task, self.season, {})
        first.mark('first')
        self.assertTrue(DailyProgress(self.task, self.season, {}).done('first'))
        self.season.game_build = 'changed'
        self.season.save(update_fields=['game_build'])
        with self.assertRaises(MonitorTaskLeaseLost):
            first.mark('stale')
        second = DailyProgress(self.task, self.season, {})
        self.assertFalse(second.done('first'))
        second.mark('second')
        with patch('django.utils.timezone.now', return_value=timezone.now() + timedelta(days=1)):
            self.assertFalse(DailyProgress(self.task, self.season, {}).done('second'))

    def test_expired_owner_and_replaced_generation_cannot_save_progress(self):
        first = DailyProgress(self.task, self.season, {})
        PlayerDailyCheckpoint.objects.update(generation='replacement')
        with self.assertRaises(MonitorTaskLeaseLost):
            first.mark('stale')
        self.task._monitor_task_lease_owner = 'old'
        MonitorTaskLease.objects.create(task=self.task, owner='new', claimed_at=timezone.now(),
                                       expires_at=timezone.now() + timedelta(minutes=10))
        with self.assertRaises(MonitorTaskLeaseLost):
            DailyProgress(self.task, self.season, {})

    def test_claim_crash_keeps_daytime_retry_eligible(self):
        now = timezone.localtime().replace(hour=8, minute=0, second=0, microsecond=0)
        claimed = claim_next_monitor_task(now=now)
        self.assertEqual(claimed.pk, self.task.pk)
        self.assertEqual(json.loads(claimed.flag)['state'], 'claimed')
        self.assertIsNone(portal_data_task_due_at(claimed, now=now + timedelta(minutes=29)))
        self.assertIsNotNone(portal_data_task_due_at(claimed, now=now + timedelta(minutes=31)))

    def test_failed_write_is_not_certified(self):
        monitor = SpecDetailPlayerMonitor(None, self.task)
        player = {'region': 'eu', 'realm': 'test', 'name': 'Tester', 'gear': [], 'talents': []}
        with patch(MODULE + '.CLASS_SPEC_MAP', {'Mage': ['Fire']}), patch('time.sleep'), \
                patch.object(monitor, '_fetch_top_players', return_value=[player]), \
                patch.object(monitor, '_save_profile_safely', side_effect=RuntimeError('写入失败')):
            self.assertFalse(monitor.scan(''))
        self.assertNotIn('Mage/Fire:profiles', PlayerDailyCheckpoint.objects.get().stages)

    def test_anonymous_source_is_unavailable_without_endless_daily_retry(self):
        monitor = SpecDetailPlayerMonitor(None, self.task)
        player = {'region': 'eu', 'realm': 'anonymous', 'name': 'Hidden', 'gear': [], 'talents': []}
        with patch.object(monitor, '_fetch_profile_with_activity', side_effect=AssertionError('匿名人物不可回源')):
            monitor._enrich_talents([player])
        self.assertTrue(player['_profile_refresh_unavailable'])
        _, identities, failed = monitor._write_daily_profiles(self.season, 'Mage', 'Fire', [player])
        self.assertEqual((identities, failed), (set(), 0))

    def test_bound_character_checkpoint_retries_only_failed_identity(self):
        from django.contrib.auth import get_user_model
        from botend.models import UserCharacterBinding
        from botend.raiderio_identity import activity_identity_digest
        from botend.services.character_bindings import refresh_bound_characters
        user = get_user_model().objects.create_user(username='checkpoint')
        digests = []
        for name in ('Good', 'Retry'):
            player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu', realm='test',
                character_name=name, class_name='Mage', spec_name='Fire',
                profile_url=f'https://raider.io/characters/eu/test/{name}')
            digest = activity_identity_digest(region='eu', realm_slug='test', character_name=name.lower())
            digests.append(digest)
            UserCharacterBinding.objects.create(user=user, player=player, identity_digest=digest,
                region='eu', realm_slug='test', character_name=name)
        progress = DailyProgress(self.task, self.season, {})
        failures = []
        monitor = SpecDetailPlayerMonitor(None, self.task)
        with patch.object(monitor, '_fetch_profile_with_activity',
                          side_effect=lambda p, s: {'name': p.character_name} if p.character_name == 'Good' else None):
            refresh_bound_characters(monitor, self.season, on_complete=lambda d: progress.mark(d),
                                     on_failure=failures.append, defer_stats=True)
        self.assertEqual(failures, [digests[1]])
        resumed = DailyProgress(self.task, self.season, {})
        with patch.object(monitor, '_fetch_profile_with_activity', return_value={'name': 'Retry'}) as fetch:
            refresh_bound_characters(monitor, self.season, completed=resumed.stages,
                                     on_complete=lambda d: resumed.mark(d), defer_stats=True)
        fetch.assert_called_once()
        self.assertEqual(set(resumed.stages), set(digests))

    def test_completed_scope_change_can_be_scheduled_again(self):
        from botend.controller.plugins.portal.SpecDetailPlayerMonitor import CLASS_SPEC_MAP
        plan = {'classes': CLASS_SPEC_MAP, 'ranking_limit': SpecDetailPlayerMonitor.PLAYER_RANKING_LIMIT,
                'source_contract': 'top20-full-wcl-statistics-separate-v1'}
        now = timezone.localtime().replace(hour=8, minute=0, second=0, microsecond=0)
        self.task.last_scan_time = now
        with patch('django.utils.timezone.now', return_value=now):
            progress = DailyProgress(self.task, self.season, plan)
            progress.finish(True)
        self.assertIsNone(portal_data_task_due_at(self.task, now=now+timedelta(hours=1)))
        self.season.game_build = 'new'
        self.season.save(update_fields=['game_build'])
        self.assertIsNotNone(portal_data_task_due_at(self.task, now=now+timedelta(hours=1)))


class ShortWriteTransactionTests(TransactionTestCase):
    def test_normalization_is_outside_transaction_and_keeps_latest_stats_and_rank(self):
        from django.db import connection
        season = SeasonMeta.objects.create(season_key='短事务', season_name='测试', is_active=True, mplus_zone_id=1, raid_zone_id=2)
        player = PlayerSpecTopPlayer.objects.create(season_id=season.pk, region='eu', realm='test',
            character_name='Tester', class_name='Mage', spec_name='Fire', rank=3, score=999,
            stats_json={'haste': 1}, stats_crawl_status=1)
        monitor = SpecDetailPlayerMonitor(None, None)
        def normalize(values):
            self.assertFalse(connection.in_atomic_block)
            PlayerSpecTopPlayer.objects.filter(pk=player.pk).update(stats_json={'haste': 2})
            return values
        with patch.object(monitor, '_normalize_gear_list', side_effect=normalize):
            _, _, failed = monitor._write_daily_profiles(season, 'Mage', 'Fire', [
                {'region': 'eu', 'realm': 'test', 'name': 'Tester', 'gear': [], 'talents': []}])
        self.assertEqual(failed, 0)
        player.refresh_from_db()
        self.assertEqual((player.rank, player.score, player.stats_json), (3, 999, {'haste': 2}))


class BattlenetQueueTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='属性队列', season_name='测试', is_active=True, rio_season='test', mplus_zone_id=1, raid_zone_id=2)
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu', realm='test',
            character_name='Tester', class_name='Mage', spec_name='Fire', rank=1)

    def test_oauth_is_budgeted_and_rio_cooldown_does_not_block_stats(self):
        enqueue_battlenet_profiles(self.season, [self.player])
        RaiderIOSourceBackoff.objects.create(key='character', until=timezone.now() + timedelta(hours=1))
        get = Mock(return_value=Mock(status_code=200, json=Mock(return_value={'health': 100})))
        post = Mock(return_value=Mock(status_code=200, json=Mock(return_value={'access_token': 'test', 'expires_in': 3600})))
        with self.settings(BATTLENET_CONFIG={'client_id': 'test', 'client_secret': 'test'}), patch('time.sleep'):
            small = EnrichmentBudget(get, post_transport=post, max_requests=1)
            self.assertEqual(consume_enrichment(self.season, small)['attempted'], 0)
            budget = EnrichmentBudget(get, post_transport=post, max_requests=2)
            self.assertEqual(consume_enrichment(self.season, budget, allow_rio=False)['ready'], 1)
        self.assertEqual(budget.requests, 2)
        get.assert_called_once()
        post.assert_called_once()
        self.player.refresh_from_db()
        self.assertEqual(self.player.stats_crawl_status, 1)
        row = CharacterEnrichmentTask.objects.get()
        self.assertGreater(row.due_at, timezone.now() + timedelta(hours=23))

    def test_batch_unchanged_enrollment_uses_one_query_and_keeps_due(self):
        players = [self.player]
        for index in range(7):
            players.append(PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu', realm='test',
                character_name=f'角色{index}', class_name='Mage', spec_name='Fire'))
        enqueue_battlenet_profiles(self.season, players)
        before = list(CharacterEnrichmentTask.objects.values())
        with self.assertNumQueries(1):
            enqueue_battlenet_profiles(self.season, players)
        self.assertEqual(list(CharacterEnrichmentTask.objects.values()), before)

    def test_new_stats_candidates_wake_task_despite_stale_rio_summary(self):
        now = timezone.now()
        task = MonitorTask(name='PlayerRaiderIOActivityMonitor', wait_time=600,
            last_scan_time=now-timedelta(minutes=2), flag=json.dumps({
                'rate_limited_until': (now+timedelta(hours=1)).isoformat(), 'enrichment': {'battlenet_due': 0}}))
        self.assertIsNone(monitor_task_due_at(task, now=now))
        enqueue_battlenet_profiles(self.season, [self.player])
        self.assertIsNotNone(monitor_task_due_at(task, now=timezone.now()))

    def test_failure_preserves_last_good_stats_and_backoff(self):
        self.player.stats_json = {'haste': 123}
        self.player.save()
        enqueue_battlenet_profiles(self.season, [self.player])
        get = Mock(return_value=Mock(status_code=503))
        post = Mock(return_value=Mock(status_code=200, json=Mock(return_value={'access_token': 'test', 'expires_in': 3600})))
        with self.settings(BATTLENET_CONFIG={'client_id': 'test', 'client_secret': 'test'}), patch('time.sleep'):
            first = consume_enrichment(self.season, EnrichmentBudget(get, post_transport=post))
            self.assertEqual(first['error'], 1)
            self.assertEqual(consume_enrichment(self.season, EnrichmentBudget(get, post_transport=post))['attempted'], 0)
        self.player.refresh_from_db()
        self.assertEqual(self.player.stats_json, {'haste': 123})
