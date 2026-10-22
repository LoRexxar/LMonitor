from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from botend.analytics.models import SiteAnalyticsConfig, SitePageView
from botend.dashboard.home import home_context, _traffic
from botend.models import (DashboardUserGroup, DashboardUserGroupMembership, MonitorTask,
                           MonitorTaskLease, PlayerSpecTopPlayer, SimcBackendBinary, SimcTask, SystemAlert,
                           UserCharacterBinding, WowArticle, WowDailyReport, WowHotfixReport)


class DashboardHomeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user('HomePlayer')
        cls.other = get_user_model().objects.create_user('OtherPlayer')
        cls.admin = get_user_model().objects.create_superuser('HomeAdmin', password='test-password')

    def binding(self, user, name, active=True, with_player=True):
        player = PlayerSpecTopPlayer.objects.create(
            season_id=1, region='us', realm='Area 52', character_name=name,
            class_name='Warrior', spec_name='Fury', item_level=0, score=3210.5,
        ) if with_player else None
        return UserCharacterBinding.objects.create(user=user, character_name=name, region='us',
            realm_slug='area-52', identity_digest=name, player=player, is_active=active)

    def test_players_receive_only_own_roster_and_no_operations(self):
        self.binding(self.user, 'MyHomeCharacter')
        self.binding(self.user, 'ClosedCharacter', active=False, with_player=False)
        self.binding(self.other, 'PrivateOtherCharacter')
        SystemAlert.objects.create(category='SECRET', dedup_key='secret', title='InternalAlarm')
        self.client.force_login(self.user)
        with patch('botend.dashboard.home._operations', side_effect=AssertionError('player queried operations')):
            response = self.client.get('/dashboard/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['home']['kind'], 'player')
        self.assertEqual(response.context['dashboard_default_section'], 'dashboard-home')
        for text in ('MyHomeCharacter', 'ClosedCharacter', '3210.5', '0.0', 'id="sidebar"', '每日更新已开启', '更新已关闭'):
            self.assertContains(response, text)
        for text in ('PrivateOtherCharacter', 'InternalAlarm', '今日浏览量', '近 7 天访问趋势', '数据统计概览', '系统活跃度'):
            self.assertNotContains(response, text)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(self.client.get('/dashboard/?section=dashboard-home').status_code, 200)
        self.assertEqual(self.client.get('/dashboard/?section=character-bindings').status_code, 403)
        self.assertEqual(self.client.get('/dashboard/?section=database-tables').status_code, 403)

    def test_player_empty_and_group_member_still_default_to_player_home(self):
        group = DashboardUserGroup.objects.create(name='News editors', permission_codes=['news.index'])
        DashboardUserGroupMembership.objects.create(user=self.user, group=group)
        self.client.force_login(self.user)
        response = self.client.get('/dashboard/')
        self.assertContains(response, '绑定第一个角色')
        self.assertEqual(response.context['dashboard_default_section'], 'dashboard-home')
        self.assertEqual(response.context['home']['counts']['total'], 0)
        self.assertEqual(self.client.get('/dashboard/?section=news').status_code, 200)

    def test_admin_monitor_states_match_scheduler_and_real_data(self):
        now = timezone.now()
        common = {'target': 'private-credential-target', 'wait_time': 3600, 'last_scan_time': now-timedelta(hours=2)}
        MonitorTask.objects.create(name='DueMonitor', **common)
        running = MonitorTask.objects.create(name='RunningMonitor', **common)
        MonitorTaskLease.objects.create(task=running, owner='worker', expires_at=now+timedelta(minutes=5))
        MonitorTask.objects.create(name='DisabledMonitor', is_active=False, **common)
        MonitorTask.objects.create(name='ScheduledMonitor', target='', wait_time=3600, last_scan_time=now)
        MonitorTask.objects.create(name='RaidBurstAggregationMonitor', **common)
        SystemAlert.objects.create(category='TEST', dedup_key='fatal', title='FatalAlarm', level=3)
        SystemAlert.objects.create(category='TEST', dedup_key='read', title='ReadAlarm', is_read=True)
        WowArticle.objects.create(title_cn='最近资讯', publish_time=now)
        WowDailyReport.objects.create(report_date=now.date())
        WowHotfixReport.objects.create(summary_title='最新热修', build_num='12345')
        backend = SimcBackendBinary.objects.create(identifier='home-test', name='Test', simc_path='/tmp/simc')
        for status in (0, 1, 3):
            SimcTask.objects.create(user_id=self.admin.pk, name='Simulation', simc_profile_id=1, current_status=status, backend=backend)
        with patch('botend.dashboard.home.timezone.now', return_value=now):
            data = home_context(self.admin)
        self.assertEqual(data['monitor'], {'total': 5, 'enabled': 4, 'running': 1, 'due': 1, 'disabled': 1})
        self.assertEqual(data['tasks'][0]['name'], 'DueMonitor')
        self.assertEqual(data['simc'], {'pending': 1, 'running': 1, 'failed': 1})
        self.client.force_login(self.admin)
        response = self.client.get('/dashboard/')
        for text in ('站点概览', '今日浏览量', '近 7 天访问趋势', '热门页面', '正在执行', '待调度', '仅手动触发'):
            self.assertContains(response, text)
        for text in ('FatalAlarm', 'ReadAlarm', '最近资讯', '最新热修', 'private-credential-target', '数据统计概览', '微信文章总数', 'id="dh-alert-title"', 'id="dh-content-title"'):
            self.assertNotContains(response, text)

    @timezone.override('Asia/Shanghai')
    def test_traffic_local_days_distinct_visitors_and_bounded_queries(self):
        now = timezone.make_aware(datetime(2026, 10, 22, 0, 5))
        start = timezone.make_aware(datetime(2026, 10, 16))
        rows = [
            (now-timedelta(minutes=1), 'visitor-a', 'session-a', '/popular/'),
            (now-timedelta(minutes=2), 'visitor-a', 'session-b', '/popular/'),
            (now-timedelta(minutes=10), 'visitor-b', 'session-c', '/yesterday/'),
            (start, 'visitor-c', 'session-d', '/oldest/'),
            (start-timedelta(seconds=1), 'excluded', 'excluded', '/outside/'),
            (now+timedelta(seconds=1), 'future', 'future', '/future/'),
        ]
        for occurred_at, visitor, session, path in rows:
            SitePageView.objects.create(event_id=uuid4(), occurred_at=occurred_at,
                visitor_key=visitor, session_key=session, path=path)
        with self.assertNumQueries(3):
            data = _traffic(now)
        self.assertEqual((data['pv'], data['uv'], data['sessions'], data['active']), (2, 1, 2, 2))
        self.assertEqual(data['week_pv'], 4)
        self.assertEqual([day['pv'] for day in data['daily']], [1, 0, 0, 0, 0, 1, 2])
        self.assertEqual(data['pages'][0], {'path': '/popular/', 'pv': 2, 'uv': 1})
        self.assertEqual(data['timezone'], 'Asia/Shanghai')
        self.assertTrue(data['enabled'])

    def test_traffic_empty_paused_and_unavailable_states(self):
        self.client.force_login(self.admin)
        response = self.client.get('/dashboard/')
        self.assertContains(response, '近 7 天暂无访问记录')
        self.assertEqual(response.context['home']['traffic']['pv'], 0)
        SiteAnalyticsConfig.objects.create(pk=1, enabled=False)
        self.assertContains(self.client.get('/dashboard/'), '访问采集已暂停')
        # An unavailable query must not be mistaken for zero visits.
        with patch('django.db.models.query.QuerySet.aggregate', side_effect=OperationalError('unavailable')):
            self.assertEqual(_traffic(timezone.now()), {'unavailable': True})
        with patch('botend.dashboard.home._traffic', return_value={'unavailable': True}):
            self.assertContains(self.client.get('/dashboard/'), '访问统计暂不可用')

    def test_fixed_schedule_uses_scheduler_instead_of_wait_interval(self):
        now = timezone.now()
        MonitorTask.objects.create(name='WowTodayMonitor', target='', wait_time=86400, last_scan_time=now)
        with patch('botend.dashboard.home.monitor_task_due_at', return_value=now-timedelta(minutes=1)) as due:
            result = home_context(self.admin)
        due.assert_called_once()
        self.assertEqual(result['monitor']['due'], 1)
        self.assertEqual(result['tasks'][0]['interval'], '每日调度')

    def test_roster_bounded_and_deleted_profile_safe(self):
        for i in range(21):
            self.binding(self.user, f'Character{i}', with_player=False)
        with self.assertNumQueries(4):
            data = home_context(self.user)
        self.assertEqual(len(data['characters']), 20)
        self.assertEqual(data['counts']['total'], 21)
        self.assertTrue(data['has_more'])
        self.assertEqual(data['characters'][0].detail_url, '')

    def test_home_requires_login(self):
        self.assertEqual(self.client.get('/dashboard/').status_code, 302)
