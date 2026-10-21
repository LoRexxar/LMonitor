"""验证同构建重试命中，版本切换、过期及损坏缓存重新获取。"""
import os
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from botend.services.wow_data_update import BranchCatalogSource, UpdateSource


class IncrementalTooltipCacheTests(SimpleTestCase):
    def setUp(self):
        # 留存项目内验证产物，不删除本轮创建文件。
        root = Path(__file__).resolve().parents[2] / 'tmp'
        root.mkdir(exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix='增量提示缓存-', dir=root))

    def source(self, build='12.0.0.10000', branch='retail'):
        source = BranchCatalogSource(branch, build, Mock(), self.directory)
        source._get_json = Mock(return_value={'name': '测试装备', 'icon': 'inv_helmet_01', 'tooltip': ''})
        return source

    def fetch(self, source):
        return source.venomstone_tooltip(123, 0, source._tooltip_cache_directory(source.target_build))

    def test_same_version_reuses_cache_across_source_instances(self):
        first = self.source()
        self.fetch(first)
        second = self.source()
        self.assertEqual(self.fetch(second)['name_zh'], '测试装备')
        first._get_json.assert_called_once()
        second._get_json.assert_not_called()

    def test_changed_version_and_branch_never_reuse_previous_response(self):
        self.fetch(self.source())
        changed = self.source('12.0.0.10001')
        self.fetch(changed)
        changed._get_json.assert_called_once()
        ptr = self.source('12.0.0.10001', 'ptr')
        self.fetch(ptr)
        ptr._get_json.assert_called_once()
        self.assertIn('/ptr-2/', ptr._get_json.call_args.args[0])

    def test_expired_and_corrupt_cache_refetch(self):
        source = self.source()
        self.fetch(source)
        path = source._tooltip_cache_directory(source.target_build) / 'live' / '123-0.json'
        os.utime(path, (time.time() - 86401, time.time() - 86401))
        self.fetch(source)
        self.assertEqual(source._get_json.call_count, 2)
        path.write_text('{损坏', encoding='utf-8')
        self.fetch(source)
        self.assertEqual(source._get_json.call_count, 3)

    def test_talent_retry_fetches_only_missing_or_expired_records(self):
        for filename, content in {
            'TraitDefinition.csv': 'ID,SpellID\n1,100\n2,200\n',
            'SpellMisc.csv': 'ID,SpellID\n',
            'Spell_zhCN.csv': 'ID,Description_lang\n',
        }.items():
            (self.directory / filename).write_text(content, encoding='utf-8')
        source = self.source()
        record = {'name_zh': '测试天赋', 'description_zh': '造成伤害', 'icon': 'spell_test'}
        # 仅隔离来源目录锁，验证实际下载筛选及缓存文件；锁不属于本次修改。
        run = UpdateSource.talent_display.__wrapped__
        with patch('botend.management.commands.sync_talent_metadata_from_wowhead.Command._record_from_payload',
                   return_value=record):
            run(source, self.directory, Mock())
            self.assertEqual(source._get_json.call_count, 2)
            source._get_json.reset_mock()
            run(source, self.directory, Mock())
            source._get_json.assert_not_called()
            path = self.directory / 'display_fallback.json'
            saved = json.loads(path.read_text(encoding='utf-8'))
            saved['100']['_cache_checked_at'] = time.time() - 86401
            saved['200']['description_zh'] = ''
            path.write_text(json.dumps(saved), encoding='utf-8')
            run(source, self.directory, Mock())
            self.assertEqual(source._get_json.call_count, 2)
            changed = self.source('12.0.0.10001')
            run(changed, self.directory, Mock())
            self.assertEqual(changed._get_json.call_count, 2)
