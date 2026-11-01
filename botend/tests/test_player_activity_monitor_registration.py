"""Scheduling contract for lightweight, independently due character activity."""
import json
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase

from botend.models import MonitorTask, MonitorTaskLease, PlayerSpecTopPlayer, SeasonMeta
from botend.plugin_sync import (
    ISOLATED_MONITOR_TASKS, NIGHT_LANE_TASKS, PORTAL_DATA_SCHEDULED_TASKS,
    claim_next_monitor_task, monitor_default_wait_time,
    sync_monitortasks_from_plugin_list,
)

NAME = 'PlayerRaiderIOActivityMonitor'


class PlayerActivityMonitorRegistrationTests(TestCase):
    def setUp(self):
        MonitorTask.objects.all().delete()
        self.now = datetime(2026, 11, 1, 12, 0, tzinfo=ZoneInfo('Asia/Shanghai'))

    def plugin(self):
        from LMonitor.config import Monitor_Type_BaseObject_List
        matches = [p for p in Monitor_Type_BaseObject_List if p.__name__ == NAME]
        self.assertEqual(len(matches), 1, 'activity maintenance must be registered once')
        return matches[0], Monitor_Type_BaseObject_List

    def test_registered_at_end_without_repurposing_historical_types(self):
        plugin, plugins = self.plugin()
        self.assertIs(plugins[-1], plugin)
        self.assertEqual(plugins[-2].__name__, 'RaidBurstAggregationMonitor')
        self.assertEqual(plugins[15].__name__, 'DedicatedSimcWorkerSlot')
        self.assertFalse(plugin.requires_browser)
        self.assertTrue(plugin.default_is_active)

    def test_ten_minute_default_is_not_daily_or_an_independent_worker(self):
        self.assertEqual(monitor_default_wait_time(NAME), 600)
        self.assertNotIn(NAME, PORTAL_DATA_SCHEDULED_TASKS)
        self.assertNotIn(NAME, NIGHT_LANE_TASKS)
        self.assertNotIn(NAME, ISOLATED_MONITOR_TASKS)

    def test_sync_creates_enabled_task_and_preserves_operator_disable(self):
        plugin, plugins = self.plugin()
        sync_monitortasks_from_plugin_list(plugins)
        task = MonitorTask.objects.get(name=NAME)
        self.assertTrue(task.is_active)
        self.assertEqual(task.wait_time, 600)
        self.assertEqual(task.type, plugins.index(plugin))
        task.is_active = False
        task.wait_time = 1800
        task.save(update_fields=['is_active', 'wait_time'])
        sync_monitortasks_from_plugin_list(plugins)
        task.refresh_from_db()
        self.assertFalse(task.is_active)
        # Like the other ordinary plugins, startup sync owns the canonical
        # interval; an operator disable must nevertheless remain disabled.
        self.assertEqual(task.wait_time, 600)

    def task(self, name, type_id):
        return MonitorTask.objects.create(name=name, type=type_id, target='',
            is_active=True, last_scan_time=self.now-timedelta(hours=2),
            wait_time=monitor_default_wait_time(name))

    def test_automatic_activity_is_not_blocked_by_nightly_wcl_but_cannot_reenter(self):
        night = self.task('SpecDetailAggregationMonitor', 901)
        MonitorTaskLease.objects.create(task=night, owner='night-owner',
            claimed_at=self.now-timedelta(minutes=1), expires_at=self.now+timedelta(hours=1))
        activity = self.task(NAME, 902)
        claimed = claim_next_monitor_task(now=self.now, lease_owner='activity-owner',
            lease_seconds=1800, task_names={NAME})
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pk, activity.pk)
        self.assertIsNone(claim_next_monitor_task(now=self.now+timedelta(minutes=20),
            lease_owner='second', task_names={NAME}))
        self.assertEqual(MonitorTaskLease.objects.get(task=night).owner, 'night-owner')

    def test_disabled_activity_is_not_automatically_claimed(self):
        activity = self.task(NAME, 902)
        activity.is_active = False
        activity.save(update_fields=['is_active'])
        self.assertIsNone(claim_next_monitor_task(now=self.now, task_names={NAME}))

    def test_expected_anonymous_gap_is_reported_without_permanent_scan_failure(self):
        plugin, _ = self.plugin()
        season = SeasonMeta.objects.create(season_key='anonymous', season_name='Current',
            is_active=True, rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2)
        PlayerSpecTopPlayer.objects.create(season_id=season.pk, class_name='Priest', spec_name='Holy',
            rank=1, region='us', realm='anonymous', character_name='Anonymous',
            profile_url='https://raider.io/characters/us/anonymous/Anonymous')
        task = self.task(NAME, 902)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('no source request permitted')):
            self.assertTrue(plugin(None, task).scan(''))
        task.refresh_from_db()
        summary = json.loads(task.flag)
        self.assertEqual(summary['unresolved'], 1)
        self.assertEqual(summary['attempted'], 0)
        self.assertEqual(summary['ready'], 0)

    def test_rate_limit_survives_new_monitor_instance_and_respects_source_retry_after(self):
        from botend.tests.test_player_raiderio_activity_due import FixtureHTTP
        plugin, _ = self.plugin()
        season = SeasonMeta.objects.create(season_key='limited', season_name='Current',
            is_active=True, rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2)
        for rank, name in enumerate(('First', 'Second'), 1):
            PlayerSpecTopPlayer.objects.create(season_id=season.pk, class_name='Priest', spec_name='Holy',
                rank=rank, region='eu', realm='Test Realm', character_name=name,
                profile_url=f'https://raider.io/characters/eu/test-realm/{name}')
        task = self.task(NAME, 902)
        http = FixtureHTTP(status=429)  # Actual collector reads Retry-After: 900.
        with patch('requests.get', http), patch('django.utils.timezone.now', return_value=self.now):
            self.assertFalse(plugin(None, task).scan(''))
        self.assertEqual(len(http.calls), 1)
        task = MonitorTask.objects.get(pk=task.pk)  # Recreated worker, persisted state only.
        with patch('requests.get', http), patch('django.utils.timezone.now',
                return_value=self.now+timedelta(seconds=600)):
            self.assertTrue(plugin(None, task).scan(''), 'waiting for a known source reset is not a new failed attempt')
        self.assertEqual(len(http.calls), 1, 'a new batch must not hit another character before Retry-After')
        http.status = 200
        task = MonitorTask.objects.get(pk=task.pk)
        with patch('requests.get', http), patch('django.utils.timezone.now',
                return_value=self.now+timedelta(seconds=901)):
            self.assertTrue(plugin(None, task).scan(''))
        self.assertEqual(len(http.calls), 3)
