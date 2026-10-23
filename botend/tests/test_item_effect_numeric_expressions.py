"""Numeric effect display is a read-only projection, including shared text storage."""
from copy import deepcopy

from django.test import SimpleTestCase

from botend.models import WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_builder import serialize_item
from botend.services.variant_text import catalog_text, encode
from botend.services.wow_item_effects import compile_effects, resolve_effects


class ItemEffectNumericExpressionTests(SimpleTestCase):
    def test_phoenix_shared_templates_project_current_values_without_rewriting_source(self):
        # Captured public variant 50886, item level 337; no scaling is inferred.
        original = (
            '装备： 你的法术和技能有几率唤起不死鸟之焰，将你的目标包围其中。 · '
            '若你的目标是友方，为其在10秒内恢复[1 * 1475 * (20)]点生命值。'
            '若你的目标是敌人，其在10秒内受到[1 * 983 * (20)]点火焰伤害。 · '
            '镶嵌宝石每有一种不同的颜色，使烈焰效果提高1%。'
        )
        item = WowItemSnapshot(pk=1, item_id=241139, name_zh='萨拉斯凤凰饰环',
            catalog_type='equipment', slot_key='neck', inventory_type=2,
            description_zh=original.replace(' · ', '\n'))
        variant = WowItemVariantSnapshot(pk=50886, item=item, item_level=337,
            variant_key='captured', variant_type='crafted_equipment', data_branch='retail',
            compatible_slots=['neck'], stats_json={'stamina': 2277, 'haste': 224, 'mastery': 188},
            effects_json=[{'description_zh': original}])
        item.effects, refs = compile_effects([variant])
        variant.effect_values = refs[variant.pk]
        variant.effects_json = encode(variant.effects_json, item.variant_text_templates)
        before = deepcopy((variant.effects_json, variant.effect_values,
                           item.effects, item.variant_text_templates, item.description_zh))
        # Use the real storage decoder, central effect reader and catalog serializers.
        result = serialize_item(item, [variant], 'Warrior', 'Fury')
        packed = catalog_text({'items': [result]})
        shown = catalog_text(packed, expand=True)['items'][0]
        for text in (shown['display_description'], shown['variants'][0]['tooltip'],
                     shown['variants'][0]['effects'][0]['description_zh']):
            # The existing item-text projection removes thousands separators.
            self.assertIn('恢复29500点生命值', text)
            self.assertIn('受到19660点火焰伤害', text)
            self.assertIn('在10秒内', text)
            self.assertIn('提高1%', text)
            self.assertNotIn('[1 *', text)
        self.assertEqual(shown['variants'][0]['item_level'], 337)
        self.assertEqual(variant.effects_data, [{'description_zh': original}])
        self.assertEqual(before, (variant.effects_json, variant.effect_values,
                                item.effects, item.variant_text_templates, item.description_zh))

    def test_numeric_projection_is_generic_and_preserves_unresolved_expressions(self):
        cases = {
            '[1 * 1475 * (20)]': '29,500',
            '[1 * 983 * (20)]': '19,660',
            '[ (1,475 + 5) * 2 ]': '2,960',
            '[(2 + 3) * 4 / 8]': '2.5',
            '[-2 * (3 + 4)]': '-14',
            '[0 * 20]': '0',
            '[20]': '[20]',
            '[1 * $s1 * (20)]': '[1 * $s1 * (20)]',
            '[1 * {v0} * (20)]': '[1 * {v0} * (20)]',
            '[1 * 数值待确认 * (20)]': '[1 * 数值待确认 * (20)]',
            '[1 / 0]': '[1 / 0]',
            '[1,47 * 20]': '[1,47 * 20]',
            '[2 ** 100000]': '[2 ** 100000]',
            '[3 // 2]': '[3 // 2]',
            '[1秒 * 2]': '[1秒 * 2]',
            '[True * 2]': '[True * 2]',
            '[__import__("os").getcwd()]': '[__import__("os").getcwd()]',
            '[x + [2 * 3]]': '[x + [2 * 3]]',
            '$?s123[2 * 3][4 * 5]': '$?s123[2 * 3][4 * 5]',
            '[' + '9' * 600 + '*2]': '[' + '9' * 600 + '*2]',
            '[' + '9' * 200 + '*' + '9' * 200 + ']': '[' + '9' * 200 + '*' + '9' * 200 + ']',
        }
        for source, expected in cases.items():
            with self.subTest(source=source[:80]):
                raw = [{'description_zh': '装备：' + source, 'template': source}]
                before = deepcopy(raw)
                result, status = resolve_effects(None, item_id=999999,
                    raw_effects=raw, branch='retail')
                self.assertEqual(result[0]['description_zh'], '装备：' + expected)
                self.assertEqual(result[0]['template'], source)
                self.assertEqual(status, 'localized')
                self.assertEqual(raw, before)
        english, status = resolve_effects(None, raw_effects=[{'description': 'Equip: [2 * 3] damage.'}])
        self.assertEqual(english[0]['description'], 'Equip: 6 damage.')
        self.assertEqual(status, 'untranslated')

    def test_reference_without_variant_does_not_reuse_numeric_values(self):
        item = WowItemSnapshot(pk=1, item_id=999999)
        variant = WowItemVariantSnapshot(pk=1, item=item, item_level=337,
            effects_json=[{'description_zh': '装备：恢复[1 * 1475 * (20)]点生命值。'}])
        item.effects, _ = compile_effects([variant])
        result, status = resolve_effects(item, allow_reference=True)
        self.assertEqual(status, 'reference')
        self.assertIn('数值待确认', result[0]['description_zh'])
        self.assertNotIn('29,500', result[0]['description_zh'])
