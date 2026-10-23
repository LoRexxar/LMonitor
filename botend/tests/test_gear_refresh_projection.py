"""验证刷新接口减载后属性、强化、身份及历史配装保持一致。"""
from copy import deepcopy
import json
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import WowItemSnapshot, WowItemVariantSnapshot
from botend.services import gear_builder as gb
from botend.services.gear_catalog_projection import compact_equipment_payload
from botend.tests.test_gear_builder import GearBuilderTestDataMixin
from botend.tests.test_gear_catalog_projection import presentation, without_private_facts


class GearRefreshProjectionTests(GearBuilderTestDataMixin, TestCase):
    def payload(self):
        return dict(v=4, c='Warrior', s='Fury', b='test-batch', current=True, strict=True,
                    e=[['head', self.crafted_item.item_id, self.crafted.variant_key,
                        ['crit', 'mastery'], [self.embellishment.item.item_id, self.embellishment.variant_key],
                        [[self.gem.item.item_id, self.gem.variant_key]],
                        [self.enchant.item.item_id, self.enchant.variant_key], 1]])

    def test_public_refresh_preserves_calculations_and_all_components_without_writes(self):
        for row in (self.crafted, self.embellishment, self.gem, self.enchant):
            row.metadata = {**row.metadata, 'simc_effect_activation_by_build': {'raw': 'x' * 85000}}
            row.save(update_fields=['metadata'])
        args = gb.share_payload_arguments(self.payload())
        original = gb.hydrate_shared_state(**args, current=True, strict=True)
        untouched = deepcopy(original)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.post('/portal/api/gear-builder/resolve-share/',
                                        json.dumps(self.payload()), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        public = response.json()
        self.assertEqual(without_private_facts(public['equipment']), without_private_facts(original['equipment']))
        self.assertEqual(original, untouched)
        before, after = original['equipment']['head'], public['equipment']['head']
        for field in ('resolvedStats', 'resolvedEffects', 'selectedStats', 'addedSocket'):
            self.assertEqual(before[field], after[field])
        for component_before, component_after in zip(
                [before, before['embellishment'], *before['gems'], before['enchant']],
                [after, after['embellishment'], *after['gems'], after['enchant']]):
            self.assertNotIn('simc_effect_activation_by_build', component_after['variant']['metadata'])
            self.assertEqual(presentation([{**component_before['item'], 'variants': [component_before['variant']]}]),
                             presentation([{**component_after['item'], 'variants': [component_after['variant']]}]))
        self.assertLess(len(response.content), len(json.dumps(original).encode()) / 4)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('UPDATE ', 'INSERT ', 'DELETE ')) for q in queries))

    def test_loads_only_referenced_variants_and_each_item_once(self):
        for number in range(30):
            WowItemVariantSnapshot.objects.create(item=self.crafted_item, season=self.season,
                batch_key='test-batch', variant_key=f'unselected-{number}', item_level=700 + number,
                variant_type='crafted_equipment')
        variant_ids, item_ids = [], []
        load_variant, load_item = WowItemVariantSnapshot.from_db, WowItemSnapshot.from_db
        def variant(db, fields, values):
            row = load_variant(db, fields, values)
            variant_ids.append(row.pk)
            return row
        def item(db, fields, values):
            row = load_item(db, fields, values)
            item_ids.append(row.pk)
            return row
        with patch.object(WowItemVariantSnapshot, 'from_db', side_effect=variant), \
                patch.object(WowItemSnapshot, 'from_db', side_effect=item):
            gb.hydrate_shared_state(**gb.share_payload_arguments(self.payload()), strict=True)
        self.assertCountEqual(variant_ids, [self.crafted.pk, self.embellishment.pk, self.gem.pk, self.enchant.pk])
        self.assertEqual(len(item_ids), len(set(item_ids)))

    def test_projection_preserves_verified_historical_snapshot_exactly(self):
        original = gb.hydrate_shared_state(**gb.share_payload_arguments(self.payload()), strict=True)
        self.assertEqual(compact_equipment_payload(original, snapshot_slots={'head'}), original)
