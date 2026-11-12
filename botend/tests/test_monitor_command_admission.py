"""CLI entry points share real admission without consuming full daily plans."""
from contextlib import ExitStack
from datetime import datetime, timedelta
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from botend.models import MonitorTask, MonitorTaskLease, PlayerSpecTopPlayer, SeasonMeta


NOW = datetime(2026, 11, 11, 1, tzinfo=ZoneInfo('Asia/Shanghai'))
COMMANDS = (
    ('aggregate_spec_stats', 'SpecDetailAggregationMonitor',
     {'class_name': 'Monk', 'spec_name': 'Windwalker', 'dungeon_only': True, 'allow_legacy': True}),
    ('sync_rio_statistics', 'RioStatisticsMonitor', {}),
    ('refresh_spec_detail_players', 'SpecDetailPlayerMonitor',
     {'class_name': 'Monk', 'spec_name': 'Windwalker'}),
)


class MonitorCommandAdmissionTests(TestCase):
    def setUp(self):
        MonitorTask.objects.all().delete()
        SeasonMeta.objects.all().delete()
        self.enterContext(patch('botend.plugin_sync.timezone.now', return_value=NOW))
        # A leaked execution must never reach the network, including a failing RED.
        self.enterContext(patch('requests.sessions.Session.request', side_effect=AssertionError('no external HTTP')))
        self.season = SeasonMeta.objects.create(
            season_key='cli-admission', season_name='CLI', is_active=True,
            rio_season='season-cli', mplus_zone_id=1, raid_zone_id=2)

    def task(self, name, **kwargs):
        from LMonitor.config import Monitor_Type_BaseObject_List
        task_type = next((index for index, plugin in enumerate(Monitor_Type_BaseObject_List)
                          if plugin.__name__ == name), 900 + MonitorTask.objects.count())
        values = dict(target='', type=task_type, is_active=False,
                      wait_time=86400, last_scan_time=NOW + timedelta(days=1),
                      flag=json.dumps({'kind': 'player_daily', 'day': '2026-11-11',
                                       'state': 'partial', 'stages': {'Mage/Fire:profiles': {'players': 20}}}))
        values.update(kwargs)
        return MonitorTask.objects.create(name=name, **values)

    def lease(self, task, *, manual=False):
        return MonitorTaskLease.objects.create(task=task, owner='running-' + str(task.pk),
            is_manual=manual, claimed_at=NOW, expires_at=NOW + timedelta(hours=24))

    def assert_preserved(self, task, before):
        task.refresh_from_db()
        self.assertEqual((task.last_scan_time, task.flag), before)
        self.assertFalse(MonitorTaskLease.objects.filter(task=task).exists())

    def assert_live(self, name):
        task = MonitorTask.objects.get(name=name)
        lease = MonitorTaskLease.objects.get(task=task)
        self.assertTrue(lease.is_manual)
        self.assertGreater(lease.expires_at, NOW)
        return task, lease

    def test_all_commands_refuse_occupied_global_slots_before_execution(self):
        for command, name, options in COMMANDS:
            with self.subTest(command=command):
                MonitorTask.objects.all().delete()
                task = self.task(name)
                before = (task.last_scan_time, task.flag)
                for index, running in enumerate(('ngaMonitor', 'PortalPostMonitor', 'wowheadMonitor')):
                    self.lease(self.task(running), manual=index == 0)
                leases_before = list(MonitorTaskLease.objects.order_by('pk').values())
                with ExitStack() as stack:
                    collector = stack.enter_context(patch('botend.management.commands.sync_rio_statistics.Collector'))
                    collector.return_value.__enter__.return_value.run.return_value = {'state': 'complete'}
                    player = stack.enter_context(patch('botend.management.commands.refresh_spec_detail_players.SpecDetailPlayerMonitor'))
                    player.return_value._fetch_top_players.return_value = []
                    methods = [stack.enter_context(patch(
                        'botend.management.commands.aggregate_spec_stats.Command.' + method))
                        for method in ('_aggregate_dungeon', '_aggregate_raid', '_aggregate_leaderboard')]
                    with self.assertRaises(CommandError):
                        call_command(command, stdout=StringIO(), **options)
                    collector.assert_not_called()
                    player.assert_not_called()
                    for method in methods:
                        method.assert_not_called()
                self.assert_preserved(task, before)
                self.assertEqual(list(MonitorTaskLease.objects.order_by('pk').values()), leases_before)
                self.assertFalse(PlayerSpecTopPlayer.objects.exists())

    def test_rio_exact_cli_scope_result_and_partial_policy_preserve_daily_schedule(self):
        task = self.task('RioStatisticsMonitor')
        before = (task.last_scan_time, task.flag)
        queries = [{'dungeon': 'test-dungeon', 'include_classes': '8'}]
        expected = dict(season='season-cli', regions=['world'], modules=['heatmaps'],
                        dungeons=['test-dungeon'], levels=['10-12'], periods=['last-reset'],
                        queries=queries, only_queries=True)
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'queries.json'
            path.write_text(json.dumps(queries), encoding='utf-8')
            for result, allow_partial in (({'state': 'complete', 'ready': 7}, False),
                    ({'state': 'cooldown', 'retry_at': '2026-11-11T02:00:00+08:00'}, True),
                    ({'state': 'partial', 'remaining': 2}, False)):
                with self.subTest(result=result['state']):
                    out = StringIO()
                    with patch('botend.management.commands.sync_rio_statistics.Collector') as collector:
                        def run(**kwargs):
                            self.assert_live(task.name)
                            self.assertEqual(kwargs, expected)
                            return result
                        collector.return_value.__enter__.return_value.run.side_effect = run
                        options = dict(expected, query_file=str(path), max_requests=17, force=True,
                                       allow_partial=allow_partial, stdout=out)
                        options.pop('queries')
                        if result['state'] != 'complete' and not allow_partial:
                            with self.assertRaisesRegex(CommandError, '本轮尚未全部完成'):
                                call_command('sync_rio_statistics', **options)
                        else:
                            call_command('sync_rio_statistics', **options)
                        collector.assert_called_once_with(max_requests=17, force=True)
                        collector.return_value.__enter__.return_value.run.assert_called_once()
                    self.assertEqual(json.loads(out.getvalue()), result)
                    self.assert_preserved(task, before)

    def test_player_scoped_refresh_uses_real_claim_without_completing_daily_plan(self):
        task = self.task('SpecDetailPlayerMonitor')
        before = (task.last_scan_time, task.flag)
        module = 'botend.management.commands.refresh_spec_detail_players'
        def fetch(monitor, class_name, spec_name, season):
            _, lease = self.assert_live(task.name)
            self.assertIsInstance(monitor.task, MonitorTask)
            self.assertEqual(monitor.task.pk, task.pk)
            self.assertEqual(monitor.task._monitor_task_lease_owner, lease.owner)
            self.assertEqual((class_name, spec_name, season), ('Monk', 'Windwalker', 'season-cli'))
            return [{'region': 'cn', 'realm': '测试服', 'name': '人物', 'score': 1234}]
        out = StringIO()
        with patch(f'{module}.SpecDetailPlayerMonitor._fetch_top_players', autospec=True, side_effect=fetch), \
                patch(f'{module}.SpecDetailPlayerMonitor._crawl_battlenet_stats', autospec=True) as stats:
            call_command('refresh_spec_detail_players', class_name='Monk', spec_name='Windwalker', stdout=out)
            self.assertEqual(stats.call_args.args[1:], (self.season.pk,))
            self.assertEqual(stats.call_args.kwargs,
                             {'class_name': 'Monk', 'spec_name': 'Windwalker', 'retry_failed': True})
        row = PlayerSpecTopPlayer.objects.get(season_id=self.season.pk, spec_name='Windwalker')
        self.assertEqual((row.class_name, row.character_name, row.score), ('Monk', '人物', 1234))
        self.assertIn('已刷新 Monk/Windwalker 人物榜，共 1 条', out.getvalue())
        self.assert_preserved(task, before)

    def test_execution_failures_release_admission_without_consuming_daily_plan(self):
        for command, name, options in COMMANDS:
            with self.subTest(command=command):
                MonitorTask.objects.all().delete()
                task = self.task(name)
                before = (task.last_scan_time, task.flag)
                def fail(*args, **kwargs):
                    self.assert_live(task.name)
                    raise ValueError('source unavailable')
                def no_players(*args, **kwargs):
                    self.assert_live(task.name)
                    return []
                with ExitStack() as stack:
                    collector = stack.enter_context(patch('botend.management.commands.sync_rio_statistics.Collector'))
                    collector.return_value.__enter__.return_value.run.side_effect = fail
                    stack.enter_context(patch(
                        'botend.management.commands.refresh_spec_detail_players.SpecDetailPlayerMonitor._fetch_top_players',
                        side_effect=no_players))
                    stack.enter_context(patch(
                        'botend.management.commands.aggregate_spec_stats.Command._aggregate_dungeon', side_effect=fail))
                    error = ValueError if command == 'aggregate_spec_stats' else CommandError
                    with self.assertRaises(error):
                        call_command(command, stdout=StringIO(), **options)
                self.assert_preserved(task, before)
                self.assertFalse(PlayerSpecTopPlayer.objects.exists())

    def test_legacy_aggregation_keeps_scope_and_dungeon_only_under_real_lease(self):
        task = self.task('SpecDetailAggregationMonitor')
        before = (task.last_scan_time, task.flag)
        module = 'botend.management.commands.aggregate_spec_stats.Command'
        for dungeon_only in (True, False):
            with self.subTest(dungeon_only=dungeon_only):
                def dungeon(*args, **kwargs):
                    self.assert_live(task.name)
                    self.assertEqual(args[:3], (self.season, 'Monk', 'Windwalker'))
                    self.assertEqual(kwargs['publish'].keywords, {'legacy_season': self.season})
                    self.assertIsNone(kwargs['cycle'])
                out = StringIO()
                with patch(f'{module}._aggregate_dungeon', side_effect=dungeon) as dungeons, \
                        patch(f'{module}._aggregate_raid') as raids, \
                        patch(f'{module}._aggregate_leaderboard') as leaderboard:
                    call_command('aggregate_spec_stats', class_name='Monk', spec_name='Windwalker',
                                 season_id=self.season.pk, allow_legacy=True,
                                 dungeon_only=dungeon_only, stdout=out)
                    dungeons.assert_called_once()
                    self.assertEqual(raids.call_count, 0 if dungeon_only else 1)
                    self.assertEqual(leaderboard.call_count, 0 if dungeon_only else 1)
                    if not dungeon_only:
                        self.assertEqual(raids.call_args.args[:3], (self.season, 'Monk', 'Windwalker'))
                        self.assertEqual(leaderboard.call_args.args[:3], (self.season.pk, 'Monk', 'Windwalker'))
                self.assertIn('完成: {} 个文件'.format(1 if dungeon_only else 3), out.getvalue())
                self.assert_preserved(task, before)
