"""部署回填仅排序轻字段，避免大型手册JSON参与MySQL filesort。"""
from io import StringIO

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.journal_models import JournalRelease, JournalState, JournalInstance, JournalEncounter
from botend.models import WowItemSnapshot


class JournalCatalogBackfillReadTests(TestCase):
    def test_sorted_loot_winners_and_builds_preserved_without_sorting_large_json(self):
        release = JournalRelease.objects.create(build='test-retail', manifest={
            'retail_build': 'test-retail', 'ptr_overlays': {'2': {'source_build': 'test-ptr'}}})
        JournalState.objects.update_or_create(pk='wow-zhCN', defaults={'active_release': release})
        retail = JournalInstance.objects.create(release=release, journal_id=1, name='Retail', kind='raid')
        ptr = JournalInstance.objects.create(release=release, journal_id=2, name='PTR', kind='raid')
        def loot(item_id, name):
            return {'item_id': item_id, 'name': name, 'source': 'wago', 'quality': 4,
                    'slot': 12, 'class_id': 4, 'subclass_id': 0, 'stat_types': [32, 36]}
        # 插入顺序故意与业务顺序相反；重复物品必须保留原先排序中的首次事实。
        late = JournalEncounter.objects.create(instance=retail, journal_id=20, name='Late', order=2,
            payload={'loot': [loot(990001, '后序名称')], 'sections': ['x' * 300000]})
        early = JournalEncounter.objects.create(instance=retail, journal_id=10, name='Early', order=1,
            payload={'loot': [loot(990001, '首序名称'), loot(990002, '另一物品')], 'sections': ['y' * 300000]})
        JournalEncounter.objects.create(instance=ptr, journal_id=30, name='PTR', order=1,
            payload={'loot': [loot(990003, '测试服物品')], 'sections': ['z' * 300000]})
        original = {row.pk: row.payload for row in JournalEncounter.objects.all()}
        with CaptureQueriesContext(connection) as captured:
            call_command('backfill_journal_item_catalog', stdout=StringIO())
        table = JournalEncounter._meta.db_table
        queries = [q['sql'] for q in captured.captured_queries if q['sql'].lstrip().upper().startswith('SELECT') and table in q['sql']]
        payload_reads = [sql for sql in queries if 'payload' in sql]
        self.assertTrue(payload_reads)
        self.assertTrue(any('ORDER BY' in sql.upper() and 'payload' not in sql for sql in queries))
        self.assertTrue(all('ORDER BY' not in sql.upper() for sql in payload_reads), payload_reads)
        rows = WowItemSnapshot.objects.in_bulk([990001, 990002, 990003], field_name='item_id')
        self.assertEqual(set(rows), {990001, 990002, 990003})
        self.assertEqual(rows[990001].name_zh, '首序名称')
        self.assertEqual(rows[990001].metadata['journal_item']['stat_types'], [32, 36])
        self.assertEqual(rows[990001].metadata['journal_builds'], ['test-retail'])
        self.assertEqual(rows[990003].metadata['journal_builds'], ['test-ptr'])
        self.assertEqual({row.pk: row.payload for row in JournalEncounter.objects.all()}, original)
        timestamps = {pk: row.updated_at for pk, row in rows.items()}
        call_command('backfill_journal_item_catalog', stdout=StringIO())
        self.assertEqual({row.item_id: row.updated_at for row in WowItemSnapshot.objects.filter(item_id__in=rows)}, timestamps)
