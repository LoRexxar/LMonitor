from django.contrib.auth import get_user_model
from django.test import TestCase

from botend.models import GearBuilderOwnedItem, SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_assistant import (
    _apply_enhancements, _beam_plan, _candidate, _conversion, _percentages, optimize_loadouts,
)
from botend.services.gear_builder import EQUIPMENT_SLOTS, GearBuilderError


class GearAssistantDataMixin:
    """Exercise the real ORM/catalog/search/enhancement path, without solver mocks."""

    def setUp(self):
        self.season = SeasonMeta.objects.create(
            season_key='assistant-priorities', season_name='Assistant priorities',
            is_active=True, gear_batch_key='priorities', mplus_zone_id=1, raid_zone_id=2,
        )
        self.user = get_user_model().objects.create_user(username='assistant-priorities')
        self.next_item_id = 810000
        self.base = {slot: self.variant(slot) for slot, _label in EQUIPMENT_SLOTS}
        self.conversion = _conversion('Warrior', 'Fury')
        self.target = _percentages({}, self.conversion)

    def variant(self, slot, *, stats=None, effects=None, level=700, intrinsic=False,
                crafted=False, item=None, unique_group='', max_equipped=0):
        self.assertIn(slot, dict(EQUIPMENT_SLOTS))
        self.next_item_id += 1
        item = item or WowItemSnapshot.objects.create(
            item_id=self.next_item_id, name=f'Equipment {self.next_item_id}',
            catalog_type='crafted_equipment' if crafted else 'equipment',
            slot_key=slot, eligible_specs=['Warrior:Fury'],
            metadata={'crafting_reagent_slot_ids': [100]},
        )
        return WowItemVariantSnapshot.objects.create(
            item=item, season=self.season, batch_key='priorities',
            variant_key=f'variant-{self.next_item_id}', item_level=level,
            variant_type=(WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT if crafted
                          else WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT),
            compatible_slots=[slot], stats_json=stats or {}, effects_json=effects or [],
            crafting_options={'stat_count': 2, 'stat_pool': ['crit', 'haste']},
            source_json=[{'type': 'mythic_plus'}],
            is_intrinsic_embellishment=intrinsic, unique_group=unique_group,
            max_equipped=max_equipped,
        )

    def reagent(self, *, slots=None, recipe_slots=None, stats=None):
        self.next_item_id += 1
        item = WowItemSnapshot.objects.create(
            item_id=self.next_item_id, name=f'Reagent {self.next_item_id}',
            catalog_type='embellishment',
        )
        return WowItemVariantSnapshot.objects.create(
            item=item, season=self.season, batch_key='priorities',
            variant_key='embellishment', variant_type=WowItemVariantSnapshot.TYPE_EMBELLISHMENT,
            compatible_slots=slots or [slot for slot, _label in EQUIPMENT_SLOTS],
            metadata={'reagent_slot_ids': recipe_slots if recipe_slots is not None else [100]},
            stats_json=stats or {'crit': 10}, effects_json=[{'description_zh': '装备：攻击有几率强化你。'}],
        )

    def optimize(self, **overrides):
        return optimize_loadouts(self.user, {
            'class_name': 'Warrior', 'spec_name': 'Fury', 'target': self.target,
            'include_gems': False, 'include_enchants': False, 'flask': 'none',
            **overrides,
        })

class GearAssistantPriorityTests(GearAssistantDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        # Independent zero-effect intrinsic fixtures satisfy the new mandatory
        # count without changing the effect/green-stat comparisons under test.
        self.required = [self.variant(slot, intrinsic=True) for slot in ('wrists', 'back')]

    def test_set_bonus_tooltip_is_not_a_per_item_special_effect(self):
        tier = self.variant('head', stats={'crit': 900}, effects=[
            {'description_zh': '(2) 组合 狂怒: 怒击的伤害提高15%。'},
            {'description': '(4) Set: Bloodthirst damage increased.'},
        ])
        for plan in self.optimize()['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], self.base['head'].id)
            self.assertEqual(plan['effect_count'], 0)
        # A real independent effect on a set piece still receives priority.
        tier.effects_json.append({'description_zh': '装备：攻击有几率触发火焰。'})
        tier.save(update_fields=['effects_json'])
        for plan in self.optimize()['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], tier.id)
            self.assertEqual(plan['effect_count'], 1)
            self.assertEqual(len(plan['equipment']['head']['resolvedEffects']), 3)

    def test_unresolved_catalog_does_not_abort_locked_or_owned_equipment(self):
        unknown = self.variant('head', effects=[{'description_zh': '装备：攻击触发火焰。'}])
        unknown.upgrade_track = 'myth'
        unknown.source_json = [{'type': 'raid', 'instance_id': 999, 'encounter_id': 888}]
        unknown.save(update_fields=['upgrade_track', 'source_json'])
        locked = {slot: {'variant': {'id': row.id}} for slot, row in self.base.items()}
        locked.update({row.compatible_slots[0]: {'variant': {'id': row.id}} for row in self.required})
        locked['head'] = {'variant': {'id': unknown.id}}
        for plan in self.optimize(equipment=locked, allow_mythic_last_two=False)['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], unknown.id)
            self.assertEqual(plan['equipped_count'], len(EQUIPMENT_SLOTS))
        GearBuilderOwnedItem.objects.create(user=self.user, item_id=unknown.item.item_id,
                                           variant=unknown, slot_key='head')
        plans = {p['key']: p for p in self.optimize(allow_mythic_last_two=False)['plans']}
        self.assertEqual(plans['prefer_owned']['equipment']['head']['variant']['id'], unknown.id)
        self.assertEqual(plans['all']['equipment']['head']['variant']['id'], self.base['head'].id)

    def test_effect_equipment_outranks_target_and_owned_then_target_breaks_effect_ties(self):
        self.base['head'].effects_json = [{'description_zh': '+10 暴击'}]
        self.base['head'].stats_json = {'crit': 10}
        self.base['head'].save(update_fields=['effects_json', 'stats_json'])
        GearBuilderOwnedItem.objects.create(
            user=self.user, item_id=self.base['head'].item.item_id, variant=self.base['head'], slot_key='head',
        )
        farther = self.variant('head', stats={'crit': 900}, effects=[{'description_zh': '装备：攻击触发火焰。'}])
        closer = self.variant('head', stats={'crit': 500}, effects=[{'description_zh': '装备：攻击触发冰霜。'}])
        result = self.optimize()
        for plan in result['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], closer.id)
            self.assertNotEqual(closer.id, farther.id)
            self.assertEqual(plan['effect_count'], 1)
            self.assertEqual(plan['embellishment_count'], 2)
            self.assertEqual(plan['average_item_level'], 700)
        self.assertIn('特效', result['explanation'])
        self.assertIn('不代表', result['explanation'])

    def test_owned_mode_remains_distinct_when_effect_counts_tie(self):
        owned = self.variant('head', stats={'crit': 900})
        GearBuilderOwnedItem.objects.create(user=self.user, item_id=owned.item.item_id, variant=owned, slot_key='head')
        plans = {plan['key']: plan for plan in self.optimize()['plans']}
        self.assertEqual(plans['prefer_owned']['equipment']['head']['variant']['id'], owned.id)
        self.assertEqual(plans['all']['equipment']['head']['variant']['id'], self.base['head'].id)

    def test_equal_target_distance_prefers_higher_total_item_level(self):
        higher = self.variant('head', level=740)
        plan = _beam_plan('all', [*self.base.values(), *self.required, higher], {}, {},
                          'Warrior', 'Fury', self.target, self.conversion)
        self.assertEqual(plan['equipment']['head']['variant'].id, higher.id)

    def test_same_item_only_highest_catalog_level_even_if_lower_matches_target(self):
        higher = self.variant('head', item=self.base['head'].item, level=740, stats={'crit': 500})
        # Remove other head items: max-level policy must not optimize by downgrading this item.
        for plan in self.optimize()['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], higher.id)

    def test_intrinsic_cap_is_global_even_without_unique_group(self):
        for slot in ('head', 'neck', 'shoulders'):
            self.variant(slot, intrinsic=True, effects=[{'description_zh': '装备：攻击触发火焰。'}])
        for plan in self.optimize()['plans']:
            intrinsic = sum(entry['variant']['is_intrinsic_embellishment'] for entry in plan['equipment'].values())
            self.assertEqual(intrinsic, 2)
            self.assertEqual(plan['embellishment_count'], 2)
            self.assertEqual(plan['effect_count'], 2)

    def test_later_locked_attachments_reserve_capacity_before_search(self):
        fixed = {}
        reagent = self.reagent()
        for slot in ('finger1', 'finger2'):
            variant = self.variant(slot, crafted=True)
            fixed[slot] = {'variant': {'id': variant.id}, 'embellishment': {'variant': {'id': reagent.id}}}
        self.variant('head', intrinsic=True, effects=[{'description_zh': '装备：攻击触发火焰。'}])
        for plan in self.optimize(equipment=fixed)['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], self.base['head'].id)
            self.assertEqual(plan['embellishment_count'], 2)
            for slot in fixed:
                self.assertEqual(plan['equipment'][slot]['embellishment']['variant']['id'], reagent.id)

    def enhancement_plan(self, entries):
        equipment = {}
        for slot, variant, reagent in entries:
            candidate = _candidate(variant, 'Warrior', 'Fury', ['crit', 'haste'])
            if reagent:
                candidate['fixed_enhancements'] = {'embellishment': reagent}
            equipment[slot] = candidate
        return {'equipment': equipment, 'stats': {}}

    def enhance(self, plan):
        _apply_enhancements(plan, self.season, 'Warrior', 'Fury',
                            _percentages({'crit': 1000}, self.conversion), self.conversion, False, False)

    def test_auto_addition_reserves_later_fixed_and_counts_duplicate_reagents(self):
        reagent = self.reagent()
        early = self.variant('head', crafted=True)
        late1 = self.variant('finger1', crafted=True)
        late2 = self.variant('finger2', crafted=True)
        plan = self.enhancement_plan([('head', early, None), ('finger1', late1, reagent), ('finger2', late2, reagent)])
        self.enhance(plan)
        self.assertIsNone(plan['enhancements']['head']['embellishment'])
        self.assertEqual(sum(bool(row['embellishment']) for row in plan['enhancements'].values()), 2)
        self.assertEqual(plan['stats']['crit'], 20)

    def test_intrinsic_uses_capacity_and_never_gets_an_attachment(self):
        self.reagent()
        innate = self.variant('head', crafted=True, intrinsic=True)
        plain = self.variant('neck', crafted=True)
        later = self.variant('shoulders', crafted=True)
        plan = self.enhancement_plan([('head', innate, None), ('neck', plain, None), ('shoulders', later, None)])
        self.enhance(plan)
        self.assertIsNone(plan['enhancements']['head']['embellishment'])
        self.assertEqual(sum(bool(row['embellishment']) for row in plan['enhancements'].values()), 1)

    def test_locked_over_cap_rejected_not_silently_stripped(self):
        reagent = self.reagent()
        fixed = {}
        for slot in ('head', 'neck', 'shoulders'):
            variant = self.variant(slot, crafted=True)
            fixed[slot] = {'variant': {'id': variant.id}, 'embellishment': {'variant': {'id': reagent.id}}}
        with self.assertRaisesRegex(GearBuilderError, '美化.*2'):
            self.optimize(equipment=fixed)

    def test_locked_intrinsic_over_cap_rejected(self):
        fixed = {}
        for slot in ('head', 'neck', 'shoulders'):
            variant = self.variant(slot, intrinsic=True)
            fixed[slot] = {'variant': {'id': variant.id}}
        with self.assertRaisesRegex(GearBuilderError, '美化.*2'):
            self.optimize(equipment=fixed)

    def test_locked_incompatible_attachment_rejected_by_recipe_facts(self):
        reagent = self.reagent(recipe_slots=[999])
        crafted = self.variant('head', crafted=True)
        with self.assertRaisesRegex(GearBuilderError, '美化.*不兼容'):
            self.optimize(equipment={'head': {'variant': {'id': crafted.id},
                                             'embellishment': {'variant': {'id': reagent.id}}}})

    def test_auto_attachment_requires_authoritative_recipe_compatibility(self):
        self.reagent(recipe_slots=[999])
        crafted = self.variant('head', crafted=True)
        plan = self.enhancement_plan([('head', crafted, None)])
        with self.assertRaisesRegex(GearBuilderError, '美化.*2'):
            self.enhance(plan)

    def test_locked_intrinsic_cannot_also_have_an_attachment(self):
        reagent = self.reagent()
        intrinsic = self.variant('head', crafted=True, intrinsic=True)
        with self.assertRaisesRegex(GearBuilderError, '美化.*不兼容'):
            self.optimize(equipment={'head': {'variant': {'id': intrinsic.id},
                                             'embellishment': {'variant': {'id': reagent.id}}}})

    def test_duplicate_intrinsic_items_consume_two_and_block_added_embellishment(self):
        self.reagent()
        intrinsic = self.variant('finger1', intrinsic=True)
        intrinsic.compatible_slots = ['finger1', 'finger2']
        intrinsic.save(update_fields=['compatible_slots'])
        crafted = self.variant('head', crafted=True)
        plan = self.enhancement_plan([
            ('head', crafted, None), ('finger1', intrinsic, None), ('finger2', intrinsic, None),
        ])
        self.enhance(plan)
        self.assertEqual(plan['embellishment_count'], 2)
        self.assertIsNone(plan['enhancements']['head']['embellishment'])

    def test_effect_priority_never_overrides_locked_unique_equipment(self):
        fixed = self.variant('finger2', unique_group='one-special-ring', max_equipped=1)
        self.variant('finger1', unique_group='one-special-ring', max_equipped=1,
                     effects=[{'description_zh': '装备：触发火焰。'}])
        for plan in self.optimize(equipment={'finger2': {'variant': {'id': fixed.id}}})['plans']:
            self.assertEqual(plan['equipment']['finger2']['variant']['id'], fixed.id)
            self.assertEqual(plan['equipment']['finger1']['variant']['id'], self.base['finger1'].id)

    def test_locked_mixed_intrinsic_and_added_over_cap_rejected(self):
        reagent = self.reagent()
        intrinsic = self.variant('head', intrinsic=True)
        fixed = {'head': {'variant': {'id': intrinsic.id}}}
        for slot in ('finger1', 'finger2'):
            crafted = self.variant(slot, crafted=True)
            fixed[slot] = {'variant': {'id': crafted.id}, 'embellishment': {'variant': {'id': reagent.id}}}
        with self.assertRaisesRegex(GearBuilderError, '美化.*2'):
            self.optimize(equipment=fixed)

    def test_beam_preserves_capacity_for_mandatory_later_intrinsics(self):
        # Over 600 tempting head choices must not eliminate the only feasible path.
        intrinsic_heads = [self.variant('head', intrinsic=True, effects=[{'description_zh': '装备：触发火焰。'}])
                           for _ in range(601)]
        for slot in ('finger1', 'finger2'):
            self.base[slot].is_intrinsic_embellishment = True
            self.base[slot].effects_json = [{'description_zh': '装备：触发冰霜。'}]
            self.base[slot].save(update_fields=['is_intrinsic_embellishment', 'effects_json'])
        plan = _beam_plan('all', [*intrinsic_heads, *self.base.values()], {}, {},
                          'Warrior', 'Fury', self.target, self.conversion)
        self.assertEqual(plan['equipment']['head']['variant'].id, self.base['head'].id)
        self.assertEqual(sum(row['variant'].is_intrinsic_embellishment for row in plan['equipment'].values()), 2)
