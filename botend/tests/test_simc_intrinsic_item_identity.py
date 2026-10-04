from django.test import TestCase

from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.services.simc_benchmark_config import _freeze_equipment_rules


class IntrinsicItemIdentityTests(TestCase):
    def test_frozen_intrinsic_items_use_game_id_not_database_foreign_key(self):
        season = SeasonMeta.objects.create(
            season_key='intrinsic-test', season_name='Identity test', mplus_zone_id=0, raid_zone_id=0,
        )
        item = WowItemSnapshot.objects.create(item_id=987654, name='Intrinsic identity fixture')
        self.assertNotEqual(item.pk, item.item_id)
        WowItemVariantSnapshot.objects.create(
            item=item, season=season, batch_key='identity-test', variant_key='fixed',
            variant_type=WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT,
            is_intrinsic_embellishment=True,
        )
        frozen = _freeze_equipment_rules()['intrinsic_item_ids']
        self.assertIn(item.item_id, frozen)
        self.assertNotIn(item.pk, frozen)
