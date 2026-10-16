"""Captured Wago/tooltip evidence through real ORM, command and public reader."""
from copy import deepcopy
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.utils.dateparse import parse_datetime
from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.journal_effect_supplement import (
    adapt_record, baseline_state, validated_supplement, supplemental_effects,
)
from botend.services.wow_item_display import load_item_tooltip_metadata


class SupplementTests(TestCase):
    def setUp(self):
        path = Path(__file__).parent / 'fixtures/kithix_current_effect_supplements.json'
        self.artifact = json.loads(path.read_text())
        first = self.artifact['records'][0]['before']
        SeasonMeta.objects.create(pk=first['season_id'], season_key='captured', season_name='Captured',
            is_active=True, gear_batch_key=first['batch_key'], mplus_zone_id=1, raid_zone_id=2)
        self.original = {}
        for record in self.artifact['records']:
            before = deepcopy(record['before'])
            item_id = before.pop('item_id')
            item, _ = WowItemSnapshot.objects.get_or_create(item_id=item_id,
                defaults={'name':'Captured', 'quality':4, 'catalog_type':'equipment'})
            updated = before.pop('updated_at')
            v = WowItemVariantSnapshot.objects.create(pk=record['variant_id'], item=item,
                variant_type='drop_equipment', **before)
            WowItemVariantSnapshot.objects.filter(pk=v.pk).update(updated_at=parse_datetime(updated))
            v.refresh_from_db()
            self.original[v.pk] = baseline_state(v)
        self.v = WowItemVariantSnapshot.objects.select_related('item').get(
            pk=self.artifact['records'][0]['variant_id'])
        self.record = adapt_record(self.v, self.artifact['records'][0], self.artifact['source_build'])
        self.record.update(evidence_ref='captured.json', evidence_sha256='a'*64)

    def test_captured_16_dry_apply_noop_and_real_strict_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'artifact.json'
            path.write_text(json.dumps(self.artifact))
            def run(**kwargs):
                out = StringIO()
                call_command('repair_journal_effect_supplements', artifact=str(path), stdout=out, **kwargs)
                return json.loads(out.getvalue())
            self.assertEqual(run()['changed'], 16)
            self.assertTrue(all('effect_supplement' not in v.metadata for v in WowItemVariantSnapshot.objects.all()))
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(run(apply=True, confirm_artifact_sha256=sha,
                backup=str(Path(directory)/'before.json'))['changed'], 16)
            self.assertEqual(run(apply=True, confirm_artifact_sha256=sha)['changed'], 0)
            for v in WowItemVariantSnapshot.objects.select_related('item'):
                self.assertEqual(baseline_state(v), self.original[v.pk])
                drop = v.metadata['difficulty_drop']
                display = load_item_tooltip_metadata([dict(item_id=v.item.item_id, data_branch='ptr',
                    instance_id=drop['instance_id'], difficulty_id=drop['difficulty_id'],
                    item_context=drop['item_context'], difficulty_rule_build=drop['rule_build'],
                    strict_difficulty=True)])[0]
                self.assertEqual(display['variant_id'], v.pk)
                self.assertIn('暗影伤害', display['tooltip'])
                self.assertIn('15%', display['tooltip'])
                self.assertNotIn('Equip:', display['tooltip'])
                self.assertEqual(v.effects_json, [])
                self.assertEqual(supplemental_effects(v)[0]['spell_id'], 1310208)

    def test_stale_baseline_drops_supplement_without_changing_frozen_effects(self):
        supplement = validated_supplement(self.v, self.record)
        self.v.metadata['effect_supplement'] = supplement
        self.v.save(update_fields=['metadata','updated_at'])
        self.v.refresh_from_db()
        self.assertEqual(validated_supplement(self.v,self.record), supplement)
        self.assertTrue(supplemental_effects(self.v))
        self.v.stats_json['stamina'] += 1
        self.assertEqual(supplemental_effects(self.v), [])

    def test_rejects_partial_conditional_wrong_tree_or_changed_values(self):
        def envelope(record, table):
            return next(e for e in record['envelopes'] if '/'+table+'?' in e['url']
                        and (table != 'ItemBonusTree' or '6297' in e['url']))
        changes = [
            lambda r: envelope(r,'ItemXBonusTree')['data'].update(total=999),
            lambda r: envelope(r,'ItemBonus')['data']['data'][0].update(Type=1),
            lambda r: envelope(r,'ItemEffect')['data']['data'][0].update(PlayerConditionID=1),
            lambda r: envelope(r,'ItemBonusTree')['data']['data'][0].update(InventoryTypeSlotMask=1),
            lambda r: r.update(url=r['url'].replace('dataEnv=10','dataEnv=1')),
            lambda r: r.update(baseline_sha256='0'*64),
            lambda r: r['response'].update(quality=3),
        ]
        for change in changes:
            record = deepcopy(self.record)
            change(record)
            with self.assertRaises(ValidationError):
                validated_supplement(self.v, record)
