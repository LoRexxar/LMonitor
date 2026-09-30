"""MDT 自动更新的版本判定、发布事务、来源校验和调度回归。"""
import copy
import importlib
import json
import tempfile
import zipfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from botend.models import (
    MonitorTask, MonitorTaskLease, MonitorTaskLeaseLost,
    MythicDungeonDataVersion, MythicDungeonRoute, MythicDungeonRouteShare,
    MythicDungeonSpawn, MythicDungeonSyncRun, MythicPlannerConfig,
)
from botend.mythic_planner import auto_update as updater
from botend.mythic_planner.importer import import_mythic_dungeon_payload
from botend.mythic_planner.mdt_converter import build_payload, _spell_names
from botend.mythic_planner.services import serialize_catalog
from botend.plugin_sync import claim_monitor_task, monitor_default_wait_time, monitor_task_due_at


RELEASE = {'tag': '6.2.21', 'commit': 'a' * 40}


def package():
    payload = json.loads(updater.builtin_package_path().read_text(encoding='utf-8'))
    payload['dungeons'] = payload['dungeons'][1:2]
    payload['data_version']['metadata']['dungeon_selection_groups'] = []
    return payload


class MdtReleaseTests(SimpleTestCase):
    def test_semantic_tag_order_excludes_prereleases_and_peels_annotated_tag(self):
        client = updater.UpstreamClient()
        refs = [
            {'ref': f'refs/tags/{tag}', 'object': {'type': 'tag', 'sha': 'b' * 40}}
            for tag in ('6.2.9', '6.2.20', '6.2.21-alpha1', '6.2.22', '6.2.21')
        ]
        responses = {
            '/git/matching-refs/tags/': refs,
            '/releases/tags/6.2.22': None,
            '/releases/tags/6.2.21': {'tag_name': '6.2.21', 'prerelease': False, 'draft': False},
            '/git/tags/' + 'b' * 40: {'object': {'type': 'commit', 'sha': 'a' * 40}},
        }
        with mock.patch.object(client, 'json', side_effect=lambda path, **kw: responses[path]):
            self.assertEqual(client.latest_release(), RELEASE)
        client.session.close()

    def test_no_stable_release_is_an_error(self):
        client = updater.UpstreamClient()
        with mock.patch.object(client, 'json', return_value=[]):
            with self.assertRaisesRegex(ValueError, '正式发布'):
                client.latest_release()
        client.session.close()

    def test_archive_rejects_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.zip'
            for name in ('root/../../outside.txt', '/absolute.txt', 'root\\outside.txt'):
                with self.subTest(name=name):
                    with zipfile.ZipFile(path, 'w') as bundle:
                        bundle.writestr(name, '不允许写出目录')
                    if '\\' in name:
                        # Windows 的 ZipInfo 写入器会规范化反斜杠，改写实际归档字节构造外部输入。
                        path.write_bytes(path.read_bytes().replace(b'root/outside.txt', b'root\\outside.txt'))
                    with self.assertRaisesRegex(ValueError, '不安全路径'):
                        updater.extract_source(path, Path(directory) / 'result')
            with zipfile.ZipFile(path, 'w') as bundle:
                entry = zipfile.ZipInfo('root/LICENSE')
                entry.external_attr = 0o120777 << 16
                bundle.writestr(entry, '链接')
            with self.assertRaisesRegex(ValueError, '符号链接'):
                updater.extract_source(path, Path(directory) / 'result')

    def test_archive_extracts_only_data_not_executable_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.zip'
            with zipfile.ZipFile(path, 'w') as bundle:
                bundle.writestr('root/LICENSE', '许可')
                bundle.writestr('root/setup.py', '禁止执行')
                bundle.writestr('root/Locales/zhCN.lua', '中文数据')
            target = updater.extract_source(path, Path(directory) / 'result')
            self.assertTrue((target / 'LICENSE').exists())
            self.assertFalse((target / 'setup.py').exists())

    def test_poi_english_description_cannot_replace_ability_chinese(self):
        self.assertEqual(_spell_names(1, {1: {'description': 'English', 'description_zh': '中文说明'}})[2], '中文说明')

    def test_daily_interval_and_registration_are_idempotent(self):
        self.assertEqual(monitor_default_wait_time(updater.TASK_NAME), 86400)
        now = timezone.now()
        task = SimpleNamespace(name=updater.TASK_NAME, last_scan_time=now, wait_time=86400)
        self.assertIsNone(monitor_task_due_at(task, now + timedelta(hours=23)))
        self.assertIsNotNone(monitor_task_due_at(task, now + timedelta(days=1)))

    def test_new_monitor_is_appended_without_changing_existing_indexes(self):
        # Windows 下某些无关插件依赖 fcntl；直接核对注册声明即可。
        import ast
        from django.conf import settings
        tree = ast.parse((Path(settings.BASE_DIR) / 'LMonitor/config.py').read_text(encoding='utf-8-sig'))
        names = next([row.id for row in node.value.elts] for node in tree.body
                     if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'Monitor_Type_BaseObject_List' for t in node.targets))
        self.assertEqual(names[34:37], ['MaxrollClassGuideMonitor', 'AdventureJournalMonitor', updater.TASK_NAME])

    def test_preparation_preserves_audited_supplements_and_iconless_events(self):
        baseline = json.loads(updater.builtin_package_path().read_text(encoding='utf-8'))
        raw = copy.deepcopy(baseline)
        audited = [(d['key'], e) for d in baseline['dungeons'] for e in d['enemies']
                   if any(a['metadata'].get('source') == 'LMonitorAbilitySupplement' for a in e['abilities'])]
        # 上游已有部分原生技能时，不能丢弃人工审核的覆盖表。
        for dungeon in raw['dungeons']:
            for enemy in dungeon['enemies']:
                for ability in enemy['abilities']:
                    ability['metadata']['source'] = 'MythicDungeonTools'
        metadata = baseline['data_version']['metadata']
        release = {'tag': metadata['source_tag'], 'commit': metadata['source_commit']}
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(updater, 'build_payload', side_effect=[raw, baseline, baseline]),
            mock.patch.object(updater.WowSpellSnapshot.objects, 'filter') as query,
            mock.patch.object(updater, 'fetch_wowhead_tooltip') as fetch,
        ):
            query.return_value.filter.return_value.order_by.return_value = []
            updater.prepare_candidate(Path(directory), release, None, mock.Mock())
            overrides = json.loads((Path(directory) / 'LMonitor/ability_overrides.json').read_text(encoding='utf-8'))
            for key, enemy in audited:
                self.assertEqual(overrides['dungeons'][key]['enemies'][str(enemy['npc_id'])],
                                 [a['spell_id'] for a in enemy['abilities']])
            fetch.assert_not_called()


class MdtUpdateTests(TestCase):
    def setUp(self):
        self.original = package()
        import_mythic_dungeon_payload(self.original, activate=True, replace=True)
        self.version = MythicDungeonDataVersion.objects.get(key='mdt-6-2-20')
        self.candidate = copy.deepcopy(self.original)
        self.candidate['data_version'].update(key='mdt-6-2-21', label='MDT 自动更新验证')
        self.candidate['data_version']['metadata'].update(source_tag=RELEASE['tag'], source_commit=RELEASE['commit'])
        self.task = updater.get_mdt_monitor_task()
        self.fake_client = mock.Mock()
        self.fake_client.latest_release.return_value = RELEASE
        self.fake_client.download.return_value = Path('测试源目录')
        self.patches = [
            mock.patch.object(updater, 'UpstreamClient', return_value=self.fake_client),
            mock.patch.object(updater, 'prepare_candidate', side_effect=lambda *args: copy.deepcopy(self.candidate)),
            mock.patch.object(updater, 'publish_maps'),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        # 发布候选文件放在测试目录；不接触实际数据包和资源。
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.override = self.settings(BASE_DIR=Path(self.temp.name))
        self.override.enable()
        self.addCleanup(self.override.disable)

    def test_success_preserves_routes_short_links_manual_position_and_config(self):
        dungeon = self.version.dungeons.get()
        spawn = MythicDungeonSpawn.objects.filter(enemy__dungeon=dungeon).first()
        spawn.x = 12.5
        spawn.metadata = {**spawn.metadata, 'manual_position_override': True}
        spawn.save()
        data = {'version': 1, 'data_version_key': self.version.key, 'dungeon_key': dungeon.key}
        route = MythicDungeonRoute.objects.create(dungeon=dungeon, name='旧路线', route_data=data)
        share = MythicDungeonRouteShare.objects.create(dungeon=dungeon, name='旧短链', route_data=data, content_hash='c' * 64)
        config = MythicPlannerConfig.objects.get()
        config.default_dungeon_level = 15
        config.save()
        run = updater.sync_latest_mdt()
        for row in (self.version, route, share, spawn, config):
            row.refresh_from_db()
        self.assertEqual(run.status, 'updated')
        self.assertEqual(self.version.key, 'mdt-6-2-21')
        self.assertEqual(route.dungeon_id, dungeon.pk)
        self.assertEqual(share.route_data, data)
        self.assertEqual(spawn.x, 12.5)
        self.assertEqual(config.default_dungeon_level, 15)
        self.assertEqual(serialize_catalog()['version']['source_tag'], '6.2.21')
        self.assertFalse(MonitorTaskLease.objects.filter(task=self.task).exists())
        self.assertEqual(updater.sync_latest_mdt().status, 'unchanged')
        self.assertEqual(self.fake_client.download.call_count, 1)

    def test_same_version_does_not_download_or_import(self):
        self.fake_client.latest_release.return_value = {
            'tag': '6.2.20', 'commit': self.version.metadata['source_commit'],
        }
        self.assertEqual(updater.sync_latest_mdt().status, 'unchanged')
        self.fake_client.download.assert_not_called()

    def test_check_only_never_downloads_or_changes_version(self):
        self.assertEqual(updater.sync_latest_mdt(check_only=True).status, 'available')
        self.fake_client.download.assert_not_called()
        self.version.refresh_from_db()
        self.assertEqual(self.version.key, 'mdt-6-2-20')

    def test_changed_tag_commit_is_rejected(self):
        self.fake_client.latest_release.return_value = {'tag': '6.2.20', 'commit': 'f' * 40}
        with self.assertRaisesRegex(ValueError, '提交发生变化'):
            updater.sync_latest_mdt()
        self.assertEqual(MythicDungeonSyncRun.objects.first().status, 'failed')
        self.fake_client.download.assert_not_called()

    def test_older_remote_does_not_downgrade(self):
        self.fake_client.latest_release.return_value = {'tag': '6.2.19', 'commit': 'f' * 40}
        self.assertEqual(updater.sync_latest_mdt().status, 'unchanged')
        self.fake_client.download.assert_not_called()

    def test_source_failure_preserves_active_version_and_records_reason(self):
        self.fake_client.latest_release.side_effect = RuntimeError('网络失败')
        with self.assertRaisesRegex(RuntimeError, '网络失败'):
            updater.sync_latest_mdt()
        self.version.refresh_from_db()
        self.assertEqual(self.version.key, 'mdt-6-2-20')
        self.assertIn('网络失败', MythicDungeonSyncRun.objects.first().error)
        self.assertFalse(MonitorTaskLease.objects.filter(task=self.task).exists())

    def test_client_initialization_failure_is_recorded_and_releases_lock(self):
        with mock.patch.object(updater, 'UpstreamClient', side_effect=RuntimeError('请求初始化失败')):
            with self.assertRaisesRegex(RuntimeError, '初始化失败'):
                updater.sync_latest_mdt()
        self.assertEqual(MythicDungeonSyncRun.objects.first().status, 'failed')
        self.assertFalse(MonitorTaskLease.objects.filter(task=self.task).exists())

    def test_concurrent_reimport_prevents_publishing_stale_candidate(self):
        def change_version(*args):
            MythicDungeonDataVersion.objects.filter(pk=self.version.pk).update(imported_at=timezone.now())
        with mock.patch.object(updater, 'publish_maps', side_effect=change_version):
            with self.assertRaisesRegex(ValueError, '重新导入'):
                updater.sync_latest_mdt()
        self.version.refresh_from_db()
        self.assertEqual(self.version.key, 'mdt-6-2-20')

    def test_sync_records_are_read_only_in_dashboard(self):
        from botend.dashboard.dashboard import DashboardView
        entry = DashboardView()._get_model_registry()['MythicDungeonSyncRun']
        self.assertTrue(entry['can_read'])
        self.assertFalse(any(entry[key] for key in ('can_create', 'can_update', 'can_delete')))

    def test_asset_failure_preserves_active_version(self):
        with mock.patch.object(updater, 'publish_maps', side_effect=RuntimeError('地图上传失败')):
            with self.assertRaisesRegex(RuntimeError, '地图上传失败'):
                updater.sync_latest_mdt()
        self.version.refresh_from_db()
        self.assertEqual(self.version.key, 'mdt-6-2-20')

    def test_post_import_count_mismatch_rolls_back(self):
        with mock.patch.object(updater, 'database_counts', return_value={}):
            with self.assertRaisesRegex(ValueError, '已回滚'):
                updater.sync_latest_mdt()
        self.version.refresh_from_db()
        self.assertEqual(self.version.key, 'mdt-6-2-20')
        self.assertFalse(MythicDungeonDataVersion.objects.filter(key='mdt-6-2-21').exists())

    def test_second_pass_failure_rolls_back_real_publish(self):
        expected = updater.payload_counts(self.candidate)
        with mock.patch.object(updater, 'database_counts', side_effect=[expected, {}]):
            with self.assertRaisesRegex(ValueError, '已回滚'):
                updater.sync_latest_mdt()
        self.version.refresh_from_db()
        self.assertEqual(self.version.key, 'mdt-6-2-20')

    def test_already_running_task_is_not_started_twice(self):
        claimed = claim_monitor_task(self.task.pk)
        with self.assertRaises(MonitorTaskLeaseLost):
            updater.sync_latest_mdt()
        self.fake_client.latest_release.assert_not_called()
        self.assertTrue(MonitorTaskLease.objects.filter(owner=claimed._monitor_task_lease_owner).exists())

    def test_lost_lease_during_preparation_prevents_publish(self):
        def lose_lease(*args):
            MonitorTaskLease.objects.filter(task=self.task).update(owner='其他执行者')
        with mock.patch.object(updater, 'publish_maps', side_effect=lose_lease):
            with self.assertRaises(MonitorTaskLeaseLost):
                updater.sync_latest_mdt()
        self.version.refresh_from_db()
        self.assertEqual(self.version.key, 'mdt-6-2-20')

    def test_migration_preserves_existing_disabled_task_and_interval(self):
        self.task.is_active = False
        self.task.wait_time = 172800
        self.task.save()
        migration = importlib.import_module('botend.migrations.0231_mdt_auto_update')
        migration.register_monitor(apps, SimpleNamespace(connection=connection))
        self.task.refresh_from_db()
        self.assertFalse(self.task.is_active)
        self.assertEqual(self.task.wait_time, 172800)

    def test_validation_blocks_large_loss_and_missing_poi_description(self):
        broken = copy.deepcopy(self.candidate)
        broken['dungeons'][0]['enemies'] = broken['dungeons'][0]['enemies'][:1]
        with self.assertRaises(ValueError):
            updater.validate_candidate(broken, RELEASE, self.original)
        broken = copy.deepcopy(self.candidate)
        poi = next(p for f in broken['dungeons'][0]['floors'] for p in f['pois'] if p['type'] == 'genericItem')
        poi['metadata']['tooltip'] = {}
        with self.assertRaisesRegex(ValueError, '交互标记'):
            updater.validate_candidate(broken, RELEASE, self.original)

    def test_monitor_reports_failure_to_existing_alert_flow(self):
        from botend.controller.plugins.wow.MythicDungeonToolsMonitor import MythicDungeonToolsMonitor
        self.fake_client.latest_release.side_effect = RuntimeError('网络失败')
        monitor = MythicDungeonToolsMonitor(None, self.task)
        self.assertFalse(monitor.scan(self.task.target))
        self.assertIn('网络失败', monitor.last_error_detail)

    def test_command_uses_same_check_only_flow(self):
        call_command('update_mythic_dungeon_tools', check_only=True)
        self.assertEqual(MythicDungeonSyncRun.objects.first().status, 'available')
