"""Slot boundary regressions using the real SimC parser and isolated ORM."""
import base64
import gzip
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from botend.models import GearBuilderOwnedItem, WowItemSnapshot, WowItemVariantSnapshot
from botend.services import gear_builder as builder
from botend.services import gear_builder_owned as owned
from botend.services import gear_builder_storage as storage
from botend.services import simc_player_config as simc
from botend.tests.test_gear_builder import GearBuilderTestDataMixin


class GearBuilderSlotRoundtripTests(GearBuilderTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username='slot-roundtrip')
        self.variants = {}
        for index, (slot, _label) in enumerate(builder.EQUIPMENT_SLOTS):
            item = WowItemSnapshot.objects.create(
                item_id=20000 + index, name='Same name', name_zh='同名装备',
                catalog_type='equipment', slot_key=slot,
                eligible_specs=['Warrior:Fury'],
            )
            self.variants[slot] = WowItemVariantSnapshot.objects.create(
                item=item, season=self.season, batch_key=self.season.gear_batch_key,
                variant_key='slot-test', variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
                item_level=710, compatible_slots=[slot],
            )

    def profile(self, *, plural=False):
        return '\n'.join(['warrior="Slots"', 'spec=fury', *[
            f'{slot if plural else simc.EQUIPMENT_SLOT_ALIASES.get(slot, slot)}=same_name,id={variant.item.item_id},ilevel=710'
            for slot, variant in self.variants.items()
        ]])

    def resolve(self, entries):
        return builder.hydrate_shared_state(
            share_version=4, class_name='Warrior', spec_name='Fury',
            batch_key=self.season.gear_batch_key, entries=entries,
        )

    def encode(self, entries):
        return 'z' + base64.urlsafe_b64encode(gzip.compress(json.dumps({
            'v': 4, 'c': 'Warrior', 's': 'Fury', 'b': self.season.gear_batch_key,
            'e': entries,
        }).encode())).decode().rstrip('=')

    def test_real_parser_import_owned_and_saved_share_keep_all_sixteen_slots(self):
        expected = dict(builder.EQUIPMENT_SLOTS)
        for plural in (False, True):
            with self.subTest(plural=plural):
                profile = self.profile(plural=plural)
                parsed_slots = {row['slot'] for row in simc.parse_simc_player_profile(profile)['profile']['equipment']}
                self.assertIn('shoulder', parsed_slots)
                self.assertIn('wrist', parsed_slots)
                imported = builder.import_simc_profile(profile)
                self.assertEqual({row['slot'] for row in imported['equipment']}, set(expected))
                saved_owned = owned.save_owned_items(self.user, imported['owned_equipment'])
                self.assertEqual({row['slot'] for row in saved_owned}, set(expected))
                self.assertEqual(set(GearBuilderOwnedItem.objects.filter(user=self.user).values_list('slot_key', flat=True)), set(expected))
                entries = [[row['slot'], row['item_id'], row['variant']['key']] for row in imported['equipment']]
                saved = storage.save_user_loadout(self.user, name='全槽位', code=self.encode(entries))
                persisted = storage.get_user_loadout(self.user, saved['id'])
                _, decoded, _ = storage._decode_state(persisted['code'])
                restored = self.resolve(decoded['e'])
                self.assertEqual(set(restored['equipment']), set(expected))
                self.assertEqual(restored['warnings'], [])
                for slot, variant in self.variants.items():
                    self.assertEqual(restored['equipment'][slot]['item']['item_id'], variant.item.item_id)
        self.assertEqual(GearBuilderOwnedItem.objects.filter(user=self.user).count(), len(expected))

    def test_bootstrap_derives_simc_names_and_distinguishes_cosmetic_slots(self):
        bootstrap = builder.bootstrap_payload()
        self.assertEqual(bootstrap['slot_aliases'], {'shoulder': 'shoulders', 'wrist': 'wrists'})
        for row in bootstrap['slots']:
            self.assertEqual(row['simc_key'], simc.EQUIPMENT_SLOT_ALIASES.get(row['key'], row['key']))
        self.assertEqual(simc.EQUIPMENT_SLOTS - {row['simc_key'] for row in bootstrap['slots']}, {'shirt', 'tabard'})
        self.assertEqual(len(bootstrap['slots']), 16)
        cosmetic = builder.import_simc_profile('warrior="Cosmetic"\nspec=fury\nshirt=shirt,id=99001\ntabard=tabard,id=99002')
        # Existing parser/import capability does not make these supported builder slots.
        self.assertEqual({row['slot'] for row in cosmetic['equipment']}, {'shirt', 'tabard'})

    def test_legacy_share_aliases_restore_canonical_slots_without_relaxing_validation(self):
        entries = [[simc.EQUIPMENT_SLOT_ALIASES.get(slot, slot), row.item.item_id, row.variant_key]
                   for slot, row in self.variants.items()]
        code = self.encode(entries)
        saved = storage.save_user_loadout(self.user, name='旧分享', code=code)
        _, decoded, _ = storage._decode_state(storage.get_user_loadout(self.user, saved['id'])['code'])
        result = self.resolve(decoded['e'])
        self.assertEqual(set(result['equipment']), set(self.variants))
        self.assertEqual(result['warnings'], [])
        shoulder = next(row for row in entries if row[0] == 'shoulder')
        for duplicate in ('shoulder', 'shoulders'):
            with self.subTest(duplicate=duplicate), self.assertRaisesMessage(builder.GearBuilderError, '重复装备槽位'):
                self.resolve([shoulder, [duplicate, *shoulder[1:]]])
        for invalid in ('shirt', 'tabard', 'unknown'):
            with self.subTest(invalid=invalid), self.assertRaisesMessage(builder.GearBuilderError, '未知或重复装备槽位'):
                self.resolve([[invalid, *shoulder[1:]]])
        wrong = self.resolve([['wrist', *shoulder[1:]]])
        self.assertEqual(wrong['equipment'], {})
        self.assertIn('腕部的装备不适用于当前专精，已置空', wrong['warnings'])

    def test_external_equipment_gems_enchants_and_bag_identity_survive_owned_storage(self):
        profile = '\n'.join([
            'warrior="External"', 'spec=fury',
            'shoulder=external,id=99011,ilevel=710,bonus_id=11/22,gem_id=99101/99101,enchant_id=99201',
            'wrist=external,id=99012,ilevel=711,crafted_stats=40/32',
            '### Gear from Bags',
            '# shoulders=external,id=99013,ilevel=712,gem_id=99102,enchant_id=99202',
            '# wrists=external,id=99014,ilevel=713',
        ])
        imported = builder.import_simc_profile(profile)
        self.assertEqual([row['slot'] for row in imported['equipment']], ['shoulders', 'wrists'])
        self.assertEqual([row['slot'] for row in imported['bag_equipment']], ['shoulders', 'wrists'])
        payloads = [{**row, 'enhancements': {'gems': row['gems'], 'enchant': row['enchant']}}
                    for row in imported['owned_equipment']]
        owned.save_owned_items(self.user, payloads)
        restored = {row['item_id']: row for row in owned.list_owned_items(self.user)}
        self.assertEqual(set(restored), {99011, 99012, 99013, 99014})
        shoulder = restored[99011]
        self.assertEqual(shoulder['slot'], 'shoulders')
        self.assertEqual(shoulder['bonus_ids'], ['11', '22'])
        self.assertEqual([gem['item_id'] for gem in shoulder['enhancements']['gems']], [99101, 99101])
        self.assertEqual(shoulder['enhancements']['enchant']['item_id'], 99201)
        self.assertEqual(restored[99012]['selected_stats'], ['crit', 'mastery'])
        self.assertEqual(restored[99013]['enhancements']['enchant']['item_id'], 99202)
        self.assertTrue(all(row['external'] for row in restored.values()))

    def test_owned_aliases_group_before_fingerprinting_and_read_historical_rows(self):
        base = {'item_id': 99021, 'slot': 'shoulder', 'item_level': 710}
        saved = owned.save_owned_items(self.user, [base, {**base, 'slot': 'shoulders'}])
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]['quantity'], 2)
        self.assertEqual(saved[0]['slot'], 'shoulders')
        row = GearBuilderOwnedItem.objects.get(id=saved[0]['id'])
        row.slot_key = 'shoulder'
        row.save(update_fields=['slot_key'])
        for slot in ('shoulder', 'shoulders'):
            listed = owned.list_owned_items(self.user, slot=slot)
            self.assertEqual([value['id'] for value in listed], [row.id])
            self.assertEqual(listed[0]['slot'], 'shoulders')
            self.assertEqual(listed[0]['slot_label'], '肩部')
