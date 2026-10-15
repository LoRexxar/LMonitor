"""Captured public response through the real parser, importer and Journal reader."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from django.test import TestCase

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.portal.adventure_journal import build_loot_projection
from botend.services.gear_builder_catalog_source import _plain_text, _tooltip_details
from botend.services.journal_tooltip import _local_item_tooltip
from botend.services.wow_item_difficulty_import import _digest, _lossless_attributes, import_difficulty_variant
from botend.services.wow_item_display import item_display_metadata, load_item_tooltip_metadata


class JournalStatOnlyTrinketTests(TestCase):
    def test_real_public_snapshot_round_trip_and_fail_closed_guards(self):
        record = json.loads((Path(__file__).parent / 'fixtures/wow_item_stat_only_trinket_250462.json').read_text())
        payload = record['payload']
        self.assertEqual(json.loads(record['raw_response']['body']), payload)
        self.assertEqual(hashlib.sha256(record['raw_response']['body'].encode()).hexdigest(), record['raw_response']['body_sha256'])
        self.assertEqual(_digest(payload), record['tooltip_evidence']['payload_sha256'])
        details = _tooltip_details(payload)
        stats, primary = _lossless_attributes(payload, details)
        expected_stats = {'random_stat_1': 93}
        self.assertEqual(details['stats'], expected_stats)
        self.assertEqual(stats, expected_stats)
        self.assertEqual(primary, {'strength': 79, 'agility': 79, 'intellect': 79})
        self.assertEqual(details['effects'], [])
        season = SeasonMeta.objects.create(season_key='stat-only-trinket', season_name='Test',
            is_active=True, mplus_zone_id=1, raid_zone_id=2, gear_batch_key='stat-only')
        item = WowItemSnapshot.objects.create(item_id=record['item_id'], name=payload['name'],
            quality=payload['quality'], icon=payload['icon'], catalog_type='equipment',
            slot_key='trinket', inventory_type=12, item_class_id=4,
            description=_plain_text(payload['tooltip']))
        legacy = WowItemVariantSnapshot.objects.create(item=item, season=season,
            batch_key=season.gear_batch_key, variant_key='legacy', variant_type='drop_equipment',
            item_level=197, stats_json={'haste': 11}, effects_json=[])
        kwargs = {k: record[k] for k in ('item_id', 'data_branch', 'difficulty_id', 'item_context', 'instance_id', 'rule', 'payload', 'tooltip_evidence')}
        kwargs['rule_build'] = record['source']['build']
        result = import_difficulty_variant(**kwargs, apply=True)
        variant = WowItemVariantSnapshot.objects.get(pk=result['variant_id'])
        self.assertEqual(variant.stats_json, expected_stats)
        self.assertEqual(variant.metadata['primary_stat_values'], primary)
        self.assertEqual(variant.metadata['tooltip_source']['response_sha256'], _digest(payload))
        request = {k: record[k] for k in ('item_id', 'data_branch', 'difficulty_id', 'item_context', 'instance_id')}
        request['difficulty_rule_build'] = record['source']['build']
        shown = load_item_tooltip_metadata([request])[0]
        self.assertEqual(shown['variant_id'], variant.pk)
        self.assertTrue(shown['tooltip_complete'])
        self.assertEqual(shown['stats'], {'random_stat_1': 93, 'stragiint': 79})
        self.assertEqual(shown['effects'], [])
        self.assertEqual((shown['item_level'], shown['quality']), (246, 4))
        self.assertIn('+93 随机属性1', shown['tooltip'])
        self.assertIn('+79 力量／敏捷／智力', shown['tooltip'])
        self.assertEqual(shown['tooltip'].count('93'), 1)
        # Already-imported primary-only rows also carry sufficient provenance;
        # they need no new metadata fields to become complete.
        variant.stats_json = {}
        self.assertTrue(item_display_metadata(item.item_id, item, variant=variant)['tooltip_complete'])
        variant.stats_json = expected_stats
        local = _local_item_tooltip(record['item_id'], record['source']['build'],
            difficulty=14, item_context=3, instance_id=1312, data_branch='retail')
        self.assertTrue(local['complete'])
        self.assertEqual(local['status'], 'complete')
        drop = dict(item_id=item.item_id, name=payload['name'], slot=12, quality=4,
            faction='', display_season_id=0, condition_id=0, difficulty_ids=[14], icon=0,
            allowable_classes=-1, inventory_type=12, item_class_id=4, item_subclass_id=0)
        owner = SimpleNamespace(journal_id=1, name='Boss', payload={'loot': [drop]})
        release = SimpleNamespace(manifest={'catalog': {'difficulties': [dict(id=14, context=3)]}})
        projected = build_loot_projection(release, 1312, [owner], 14, record['source'])['loot'][0]
        self.assertTrue(projected['details']['complete'])
        self.assertEqual((projected['details']['item_level'], projected['quality']), (246, 4))
        self.assertEqual(projected['details']['effects'], [])
        self.assertIn('+93 随机属性1', projected['details']['stats'])
        # A stale base layout cannot restore an effect absent from this native snapshot.
        item.description = '+999 Intellect\nEquip: Old effect.'
        item.description_zh = '史诗钥石\n升级：神话6/6\n+999 智力\n装备：旧效果999'
        item.save(update_fields=['description', 'description_zh'])
        stale = load_item_tooltip_metadata([request])[0]
        self.assertTrue(stale['tooltip_complete'])
        self.assertEqual(stale['effects'], [])
        self.assertNotIn('Old effect', stale['tooltip'])
        self.assertNotIn('999', stale['tooltip'])
        self.assertNotIn('史诗钥石', stale['tooltip'])
        self.assertNotIn('神话6/6', stale['tooltip'])
        self.assertEqual(stale['stats'], shown['stats'])
        ordinary = load_item_tooltip_metadata([dict(item_id=item.item_id, allow_default_variant=True)])[0]
        self.assertEqual(ordinary['variant_id'], legacy.pk)
        self.assertFalse(ordinary['tooltip_complete'])
        self.assertIn('史诗钥石', ordinary['tooltip'])
        self.assertIn('神话6/6', ordinary['tooltip'])
        # Explicit effect obligations must never be swallowed by the exception.
        for refs, metadata in [([{'spell_id': 1}], {}), ([], {'requires_effect_mapping': True})]:
            item.effect_refs, item.metadata = refs, metadata
            self.assertFalse(item_display_metadata(item.item_id, item, variant=variant)['tooltip_complete'])
        item.effect_refs, item.metadata = [], {}
        original = deepcopy(variant.metadata)
        for key in ('difficulty_drop', 'tooltip_source', 'difficulty_rule_evidence'):
            variant.metadata = deepcopy(original)
            variant.metadata.pop(key)
            self.assertFalse(item_display_metadata(item.item_id, item, variant=variant)['tooltip_complete'])
        variant.metadata = deepcopy(original)
        variant.metadata['tooltip_source']['response_sha256'] = 'invalid'
        self.assertFalse(item_display_metadata(item.item_id, item, variant=variant)['tooltip_complete'])
