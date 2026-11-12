"""缓存收益、依赖屏障和配额的行为验证。"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, TestCase
from botend.models import MonitorTask
from botend.plugin_sync import claim_next_monitor_task, release_monitor_task_lease
from botend.services.player_normalization_cache import normalization_batch, normalized_view
from botend.wow.talents.view_model import build_talent_view_model
from botend.tests.test_rio_statistics import RioStatisticsTests
from botend.services import rio_statistics as rio


class NormalizationReuseTests(SimpleTestCase):
    def test_real_file_roundtrip_matches_uncached_nodes(self):
        import tempfile
        provider = SimpleNamespace(merge_into_node=lambda node, *args, **kwargs: {**node, 'name': '冰霜'})
        nodes = [{'talent_id': 1, 'spell_id': 2, 'points': 1, 'tree_type': 'spec', 'row': 1, 'column': 1}]
        expected = build_talent_view_model(nodes, 'Mage', 'Frost', metadata_provider=provider)['nodes']
        build = Mock(wraps=build_talent_view_model)
        with self.settings(SPEC_AGGREGATION_WORK_ROOT=tempfile.mkdtemp(prefix='player-normalization-')), \
             patch('botend.wow.talents.metadata.TalentMetadataProvider', return_value=provider):
            for _ in range(2):
                with normalization_batch(1, 'Mage', 'Frost'):
                    actual = normalized_view(nodes, 'Mage', 'Frost', build)
                    self.assertEqual(actual['nodes'], expected)
            self.assertEqual(build.call_count, 1)

    def test_persistent_reuse_effective_metadata_invalidation_and_copy_isolation(self):
        storage = {}
        revision = ['甲']
        provider = SimpleNamespace(merge_into_node=lambda node, *args, **kwargs: {**node, 'name': revision[0]})
        nodes = [{'talent_id': 1, 'spell_id': 2, 'points': 1, 'tree_type': 'spec', 'row': 1, 'column': 1}]
        build = Mock(wraps=build_talent_view_model)
        with patch('botend.services.spec_aggregation_cache.load_work', side_effect=lambda *args: storage.copy()), \
             patch('botend.services.spec_aggregation_cache.save_work', side_effect=lambda *args: storage.update(args[-1])), \
             patch('botend.wow.talents.metadata.TalentMetadataProvider', return_value=provider):
            with normalization_batch(1, 'Mage', 'Frost'):
                first = normalized_view(nodes, 'Mage', 'Frost', build)
                first['nodes'][0]['name'] = '不得污染缓存'
                self.assertEqual(normalized_view(nodes, 'Mage', 'Frost', build)['nodes'][0]['name'], '甲')
            with normalization_batch(1, 'Mage', 'Frost'):
                self.assertEqual(normalized_view(nodes, 'Mage', 'Frost', build)['nodes'][0]['name'], '甲')
            self.assertEqual(build.call_count, 1)
            revision[0] = '乙'
            with normalization_batch(1, 'Mage', 'Frost'):
                self.assertEqual(normalized_view(nodes, 'Mage', 'Frost', build)['nodes'][0]['name'], '乙')
            self.assertEqual(build.call_count, 2)


class ParallelNightTests(TestCase):
    def setUp(self):
        MonitorTask.objects.all().delete()
        self.now = datetime(2026, 11, 9, 1, tzinfo=ZoneInfo('Asia/Shanghai'))

    def task(self, name):
        return MonitorTask.objects.create(name=name, type=900+MonitorTask.objects.count(),
            target='', is_active=True, last_scan_time=self.now-timedelta(days=1))

    def test_observation_and_player_run_together_but_rankings_wait(self):
        rio_task = self.task('RioStatisticsMonitor')
        player = self.task('SpecDetailPlayerMonitor')
        ranking = self.task('SpecDetailRankingMonitor')
        self.assertEqual(claim_next_monitor_task(now=self.now, task_names={rio_task.name}).pk, rio_task.pk)
        claimed = claim_next_monitor_task(now=self.now)
        self.assertEqual(claimed.pk, player.pk)
        self.assertIsNone(claim_next_monitor_task(now=self.now))
        release_monitor_task_lease(player.pk, claimed._monitor_task_lease_owner)
        self.assertEqual(claim_next_monitor_task(now=self.now).pk, ranking.pk)

    def test_version_barrier_blocks_both_chains(self):
        version = self.task('WowDataVersionMonitor')
        self.task('RioStatisticsMonitor')
        self.task('SpecDetailPlayerMonitor')
        self.assertEqual(claim_next_monitor_task(now=self.now).pk, version.pk)
        self.assertIsNone(claim_next_monitor_task(now=self.now, task_names={'RioStatisticsMonitor'}))
        self.assertIsNone(claim_next_monitor_task(now=self.now))

    def test_three_short_tasks_fill_shared_pool_and_long_task_waits(self):
        shorts = [self.task(name) for name in
                  ('PortalPeakSpecRankMonitor', 'wowheadMonitor', 'PortalPostMonitor')]
        claims = [claim_next_monitor_task(now=self.now) for _ in range(3)]
        self.assertTrue(all(claims))
        self.assertCountEqual([row.pk for row in claims], [row.pk for row in shorts])
        self.assertIsNone(claim_next_monitor_task(now=self.now))
        player = self.task('SpecDetailPlayerMonitor')
        self.assertIsNone(claim_next_monitor_task(now=self.now))
        self.assertTrue(release_monitor_task_lease(claims[0].pk, claims[0]._monitor_task_lease_owner))
        self.assertEqual(claim_next_monitor_task(now=self.now).pk, player.pk)

    def test_night_ordinary_workers_poll_short_and_rio_tasks_from_shared_pool(self):
        short = self.task('PortalPeakSpecRankMonitor')
        observation = self.task('RioStatisticsMonitor')
        self.assertEqual(claim_next_monitor_task(now=self.now).pk, short.pk)
        self.assertEqual(claim_next_monitor_task(now=self.now).pk, observation.pk)
        self.assertIsNone(claim_next_monitor_task(now=self.now, task_names={short.name}))
        second_short = self.task('wowheadMonitor')
        self.assertEqual(claim_next_monitor_task(now=self.now).pk, second_short.pk)
        self.assertIsNone(claim_next_monitor_task(now=self.now))

    def test_short_polling_crosses_six_without_interrupting_long_lease(self):
        from botend.models import MonitorTaskLease
        player = self.task('SpecDetailPlayerMonitor')
        short = self.task('PortalPeakSpecRankMonitor')
        before = self.now.replace(hour=5, minute=59)
        # 用 UTC 时钟验证北京时间 06:00 前后短任务持续可领。
        claimed = claim_next_monitor_task(now=before.astimezone(ZoneInfo('UTC')),
            task_names={player.name}, lease_seconds=3600)
        self.assertEqual(claimed.pk, player.pk)
        self.assertEqual(claim_next_monitor_task(now=before.astimezone(ZoneInfo('UTC'))).pk, short.pk)
        after = before + timedelta(minutes=1)
        self.assertIsNone(claim_next_monitor_task(now=after, task_names={short.name}))
        second_short = self.task('wowheadMonitor')
        self.assertEqual(claim_next_monitor_task(now=after.astimezone(ZoneInfo('UTC'))).pk, second_short.pk)
        lease = MonitorTaskLease.objects.get(task_id=player.pk)
        self.assertEqual(lease.owner, claimed._monitor_task_lease_owner)
        self.assertEqual(lease.expires_at, before + timedelta(hours=1))

    def test_daytime_long_quota_leaves_two_shared_slots_for_short_work(self):
        from botend.models import MonitorTaskLease
        now = self.now.replace(hour=8)
        for name in ('PortalPeakSpecRankMonitor', 'PortalPostMonitor'):
            self.task(name)
        player = self.task('SpecDetailPlayerMonitor')
        ordinary = [claim_next_monitor_task(now=now) for _ in range(3)]
        self.assertTrue(all(ordinary))
        self.assertIn(player.pk, [row.pk for row in ordinary])
        observation = self.task('RioStatisticsMonitor')
        observation.last_scan_time = self.now
        observation.save(update_fields=['last_scan_time'])
        self.assertIsNone(claim_next_monitor_task(now=now, task_names={observation.name}))
        self.assertIsNone(claim_next_monitor_task(now=now))
        player_claim = next(row for row in ordinary if row.pk == player.pk)
        release_monitor_task_lease(player.pk, player_claim._monitor_task_lease_owner)
        self.assertEqual(claim_next_monitor_task(now=now, task_names={observation.name}).pk, observation.pk)
        self.assertIsNone(claim_next_monitor_task(now=now))
        self.assertEqual(MonitorTaskLease.objects.filter(expires_at__gt=now).count(), 3)

    def test_daytime_single_long_head_is_claimed_by_an_unfiltered_worker(self):
        player = self.task('SpecDetailPlayerMonitor')
        observation = self.task('RioStatisticsMonitor')
        observation.last_scan_time = self.now
        observation.save(update_fields=['last_scan_time'])
        now = self.now.replace(hour=6)
        # Player has waited since midnight; RIO already attempted at 01:00.
        self.assertEqual(claim_next_monitor_task(now=now).pk, player.pk)
        self.assertIsNone(claim_next_monitor_task(now=now))

    def test_crossing_six_drains_existing_long_tasks_before_claiming_another(self):
        from botend.models import MonitorTaskLease
        player = self.task('SpecDetailPlayerMonitor')
        observation = self.task('RioStatisticsMonitor')
        ranking = self.task('SpecDetailRankingMonitor')
        before = self.now.replace(hour=5, minute=59)
        player_claim = claim_next_monitor_task(now=before, lease_seconds=3600)
        rio_claim = claim_next_monitor_task(now=before, task_names={observation.name}, lease_seconds=3600)
        self.assertEqual(player_claim.pk, player.pk)
        after = before + timedelta(minutes=1)
        short = self.task('PortalPeakSpecRankMonitor')
        short_claim = claim_next_monitor_task(now=after)
        self.assertEqual(short_claim.pk, short.pk)
        second_short = self.task('wowheadMonitor')
        self.assertEqual(MonitorTaskLease.objects.filter(expires_at__gt=after).count(), 3)
        self.assertIsNone(claim_next_monitor_task(now=after))
        release_monitor_task_lease(player.pk, player_claim._monitor_task_lease_owner)
        self.assertEqual(claim_next_monitor_task(now=after).pk, second_short.pk)
        self.assertIsNone(claim_next_monitor_task(now=after))
        release_monitor_task_lease(observation.pk, rio_claim._monitor_task_lease_owner)
        self.assertEqual(claim_next_monitor_task(now=after).pk, ranking.pk)

    def test_night_cooldown_gap_allows_short_work(self):
        observation = self.task('RioStatisticsMonitor')
        observation.last_scan_time = self.now - timedelta(minutes=5)
        observation.save(update_fields=['last_scan_time'])
        short = self.task('PortalPeakSpecRankMonitor')
        self.assertEqual(claim_next_monitor_task(now=self.now).pk, short.pk)
        self.assertIsNone(claim_next_monitor_task(now=self.now, task_names={observation.name}))

    def test_retirement_short_only_admission_ignores_night_long_priority(self):
        self.task('SpecDetailPlayerMonitor')
        observation = self.task('RioStatisticsMonitor')
        claim_next_monitor_task(now=self.now)
        short = self.task('PortalPeakSpecRankMonitor')
        self.assertEqual(claim_next_monitor_task(now=self.now, allow_long_tasks=False).pk, short.pk)
        self.assertIsNone(claim_next_monitor_task(now=self.now, task_names={observation.name}, allow_long_tasks=False))

    def test_real_scan_keeps_claiming_short_work_during_long_task_retirement(self):
        from types import ModuleType
        from botend.backend_lifecycle import BackendLifecycle
        registry = ModuleType('LMonitor.config')
        registry.Monitor_Type_BaseObject_List = []
        with patch.dict('sys.modules', {'LMonitor.config': registry}):
            from botend.views import LMonitorCore
        life = BackendLifecycle(retire_after=0)
        player = self.task('SpecDetailPlayerMonitor')
        short = self.task('PortalPeakSpecRankMonitor')
        executed = []
        with life.monitor_admission() as ongoing:
            ongoing.classify(is_long=True)
            life.request_retirement()

            def execute(task, owner):
                executed.append(task.pk)
                release_monitor_task_lease(task.pk, owner)
                ongoing.classify(is_long=False)

            core = LMonitorCore(life)
            with patch.object(core, '_execute_claimed_task', side_effect=execute), \
                 patch('botend.plugin_sync.timezone.now', return_value=self.now), \
                 patch('botend.views.close_old_connections'):
                core.scan()
        self.assertEqual(executed, [short.pk])
        player.refresh_from_db()
        self.assertLess(player.last_scan_time, self.now)
        self.assertTrue(life.retiring)


class RankingFallbackTests(TestCase):
    def test_complete_profile_skips_sql_and_missing_profile_uses_exact_identity(self):
        from botend.models import SpecDungeonRanking, SeasonMeta
        from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
        monitor = SpecDetailPlayerMonitor(Mock(), Mock())
        monitor._activity_season = SeasonMeta.objects.create(pk=11, season_key='回填身份',
            season_name='测试', mplus_zone_id=1, raid_zone_id=2)
        player = dict(name='同名人物', region='eu', realm='A', _class_name='Mage', _spec_name='Frost',
                      gear=[{'id': 1}], talents=[{'talent_id': i+1, 'name': '天赋', 'icon': '图标'} for i in range(5)])
        with self.assertNumQueries(0):
            monitor._enrich_from_local_rankings([player])
        for season, region, realm in ((10, 'eu', 'A'), (11, 'us', 'A'), (11, 'eu', 'B'), (11, 'eu', 'A')):
            SpecDungeonRanking.objects.create(season_id=season, region=region, realm=realm,
                dungeon_id=1, character_name=player['name'], class_name='Mage', spec_name='Frost', dps=1,
                talents_json=[{'identity': [season, region, realm]}])
        player['talents'] = []
        with patch.object(monitor, '_normalize_talent_nodes', side_effect=lambda nodes, *args: nodes):
            monitor._enrich_from_local_rankings([player])
        self.assertEqual(player['talents'], [{'identity': [11, 'eu', 'A']}])


class ClosedWeekReuseTests(RioStatisticsTests):
    def test_closed_week_requires_unchanged_result_before_longer_recheck(self):
        c = self.heat_coordinate(period='last-reset')
        now = datetime(2026, 11, 9, 1, tzinfo=ZoneInfo('Asia/Shanghai'))
        with patch.object(rio.timezone, 'now', return_value=now):
            rio._publish(c, {'value': 1})
            _, first = rio.store().read(c)
            self.assertLess(rio._stamp(first['next_at'])-now, timedelta(days=1))
            rio._publish(c, {'value': 1})
            _, same = rio.store().read(c)
            self.assertEqual(rio._stamp(same['next_at'])-now, timedelta(days=3))
            rio._publish(c, {'value': 2})
            _, changed = rio.store().read(c)
            self.assertLess(rio._stamp(changed['next_at'])-now, timedelta(days=1))
