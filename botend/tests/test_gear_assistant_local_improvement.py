"""Real ORM counterexamples for the final, bounded single-slot search pass."""
from django.test import TestCase

from botend.models import GearBuilderOwnedItem, WowItemVariantSnapshot
from botend.services.gear_assistant import (
    _apply_enhancements, _beam_plan, _distance, _fixed_entries, _owned_pool,
    _percentages,
)
from botend.services.gear_builder import EQUIPMENT_SLOTS
from botend.tests.test_gear_assistant_priorities import GearAssistantDataMixin


class GearAssistantLocalImprovementTests(GearAssistantDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        for slot in ('back', 'wrists'):
            self.base[slot].is_intrinsic_embellishment = True
            self.base[slot].save(update_fields=['is_intrinsic_embellishment'])

    def search(self, mode='all', *, fixed=None, **kwargs):
        rows = list(WowItemVariantSnapshot.objects.filter(
            season=self.season,
            variant_type__in=('drop_equipment', 'crafted_equipment'),
        ).select_related('item'))
        return _beam_plan(mode, rows, _owned_pool(self.user, 'Warrior', 'Fury'),
                          fixed or {}, 'Warrior', 'Fury', self.target, self.conversion, **kwargs)

    def pruning_counterexample(self):
        # The production width is 600 (not mocked). All 600 head states match
        # the 1/16 progress target exactly and evict the final-target choice.
        # Later slots contribute nothing, so a one-head replacement dominates.
        self.target = _percentages({'crit': 1600}, self.conversion)
        for _ in range(600):
            self.variant('head', stats={'crit': 100}, level=331)
        return self.variant('head', stats={'crit': 1600}, level=334)

    def assert_legal(self, plan):
        equipment = plan['equipment']
        self.assertEqual(set(equipment), {slot for slot, _ in EQUIPMENT_SLOTS})
        self.assertEqual(len({row['variant'].item_id for row in equipment.values()}), len(equipment))
        self.assertEqual(plan['embellishment_count'], 2)
        self.assertLessEqual(plan['delve_myth_count'], 2)

    def test_real_width_pruning_recovers_higher_level_better_single_slot(self):
        better = self.pruning_counterexample()
        for mode in ('all', 'prefer_owned', 'dungeon'):
            with self.subTest(mode=mode):
                plan = self.search(mode)
                self.assertEqual(plan['equipment']['head']['variant'].id, better.id)
                self.assertEqual(plan['stats']['crit'], 1600)
                self.assertEqual(_distance(plan['stats'], self.target, self.conversion), 0)
                self.assertEqual(plan['effect_count'], 0)
                self.assertEqual(plan['source_preference_count'], 0)
                self.assertEqual(plan['embellishment_slot_cost'], 0)
                self.assertEqual(plan['delve_lower_count'], 0)
                self.assert_legal(plan)

    def test_final_flask_score_keeps_original_when_equipment_only_gain_regresses(self):
        self.target = _percentages({'crit': 100}, self.conversion)
        for _ in range(600):
            self.variant('head', level=331)
        equipment_best = self.variant('head', stats={'crit': 100}, level=334)
        for plan in self.optimize(flask='crit')['plans']:
            # Bare gear 100 is better than 0, but mandatory +165 makes 0 better.
            self.assertNotEqual(plan['equipment']['head']['variant']['id'], equipment_best.id)
            self.assertEqual(plan['stats']['crit'], 165)

    def test_single_pass_is_safe_without_claiming_global_or_local_optimality(self):
        globally_best = self.pruning_counterexample()
        first_improvement = self.variant('head', stats={'crit': 1500}, level=334)
        self.variant('off_hand', stats={'crit': 300})
        plan = self.search()
        # Beam: 100+300. Head pass: 1500+300. Off-hand pass: 1500+0.
        # Revisiting head would reach 1600+0, but there is deliberately no loop
        # until convergence (nor any promise of global optimality).
        self.assertEqual(plan['equipment']['head']['variant'].id, first_improvement.id)
        self.assertEqual(plan['equipment']['off_hand']['variant'].id, self.base['off_hand'].id)
        self.assertLess(_distance(plan['stats'], self.target, self.conversion),
                        _distance({'crit': 400}, self.target, self.conversion))
        self.assertGreater(_distance(plan['stats'], self.target, self.conversion),
                           _distance(globally_best.stats_json, self.target, self.conversion))
        self.assert_legal(plan)

    def test_full_priority_prefix_beats_closer_or_higher_level_replacements(self):
        effect = self.variant('head', stats={'crit': 900}, level=331,
                              effects=[{'description_zh': '装备：攻击触发火焰。'}])
        raid = self.variant('neck', stats={'crit': 900}, level=331)
        raid.source_json = [{'type': 'raid'}]
        raid.save(update_fields=['source_json'])
        owned = self.variant('shoulders', stats={'crit': 900}, level=331)
        GearBuilderOwnedItem.objects.create(user=self.user, item_id=owned.item.item_id,
                                           variant=owned, slot_key='shoulders')
        # Higher level never compensates for worse green stats across items.
        self.variant('feet', stats={'crit': 100}, level=999)
        hero = self.variant('chest', level=999)
        hero.source_json = [{'type': 'delve'}]
        hero.upgrade_track = 'hero'
        hero.save(update_fields=['source_json', 'upgrade_track'])
        # A prettier green match cannot move the two embellishments off small slots.
        for slot in ('back', 'wrists'):
            self.base[slot].stats_json = {'crit': 100}
            self.base[slot].save(update_fields=['stats_json'])
        self.variant('legs', intrinsic=True)
        plan = self.search('prefer_owned', source_preference='raid')
        for slot, expected in [('head', effect), ('neck', raid), ('shoulders', owned),
                               ('feet', self.base['feet']), ('chest', self.base['chest']),
                               ('back', self.base['back']), ('wrists', self.base['wrists'])]:
            self.assertEqual(plan['equipment'][slot]['variant'].id, expected.id)
        self.assertEqual(plan['effect_count'], 1)
        self.assertEqual(plan['source_preference_count'], 1)
        self.assertEqual(sum(plan['owned'].values()), 1)
        self.assertEqual(plan['embellishment_slot_cost'], 0)
        self.assertEqual(plan['delve_lower_count'], 0)
        self.assert_legal(plan)

    def test_locks_and_fixed_enhancements_survive_without_double_counting(self):
        material = self.reagent(stats={'crit': 17})
        craft = self.variant('back', crafted=True, stats={'crit': 100, 'haste': 100})
        gem = self.reagent(stats={'crit': 23})
        gem.variant_type = WowItemVariantSnapshot.TYPE_GEM
        gem.save(update_fields=['variant_type'])
        enchant = self.reagent(stats={'crit': 31})
        enchant.variant_type = WowItemVariantSnapshot.TYPE_ENCHANT
        enchant.save(update_fields=['variant_type'])
        fixed = _fixed_entries({'back': {
            'variant': {'id': craft.id}, 'selectedStats': ['crit', 'haste'],
            'embellishment': {'variant': {'id': material.id}},
            'gems': [{'variant': {'id': gem.id}}],
            'enchant': {'variant': {'id': enchant.id}}, 'addedSocket': True,
        }}, 'Warrior', 'Fury')
        # Exact target would prefer another carrier, but the lock is immutable.
        self.variant('back', crafted=True, level=999)
        better = self.pruning_counterexample()
        # Fixed attachment is included in the equipment-stage objective, unlike
        # gems/enchants which are applied once afterwards.
        self.target = _percentages({'crit': 1600 + fixed['back']['stats']['crit'] + 17,
                                    'haste': fixed['back']['stats']['haste']}, self.conversion)
        plan = self.search(fixed=fixed, materials=[material])
        self.assertEqual(plan['equipment']['head']['variant'].id, better.id)
        selected = plan['equipment']['back']
        self.assertEqual(selected['variant'].id, craft.id)
        self.assertEqual(selected['fixed_enhancements'], fixed['back']['fixed_enhancements'])
        equipment_crit = sum(row['stats']['crit'] for row in plan['equipment'].values())
        self.assertEqual(plan['stats']['crit'], equipment_crit + 17)
        _apply_enhancements(plan, self.season, 'Warrior', 'Fury', self.target, self.conversion,
                            False, False, variants={})
        self.assertEqual(plan['stats']['crit'], equipment_crit + 17 + 23 + 31)
        self.assertEqual(plan['enhancements']['back']['gems'], [gem])
        self.assertEqual(plan['enhancements']['back']['enchant'], enchant)
        self.assertTrue(plan['enhancements']['back']['added_socket'])
        self.assert_legal(plan)
