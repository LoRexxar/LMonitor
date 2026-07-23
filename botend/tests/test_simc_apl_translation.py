from django.test import SimpleTestCase

from botend.services.simc_apl.translation import (
    TranslationDemand, extract_translation_demands, resolve_demand_mappings,
    translate_apl_ranges,
)


class SimcAplTranslationTests(SimpleTestCase):
    def test_extracts_typed_demands_from_actions_and_expressions(self):
        source = (
            "actions+=/bloodthirst,if=buff.enrage.up&debuff.armor_break.down\n"
            "actions+=/immolate,if=dot.immolate.remains<3&cooldown.fire_blast.ready\n"
            "actions+=/spell,if=talent.some_talent.enabled\n"
            "# buff.enrage.up and spell must remain comments\n"
        )

        demands = extract_translation_demands(source)

        self.assertEqual(
            [(item.kind, item.token) for item in demands],
            [
                ('action', 'bloodthirst'),
                ('buff', 'enrage'),
                ('debuff', 'armor_break'),
                ('action', 'immolate'),
                ('dot', 'immolate'),
                ('cooldown', 'fire_blast'),
                ('action', 'spell'),
                ('talent', 'some_talent'),
            ],
        )

    def test_range_translation_only_changes_typed_demand_sites(self):
        source = "actions=/bloodthirst,if=buff.enrage.up\n"
        result = translate_apl_ranges(
            source,
            {('action', 'bloodthirst'): '嗜血', ('buff', 'enrage'): '激怒'},
        )
        self.assertEqual(result, "actions=/嗜血,if=buff.激怒.up\n")

    def test_invalid_document_is_not_partially_translated(self):
        source = "actions=/bloodthirst,if=(buff.enrage.up\n"
        self.assertEqual(
            translate_apl_ranges(source, {('action', 'bloodthirst'): '嗜血'}),
            source,
        )

    def test_control_actions_never_resolve_as_spells(self):
        demands = extract_translation_demands("actions=/use_item,name=trinket\n")
        mapping, failures = resolve_demand_mappings(
            demands,
            [{'symbol_kind': 'action', 'token': 'use_item', 'spell_id': 123}],
            {('spell', 123): '不应使用'},
        )
        self.assertEqual(mapping, {})
        self.assertIn(('action', 'use_item', 'control_action'), failures)

    def test_typed_identity_resolution_does_not_conflate_spell_and_trait_ids(self):
        demands = (
            TranslationDemand('buff', 'enrage'),
            TranslationDemand('talent', 'titans_torment'),
        )
        facts = [
            {'symbol_kind': 'buff', 'token': 'enrage', 'spell_id': 184362},
            {'symbol_kind': 'talent', 'token': 'titans_torment',
             'trait_id': 999, 'spell_id': 390135},
        ]
        mapping, failures = resolve_demand_mappings(
            demands, facts,
            {('spell', 184362): '激怒', ('trait', 999): '泰坦之怒'},
        )
        self.assertEqual(mapping, {
            ('buff', 'enrage'): '激怒',
            ('talent', 'titans_torment'): '泰坦之怒',
        })
        self.assertEqual(failures, ())

    def test_conflicting_or_unlocalized_id_stays_unresolved(self):
        demands = (TranslationDemand('dot', 'doom'), TranslationDemand('buff', 'foo'))
        facts = [
            {'symbol_kind': 'dot', 'token': 'doom', 'spell_id': 1},
            {'symbol_kind': 'dot', 'token': 'doom', 'spell_id': 2},
            {'symbol_kind': 'buff', 'token': 'foo', 'spell_id': 3},
        ]
        mapping, failures = resolve_demand_mappings(demands, facts, {})
        self.assertEqual(mapping, {})
        self.assertIn(('dot', 'doom', 'conflicting_authoritative_identity'), failures)
        self.assertIn(('buff', 'foo', 'missing_current_zh_snapshot'), failures)
