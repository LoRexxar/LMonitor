"""Difficulty variant import from frozen legacy and PTR2 sources; retain branch-specific ORM setup and provenance boundaries."""
import json
from copy import deepcopy
from pathlib import Path
from django.core.exceptions import ValidationError
from django.test import TestCase
from botend.services.wow_item_difficulty_import import import_difficulty_variant
from django.conf import settings
from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.wow_item_display import load_item_tooltip_metadata


class LegacyBulkImportTests(TestCase):
    def setUp(self):
        p = Path(__file__).parent / 'fixtures'
        samples = json.loads((p / 'wow_item_legacy_difficulty_69587.json').read_text())['samples']
        self.data = [
            {'rule': {**sample['facts'], **sample['import']['binding']},
             'payload': sample['import']['payload'],
             'tooltip_evidence': sample['import']['tooltip_evidence']}
            for sample in samples
        ]
        self.season = SeasonMeta.objects.create(season_key='legacy-bulk-import', season_name='Import', is_active=True, mplus_zone_id=1, raid_zone_id=2, gear_batch_key='proof-batch')
        for row in self.data:
            WowItemSnapshot.objects.get_or_create(item_id=row['rule']['item_id'], defaults=dict(catalog_type='equipment', slot_key='back', quality=row['rule']['quality'], name='Frozen source item'))

    def call_import(self, row):
        r = row['rule']
        return import_difficulty_variant(rule=r, payload=row['payload'], tooltip_evidence=row['tooltip_evidence'],
            item_id=r['item_id'], data_branch=r['data_branch'], instance_id=r['instance_id'],
            difficulty_id=r['difficulty_id'], item_context=r['item_context'], rule_build=r['build'])

    def test_real_legacy_responses_recompute_before_preview(self):
        for row in self.data:
            with self.subTest(item=row['rule']['item_id'], context=row['rule']['item_context']):
                result = self.call_import(row)
                self.assertEqual(result['status'], 'dry_run')

    def test_real_selector_zero_delta_source_is_valid(self):
        from botend.services.wow_item_difficulty import resolve_difficulty_bonus_facts
        from botend.services.wow_item_difficulty_import import _verified_rule
        fixture = json.loads((Path(__file__).parent / 'fixtures/wow_item_selector_public_69587.json').read_text())
        def source(table, field, key):
            return next((e for e in fixture['envelopes'] if e['table'] == table and e['filter'] == ({} if field is None else {field: key})), None)
        rule = resolve_difficulty_bonus_facts(128032, 4, fixture['build'], tables=source)
        self.assertEqual(_verified_rule(rule, 128032, 4, fixture['build'])['item_level'], 16)

    def test_bulk_url_count_and_identity_are_not_optional(self):
        original = self.data[0]
        for mutation in ('url', 'count', 'file_hash', 'unfiltered_identity'):
            row = deepcopy(original)
            d = next(d for d in row['rule']['evidence'] if d['table'] == 'ItemSquishEra')
            if mutation == 'url': d['url'] = d['url'].replace('wago.tools', 'example.com')
            elif mutation == 'count': d['source_total'] -= 1
            elif mutation == 'file_hash': d['file_sha256'] = 'not-a-sha256'
            else: d['key'] = 1
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                self.call_import(row)


class PTR2DifficultyImportTests(TestCase):
    def setUp(self):
        self.row = json.loads((Path(settings.BASE_DIR) / 'botend/tests/fixtures/wow_item_ptr2_difficulty_280617.json').read_text())
        self.season = SeasonMeta.objects.create(season_key='ptr2-test', season_name='PTR2 test',
            is_active=True, gear_batch_key='ptr2-test-batch', mplus_zone_id=1, raid_zone_id=2)
        self.item = WowItemSnapshot.objects.create(item_id=self.row['item_id'],
            name=self.row['payload']['name'], quality=self.row['base_quality'],
            catalog_type='equipment', inventory_type=self.row['slot'], slot_key='trinket')
        self.kw = {k:self.row[k] for k in ('rule','payload','tooltip_evidence','item_id','data_branch','difficulty_id','item_context','instance_id')}
        self.kw['rule_build'] = self.row['source']['build']

    def test_actual_ptr2_import_and_reader_keep_branch_build_and_attributes(self):
        preview = import_difficulty_variant(**self.kw)
        self.assertEqual(preview['status'], 'dry_run')
        saved = import_difficulty_variant(**self.kw, apply=True, expected_before_hash=preview['before_hash'])
        variant = WowItemVariantSnapshot.objects.get(pk=saved['variant_id'])
        self.assertEqual(variant.game_build, '')
        self.assertEqual(variant.data_branch, 'ptr')
        self.assertEqual(variant.metadata['tooltip_source']['data_env'], '10')
        displayed = load_item_tooltip_metadata([dict(item_id=self.item.item_id, data_branch='ptr',
            difficulty_id=self.row['difficulty_id'], item_context=self.row['item_context'],
            instance_id=self.row['instance_id'], difficulty_rule_build=self.kw['rule_build'])])[0]
        self.assertEqual((displayed['item_level'],displayed['quality']), (292,4))
        self.assertTrue(displayed['tooltip_complete'])
        self.assertTrue(displayed['effects'])
        self.assertEqual(import_difficulty_variant(**self.kw, apply=True)['status'], 'noop')

    def test_live_wrong_ptr_and_duplicate_environment_cannot_be_relabelled(self):
        for replacement in ('dataEnv=1','dataEnv=2','dataEnv=3','dataEnv=10&dataEnv=1'):
            kw = deepcopy(self.kw)
            kw['tooltip_evidence']['url'] = kw['tooltip_evidence']['url'].replace('dataEnv=10',replacement)
            with self.assertRaises(ValidationError):
                import_difficulty_variant(**kw, apply=True)
            self.assertFalse(WowItemVariantSnapshot.objects.exists())
        kw=deepcopy(self.kw)
        kw['data_branch']=kw['rule']['data_branch']=kw['tooltip_evidence']['data_branch']='retail'
        with self.assertRaises(ValidationError):
            import_difficulty_variant(**kw, apply=True)
