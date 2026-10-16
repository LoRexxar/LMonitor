"""Current captured tooltip values, not historical overlay values."""
from copy import deepcopy
from django.test import SimpleTestCase, TestCase


class ItemEffectProjectionTests(TestCase):
    def test_real_catalog_and_strict_display_keep_current_values_and_frozen_effects(self):
        from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
        from botend.services.gear_builder import serialize_item
        from botend.services.wow_item_display import item_display_metadata
        season = SeasonMeta.objects.create(season_key='effect-language', season_name='Language',
            is_active=True, gear_batch_key='language', mplus_zone_id=1, raid_zone_id=2)
        item = WowItemSnapshot.objects.create(item_id=281029, name='Acidbrood Scourge',
            catalog_type='equipment', slot_key='finger')
        effects = [{'description': "Equip: Your attacks have a chance to amplify the band's whispers, calling forth an Acidbrood Scourge. The scourge flies into your target and detonates, causing (1 * 3748) Nature damage to all nearby enemies."}]
        variant = WowItemVariantSnapshot.objects.create(item=item, season=season, batch_key='language',
            data_branch='ptr', variant_key='current', variant_type='drop_equipment', item_level=292,
            stats_json={'haste': 84}, effects_json=effects)
        display = item_display_metadata(item.item_id, item, variant=variant, strict_difficulty=True)
        catalog = serialize_item(item, [variant])
        self.assertIn('酸巢灾虫', display['tooltip'])
        self.assertIn('(1 * 3748)', display['tooltip'])
        self.assertIn('酸巢灾虫', catalog['display_description'])
        # 缺少中央基础记录时，已有编号与原文仍能使用线上中文补充。
        standalone = item_display_metadata(item.item_id, effects=effects, is_ptr=True)
        self.assertIn('酸巢灾虫', standalone['tooltip'])
        variant.refresh_from_db()
        self.assertEqual(variant.effects_json, effects)


class ItemEffectLocalizationTests(SimpleTestCase):
    def test_current_english_values_are_projected_without_mutating_frozen_facts(self):
        from botend.services.item_effect_localization import localize_item_effects
        source = [{'description': "Equip: Your attacks have a chance to amplify the band's whispers, calling forth an Acidbrood Scourge. The scourge flies into your target and detonates, causing (1 * 3748) Nature damage to all nearby enemies."}]
        before = deepcopy(source)
        result = localize_item_effects(281029, source, branch='ptr')
        self.assertEqual(source, before)
        self.assertEqual(result[0]['description'], source[0]['description'])
        self.assertIn('酸巢灾虫', result[0]['description_zh'])
        self.assertIn('(1 * 3748)', result[0]['description_zh'])
        self.assertNotIn('702.67', result[0]['description_zh'])
        self.assertEqual(localize_item_effects(281029, source, branch='retail'), source)
        self.assertEqual(localize_item_effects(1, source, branch='ptr'), source)
        wrong = [{'description': source[0]['description'].replace('Nature damage', 'Fire damage')}]
        self.assertEqual(localize_item_effects(281029, wrong, branch='ptr'), wrong)
        self.assertEqual(localize_item_effects(281235, [], branch='ptr'), [])

    def test_current_durations_conditions_and_cooldown_keep_values(self):
        from botend.services.item_effect_localization import localize_item_effects
        source = [{'description': 'Use: Bless the ground around you for 10 sec, granting you a holy barrier which absorbs the next 1017432 damage taken as long as you remain within. Allies who stand upon the ground will also redirect 10% of their incoming damage to you until the barrier falls. · Valid only for tank specializations. (3 Min Cooldown)'}]
        text = localize_item_effects(280617, source, branch='ptr')[0]['description_zh']
        self.assertIn('1017432', text)
        self.assertIn('10秒', text)
        self.assertIn('坦克', text)
        self.assertIn('3分钟', text)
        self.assertNotRegex(text, r'[A-Za-z]{3,}|\$')
