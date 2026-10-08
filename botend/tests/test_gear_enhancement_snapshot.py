"""增强目录与原算法、实际浏览器函数对照，验证公开只读和失败隔离。"""
import gzip
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, RequestFactory
from django.utils import timezone

from botend.models import WowItemSnapshot, WowItemVariantSnapshot
from botend.portal.gear_builder import PortalGearBuilderEnhancementsAPIView
from botend.services import gear_builder as gb, gear_catalog_snapshot as snapshots
from botend.tests.test_gear_builder import GearBuilderTestDataMixin


class GearEnhancementSnapshotTests(GearBuilderTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix='enhancement-snapshot-'))
        override = self.settings(GEAR_CATALOG_SNAPSHOT_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.key = snapshots.coordinate('Warrior', 'Fury', 'head', 'enhancements')
        self.identity = {name: self.key[name] for name in ('class_name', 'spec_name', 'slot')}
        self.view = PortalGearBuilderEnhancementsAPIView.as_view()
        self.factory = RequestFactory()
        self.crafted_item.metadata = {'crafting_reagent_slot_ids': [100]}
        self.crafted_item.save()
        self.embellishment.metadata = {'reagent_slot_ids': [100], 'simc_name': 'rift_lining_2'}
        self.embellishment.save()

    def build(self):
        snapshots.request_refresh(self.key)
        self.assertEqual(snapshots.refresh_catalog_snapshots(), [self.key])
        return snapshots._index(self.key)

    def read(self, **params):
        return self.view(self.factory.get('/', {'class': 'Warrior', 'spec': 'Fury', 'slot': 'head', **params}))

    def body(self, response):
        try:
            data = b''.join(response.streaming_content) if response.streaming else response.content
            if response.get('Content-Encoding') == 'gzip':
                data = gzip.decompress(data)
            return json.loads(data)
        finally:
            response.close()

    def test_cold_warm_compatibility_and_invalid_requests_never_query_or_build(self):
        with self.assertNumQueries(0), patch.object(gb, 'enhancement_items', side_effect=AssertionError('不得回源')):
            self.assertEqual(self.read().status_code, 202)
        self.build()
        with self.assertNumQueries(0), patch.object(gb, 'enhancement_snapshot_payload', side_effect=AssertionError('不得构建')):
            for params in ({'snapshot': '1'}, {'variant_id': self.crafted.pk}, {'variant_id': self.hero.pk},
                           {'variant_id': '../../bad'}, {'variant_id': 999999}, {'class': 'bad'}, {'slot': '../bad'}):
                response = self.read(**params)
                self.assertIn(response.status_code, (200, 400))
                self.body(response)

    def test_browser_and_compatibility_api_equal_internal_groups_for_every_carrier(self):
        other_item = WowItemSnapshot.objects.create(item_id=19999, name='其他配方', catalog_type='crafted_equipment',
            slot_key='head', metadata={'crafting_reagent_slot_ids': [999]})
        other = WowItemVariantSnapshot.objects.create(item=other_item, season=self.season, batch_key='test-batch',
            variant_key='other-recipe', variant_type='crafted_equipment', compatible_slots=['head'])
        fixed = WowItemVariantSnapshot.objects.create(item=self.crafted_item, season=self.season, batch_key='test-batch',
            variant_key='fixed', variant_type='crafted_equipment', compatible_slots=['head'], is_intrinsic_embellishment=True)
        self.build()
        payload = self.body(self.read(snapshot='1'))
        cases, expected = [], []
        for carrier in (None, self.crafted.pk, self.hero.pk, other.pk, fixed.pk, 999999):
            groups = gb.enhancement_items(**self.identity, equipment_variant_id=carrier)['groups']
            self.assertEqual(self.body(self.read(variant_id=carrier or ''))['groups'], groups)
            cases.append({'payload': payload, 'variantId': carrier})
            expected.append(groups)
        result = subprocess.run(['node', str(Path(settings.BASE_DIR) / 'botend/tests/js/gear-enhancement-oracle.mjs')],
            input=json.dumps(cases), text=True, encoding='utf-8', capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual(len(payload['embellishment_options']), 1)

    def test_quality_selection_happens_after_recipe_and_branch_eligibility(self):
        stronger_item = WowItemSnapshot.objects.create(item_id=19998, name='更高品质', name_zh='更高品质',
            catalog_type='embellishment', quality=4)
        stronger = WowItemVariantSnapshot.objects.create(item=stronger_item, season=self.season, batch_key='test-batch',
            variant_key='stronger', variant_type='embellishment', crafting_quality=5, compatible_slots=['head'],
            metadata={'simc_name': 'rift_lining_5', 'reagent_slot_ids': [999]})
        ptr = WowItemVariantSnapshot.objects.create(item=self.crafted_item, season=self.season, batch_key='test-batch',
            data_branch='ptr', variant_key='ptr-carrier', variant_type='crafted_equipment', compatible_slots=['head'])
        ptr_material = WowItemVariantSnapshot.objects.create(item=self.embellishment_item, season=self.season,
            batch_key='test-batch', data_branch='ptr', variant_key='ptr-material', variant_type='embellishment',
            crafting_quality=4, compatible_slots=['head'], metadata={'simc_name': 'rift_lining_4', 'reagent_slot_ids': [100]})
        self.build()
        for carrier, selected in ((self.crafted, self.embellishment), (ptr, ptr_material)):
            actual = self.body(self.read(variant_id=carrier.pk))['groups']
            self.assertEqual(actual, gb.enhancement_items(**self.identity, equipment_variant_id=carrier.pk)['groups'])
            self.assertEqual(actual['embellishments'][0]['variants'][0]['id'], selected.pk)
            self.assertNotEqual(actual['embellishments'][0]['variants'][0]['id'], stronger.pk)

    def test_multiple_categories_of_same_item_keep_one_group_with_all_variants(self):
        WowItemVariantSnapshot.objects.create(item=self.gem_item, season=self.season, batch_key='test-batch',
            variant_key='gem-second-category', variant_type='gem', crafting_quality=4, compatible_slots=['head'],
            metadata={'category_name': '另一分类', 'simc_name': 'quick_gem_4'})
        self.build()
        groups = self.body(self.read())['groups']
        self.assertEqual(groups, gb.enhancement_items(**self.identity)['groups'])
        self.assertEqual(len(groups['gems']), 1)
        self.assertEqual(len(groups['gems'][0]['variants']), 2)

    def test_failure_and_source_change_preserve_published_file_then_retry(self):
        index = self.build()
        original = gb.enhancement_snapshot_payload
        with self.assertLogs(snapshots.logger, level='ERROR'), patch.object(gb, 'enhancement_snapshot_payload', side_effect=RuntimeError('模拟失败')):
            self.assertEqual(snapshots.refresh_catalog_snapshots(force=True), [])
        self.assertEqual(snapshots._index(self.key), index)
        def changing(**kwargs):
            payload = original(**kwargs)
            self.gem.updated_at = timezone.now()
            self.gem.save(update_fields=['updated_at'])
            return payload
        with patch.object(gb, 'enhancement_snapshot_payload', side_effect=changing):
            self.assertEqual(snapshots.refresh_catalog_snapshots(force=True), [])
        self.assertEqual(snapshots._index(self.key), index)
        self.assertEqual(snapshots.refresh_catalog_snapshots(), [self.key])

    def test_batch_change_empty_data_and_corrupt_file_never_fallback(self):
        index = self.build()
        (snapshots._directory(self.key) / index['file']).write_text('{', encoding='utf-8')
        with self.assertNumQueries(0):
            self.assertEqual(self.read(snapshot='1').status_code, 202)
        self.assertEqual(snapshots.refresh_catalog_snapshots(), [self.key])
        self.season.gear_batch_key = 'new-batch'
        self.season.save()
        self.assertEqual(snapshots.refresh_catalog_snapshots(), [self.key])
        self.assertEqual(self.body(self.read())['groups'], {'embellishments': [], 'gems': [], 'enchants': []})

    def test_gzip_etag_content_reuse_and_idle_worker_zero_queries(self):
        index = self.build()
        response = self.view(self.factory.get('/', {'snapshot': '1'}, HTTP_ACCEPT_ENCODING='gzip'))
        etag = response['ETag']
        self.assertIn('groups', self.body(response))
        response = self.view(self.factory.get('/', {'snapshot': '1'}, HTTP_IF_NONE_MATCH=etag, HTTP_ACCEPT_ENCODING='gzip'))
        self.assertEqual(response.status_code, 304)
        response.close()
        self.assertEqual(snapshots.refresh_catalog_snapshots(force=True), [self.key])
        self.assertEqual(snapshots._index(self.key)['file'], index['file'])
        with self.assertNumQueries(0):
            self.assertEqual(snapshots.refresh_catalog_snapshots(poll=True), [])
