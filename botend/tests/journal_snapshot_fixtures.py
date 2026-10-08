"""手册测试显式模拟后台预热，文件与真实运行目录隔离。"""
from io import StringIO
from pathlib import Path
import tempfile
from django.core.management import call_command
from django.test import override_settings


def isolate_journal_snapshots(test):
    root = Path(tempfile.mkdtemp(prefix='journal-loot-test-'))
    override = override_settings(JOURNAL_LOOT_SNAPSHOT_ROOT=root / 'journal',
                                 JOURNAL_SNAPSHOT_ROOT=root / 'catalog', GEAR_CATALOG_SNAPSHOT_ROOT=root / 'gear')
    override.enable()
    test.addCleanup(override.disable)
    return root


def warm_journal(instance=None):
    call_command('refresh_journal_snapshot', stdout=StringIO())
    call_command('refresh_journal_loot_snapshots', force=True, instance=instance, stdout=StringIO())
