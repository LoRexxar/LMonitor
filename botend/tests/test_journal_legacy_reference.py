"""Schema-1 last-good references remain read-only, never native difficulty proof."""
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase
from botend.services import journal_loot_snapshot as snapshots
from botend.tests import test_journal_loot_snapshot as base


class JournalLegacyReferenceTests(TestCase):
    setUp = base.JournalLootSnapshotTests.setUp
    read = base.JournalLootSnapshotTests.read

    def legacy(self):
        self.read()
        key = snapshots._load(next(snapshots.snapshot_root().glob('*/request.json')))
        snapshots.refresh_journal_loot_snapshots(strict=True, targets=[key])
        native_path = snapshots._directory(key) / 'data.json'
        native = snapshots._load(native_path)
        old = deepcopy(native)
        old.pop('coverage')
        old['coordinate']['schema'] = 1
        old['loot'][0]['details']['note'] = '参考装等快照，不代表所选难度的初始掉落装等。'
        path = snapshots._directory(old['coordinate']) / 'data.json'
        snapshots._write(path, old)
        native_path.unlink()
        return key, path, old, native

    def test_same_coordinate_reference_preserves_properties_and_registers_native_without_queries(self):
        key, path, old, _ = self.legacy()
        before, stamp = path.read_bytes(), path.stat().st_mtime_ns
        with self.assertNumQueries(0), patch('requests.sessions.Session.request', side_effect=AssertionError('network')):
            data = self.read()
        self.assertEqual(data['snapshot']['state'], 'ready')
        self.assertEqual(data['snapshot']['representation'], 'legacy_reference')
        self.assertIs(data['snapshot']['native_difficulty_verified'], False)
        self.assertTrue(data['snapshot']['reference_note'])
        self.assertEqual(data['loot'], old['loot'])
        self.assertNotIn('coverage', data)
        self.assertIsNone(snapshots._read_projection(key))
        self.assertEqual(snapshots._load(snapshots._directory(key) / 'request.json'), key)
        self.assertFalse((snapshots._directory(key) / 'data.json').exists())
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), (before, stamp))

    def test_legacy_reference_page_never_claims_verified_difficulty_facts(self):
        key, _, _, native = self.legacy()
        url = '/portal/adventure-journal/10/?tab=loot&difficulty=1'
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '沿用此坐标原有参考快照')
        self.assertContains(response, '参考装等 / 属性')
        self.assertNotContains(response, '品质、装等、属性与特效使用同一已验证难度变体')
        snapshots._write(snapshots._directory(key) / 'data.json', native)
        response = self.client.get(url)
        self.assertEqual(response.context['loot_snapshot'], native['snapshot'])
        self.assertNotEqual(response.context['loot_snapshot'].get('representation'), 'legacy_reference')
        self.assertContains(response, '难度装等 / 属性')
        self.assertNotContains(response, '沿用此坐标原有参考快照')

    def test_legacy_basic_reference_does_not_need_native_completeness(self):
        _, path, old, _ = self.legacy()
        old['loot'][0]['details'].update(status='basic', complete=False)
        snapshots._write(path, old)
        with self.assertNumQueries(0):
            data = self.read()
        self.assertEqual(data['snapshot']['state'], 'ready')
        self.assertEqual(data['loot'], old['loot'])
        self.assertFalse(data['snapshot']['native_difficulty_verified'])

    def test_legacy_read_failure_is_building_not_a_fabricated_payload(self):
        _, path, _, _ = self.legacy()
        load = snapshots._load
        def fail_legacy(target, *args):
            if target == path:
                raise OSError('unreadable legacy file')
            return load(target, *args)
        with patch.object(snapshots, '_load', side_effect=fail_legacy), self.assertNumQueries(0):
            self.assertEqual(self.read(), {'snapshot': {'state': 'building'}})

    def test_complete_native_unconditionally_wins(self):
        key, _, _, native = self.legacy()
        snapshots._write(snapshots._directory(key) / 'data.json', native)
        with self.assertNumQueries(0):
            self.assertEqual(self.read(), native)

    def test_coordinates_never_cross_fallback_even_with_same_item_names(self):
        key, path, old, _ = self.legacy()
        for field, value in [('difficulty', 2), ('instance_id', 11), ('release_id', key['release_id'] + 1),
                             ('source', {**key['source'], 'data_branch': 'ptr'}), ('journal_version', 'other')]:
            with self.subTest(field=field):
                wrong = deepcopy(old)
                wrong['coordinate'][field] = value
                snapshots._write(path, wrong)
                with self.assertNumQueries(0):
                    self.assertEqual(self.read()['snapshot']['state'], 'building')
        snapshots._write(path, old)
        for changes in ({'difficulty': 2}, {'instance_id': 11}, {'release_id': key['release_id'] + 1},
                        {'source': {**key['source'], 'data_branch': 'ptr'}}, {'journal_version': 'other'}):
            request = {**key, **changes}
            with self.assertNumQueries(0):
                result = snapshots.read_loot_projection(request['release_id'], request['instance_id'],
                    request['difficulty'], request['source'], journal_version=request['journal_version'])
            self.assertEqual(result['snapshot']['state'], 'building')

    def test_corrupt_incomplete_duplicate_and_unready_legacy_are_not_references(self):
        _, path, old, _ = self.legacy()
        bad = []
        for field, value in [('loot_total', 99), ('loot', []), ('snapshot', {'state': 'building'})]:
            bad.append({**deepcopy(old), field: value})
        duplicate = deepcopy(old)
        duplicate['loot'] *= 2
        duplicate['loot_total'] = 2
        bad.append(duplicate)
        for field, value in [('item_id', True), ('name', ''), ('details', {}), ('details', None)]:
            data = deepcopy(old)
            data['loot'][0][field] = value
            bad.append(data)
        for data in bad:
            snapshots._write(path, data)
            with self.assertNumQueries(0):
                self.assertEqual(self.read()['snapshot']['state'], 'building')
        path.write_text('{', encoding='utf-8')
        self.assertEqual(self.read()['snapshot']['state'], 'building')

    def test_strict_empty_and_unresolved_cannot_publish_over_last_good(self):
        key, path, _, native = self.legacy()
        native_path = snapshots._directory(key) / 'data.json'
        snapshots._write(native_path, native)
        previous = [(p, p.read_bytes(), p.stat().st_mtime_ns) for p in (path, native_path)]
        unresolved = deepcopy(native)
        unresolved['loot'][0]['details'].update(status='unresolved', complete=False)
        for payload in ({'loot': [], 'loot_total': 0}, unresolved):
            with patch('botend.portal.adventure_journal.build_loot_projection', return_value=payload):
                with self.assertLogs(snapshots.logger, level='ERROR'), self.assertRaises(RuntimeError):
                    snapshots.refresh_journal_loot_snapshots(force=True, strict=True, targets=[key])
            for p, content, stamp in previous:
                self.assertEqual((p.read_bytes(), p.stat().st_mtime_ns), (content, stamp))
