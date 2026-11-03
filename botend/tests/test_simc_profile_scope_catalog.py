"""只在可选天赋配置中出现的全局目录也必须进入上表，且不能跨专精借用。"""
from unittest import TestCase

from botend.services.simc_skill_damage import classify_global_skill_effects, reviewed_global_display_effects


class ProfileScopeCatalogTests(TestCase):
    def test_racial_fact_cannot_return_through_variant_catalog_union(self):
        base = {**self.actor(), 'excluded_racial_spell_ids': [700]}
        selected = {**base, 'reviewed_global_effects': [self.fact()]}
        catalog = {}
        classify_global_skill_effects(base, base, [{'high': selected, 'low': selected}], scope_catalog=catalog)
        self.assertEqual(catalog, {})

    def actor(self, spec='fury'):
        return {'class': 'warrior', 'spec': spec, 'actions': [], 'global_damage_policy': 'exclude_before_probe'}

    def fact(self):
        return {'effect_id': 'semantic_scope:700', 'source_type': 'reviewed_scope', 'source_kind': 'buff',
                'source_class': 'warrior', 'specializations': ['fury'], 'source_spell_ids': [700],
                'source_name': '可选配置公共效果', 'excluded_before_probe': True,
                'global_components': [{'spell_id': 700, 'effect_index': 1, 'effect_id': 701}],
                'effect_details': [{'source_spell_id': 700, 'effect_index': 1, 'base_value': 10, 'value_kind': 'percent'}],
                'projections': []}

    def test_variant_only_fact_survives_display_projection_and_is_owned(self):
        base = self.actor()
        selected = {**base, 'reviewed_global_effects': [self.fact()]}
        catalog = {}
        variants = [{'talent': {'id': 1, 'node_id': 1, 'tree_type': 'spec'},
                     'high': selected, 'low': selected, 'reference_high': base, 'reference_low': base}]
        effects = classify_global_skill_effects(base, base, variants, scope_catalog=catalog)
        self.assertEqual(len(catalog), 1)
        actor = {**base, 'reviewed_global_effects': list(catalog.values()), 'global_skill_effects': effects}
        displayed = reviewed_global_display_effects(actor)
        self.assertTrue(any(700 in f['source_spell_ids'] for f in displayed))
        selected['reviewed_global_effects'][0]['effect_details'][0]['base_value'] = 999
        self.assertEqual(list(catalog.values())[0]['effect_details'][0]['base_value'], 10)

    def test_wrong_actor_or_fact_specialization_cannot_enter_catalog(self):
        base = self.actor()
        for wrong in ({**self.actor('arms'), 'reviewed_global_effects': [self.fact()]},
                      {**base, 'reviewed_global_effects': [{**self.fact(), 'specializations': ['arms']}]},
                      {**base, 'class': 'mage', 'reviewed_global_effects': [self.fact()]}):
            with self.subTest(actor=wrong['class'], spec=wrong['spec']):
                catalog = {}
                classify_global_skill_effects(base, base, [{'high': wrong, 'low': wrong}], scope_catalog=catalog)
                self.assertEqual(catalog, {})
