from django.test import TestCase

from botend.models import WowItemSnapshot
from botend.services.wow_item_catalog_import import merge_item_catalog_metadata


class ItemCatalogMetadataMergeTests(TestCase):
    def test_metadata_repair_preserves_raw_empty_fields_and_is_idempotent(self):
        item = WowItemSnapshot.objects.create(
            item_id=237901, catalog_type='', source='', name='原始名称',
            description='原始说明', metadata={'existing': {'fact': 1}},
        )
        before = WowItemSnapshot.objects.values().get(pk=item.pk)
        facts = {'crafting_reagent_slot_ids': [389], 'crafting_facts_source': {'build': '12.1.0.69933'}}
        self.assertTrue(merge_item_catalog_metadata(item, facts))
        after = WowItemSnapshot.objects.values().get(pk=item.pk)
        self.assertEqual(after['metadata'], {**before['metadata'], **facts})
        for field in before.keys() - {'metadata', 'updated_at'}:
            self.assertEqual(after[field], before[field], field)
        with self.assertNumQueries(0):
            self.assertFalse(merge_item_catalog_metadata(item, facts))
        self.assertEqual(WowItemSnapshot.objects.values().get(pk=item.pk), after)
