from django.test import TestCase

from botend.models import GearBuilderOwnedItem
from botend.services.gear_builder import EQUIPMENT_SLOTS
from botend.tests.test_gear_assistant_priorities import GearAssistantDataMixin


class GearAssistantPresentationTests(GearAssistantDataMixin, TestCase):
    def test_result_details_identify_slots_routes_and_selection_origins(self):
        for slot in ('wrists', 'back'):
            self.variant(slot, intrinsic=True)
        head = self.base['head']
        head.source_json = [
            {'type': 'raid', 'instance_zh': '不能展示为选中来源的团本'},
            {'type': 'mythic_plus', 'instance_zh': '实际获取地下城'},
        ]
        head.save(update_fields=['source_json'])
        neck = self.base['neck']
        GearBuilderOwnedItem.objects.create(
            user=self.user, item_id=neck.item.item_id, variant=neck,
            slot_key='neck', quantity=1,
        )
        fixed = {'chest': {'variant': {'id': self.base['chest'].id}}}
        plans = {row['key']: row for row in self.optimize(equipment=fixed)['plans']}
        for plan in plans.values():
            for slot, row in plan['equipment'].items():
                self.assertEqual(row['slot_label'], dict(EQUIPMENT_SLOTS)[slot])
                self.assertIn(row['selection_origin'], ('locked', 'owned', 'catalog'))
                self.assertTrue(row['acquisition_source_label'])
            self.assertEqual(plan['equipment']['chest']['selection_origin'], 'locked')
            missing = {row['slot']: row for row in plan['missing_items']}
            for slot, row in missing.items():
                self.assertEqual(row['source'], plan['equipment'][slot]['acquisition_source_label'])
                self.assertEqual(plan['equipment'][slot]['selection_origin'], 'catalog')
        self.assertEqual(plans['prefer_owned']['equipment']['neck']['selection_origin'], 'owned')
        head_result = plans['dungeon']['equipment']['head']
        self.assertEqual(head_result['selection_origin'], 'catalog')
        self.assertIn('实际获取地下城', head_result['acquisition_source_label'])
        self.assertNotIn('不能展示', head_result['acquisition_source_label'])
        self.assertEqual([s['instance_zh'] for s in head_result['acquisition_sources']], ['实际获取地下城'])
        self.assertEqual([s['type'] for s in head_result['acquisition_sources']], ['mythic_plus'])
