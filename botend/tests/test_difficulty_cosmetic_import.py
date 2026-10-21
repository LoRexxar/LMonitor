"""Real captured tooltip + DB2 replay, isolated central ORM import and strict reader."""
from copy import deepcopy
import json
from django.core.exceptions import ValidationError
from django.test import TestCase, SimpleTestCase
from botend.services.wow_item_difficulty_import import _lossless_attributes, _tooltip_details
from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.wow_item_difficulty_import import import_difficulty_variant, _digest
from botend.services.wow_item_display import load_item_tooltip_metadata
from botend.tests.test_wow_item_fixed_cosmetic import BUILD, FIXTURES, resolve


class CosmeticImportTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='cosmetic-test', season_name='Test',
            is_active=True, mplus_zone_id=1, raid_zone_id=2, gear_batch_key='cosmetic-batch')
        self.item = WowItemSnapshot.objects.create(item_id=258045, name='Keep name', quality=3,
            catalog_type='equipment', slot_key='main_hand', inventory_type=13, item_class_id=2,
            metadata={'keep': 1})
        self.legacy = WowItemVariantSnapshot.objects.create(item=self.item, season=self.season,
            batch_key='cosmetic-batch', variant_key='legacy', variant_type='drop_equipment',
            item_level=99, stats_json={'stamina': 99})
        self.base_before = list(WowItemSnapshot.objects.values())
        self.variant_before = list(WowItemVariantSnapshot.objects.values())

    def kwargs(self, context=1):
        rule = resolve(context)
        rule.update(data_branch='retail', difficulty_id=context, instance_id=1304)
        return dict(rule=rule,
            payload=json.loads((FIXTURES / 'wow_item_fixed_cosmetic_258045_tooltip_zhcn.json').read_text()),
            tooltip_evidence=dict(identity_validated=True, item_id=258045, data_branch='retail',
                game_build='', url='https://nether.wowhead.com/tooltip/item/258045?locale=zhcn&dataEnv=1'),
            item_id=258045, data_branch='retail', difficulty_id=context, item_context=context,
            instance_id=1304, rule_build=BUILD)

    def assert_unchanged(self):
        self.assertEqual(list(WowItemSnapshot.objects.values()), self.base_before)
        self.assertEqual(list(WowItemVariantSnapshot.objects.values()), self.variant_before)

    def test_real_fixed_cosmetic_import_three_contexts_and_exact_reader(self):
        for context in (1, 2, 23):
            kw = self.kwargs(context)
            preview = import_difficulty_variant(**kw)
            self.assertEqual(preview['status'], 'dry_run')
            created = import_difficulty_variant(**kw, apply=True, expected_before_hash=preview['before_hash'])
            row = WowItemVariantSnapshot.objects.get(pk=created['variant_id'])
            self.assertEqual(row.stats_json, dict(min_damage=1, max_damage=1, weapon_speed=3.0, weapon_dps=0.3))
            self.assertEqual(row.bonus_ids, [])
            self.assertEqual(row.effects_json, [{'description_zh': '使用：将此外观添加到你的战团收藏中。'}])
            self.assertEqual(row.game_build, '')
            self.assertEqual(row.metadata['difficulty_drop']['initial_drop_kind'], 'fixed_cosmetic')
            self.assertEqual(row.metadata['difficulty_drop']['rule_build'], BUILD)
            before = list(WowItemVariantSnapshot.objects.values())
            self.assertEqual(import_difficulty_variant(**kw, apply=True)['status'], 'noop')
            self.assertEqual(list(WowItemVariantSnapshot.objects.values()), before)
            selected = load_item_tooltip_metadata([dict(item_id=258045, data_branch='retail',
                difficulty_id=context, item_context=context, instance_id=1304, difficulty_rule_build=BUILD)])[0]
            self.assertEqual(selected['variant_id'], row.pk)
            self.assertEqual(selected['item_level'], 1)
            self.assertEqual(selected['quality'], 3)
        # Import now publishes central effect definitions. All non-effect base
        # facts remain immutable; the sole permitted change is verified below.
        current = list(WowItemSnapshot.objects.values())
        stable = lambda rows: [{k: v for k, v in row.items()
                                if k not in ('effects', 'updated_at')} for row in rows]
        self.assertEqual(stable(current), stable(self.base_before))
        definitions = current[0]['effects']['branches']['retail']['definitions']
        self.assertEqual(len(definitions), 1)
        definition = next(iter(definitions.values()))
        self.assertEqual(definition['template_zh'], '使用：将此外观添加到你的战团收藏中。')
        self.assertEqual(definition['source']['item_id'], 258045)
        self.assertEqual(definition['source']['provider'], 'wowhead')
        self.assertEqual(load_item_tooltip_metadata([dict(item_id=258045, allow_default_variant=True)])[0]['variant_id'], self.legacy.pk)

    def test_spoofed_kind_or_modified_typed_sparse_rejected(self):
        for field, value in [('Flags_3', 0), ('Flags_3', True), ('Flags_3', 65536.0),
                             ('PlayerLevelToItemLevelCurveID', 1), ('ItemLevelOffsetCurveID', False),
                             ('ItemLevelOffsetItemLevel', None)]:
            kw = self.kwargs()
            # Rehash invalid typed sources: reject semantics, not merely checksum mismatch.
            sparse = next(d for d in kw['rule']['evidence'] if d['table'] == 'ItemSparse')
            if value is None:
                sparse['rows'][0].pop(field)
            else:
                sparse['rows'][0][field] = value
            sparse['source_sha256'] = _digest({k:sparse[k] for k in ('build','table','filter','rows','total')})
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                import_difficulty_variant(**kw, apply=True)
            self.assert_unchanged()
        kw = self.kwargs(); kw['rule']['initial_drop_kind'] = 'caller_claim'
        with self.assertRaises(ValidationError):
            import_difficulty_variant(**kw, apply=True)
        self.assert_unchanged()

    def test_unsafe_urls_bonus_sets_and_tooltip_values_never_write(self):
        root = 'https://nether.wowhead.com/tooltip/item/258045?locale=zhcn'
        for suffix in ('', '&dataEnv=0', '&dataEnv=1&bonus=0', '&dataEnv=1&bonus=',
                       '&dataEnv=1&bonus=999', '&dataEnv=1&bonus=1:1',
                       '&dataEnv=1&bonus=1&bonus=2', '&dataEnv=1&unknown=1', '&dataEnv=1&ilvl=1'):
            kw = self.kwargs(); kw['tooltip_evidence']['url'] = root + suffix
            with self.subTest(suffix=suffix), self.assertRaises(ValidationError):
                import_difficulty_variant(**kw, apply=True)
            self.assert_unchanged()
        for container, field, value in [('rule','bonus_ids',[999]), ('payload','quality',True),
                ('payload','quality',4), ('payload','itemLevel',2), ('tooltip_evidence','game_build',BUILD)]:
            kw = self.kwargs(); kw[container][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                import_difficulty_variant(**kw, apply=True)
            self.assert_unchanged()

    def test_missing_native_weapon_component_is_not_complete(self):
        for old, new in [('<!--dmg-->', ''), ('<!--dps-->', ''), ('<!--spd-->', ''),
                         ('<!--dmg-->1点伤害', '<!--dmg-->0点伤害'),
                         ('<!--dps-->（每秒伤害0.3点）', '<!--dps-->（每秒伤害0点）')]:
            kw = self.kwargs(); kw['payload']['tooltip'] = kw['payload']['tooltip'].replace(old, new)
            with self.subTest(old=old), self.assertRaises(ValidationError):
                import_difficulty_variant(**kw, apply=True)
            self.assert_unchanged()


class LosslessWeaponNumberTests(SimpleTestCase):
    def payload(self):
        return json.loads((FIXTURES / 'wow_item_weapon_222448_tooltip_enus.json').read_text())

    def test_real_enus_grouped_weapon_dps_is_lossless(self):
        payload = self.payload()
        stats, primary = _lossless_attributes(payload, _tooltip_details(payload))
        self.assertEqual((stats['min_damage'], stats['max_damage'], stats['weapon_dps'], stats['weapon_speed']),
                         (3995, 7420, 1585.0, 3.6))
        self.assertEqual(primary, {'agility': 2106, 'strength': 2106})

    def test_decimal_grouping_validation_precedes_comma_removal(self):
        for value in ('1,58', '1,,585', '1,585,', '01,585', '1,585.0.0', '0'):
            payload = self.payload()
            payload['tooltip'] = payload['tooltip'].replace('<!--dps-->(1,585 ', '<!--dps-->(' + value + ' ')
            with self.subTest(value=value), self.assertRaises(ValidationError):
                _lossless_attributes(payload, _tooltip_details(payload))
        for old, new, key, value in [('<!--dps-->(1,585 ', '<!--dps-->(1,585.125 ', 'weapon_dps', 1585.125),
                                     ('<!--spd-->3.60', '<!--spd-->1,234.8125', 'weapon_speed', 1234.8125)]:
            payload = self.payload(); payload['tooltip'] = payload['tooltip'].replace(old, new)
            stats, _ = _lossless_attributes(payload, _tooltip_details(payload))
            self.assertEqual(stats[key], value)
