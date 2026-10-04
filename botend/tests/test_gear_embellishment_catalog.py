"""Reduced Raidbots live source facts, build 12.1.0.69933 (no network)."""

from copy import deepcopy

from django.test import SimpleTestCase

from botend.services.gear_builder_catalog_source import CurrentGearCatalogSource


SOURCE = {'items': [{'id': 239648,
            'name': "Martyr's Bindings",
            'quality': 4,
            'itemClass': 4,
            'itemSubClass': 1,
            'inventoryType': 9,
            'sources': [{'instanceId': -88, 'encounterId': -39}],
            'profession': {'id': 197,
                           'recipeSpellId': 1228945,
                           'optionalCraftingSlots': [{'id': 454},
                                                     {'id': 403},
                                                     {'id': 401},
                                                     {'id': 391},
                                                     {'id': 393},
                                                     {'id': 392},
                                                     {'id': 388},
                                                     {'id': 427}]},
            'expansion': 11},
           {'id': 239660,
            'name': 'Arcanoweave Bracers',
            'quality': 4,
            'itemClass': 4,
            'itemSubClass': 1,
            'inventoryType': 9,
            'itemLimit': {'category': 512, 'quantity': 2},
            'sources': [{'instanceId': -88, 'encounterId': -39}],
            'profession': {'id': 197,
                           'recipeSpellId': 1228984,
                           'optionalCraftingSlots': [{'id': 454},
                                                     {'id': 403},
                                                     {'id': 453},
                                                     {'id': 401},
                                                     {'id': 392},
                                                     {'id': 388},
                                                     {'id': 427}]},
            'expansion': 11},
           {'id': 240949,
            'name': "Masterwork Sin'dorei Band",
            'quality': 4,
            'itemClass': 4,
            'itemSubClass': 0,
            'inventoryType': 11,
            'uniqueEquipped': True,
            'sources': [{'instanceId': -88, 'encounterId': -37}],
            'profession': {'id': 755,
                           'recipeSpellId': 1230485,
                           'optionalCraftingSlots': [{'id': 418},
                                                     {'id': 417},
                                                     {'id': 414},
                                                     {'id': 500},
                                                     {'id': 389},
                                                     {'id': 393},
                                                     {'id': 392},
                                                     {'id': 388}]},
            'expansion': 11},
           {'id': 240950,
            'name': "Masterwork Sin'dorei Amulet",
            'quality': 4,
            'itemClass': 4,
            'itemSubClass': 0,
            'inventoryType': 2,
            'sources': [{'instanceId': -88, 'encounterId': -37}],
            'profession': {'id': 755,
                           'recipeSpellId': 1230486,
                           'optionalCraftingSlots': [{'id': 418},
                                                     {'id': 417},
                                                     {'id': 500},
                                                     {'id': 389},
                                                     {'id': 393},
                                                     {'id': 392},
                                                     {'id': 388},
                                                     {'id': 388}]},
            'expansion': 11},
           {'id': 241139,
            'name': 'Thalassian Phoenix Torque',
            'quality': 4,
            'itemClass': 4,
            'itemSubClass': 0,
            'inventoryType': 2,
            'itemLimit': {'category': 512, 'quantity': 2},
            'sources': [{'instanceId': -88, 'encounterId': -37}],
            'profession': {'id': 755,
                           'recipeSpellId': 1230488,
                           'optionalCraftingSlots': [{'id': 418},
                                                     {'id': 417},
                                                     {'id': 412},
                                                     {'id': 500},
                                                     {'id': 392},
                                                     {'id': 388}]},
            'expansion': 11},
           {'id': 241140,
            'name': 'Signet of Azerothian Blessings',
            'quality': 4,
            'itemClass': 4,
            'itemSubClass': 0,
            'inventoryType': 11,
            'uniqueEquipped': True,
            'itemLimit': {'category': 512, 'quantity': 2},
            'sources': [{'instanceId': -88, 'encounterId': -37}],
            'profession': {'id': 755,
                           'recipeSpellId': 1230487,
                           'optionalCraftingSlots': [{'id': 418},
                                                     {'id': 417},
                                                     {'id': 411},
                                                     {'id': 500},
                                                     {'id': 392},
                                                     {'id': 388}]},
            'expansion': 11},
           {'id': 268477,
            'name': 'P.O.W. x3',
            'quality': 4,
            'itemClass': 2,
            'itemSubClass': 3,
            'inventoryType': 26,
            'sources': [{'instanceId': -88, 'encounterId': -35}],
            'profession': {'id': 202,
                           'recipeSpellId': 1282456,
                           'optionalCraftingSlots': [{'id': 399},
                                                     {'id': 400},
                                                     {'id': 401},
                                                     {'id': 501},
                                                     {'id': 459},
                                                     {'id': 392},
                                                     {'id': 388}]},
            'expansion': 11},
           {'id': 271680,
            'name': 'Sinseared Repeater',
            'quality': 3,
            'itemClass': 2,
            'itemSubClass': 3,
            'inventoryType': 26,
            'sources': [{'instanceId': 1304, 'encounterId': 2679},
                        {'instanceId': -1, 'encounterId': 1304},
                        {'instanceId': -32, 'encounterId': 1304}],
            'expansion': 11}],
 'crafting': {'slots': {'389': {'reagentSlotId': 389,
                                'name': 'Add Embellishment',
                                'reagentIds': [251487,
                                               251488,
                                               244603,
                                               244604,
                                               248130,
                                               251489,
                                               251490,
                                               273065,
                                               273066]},
                        '501': {'reagentSlotId': 501,
                                'name': 'Add Embellishment',
                                'reagentIds': [248135,
                                               248592,
                                               257735,
                                               257741,
                                               255843,
                                               255844,
                                               248132,
                                               248133,
                                               248136,
                                               248130,
                                               245871,
                                               245872,
                                               245875,
                                               245876,
                                               245877,
                                               245878,
                                               245873,
                                               245874,
                                               273062,
                                               273063]}},
              'reagents': [{'id': 251487,
                            'name': 'Prismatic Focusing Iris',
                            'icon': 'item_cutmetagem',
                            'quality': 3,
                            'itemClass': 7,
                            'itemSubClass': 18,
                            'itemLevel': 72,
                            'itemLimit': {'category': 512, 'quantity': 2},
                            'craftingQuality': 1,
                            'craftingBonusIds': [13453, 8960],
                            'craftingCategoryId': 860,
                            'expansion': 11,
                            'squishEra': 2,
                            'itemId': 251487,
                            'reagentType': 'item'},
                           {'id': 251488,
                            'name': 'Prismatic Focusing Iris',
                            'icon': 'item_cutmetagem',
                            'quality': 3,
                            'itemClass': 7,
                            'itemSubClass': 18,
                            'itemLevel': 72,
                            'itemLimit': {'category': 512, 'quantity': 2},
                            'craftingQuality': 2,
                            'craftingBonusIds': [13453, 8960],
                            'craftingCategoryId': 860,
                            'expansion': 11,
                            'squishEra': 2,
                            'itemId': 251488,
                            'reagentType': 'item'},
                           {'id': 273062,
                            'name': 'Coiled Snake-Eye',
                            'icon': 'inv_10_engineering_scope_color3',
                            'quality': 3,
                            'itemClass': 7,
                            'itemSubClass': 18,
                            'itemLevel': 72,
                            'itemLimit': {'category': 512, 'quantity': 2},
                            'craftingQuality': 1,
                            'craftingBonusIds': [13769, 8960],
                            'craftingCategoryId': 919,
                            'expansion': 11,
                            'squishEra': 2,
                            'itemId': 273062,
                            'reagentType': 'item'},
                           {'id': 273063,
                            'name': 'Coiled Snake-Eye',
                            'icon': 'inv_10_engineering_scope_color3',
                            'quality': 3,
                            'itemClass': 7,
                            'itemSubClass': 18,
                            'itemLevel': 72,
                            'itemLimit': {'category': 512, 'quantity': 2},
                            'craftingQuality': 2,
                            'craftingBonusIds': [13769, 8960],
                            'craftingCategoryId': 919,
                            'expansion': 11,
                            'squishEra': 2,
                            'itemId': 273063,
                            'reagentType': 'item'}]}}


class GearEmbellishmentCatalogTests(SimpleTestCase):
    def build_catalog(self, source=None):
        source = deepcopy(SOURCE if source is None else source)
        instances = [
            {'id': -1, 'type': 'mplus-chest'},
            {'id': -2, 'type': 'delve-mid2'},
            {'id': -3, 'type': 'raid', 'name': 'Season 2 Raids'},
            {'id': -88, 'type': 'professionMidnightEpic'},
        ]
        items, _ = CurrentGearCatalogSource(no_proxy=True)._build_items(
            {'shortName': 'mid2', 'name': 'Midnight Season 2'},
            {'tracks': {'hero': [318]}, 'crafted': {'hero': [318, 321]}},
            instances, source['items'], [], [], source['crafting'], [],
        )
        return {item['item_id']: item for item in items}

    def test_iris_joins_recipe_slots_for_ring_neck_not_wrists(self):
        items = self.build_catalog()
        self.assertNotIn(251487, items)  # Preserve highest-quality selection.
        iris = items[251488]['variants'][0]
        self.assertEqual(iris['compatible_slots'], ['finger', 'neck'])
        self.assertEqual(iris['metadata']['reagent_slot_ids'], [389])
        self.assertEqual(iris['bonus_ids'], [13453, 8960])
        for item_id in (240949, 240950):
            with self.subTest(item_id=item_id):
                meta = items[item_id]['metadata']
                self.assertIn(389, meta['crafting_reagent_slot_ids'])
                self.assertEqual(meta['crafting_profession_id'], 755)
        self.assertNotIn(389, items[239648]['metadata']['crafting_reagent_slot_ids'])
        self.assertEqual(items[240950]['metadata']['crafting_recipe_spell_id'], 1230486)
        self.assertEqual(items[240950]['metadata']['crafting_reagent_slot_ids'],
                         [388, 389, 392, 393, 417, 418, 500])

    def test_engineering_gun_retains_recipe_identity_not_any_gun(self):
        items = self.build_catalog()
        scope = items[273063]['variants'][0]
        self.assertEqual(scope['compatible_slots'], ['main_hand'])
        self.assertEqual(scope['metadata']['reagent_slot_ids'], [501])
        gun = items[268477]
        other_gun = items[271680]
        self.assertEqual(gun['inventory_type'], other_gun['inventory_type'])
        self.assertIn(501, gun['metadata']['crafting_reagent_slot_ids'])
        self.assertEqual(gun['metadata']['crafting_recipe_spell_id'], 1282456)
        self.assertEqual(other_gun['metadata']['crafting_reagent_slot_ids'], [])

    def test_intrinsic_limit_survives_all_crafted_variants(self):
        items = self.build_catalog()
        for item_id in (239660, 241139, 241140):
            with self.subTest(item_id=item_id):
                item = items[item_id]
                self.assertEqual(item['metadata']['item_limit'], {'category': 512, 'quantity': 2})
                for variant in item['variants']:
                    self.assertTrue(variant['is_intrinsic_embellishment'])
                    self.assertEqual(variant['unique_group'], 'embellishment-limit')
                    self.assertEqual(variant['max_equipped'], 2)
                    self.assertEqual(variant['type'], 'crafted_equipment')
        ordinary = items[239648]['variants'][0]
        self.assertFalse(ordinary.get('is_intrinsic_embellishment', False))
        self.assertEqual(ordinary['crafting_options']['stat_count'], 2)
        self.assertEqual(ordinary['compatible_slots'], ['wrists'])

    def test_missing_relations_and_old_expansion_are_not_unrestricted(self):
        source = deepcopy(SOURCE)
        source['crafting']['slots'] = {}
        iris = self.build_catalog(source)[251488]['variants'][0]
        self.assertEqual(iris['metadata']['reagent_slot_ids'], [])
        self.assertEqual(iris['compatible_slots'], [])
        source = deepcopy(SOURCE)
        for row in source['items']:
            if row['id'] == 240949:
                row['expansion'] = 10
            if row['id'] == 240950:
                row['profession'].pop('optionalCraftingSlots')
        items = self.build_catalog(source)
        iris = items[251488]['variants'][0]
        self.assertEqual(iris['metadata']['reagent_slot_ids'], [389])
        self.assertEqual(iris['compatible_slots'], [])
        self.assertEqual(items[240950]['metadata']['crafting_reagent_slot_ids'], [])

    def test_intrinsic_quantity_is_source_fact_not_constant(self):
        source = deepcopy(SOURCE)
        for row in source['items']:
            if row['id'] == 239660:
                row['itemLimit']['quantity'] = 1  # Deliberate contract perturbation.
        variants = self.build_catalog(source)[239660]['variants']
        self.assertTrue(all(v['max_equipped'] == 1 for v in variants))
