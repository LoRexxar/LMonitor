"""共享模板的无损性、写入幂等、历史证据与浏览器协议回归。"""
from copy import deepcopy
import json
import gzip
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings, RequestFactory
from django.test.utils import CaptureQueriesContext

from botend.models import WowItemSnapshot, WowItemVariantSnapshot
from botend.services.variant_text import encode, decode, catalog_text, compact_catalog_text, compact_item, prepare_effects
from botend.services.gear_builder import serialize_variant
from botend.services.journal_effect_supplement import baseline_sha256
from botend.tests.test_gear_builder import GearBuilderTestDataMixin

ROOT = Path(__file__).resolve().parents[2]


class VariantTextCodecTests(SimpleTestCase):
    def test_lossless_numeric_text_and_structural_fields(self):
        raw = [{'description_zh': '获得 1,234.50 急速，持续 -10 秒，概率 5%。保留 {v0} 与 $s1。',
                'description': 'Gain 1,234.50 Haste.\n+10% / 0.00', 'spell_id': 123,
                'other': [True, False, None, 0, 1.5, {'raw': '字面值不丢失'}]}]
        templates = {}
        packed = encode(raw, templates)
        self.assertEqual(decode(packed, templates), raw)
        second = deepcopy(raw)
        second[0]['description_zh'] = second[0]['description_zh'].replace('1,234.50', '2,345.60')
        other = encode(second, templates)
        self.assertEqual(packed['template'], other['template'])
        self.assertEqual(len(templates), 1)
        self.assertEqual(decode(other, templates), second)
        second[0]['description_zh'] = '完全不同的机制：治疗 500 点生命。'
        self.assertNotEqual(encode(second, templates)['template'], packed['template'])

    def test_missing_or_corrupt_template_refuses_to_guess(self):
        templates = {}
        packed = encode(['伤害 123'], templates)
        with self.assertRaises(ValueError):
            decode(packed, {})
        with self.assertRaises(ValueError):
            decode({**packed, 'values': []}, templates)
        with self.assertRaises(ValueError):
            decode({**packed, '$variant_text': 2}, templates)
        templates[packed['template']][0] = 'invalid'
        with self.assertRaises(ValueError):
            decode(packed, templates)

    def test_small_catalog_is_not_expanded_by_template_overhead(self):
        raw = {'items': [{'variants': [{'effects': ['伤害 1'], 'tooltip': '测试'}]}]}
        self.assertEqual(compact_catalog_text(raw), raw)

    def test_python_and_browser_roundtrip_full_catalog_and_enhancements(self):
        variants = [{'id': n, 'stats': {'haste': n}, 'effects': [{'description_zh': f'造成 {n} 点伤害。'}],
                     'tooltip': f'物品等级 {n}\n+{n * 2} 急速', '_item': {'name': '测试物品', 'metadata': {}}}
                    for n in range(300, 330)]
        item = {'item_id': 1001, 'variants': variants}
        raw = {'items': [item], 'groups': {'gems': [item]}, 'embellishment_options': {'1': item}}
        packed = catalog_text(raw)
        self.assertEqual(catalog_text(packed, expand=True), raw)
        self.assertLess(len(json.dumps(packed)), len(json.dumps(raw)))
        js = """
const fs=require('fs'),vm=require('vm');const context={window:{}};
vm.runInNewContext(fs.readFileSync('static/portal/js/gear-catalog.js','utf8'),context);
process.stdout.write(JSON.stringify(context.window.WowGearCatalog.expandTexts(JSON.parse(fs.readFileSync(0,'utf8')))));
"""
        result = subprocess.run(['node', '-e', js], input=json.dumps(packed), cwd=ROOT,
                                text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), raw)


class VariantTextStorageTests(GearBuilderTestDataMixin, TestCase):
    def seed(self):
        variants = []
        for level in range(300, 330):
            raw = [{'spell_id': 12345, 'description_zh': f'装备：使你的攻击有一定几率获得 {level * 10} 点急速，持续 15 秒。',
                    'description': f'Equip: Your attacks have a chance to grant {level * 10} Haste for 15 sec.',
                    'other': {'name': '保留来源字段', 'source': '测试'}}]
            variants.append(WowItemVariantSnapshot.objects.create(item=self.helm, season=self.season,
                batch_key='test-batch', variant_key=f'level-{level}', item_level=level,
                stats_json={'strength': level}, effects_json=raw, variant_type='drop_equipment',
                compatible_slots=['head']))
        return variants

    def test_storage_roundtrip_read_only_display_and_zero_repeat_writes(self):
        rows = self.seed()
        original = {row.pk: (row.effects_data, serialize_variant(row), baseline_sha256(row)) for row in rows}
        from botend.services.dungeon_aggregation import _content_rows
        dependencies = _content_rows(WowItemVariantSnapshot.objects.filter(pk__in=original))
        with CaptureQueriesContext(connection) as queries:
            compact_item(self.helm.pk)
        self.assertFalse(any(q['sql'].startswith(('UPDATE ', 'INSERT ', 'DELETE ')) for q in queries))
        report = compact_item(self.helm.pk, apply=True)
        self.assertEqual(_content_rows(WowItemVariantSnapshot.objects.filter(pk__in=original)), dependencies)
        self.assertLess(report['after_bytes'], report['before_bytes'])
        self.helm.refresh_from_db()
        self.assertEqual(len({row.effects_json['template'] for row in
                              WowItemVariantSnapshot.objects.filter(pk__in=original)}), 1)
        for row in WowItemVariantSnapshot.objects.filter(pk__in=original).select_related('item'):
            self.assertIsInstance(row.effects_json, dict)
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual((row.effects_data, serialize_variant(row), baseline_sha256(row)), original[row.pk])
            self.assertFalse(any(q['sql'].startswith(('UPDATE ', 'INSERT ', 'DELETE ')) for q in queries))
        with CaptureQueriesContext(connection) as queries:
            compact_item(self.helm.pk, apply=True)
        self.assertFalse(any(q['sql'].startswith(('UPDATE ', 'INSERT ', 'DELETE ')) for q in queries))

    def test_template_gc_preserves_all_batches_and_missing_reference_rolls_back(self):
        row = self.seed()[0]
        old = WowItemVariantSnapshot.objects.create(item=self.helm, season=self.season,
            batch_key='historical-batch', variant_key='old', effects_json=['历史批次独有描述 789'])
        compact_item(self.helm.pk, apply=True)
        row.refresh_from_db()
        self.helm.refresh_from_db()
        kept = set(self.helm.variant_text_templates)
        templates = deepcopy(self.helm.variant_text_templates)
        orphan = encode(['没有任何变体引用的退役描述 123'], templates)
        WowItemSnapshot.objects.filter(pk=self.helm.pk).update(variant_text_templates=templates)
        self.assertNotIn(orphan['template'], kept)
        compact_item(self.helm.pk, apply=True)
        self.helm.refresh_from_db()
        self.assertEqual(set(self.helm.variant_text_templates), kept)
        old.refresh_from_db()
        self.assertEqual(old.effects_data, ['历史批次独有描述 789'])
        broken = {**row.effects_json, 'template': '0' * 64}
        WowItemVariantSnapshot.objects.filter(pk=row.pk).update(effects_json=broken)
        before = list(WowItemVariantSnapshot.objects.filter(item=self.helm).values())
        with self.assertRaises(ValueError):
            compact_item(self.helm.pk, apply=True)
        self.assertEqual(list(WowItemVariantSnapshot.objects.filter(item=self.helm).values()), before)
        self.helm.refresh_from_db()
        self.assertEqual(set(self.helm.variant_text_templates), kept)

    def test_same_collection_writes_neither_variants_nor_templates(self):
        from botend.management.commands.sync_gear_builder_catalog import Command
        row = self.seed()[0]
        defaults = {f.name: getattr(row, f.name) for f in row._meta.concrete_fields
                    if f.name not in ('id', 'item', 'season', 'batch_key', 'variant_key', 'created_at', 'updated_at')}
        compact_item(self.helm.pk, apply=True)
        Command._upsert_variant(season=self.season, batch_key='test-batch', item=self.helm,
                                variant_key=row.variant_key, defaults=defaults)
        with CaptureQueriesContext(connection) as queries:
            Command._upsert_variant(season=self.season, batch_key='test-batch', item=self.helm,
                                    variant_key=row.variant_key, defaults=defaults)
        self.assertFalse(any(q['sql'].startswith(('UPDATE ', 'INSERT ', 'DELETE ')) for q in queries))

    def test_restore_inline_preserves_content_and_removes_all_references(self):
        import importlib
        from types import SimpleNamespace
        from django.apps import apps
        guard = importlib.import_module('botend.migrations.0256_variant_text_templates').protect_reverse
        rows = self.seed()
        original = {row.pk: row.effects_data for row in rows}
        compact_item(self.helm.pk, apply=True)
        with self.assertRaisesRegex(RuntimeError, '仍有变体引用'):
            guard(apps, SimpleNamespace(connection=connection))
        compact_item(self.helm.pk, apply=True, restore=True)
        guard(apps, SimpleNamespace(connection=connection))
        self.helm.refresh_from_db()
        self.assertEqual(self.helm.variant_text_templates, {})
        for row in WowItemVariantSnapshot.objects.filter(pk__in=original):
            self.assertEqual(row.effects_json, original[row.pk])

    def test_repository_artifact_all_180_variants_roundtrip_and_size(self):
        artifact = json.loads((ROOT / 'botend/data/ptr_kithix_unbound_12_1_5.json').read_text(encoding='utf-8'))
        total_before = total_after = count = 0
        for source in artifact['gear']['items']:
            item = WowItemSnapshot.objects.create(item_id=source['item_id'], name=source['name'])
            original = {}
            for variant in source['variants']:
                row = WowItemVariantSnapshot.objects.create(item=item, season=self.season, batch_key='artifact',
                    variant_key=variant['key'], item_level=variant['item_level'], effects_json=variant['effects'],
                    stats_json=variant.get('stats', {}), metadata=variant.get('metadata', {}))
                original[row.pk] = (row.effects_data, serialize_variant(row), baseline_sha256(row))
            report = compact_item(item.pk, apply=True)
            total_before += report['before_bytes']
            total_after += report['after_bytes']
            count += len(original)
            for row in WowItemVariantSnapshot.objects.filter(pk__in=original).select_related('item'):
                self.assertEqual((row.effects_data, serialize_variant(row), baseline_sha256(row)), original[row.pk])
        self.assertEqual(count, 180)
        self.assertLess(total_after, total_before)
        print(f'资料样本：{count} 个变体，原描述 {total_before} 字节，含共享模板 {total_after} 字节。')

    def test_maintenance_resumes_persistent_cursor_and_passes_corrupt_item(self):
        from botend.services.variant_text import maintain_variant_text
        ids = list(WowItemSnapshot.objects.order_by('pk').values_list('pk', flat=True))
        self.assertGreaterEqual(len(ids), 2)
        with tempfile.TemporaryDirectory() as directory, override_settings(VARIANT_TEXT_MAINTENANCE_ROOT=directory):
            with patch('botend.services.variant_text.compact_item', side_effect=ValueError('模板损坏')):
                first = maintain_variant_text(limit=1)
            self.assertEqual(first['status'], 'error')
            self.assertEqual(first['next_cursor'], ids[0])
            with patch('botend.services.variant_text.compact_item', return_value={'variants': 0}) as compact:
                second = maintain_variant_text(limit=1)
            compact.assert_called_once_with(ids[1], apply=True)
            self.assertEqual(second['next_cursor'], ids[1])

    def test_packed_snapshot_serves_new_and_open_old_pages_without_database_queries(self):
        from botend.services import gear_catalog_snapshot as snapshots
        key = snapshots.coordinate('Warrior', 'Fury', 'head')
        url = '/?class=Warrior&spec=Fury&slot=head&snapshot=1'
        with tempfile.TemporaryDirectory() as directory, override_settings(GEAR_CATALOG_SNAPSHOT_ROOT=directory):
            snapshots.request_refresh(key)
            with patch('botend.services.variant_text.compact_catalog_text', side_effect=catalog_text):
                self.assertEqual(snapshots.refresh_catalog_snapshots(force=True), [key])
            def body(response):
                try:
                    data = b''.join(response.streaming_content) if response.streaming else response.content
                    return json.loads(gzip.decompress(data) if response.get('Content-Encoding') == 'gzip' else data)
                finally:
                    response.close()
            with self.assertNumQueries(0):
                packed = body(snapshots.catalog_snapshot_response(RequestFactory().get(url + '&text_format=1')))
                expected = catalog_text(packed, expand=True)
                for encoding in ('', 'gzip'):
                    response = snapshots.catalog_snapshot_response(RequestFactory().get(url, HTTP_ACCEPT_ENCODING=encoding))
                    etag = response['ETag']
                    self.assertEqual(body(response), expected)
                    cached = snapshots.catalog_snapshot_response(RequestFactory().get(url, HTTP_ACCEPT_ENCODING=encoding,
                        HTTP_IF_NONE_MATCH=etag))
                    self.assertEqual(cached.status_code, 304)
