"""受管来源的两份保留、引用保护及真实跨进程锁回归。"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Thread
from unittest.mock import patch
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from botend.models import WowTalentVersion, WowDataUpdateState
from botend.services.download_checkpoint import encoded
from botend.services.talent_source_retention import (
    managed_source_matches, prune_talent_sources, retained_sources, source_access, source_root,
)


class TalentSourceRetentionTests(TestCase):
    def setUp(self):
        # 所有实际删除只发生在项目内明确创建并验证过的测试沙箱。
        parent = Path(settings.BASE_DIR).resolve() / '.cache' / 'retention-tests'
        parent.mkdir(parents=True, exist_ok=True)
        sandbox = tempfile.TemporaryDirectory(dir=parent)
        self.base = Path(sandbox.name).resolve()
        self.assertTrue(self.base.is_relative_to(parent))
        self.addCleanup(sandbox.cleanup)
        override = override_settings(BASE_DIR=self.base)
        override.enable()
        self.addCleanup(override.disable)
        self.current = self.make_source('当前', 'a')
        self.previous = self.make_source('上一份', 'b')
        self.old = self.make_source('更旧', 'a')
        self.version, _ = WowTalentVersion.objects.update_or_create(key='retail', defaults={
            'branch': 'retail', 'source_dir': str(self.current), 'is_active': True})
        self.state = WowDataUpdateState.objects.create(branch='retail', report={
            'talent_sources': {'current': str(self.current), 'previous': str(self.previous)}})

    def make_source(self, label, slot, branch='retail'):
        data = label.encode('utf-8')
        manifest = {'TraitNode': {'sha256': hashlib.sha256(data).hexdigest(),
                                 'identity': {'branch': branch, 'build': label}}}
        digest = hashlib.sha256(encoded(manifest)).hexdigest()
        path = source_root() / branch / slot / 'talents' / digest
        path.mkdir(parents=True)
        (path / 'manifest.json').write_bytes(encoded(manifest))
        (path / 'TraitNode.csv').write_bytes(data)
        return path

    def test_only_unreferenced_old_source_is_removed_and_repeat_is_noop(self):
        report = prune_talent_sources('retail')
        self.assertEqual(report['removed'], [str(self.old)])
        self.assertTrue(self.current.is_dir())
        self.assertTrue(self.previous.is_dir())
        self.assertFalse(self.old.exists())
        self.assertEqual(prune_talent_sources('retail')['removed'], [])

    def test_inactive_database_reference_delays_cleanup_until_released(self):
        row = WowTalentVersion.objects.create(key='引用旧目录', branch='retail', is_active=False, source_dir=str(self.old))
        self.assertEqual(prune_talent_sources('retail')['removed'], [])
        row.source_dir = ''
        row.save(update_fields=['source_dir'])
        self.assertEqual(prune_talent_sources('retail')['removed'], [str(self.old)])

    def test_nested_file_reference_and_other_branch_are_protected(self):
        WowTalentVersion.objects.create(key='引用文件', branch='retail', source_dir=str(self.old / 'TraitNode.csv'))
        ptr = self.make_source('测试服', 'a', 'ptr')
        self.assertEqual(prune_talent_sources('retail')['removed'], [])
        self.assertTrue(ptr.is_dir())

    def test_unknown_file_prevents_deleting_directory(self):
        (self.old / 'manual.txt').write_text('不能误删', encoding='utf-8')
        report = prune_talent_sources('retail')
        self.assertEqual(report['removed'], [])
        self.assertIn(str(self.old), report['blocked'])

    def test_unproven_initial_history_is_not_guessed(self):
        self.state.report = {}
        self.state.save()
        self.assertEqual(prune_talent_sources('retail')['removed'], [])
        self.assertTrue(self.old.exists())

    def test_owned_failed_staging_is_reclaimed_but_unknown_staging_is_retained(self):
        path = self.old.parent / '.preparing-owned'
        path.mkdir()
        (path / 'retention_owner.json').write_text(json.dumps({
            'branch': 'retail', 'purpose': 'talent-source-staging'}), encoding='utf-8')
        unknown = self.old.parent / '.preparing-unknown'
        unknown.mkdir()
        report = prune_talent_sources('retail')
        self.assertIn(str(path), report['removed'])
        self.assertTrue(unknown.exists())

    def test_symlink_is_not_followed(self):
        outside = self.base / '保留目录'
        outside.mkdir()
        link = self.old.parent / ('f' * 64)
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('当前系统不允许创建测试符号链接')
        report = prune_talent_sources('retail')
        self.assertIn(str(link), report['blocked'])
        self.assertTrue(outside.is_dir())

    def test_reader_blocks_cleanup_and_threads_can_share_read_reference(self):
        entered, release = Event(), Event()
        def reader():
            with source_access():
                entered.set()
                release.wait(5)
        with source_access():
            thread = Thread(target=reader)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(prune_talent_sources('retail')['busy'])
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(prune_talent_sources('retail')['busy'])

    def test_other_process_cannot_reclaim_during_read(self):
        code = '''import sys
from django.conf import settings
settings.configure(BASE_DIR=sys.argv[1])
from botend.services.talent_source_retention import source_access
with source_access(cleanup=True) as acquired:
    print(acquired)
'''
        with source_access():
            process = subprocess.run([sys.executable, '-c', code, str(self.base)], capture_output=True,
                                     text=True, timeout=10, check=True)
        self.assertEqual(process.stdout.strip(), 'False')

    def test_digest_manifest_validates_exact_source_build(self):
        self.assertTrue(managed_source_matches(self.current, 'retail', '当前'))
        self.assertFalse(managed_source_matches(self.current, 'ptr', '当前'))
        self.assertFalse(managed_source_matches(self.current, 'retail', '其他构建'))

    def test_grant_import_accepts_verified_digest_directory_then_checks_tables(self):
        self.version.current_build = '当前'
        self.version.save(update_fields=['current_build'])
        with self.assertRaisesMessage(CommandError, '缺少 DB2 表'):
            call_command('sync_talent_granted_entries', version_key='retail', build='当前', dump_dir=str(self.current))

    def test_failed_removal_keeps_reference_history_for_retry(self):
        with patch('botend.services.talent_source_retention._remove_source', side_effect=OSError('文件占用')):
            with self.assertRaises(OSError):
                prune_talent_sources('retail')
        self.state.refresh_from_db()
        self.assertEqual(self.state.report['talent_sources']['previous'], str(self.previous))
        self.assertEqual(prune_talent_sources('retail')['removed'], [str(self.old)])

    def test_partial_removal_keeps_manifest_until_payload_is_removed(self):
        unlink = Path.unlink
        def occupied(path, *args, **kwargs):
            if path == self.old / 'TraitNode.csv':
                raise PermissionError('文件被占用')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', occupied):
            with self.assertRaises(PermissionError):
                prune_talent_sources('retail')
        self.assertTrue((self.old / 'manifest.json').exists())
        self.assertEqual(prune_talent_sources('retail')['removed'], [str(self.old)])

    def test_repeated_publish_keeps_previous_and_next_publish_advances_it(self):
        report = {'talent_sources': {'current': '当前', 'previous': '上一份'}}
        self.assertEqual(retained_sources('当前', '当前', report), report['talent_sources'])
        self.assertEqual(retained_sources('更新', '当前', report), {'current': '更新', 'previous': '当前'})
