"""手册掉落的真实数据库、文件发布及浏览器筛选一致性验证。"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from unittest.mock import patch
from django.db import connection
from django.test import TestCase, RequestFactory
from django.test.utils import CaptureQueriesContext
from botend.journal_models import JournalRelease, JournalState, JournalInstance, JournalEncounter
from botend.models import WowItemSnapshot
from botend.portal.adventure_journal import detail_data, instance_source, build_loot_projection, loot_matches
from botend.services.journal_service import compile_journal
from botend.services import journal_loot_snapshot as snapshots
from botend.services.gear_catalog_snapshot import invalidate_catalog_snapshots
from botend.tests.test_adventure_journal import fixture
from botend.tests.journal_snapshot_fixtures import isolate_journal_snapshots, warm_journal


class JournalLootSnapshotTests(TestCase):
    def setUp(self):
        self.root = isolate_journal_snapshots(self)
        rows, catalog, _ = compile_journal(fixture())
        payload = deepcopy(rows[0])
        bosses = payload.pop('encounters')
        self.release = JournalRelease.objects.create(build='12.1.0.69587', status='completed', manifest={'catalog': catalog})
        self.state = JournalState.objects.create(active_release=self.release)
        self.instance = JournalInstance.objects.create(release=self.release, journal_id=10, name=payload['name'], kind=payload['kind'], payload=payload)
        for boss in bosses:
            JournalEncounter.objects.create(instance=self.instance, journal_id=boss['id'], name=boss['name'], payload=boss)
        self.source = instance_source(self.release, 10)

    def read(self, difficulty=1):
        return snapshots.read_loot_projection(self.release.id, 10, difficulty, self.source)

    def test_cold_and_warm_read_are_zero_queries_and_idle_poll_is_zero_queries(self):
        with self.assertNumQueries(0):
            self.assertEqual(self.read()['snapshot']['state'], 'building')
        self.assertEqual(len(snapshots.refresh_journal_loot_snapshots()), 1)
        with self.assertNumQueries(0):
            self.assertEqual(self.read()['loot'][0]['item_id'], 60)
            self.assertEqual(snapshots.refresh_journal_loot_snapshots(poll=True), [])
        with patch('botend.portal.adventure_journal.build_loot_projection', side_effect=AssertionError('请求内聚合')), \
                patch('botend.portal.adventure_journal.cached_tooltip', side_effect=AssertionError('请求补全')):
            with CaptureQueriesContext(connection) as queries:
                result = detail_data(RequestFactory().get('/', {'loot_q': '60'}), 10)
            self.assertEqual([row['item_id'] for row in result['loot']], [60])
            self.assertFalse(any('wowitemsnapshot' in q['sql'].lower() or 'wowitemvariantsnapshot' in q['sql'].lower() for q in queries))
            print(f'冒险手册：页面 {len(queries)} 次目录/赛季查询，掉落文件读取 0 次查询。')

    def test_failure_and_source_race_keep_previous_then_update_names_and_spec_rules(self):
        warm_journal()
        before = self.read()
        WowItemSnapshot.objects.create(item_id=60, name='Original', name_zh='新名称', catalog_type='equipment', eligible_specs=['Paladin:Holy'])
        with patch('botend.portal.adventure_journal.build_loot_projection', side_effect=RuntimeError('生成失败')):
            self.assertEqual(snapshots.refresh_journal_loot_snapshots(force=True), [])
        self.assertEqual(self.read(), before)
        from botend.services.gear_catalog_snapshot import _source
        facts = _source()
        with patch('botend.services.gear_catalog_snapshot._source', side_effect=[facts, (facts[0], facts[1], 'changed')]):
            self.assertEqual(snapshots.refresh_journal_loot_snapshots(force=True), [])
        self.assertEqual(self.read(), before)
        warm_journal()
        row = self.read()['loot'][0]
        self.assertEqual(row['name'], '新名称')
        self.assertIn(65, row['filter_specs'])
        self.assertNotIn(70, row['filter_specs'])

    def test_difficulty_source_release_isolation_and_corrupt_recovery(self):
        warm_journal()
        self.assertEqual([row['item_id'] for row in self.read(1)['loot']], [60])
        self.assertEqual([row['item_id'] for row in self.read(2)['loot']], [61])
        with self.assertNumQueries(0):
            self.assertEqual(snapshots.read_loot_projection(self.release.id + 1, 10, 1, self.source)['snapshot']['state'], 'building')
            self.assertEqual(snapshots.read_loot_projection(self.release.id, 10, 1, {**self.source, 'key': 'ptr'})['snapshot']['state'], 'building')
        key = self.read()['coordinate']
        (snapshots._directory(key) / 'data.json').write_text('{', encoding='utf-8')
        self.assertEqual(self.read()['snapshot']['state'], 'building')
        snapshots.refresh_journal_loot_snapshots()
        self.assertEqual(self.read()['snapshot']['state'], 'ready')

    def test_revision_wakes_worker_and_status_is_small(self):
        warm_journal()
        with self.captureOnCommitCallbacks(execute=True):
            invalidate_catalog_snapshots()
        self.assertTrue(snapshots.refresh_journal_loot_snapshots(poll=True))
        response = self.client.get('/portal/api/adventure-journal/10/?snapshot_status=1&difficulty=1')
        self.assertEqual(set(response.json()), {'snapshot'})
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_javascript_matches_server_filters_and_background_rules(self):
        warm_journal()
        rows = self.read()['loot'] + self.read(2)['loot']
        filters = [dict(slot='', item_type='', loot_boss='', loot_q='', **{'class': '', 'spec': ''})]
        for changes in ({'class': '1'}, {'class': '8'}, {'class': '2', 'spec': '65'}, {'slot': '1'},
                        {'item_type': 'slot:12'}, {'loot_boss': '30'}, {'loot_boss': '99'},
                        {'loot_q': '饰品'}, {'loot_q': '61'}, {'loot_q': '不存在'}):
            filters.append({**filters[0], **changes})
        expected = []
        for selected in filters:
            context = {**selected, 'class_id': int(selected['class'] or 0), 'spec_id': int(selected['spec'] or 0), 'loot_boss': int(selected['loot_boss'] or 0)}
            expected.append([row['item_id'] for row in rows if loot_matches(row, context)])
        completed = subprocess.run(['node', 'botend/tests/js/journal-loot-filter-oracle.mjs'], input=json.dumps({'rows': rows, 'filters': filters}), text=True, capture_output=True, check=True, encoding='utf-8')
        self.assertEqual(json.loads(completed.stdout), expected)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('后台不得补取外源')):
            direct = build_loot_projection(self.release, 10, list(self.instance.encounters.all()), 1, self.source)
        self.assertEqual(direct['loot'], self.read()['loot'])
