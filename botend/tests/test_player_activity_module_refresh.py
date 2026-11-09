"""活动模块增量刷新：真实数据库、请求字段和成功时钟的边界回归。"""
import copy
from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from django.test import TestCase
from django.utils import timezone

from botend.models import RaiderIOCharacterActivity
from botend.services.player_raiderio_activity import (
    ActivityCollector, MODULES, REFRESH_META, TTL, mplus_modules_ready, refresh_due_top20_activity,
)
from botend.tests import test_player_raiderio_activity as activity_fixtures
from botend.tests.test_player_raiderio_activity import payload


class ActivityModuleRefreshTests(TestCase):
    make_player = activity_fixtures.PlayerActivityTests.make_player
    setUp = activity_fixtures.PlayerActivityTests.setUp

    def start(self, missing=()):
        self.now = timezone.now()
        self.data = payload()
        self.data['mythic_plus_best_runs'] = []
        for field in missing:
            self.data.pop(field)
        self.http = Mock(side_effect=lambda *args, **kwargs: Mock(
            status_code=200, headers={}, json=lambda: copy.deepcopy(self.data)))
        self.collector = ActivityCollector(transport=self.http, defer_enrichment=True)
        return self.refresh(self.now)

    def refresh(self, stamp, **kwargs):
        with patch('botend.services.player_raiderio_activity.timezone.now', return_value=stamp):
            result = self.collector.refresh(self.player, self.season, **kwargs)
        self.row = RaiderIOCharacterActivity.objects.get()
        return result

    def fields(self):
        return self.http.call_args.kwargs['params']['fields'].split(',')

    def test_missing_module_retry_reuses_other_clocks_and_completes(self):
        self.assertEqual(self.start(['mythic_plus_recent_runs'])['status'], 'partial')
        clocks = dict(self.row.module_fetched_at)
        self.data['mythic_plus_recent_runs'] = []
        retry = self.now + timedelta(hours=1)
        self.assertEqual(self.refresh(retry)['status'], 'ready')
        self.assertEqual(self.fields(), ['mythic_plus_recent_runs', 'mythic_plus_scores_by_season:current'])
        self.assertEqual({key: self.row.module_fetched_at[key] for key in clocks}, clocks)
        self.assertEqual(self.row.next_attempt_at, self.now + TTL)
        with patch('botend.services.player_raiderio_activity.timezone.now', return_value=retry):
            self.assertTrue(mplus_modules_ready(self.row))
        self.assertEqual(self.refresh(retry + timedelta(minutes=1))['status'], 'fresh')
        self.assertEqual(self.http.call_count, 2)

    def test_expired_module_does_not_shorten_other_modules_retry_backoff(self):
        self.start(['mythic_plus_recent_runs'])
        old_best = self.now - timedelta(hours=23, minutes=30)
        self.row.module_fetched_at['mythic_plus_best_runs'] = old_best.isoformat()
        self.row.next_attempt_at = self.now + timedelta(minutes=30)
        self.row.save()
        # 未请求的字段即使随响应出现，也不能提前发布或清除重试状态。
        self.data['mythic_plus_recent_runs'] = []
        self.assertEqual(self.refresh(self.now + timedelta(minutes=30))['status'], 'partial')
        self.assertEqual(self.fields(), ['mythic_plus_best_runs:all', 'mythic_plus_scores_by_season:current'])
        self.assertNotIn('mythic_plus_recent_runs', self.row.payload_json)
        self.assertEqual(self.row.next_attempt_at, self.now + timedelta(hours=1))
        self.assertEqual(self.refresh(self.now + timedelta(hours=1))['status'], 'ready')
        self.assertEqual(self.fields()[0], 'mythic_plus_recent_runs')

    def test_forced_missing_module_cannot_borrow_fresh_last_good(self):
        self.start()
        clocks = dict(self.row.module_fetched_at)
        self.data.pop('mythic_plus_recent_runs')
        attempt = self.now + timedelta(minutes=1)
        self.assertEqual(self.refresh(attempt, force=True)['status'], 'partial')
        self.assertEqual(self.row.module_fetched_at['mythic_plus_recent_runs'], clocks['mythic_plus_recent_runs'])
        with patch('botend.services.player_raiderio_activity.timezone.now', return_value=attempt):
            self.assertFalse(mplus_modules_ready(self.row))
        self.assertEqual(self.refresh(attempt + timedelta(minutes=1))['status'], 'backoff')
        self.data['mythic_plus_recent_runs'] = []
        self.assertEqual(self.refresh(attempt + timedelta(hours=1))['status'], 'ready')
        self.assertEqual(self.fields()[0], 'mythic_plus_recent_runs')
        self.assertEqual(len(self.fields()), 2)

    def test_transport_failure_retries_only_requested_module_and_keeps_clocks(self):
        self.start(['mythic_plus_recent_runs'])
        clocks = dict(self.row.module_fetched_at)
        self.http.side_effect = requests.ConnectionError('测试断网')
        self.assertEqual(self.refresh(self.now + timedelta(hours=1))['status'], 'transport_error')
        self.assertEqual(self.row.module_fetched_at, clocks)
        retry = self.row.next_attempt_at
        self.assertEqual(self.refresh(retry - timedelta(seconds=1))['status'], 'backoff')
        self.data['mythic_plus_recent_runs'] = []
        self.http.side_effect = lambda *a, **k: Mock(status_code=200, headers={}, json=lambda: self.data)
        self.assertEqual(self.refresh(retry)['status'], 'ready')
        self.assertEqual(self.fields(), ['mythic_plus_recent_runs', 'mythic_plus_scores_by_season:current'])

    def test_crashed_attempt_lease_recovers_only_pending_module(self):
        self.start(['mythic_plus_recent_runs'])
        retry = self.now + timedelta(hours=1)
        self.http.side_effect = RuntimeError('模拟进程中断')
        with self.assertRaises(RuntimeError):
            self.refresh(retry)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'fetching')
        lease = self.row.next_attempt_at
        self.assertEqual(self.refresh(retry + timedelta(seconds=1), force=True)['status'], 'busy')
        self.data['mythic_plus_recent_runs'] = []
        self.http.side_effect = lambda *a, **k: Mock(status_code=200, headers={}, json=lambda: self.data)
        self.assertEqual(self.refresh(lease)['status'], 'ready')
        self.assertEqual(self.fields()[0], 'mythic_plus_recent_runs')
        self.assertEqual(len(self.fields()), 2)

    def test_profile_and_due_module_still_share_one_request(self):
        self.start(['mythic_plus_recent_runs'])
        self.data['gear'] = {'items': {}}
        self.data['talents'] = []
        self.data['mythic_plus_recent_runs'] = []
        self.http.reset_mock()
        result = self.refresh(self.now + timedelta(hours=1), include_profile=True)
        self.assertEqual(result['status'], 'ready')
        self.assertIn('gear', result['profile_payload'])
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.fields(), ['gear', 'talents', 'mythic_plus_recent_runs', 'mythic_plus_scores_by_season:current'])

    def test_unrequested_bad_data_cannot_replace_good_modules(self):
        self.start(['mythic_plus_recent_runs'])
        self.data['mythic_plus_best_runs'] = '非本次请求的数据'
        self.data['mythic_plus_recent_runs'] = []
        self.assertEqual(self.refresh(self.now + timedelta(hours=1))['status'], 'ready')
        self.assertEqual(self.row.payload_json['mythic_plus_best_runs'], [])

    def test_unresolved_raid_does_not_repeat_fresh_mplus_requests(self):
        with patch('botend.services.player_raid_progression.bound_scope', side_effect=ValueError('团本引用未就绪')):
            self.assertEqual(self.start()['status'], 'partial')
            clocks = dict(self.row.module_fetched_at)
            self.assertEqual(self.refresh(self.now + timedelta(hours=1))['status'], 'backoff')
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.row.module_fetched_at, clocks)
        self.assertNotIn('raid_progression:current-tier', self.fields())

    def test_invalid_identity_cannot_publish_selected_module(self):
        self.start(['mythic_plus_recent_runs'])
        self.data['name'] = 'Wrong'
        self.data['profile_url'] = 'https://raider.io/characters/eu/test-realm/Wrong'
        self.data['mythic_plus_recent_runs'] = []
        self.assertEqual(self.refresh(self.now + timedelta(hours=1))['status'], 'invalid_payload')
        self.assertNotIn('mythic_plus_recent_runs', self.row.payload_json)
        self.assertIn('mythic_plus_recent_runs', self.row.payload_json[REFRESH_META]['pending'])

    def test_expired_modules_are_combined_and_force_refresh_keeps_full_scope(self):
        self.start()
        self.assertEqual(self.refresh(self.now + TTL)['status'], 'ready')
        self.assertEqual(len(self.fields()), len(MODULES) + 1)
        self.assertEqual(self.refresh(self.now + TTL + timedelta(minutes=1), force=True)['status'], 'ready')
        self.assertEqual(len(self.fields()), len(MODULES) + 1)

    def test_due_monitor_counts_actual_requested_modules(self):
        self.start(['mythic_plus_recent_runs'])
        self.data['mythic_plus_recent_runs'] = []
        with patch('botend.services.player_raiderio_activity.timezone.now', return_value=self.now + timedelta(hours=1)):
            result = refresh_due_top20_activity(self.season, collector=self.collector)
        self.assertEqual(result['module_requests'], {'mythic_plus_recent_runs': 1})
        self.assertEqual((result['ready'], result['partial'], result['remaining_due']), (1, 0, 0))
