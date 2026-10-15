"""PTR2 import consumes a real frozen response, not relabelled retail data."""
import json
from copy import deepcopy
from pathlib import Path
from django.test import TestCase
from django.core.exceptions import ValidationError
from django.conf import settings
from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.wow_item_difficulty_import import import_difficulty_variant
from botend.services.wow_item_display import load_item_tooltip_metadata


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
