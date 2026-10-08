"""Central recipe relationships, not equipment slot guesses, authorize embellishments."""
from django.test import TestCase

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.gear_builder import (
    GearBuilderError, enhancement_items, resolve_crafted_variant, slot_matches,
)
from botend.services.simc_equipment_eligibility import EquipmentEligibility


class EmbellishmentEligibilityTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(
            season_key='embellishment-test', season_name='Test', mplus_zone_id=1,
            raid_zone_id=2, is_active=True, gear_batch_key='current')
        self.ring = self.carrier(900001, 'finger', 11, [101])
        self.wrists = self.carrier(900002, 'wrists', 9, [202])
        self.other_recipe = self.carrier(900003, 'finger', 11, [303])
        self.unknown = self.carrier(900004, 'finger', 11, None)
        self.fixed = self.carrier(900005, 'finger', 11, [101], intrinsic=True)
        self.iris = self.material(910001, 'prismatic_focusing_iris', 13453)
        # Real-world token spelling drift must not defeat the effect bonus join.
        self.snake = self.material(910002, 'coiled_snake_eye', 13769)

    def carrier(self, item_id, slot, inventory, slots, intrinsic=False):
        metadata = {'primary_stat_options': ['strength']}
        if slots is not None:
            metadata.update(crafting_reagent_slot_ids=slots,
                            crafting_profession_id=755, crafting_recipe_spell_id=item_id)
        item = WowItemSnapshot.objects.create(item_id=item_id, slot_key=slot,
            inventory_type=inventory, item_class_id=4,
            item_subclass_id=4 if inventory == 9 else 0, metadata=metadata)
        return WowItemVariantSnapshot.objects.create(item=item, season=self.season,
            batch_key='current', game_build='12.1.0.69933', variant_key=str(item_id),
            variant_type='crafted_equipment', item_level=289, compatible_slots=[slot],
            is_intrinsic_embellishment=intrinsic)

    def material(self, item_id, token, bonus):
        item = WowItemSnapshot.objects.create(item_id=item_id, catalog_type='embellishment',
            simc_token=token, name=token)
        return WowItemVariantSnapshot.objects.create(item=item, season=self.season,
            batch_key='current', game_build='12.1.0.69933', variant_key=str(item_id),
            variant_type='embellishment', bonus_ids=[8960, bonus],
            compatible_slots=['finger', 'neck'], metadata={'reagent_slot_ids': [101]})

    def params(self, carrier, suffix=',bonus_id=13453'):
        slot = 'finger1' if carrier.item.slot_key == 'finger' else carrier.item.slot_key
        return {'candidate_type': 'gear_swap', 'gear_swap': {'item_id': carrier.item.item_id,
            'slot': slot, 'raw_value': f',id={carrier.item.item_id},ilevel=289{suffix}'}}

    def test_benchmark_rejects_illegal_unknown_and_fixed_carriers_without_spec_queries(self):
        carriers = [self.ring, self.wrists, self.other_recipe, self.unknown, self.fixed]
        candidates = [self.params(row) for row in carriers]
        eligibility = EquipmentEligibility(candidates)
        with self.assertNumQueries(0):
            for _ in range(3):
                self.assertIsNone(eligibility.reason(candidates[0], 'warrior_fury'))
                for index in (1, 2, 4):
                    self.assertEqual(eligibility.reason(candidates[index], 'warrior_fury')['code'],
                                     'embellishment_incompatible')
                self.assertEqual(eligibility.reason(candidates[3], 'warrior_fury')['code'],
                                 'embellishment_unknown')
        accepted, excluded = eligibility.filter([
            {'key': str(index), 'params': row} for index, row in enumerate(candidates)], 'warrior_fury')
        self.assertEqual([row['key'] for row in accepted], ['0'])
        self.assertEqual({row['candidate_key'] for row in excluded}, {'1', '2', '3', '4'})

    def test_bonus_join_ignores_common_marker_and_handles_noncanonical_token(self):
        for suffix in (',bonus_id=13769', ',embellishment=coiled_snakeeye',
                       ',bonus_id=8960/13453'):
            candidate = self.params(self.ring, suffix)
            self.assertIsNone(EquipmentEligibility([candidate]).reason(candidate, 'warrior_fury'))

    def test_unknown_effect_and_missing_current_material_fail_closed(self):
        candidate = self.params(self.ring, ',embellishment=unverified_effect')
        self.assertEqual(EquipmentEligibility([candidate]).reason(candidate, 'warrior_fury')['code'],
                         'embellishment_unknown')
        self.iris.batch_key = 'old'
        self.iris.save()
        candidate = self.params(self.ring)
        self.assertEqual(EquipmentEligibility([candidate]).reason(candidate, 'warrior_fury')['code'],
                         'embellishment_unknown')

    def test_ordinary_candidates_and_fixed_without_addition_are_unchanged(self):
        for carrier in (self.unknown, self.fixed, self.wrists):
            for suffix in ('', ',bonus_id=8960', ',embellishment=none'):
                candidate = self.params(carrier, suffix)
                self.assertIsNone(EquipmentEligibility([candidate]).reason(candidate, 'warrior_fury'))

    def test_gear_builder_listing_and_resolution_share_recipe_gate(self):
        for carrier, allowed in ((self.ring, True), (self.other_recipe, False),
                                 (self.unknown, False), (self.fixed, False)):
            groups = enhancement_items(class_name='Warrior', spec_name='Fury',
                slot='finger1', equipment_variant_id=carrier.pk)['groups']
            self.assertEqual(bool(groups['embellishments']), allowed)
            kwargs = dict(variant_id=carrier.pk, selected_stats=['crit', 'haste'],
                          embellishment_variant_id=self.iris.pk)
            if allowed:
                self.assertIsNotNone(resolve_crafted_variant(**kwargs)['embellishment'])
            else:
                with self.assertRaises(GearBuilderError):
                    resolve_crafted_variant(**kwargs)

    def test_fury_two_hand_offhand_embellishment_preserves_recipe_gate(self):
        from botend.services.gear_builder import _resolve_crafted_rows

        # Frozen source: Blood Knight's Warblade + Blessed Pango Charm share 390.
        weapon = self.carrier(237846, 'main_hand', 17, [390])
        weapon.item.item_class_id = 2
        weapon.item.item_subclass_id = 8
        weapon.item.save()
        charm = self.material(244604, 'blessed_pango_charm', 12686)
        charm.compatible_slots = ['main_hand']
        charm.metadata = {'reagent_slot_ids': [390]}
        charm.save()
        for slot in ('main_hand', 'off_hand'):
            candidate = self.params(weapon, ',bonus_id=12686')
            candidate['gear_swap']['slot'] = slot
            with self.subTest(path='benchmark', slot=slot):
                eligibility = EquipmentEligibility([candidate])
                with self.assertNumQueries(0):
                    self.assertIsNone(eligibility.reason(candidate, 'warrior_fury'))
            with self.subTest(path='listing', slot=slot):
                groups = enhancement_items(class_name='warrior', spec_name='fury',
                    slot=slot, equipment_variant_id=weapon.pk)['groups']
                self.assertEqual([row['item_id'] for row in groups['embellishments']], [244604])
            with self.subTest(path='resolution', slot=slot):
                _resolve_crafted_rows(weapon, ['crit', 'haste'], charm,
                                      'Warrior', 'Fury', target_slot=slot)
        candidate['gear_swap']['slot'] = 'off_hand'
        self.assertIsNotNone(EquipmentEligibility([candidate]).reason(candidate, 'warrior_arms'))
        self.assertFalse(enhancement_items(class_name='Warrior', spec_name='Arms',
            slot='off_hand', equipment_variant_id=weapon.pk)['groups']['embellishments'])
        with self.assertRaises(GearBuilderError):
            _resolve_crafted_rows(weapon, ['crit', 'haste'], charm,
                                  'Warrior', 'Arms', target_slot='off_hand')
        # A weapon-slot material still requires a matching recipe, not just Titan's Grip.
        charm.metadata = {'reagent_slot_ids': [999]}
        charm.save()
        self.assertEqual(EquipmentEligibility([candidate]).reason(candidate, 'warrior_fury')['code'],
                         'embellishment_incompatible')
        self.assertFalse(enhancement_items(class_name='Warrior', spec_name='Fury',
            slot='off_hand', equipment_variant_id=weapon.pk)['groups']['embellishments'])
        with self.assertRaises(GearBuilderError):
            _resolve_crafted_rows(weapon, ['crit', 'haste'], charm,
                                  'Warrior', 'Fury', target_slot='off_hand')
        self.assertFalse(slot_matches(self.iris, 'off_hand', 'Warrior', 'Fury'))

    def test_empty_embellishment_slots_are_not_universal_but_gems_stay_unchanged(self):
        self.iris.compatible_slots = []
        self.assertFalse(slot_matches(self.iris, 'wrists'))
        self.iris.variant_type = 'gem'
        self.assertTrue(slot_matches(self.iris, 'wrists'))
