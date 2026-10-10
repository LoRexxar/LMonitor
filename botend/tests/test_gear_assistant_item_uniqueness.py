from django.test import TestCase

from botend.models import GearBuilderOwnedItem
from botend.services.gear_builder import GearBuilderError
from botend.tests.test_gear_assistant_priorities import GearAssistantDataMixin


class GearAssistantItemUniquenessTests(GearAssistantDataMixin, TestCase):
    pairs = (('finger1', 'finger2'), ('trinket1', 'trinket2'), ('main_hand', 'off_hand'))

    def setUp(self):
        super().setUp()
        for slot in ('wrists', 'back'):
            self.variant(slot, intrinsic=True)

    def shared_item(self, pair, *, item=None, level=710, crafted=False):
        row = self.variant(pair[0], item=item, level=level, crafted=crafted,
                           effects=[{'description_zh': '装备：攻击触发火焰。'}])
        row.compatible_slots = list(pair)
        row.save(update_fields=['compatible_slots'])
        return row

    def assert_no_duplicate_items(self, result):
        for plan in result['plans']:
            ids = [row['item']['item_id'] for row in plan['equipment'].values()]
            self.assertEqual(len(ids), len(set(ids)), plan['key'])
            self.assertEqual(plan['embellishment_count'], 2)

    def test_catalog_recommendations_never_repeat_rings_trinkets_or_weapons(self):
        for pair in self.pairs:
            self.shared_item(pair)
        self.assert_no_duplicate_items(self.optimize())

    def test_owned_quantity_and_separate_owned_variants_cannot_bypass_item_identity(self):
        for pair in self.pairs:
            first = self.shared_item(pair)
            second = self.shared_item(pair, item=first.item, level=700)
            for row, slot in ((first, pair[0]), (second, pair[1])):
                GearBuilderOwnedItem.objects.create(user=self.user, item_id=row.item.item_id,
                                                    variant=row, slot_key=slot, quantity=2,
                                                    fingerprint=f'unique-test-{row.id}-{slot}')
        self.assert_no_duplicate_items(self.optimize())

    def test_later_locked_item_reserves_identity_against_other_variants(self):
        fixed = {}
        for pair in self.pairs:
            first = self.shared_item(pair)
            second = self.shared_item(pair, item=first.item, level=700)
            fixed[pair[1]] = {'variant': {'id': second.id}}
        result = self.optimize(equipment=fixed)
        self.assert_no_duplicate_items(result)
        for plan in result['plans']:
            for slot, row in fixed.items():
                self.assertEqual(plan['equipment'][slot]['variant']['id'], row['variant']['id'])

    def test_two_locked_copies_are_rejected_even_with_different_levels_or_crafted_stats(self):
        for pair in self.pairs:
            with self.subTest(pair=pair):
                first = self.shared_item(pair, crafted=True)
                first.crafting_options = {'stat_count': 2, 'stat_pool': ['crit', 'haste', 'mastery'], 'secondary_total': 100}
                first.save(update_fields=['crafting_options'])
                second = self.shared_item(pair, item=first.item, level=700, crafted=True)
                second.crafting_options = first.crafting_options
                second.save(update_fields=['crafting_options'])
                with self.assertRaisesRegex(GearBuilderError, '同一物品.*只能.*1.*次'):
                    self.optimize(equipment={
                        pair[0]: {'variant': {'id': first.id}, 'selectedStats': ['crit', 'haste']},
                        pair[1]: {'variant': {'id': second.id}, 'selectedStats': ['haste', 'mastery']},
                    })

    def test_single_available_item_for_two_slots_fails_instead_of_duplicating(self):
        for pair in self.pairs:
            with self.subTest(pair=pair):
                shared = self.shared_item(pair)
                originals = [self.base[slot] for slot in pair]
                for row in originals:
                    row.compatible_slots = []
                    row.save(update_fields=['compatible_slots'])
                # Remove from the active pool without deleting shared canonical item records.
                for row in originals:
                    row.batch_key = 'not-current'
                    row.save(update_fields=['batch_key'])
                try:
                    with self.assertRaisesRegex(GearBuilderError, '装备约束'):
                        self.optimize()
                finally:
                    for row in originals:
                        row.batch_key = 'priorities'
                        row.compatible_slots = [row.item.slot_key]
                        row.save(update_fields=['batch_key', 'compatible_slots'])
                    shared.delete()
