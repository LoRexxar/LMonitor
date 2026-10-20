"""Real historical codecs: 08ea9e21 (v2), 98aa6110 (v3), b8d42edb (v4).

The pre-compact codec serialized freshState(version=1) verbatim.
These tests use isolated ORM identities, never production catalog or SimC runs.
"""
import base64
import copy
import gzip
import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from botend.models import GearBuilderUserLoadout, WowItemSnapshot, WowItemVariantSnapshot
from botend.services import gear_builder as builder
from botend.services import gear_builder_storage as storage
from botend.tests.test_gear_builder import GearBuilderTestDataMixin


class GearShareHistoryTests(GearBuilderTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username='history-owner')

    def reference(self, variant, version, equipment=False):
        if version == 2:
            return variant.pk
        if version == 3:
            return [variant.pk, variant.item.item_id, variant.variant_key, variant.item_level]
        return variant.variant_key if equipment else [variant.item.item_id, variant.variant_key]

    def payload(self, version=4):
        return {'v': version, 'c': 'Warrior', 's': 'Fury', 'b': 'test-batch', 'e': [[
            'head', self.helm.item_id, self.reference(self.hero, version, True), [], 0,
            [self.reference(self.gem, version)], self.reference(self.enchant, version), 0,
        ]]}

    def resolve(self, payload, **kwargs):
        return builder.hydrate_shared_state(
            share_version=payload['v'], class_name=payload['c'], spec_name=payload['s'],
            batch_key=payload['b'], entries=payload['e'], **kwargs,
        )

    def encode(self, payload, compressed=True):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        return ('z' if compressed else 'j') + base64.urlsafe_b64encode(
            gzip.compress(raw) if compressed else raw).decode().rstrip('=')

    def snapshot(self):
        return self.resolve(self.payload())['equipment']['head']

    def post(self, payload):
        return self.client.post('/portal/api/gear-builder/resolve-share/',
                                data=json.dumps(payload), content_type='application/json')

    def test_real_v2_v3_v4_references_and_all_enhancement_types(self):
        for version in (2, 3, 4):
            with self.subTest(version=version):
                payload = self.payload(version)
                payload['e'][0][1:5] = [self.crafted_item.item_id,
                    self.reference(self.crafted, version, True), ['crit', 'mastery'],
                    self.reference(self.embellishment, version)]
                payload['e'][0][7] = 1
                restored = self.resolve(payload, strict=True)
                row = restored['equipment']['head']
                self.assertEqual(row['variant']['id'], self.crafted.pk)
                self.assertEqual(row['embellishment']['variant']['id'], self.embellishment.pk)
                self.assertEqual(row['gems'][0]['variant']['id'], self.gem.pk)
                self.assertEqual(row['enchant']['variant']['id'], self.enchant.pk)
                self.assertEqual(restored['issues'], [])
                code = self.encode(payload)
                saved = storage.save_user_loadout(self.user, name=f'v{version}', code=code)
                self.assertEqual(storage.get_user_loadout(self.user, saved['id'])['code'], code)

    def test_legacy_dbpk_never_guessed_from_item_or_new_key(self):
        for version in (2, 3):
            payload = self.payload(version)
            for mutation in ('pk', 'item', 'batch', 'key', 'level'):
                if version == 2 and mutation in ('key', 'level'):
                    continue
                broken = copy.deepcopy(payload)
                if mutation == 'pk':
                    if version == 2:
                        broken['e'][0][2] = 999999
                    else:
                        broken['e'][0][2][0] = 999999
                elif mutation == 'item':
                    broken['e'][0][1] = self.crafted_item.item_id
                elif mutation == 'batch':
                    broken['b'] = 'unrelated-batch'
                else:
                    broken['e'][0][2][2 if mutation == 'key' else 3] = 'wrong' if mutation == 'key' else 999
                with self.subTest(version=version, mutation=mutation), self.assertRaises(builder.GearBuilderError):
                    self.resolve(broken, strict=True)

    def test_strict_failure_has_issues_and_no_partial_equipment(self):
        for field, value in ((2, 'missing'), (4, [999991, 'gone']),
                             (5, [[999992, 'gone']]), (6, [999993, 'gone'])):
            payload = self.payload()
            payload['e'][0][field] = value
            response = self.post({**payload, 'strict': True})
            self.assertEqual(response.status_code, 400, response.content)
            self.assertTrue(response.json()['issues'])
            self.assertNotIn('equipment', response.json())
            legacy = self.resolve(payload)
            self.assertTrue(legacy['issues'])
        payload = self.payload()
        payload['e'][0][5] *= 2  # Real capacity is one: never silently slice a gem.
        self.assertEqual(self.post({**payload, 'strict': True}).status_code, 400)

    def test_strict_flag_requires_json_boolean_and_unknown_versions_rejected(self):
        for value in ('true', 'false', 0, 1, None, [], {}):
            response = self.post({**self.payload(), 'strict': value})
            self.assertEqual(response.status_code, 400, (value, response.content))
        for version in (0, 1, 5, '4', True, 4.5):
            with self.assertRaises(builder.GearBuilderError):
                self.resolve({**self.payload(), 'v': version}, strict=True)

    def test_old_eighteen_rows_merge_only_equal_aliases_and_reject_raw_duplicates(self):
        entries = []
        for index, (slot, _) in enumerate(builder.EQUIPMENT_SLOTS):
            item = WowItemSnapshot.objects.create(item_id=30000 + index, slot_key=slot,
                catalog_type='equipment', eligible_specs=['Warrior:Fury'])
            variant = WowItemVariantSnapshot.objects.create(item=item, season=self.season,
                batch_key='test-batch', variant_key='history', variant_type='drop_equipment',
                compatible_slots=[slot], item_level=710)
            entries.append([slot, item.item_id, variant.variant_key])
        for alias, canonical in builder.SLOT_ALIASES.items():
            entries.append([alias, *next(row[1:] for row in entries if row[0] == canonical)])
        self.assertEqual(len(entries), 18)
        payload = {**self.payload(), 'e': entries}
        restored = self.resolve(payload, strict=True)
        self.assertEqual(len(restored['equipment']), 16)
        code = self.encode(payload)
        self.assertEqual(storage.save_user_loadout(self.user, name='18', code=code)['code'], code)
        for extra in (entries[0], ['shoulder', entries[-2][1], 'different']):
            broken = copy.deepcopy(entries if extra[0] == 'head' else entries[:-2])
            if extra[0] == 'head':
                broken = broken[:1]
            broken.append(extra)
            with self.assertRaises(builder.GearBuilderError):
                self.resolve({**payload, 'e': broken}, strict=True)

    def test_snapshot_and_full_state_survive_deleted_catalog_without_rewriting_code(self):
        snapshot = self.snapshot()
        payload = self.payload()
        payload['e'][0].append(snapshot)
        full = {'version': 1, 'className': 'Warrior', 'specName': 'Fury',
                'batchKey': 'test-batch', 'equipment': {'head': snapshot}}
        WowItemVariantSnapshot.objects.all().delete()
        self.client.force_login(self.user)
        for document in (payload, full):
            with self.subTest(full='equipment' in document):
                response = self.post({**document, 'strict': True})
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()['equipment']['head']['gems'], snapshot['gems'])
                for compressed in (True, False):
                    code = self.encode(document, compressed)
                    saved = self.client.post('/portal/api/gear-builder/online-loadouts/',
                        data=json.dumps({'name': 'preserved', 'code': code}), content_type='application/json')
                    self.assertEqual(saved.status_code, 200, saved.content)
                    loadout_id = saved.json()['loadout']['id']
                    detail = self.client.get(f'/portal/api/gear-builder/online-loadouts/{loadout_id}/')
                    self.assertEqual(detail.json()['loadout']['code'], code)
                    link = storage.create_short_link(self.user, code)
                    self.assertEqual(storage.resolve_short_link(link.token)['code'], code)
        stranger = get_user_model().objects.create_user(username='history-stranger')
        self.client.force_login(stranger)
        self.assertEqual(self.client.get(f'/portal/api/gear-builder/online-loadouts/{loadout_id}/').status_code, 404)
        forbidden = self.client.post('/portal/api/gear-builder/online-loadouts/',
            data=json.dumps({'id': loadout_id, 'name': 'stolen', 'code': code}), content_type='application/json')
        self.assertEqual(forbidden.status_code, 400)

    def test_snapshot_cannot_override_wrong_identity_slot_or_spec(self):
        snapshot = self.snapshot()
        for mutation in ('item', 'variant_item', 'slot', 'key', 'spec', 'branch', 'type'):
            payload = self.payload()
            snap = copy.deepcopy(snapshot)
            if mutation == 'item':
                snap['item']['item_id'] = 99999
            elif mutation == 'variant_item':
                snap['variant']['item_id'] = 99999
            elif mutation == 'slot':
                payload['e'][0][0] = 'neck'
            elif mutation == 'key':
                snap['variant']['key'] = 'wrong'
            elif mutation == 'branch':
                snap['variant']['data_branch'] = 'ptr'
            elif mutation == 'type':
                snap['variant']['type'] = 'crafted_equipment'
            else:
                snap['item']['eligible_specs'] = ['Mage:Fire']
            payload['e'][0].append(snap)
            with self.subTest(mutation=mutation):
                self.assertEqual(self.post({**payload, 'strict': True}).status_code, 400)

    def test_missing_reference_cannot_replace_saved_original_or_create_partial_record(self):
        code = self.encode(self.payload())
        saved = storage.save_user_loadout(self.user, name='original', code=code)
        self.gem.delete()
        with self.assertRaises(storage.GearBuilderStorageError):
            storage.save_user_loadout(self.user, name='replaced', code=code, loadout_id=saved['id'])
        row = GearBuilderUserLoadout.objects.get(pk=saved['id'])
        self.assertEqual((row.name, row.encoded_state), ('original', code))
        with self.assertRaises(storage.GearBuilderStorageError):
            storage.save_user_loadout(self.user, name='partial', code=code)
        self.assertEqual(GearBuilderUserLoadout.objects.count(), 1)

    def test_malformed_identity_and_slot_inputs_fail_closed(self):
        for value in (True, 1.5, '10001', -1, 0):
            payload = self.payload()
            payload['e'][0][1] = value
            self.assertEqual(self.post({**payload, 'strict': True}).status_code, 400)
        for slot in ('shirt', 'tabard', 'unknown'):
            payload = self.payload()
            payload['e'][0][0] = slot
            self.assertEqual(self.post({**payload, 'strict': True}).status_code, 400)
        for field, value in (('c', 'Unknown'), ('s', 'Unknown'), ('b', ''), ('v', 99)):
            payload = {**self.payload(), field: value}
            with self.assertRaises(storage.GearBuilderStorageError):
                storage._decode_state(self.encode(payload))

    def test_external_historical_snapshot_retains_duplicate_gems_and_enchant(self):
        external = {'item': {'item_id': 990001, 'name': 'External'},
            'variant': {'id': None, 'item_level': 710, 'type': 'external',
                        'stats': {}, 'effects': [], 'bonus_ids': ['12', '34']},
            'itemLevel': 710, 'external': True, 'selectedStats': [], 'addedSocket': True,
            'gems': [{'item': {'item_id': 990002}, 'variant': {'stats': {}, 'effects': []},
                      'external': True}] * 2,
            'enchant': {'item': {'item_id': 990003}, 'variant': {'stats': {}, 'effects': []},
                        'external': True}, 'embellishment': None}
        for version in (2, 3, 4):
            payload = {**self.payload(version), 'e': [['wrist', 0, 0, [], 0, [], 0, 1, external]]}
            result = self.resolve(payload, strict=True)
            self.assertEqual(result['equipment']['wrists'], external)
            code = self.encode(payload)
            self.assertEqual(storage.save_user_loadout(self.user, name=f'ext-{version}', code=code)['code'], code)

    def test_snapshot_metadata_errors_are_400_and_no_slot_bypass_without_pk(self):
        for field, value in (('metadata', ['bad']), ('type', []), ('compatible_slots', [1]),
                             ('data_branch', {}), ('stats', []), ('item_id', True)):
            payload = self.payload()
            snapshot = self.snapshot()
            snapshot['variant'][field] = value
            payload['e'][0].append(snapshot)
            with self.subTest(field=field):
                self.assertEqual(self.post({**payload, 'strict': True}).status_code, 400)
        snapshot = self.snapshot()
        snapshot['variant']['id'] = None
        snapshot['item']['slot'] = 'neck'
        snapshot['variant']['compatible_slots'] = ['neck']
        payload = {**self.payload(), 'e': [['neck', 0, 0, [], 0, [], 0, 0, snapshot]]}
        self.assertEqual(self.post({**payload, 'strict': True}).status_code, 400)

    def test_snapshot_enhancement_category_is_not_the_carrier_slot(self):
        # Real catalog categories are not equipment slots. Their applicability is
        # given by compatible_slots, even when item.slot is gem/embellishment.
        self.gem_item.slot_key = 'gem'
        self.gem_item.save(update_fields=['slot_key'])
        self.embellishment_item.slot_key = 'embellishment'
        self.embellishment_item.save(update_fields=['slot_key'])
        payload = self.payload()
        payload['e'][0][1:5] = [self.crafted_item.item_id, self.crafted.variant_key,
            ['crit', 'mastery'], [self.embellishment_item.item_id, self.embellishment.variant_key]]
        payload['e'][0][7] = 1
        snapshot = self.resolve(payload, strict=True)['equipment']['head']
        payload['e'][0].append(snapshot)
        for catalog_present in (True, False):
            with self.subTest(catalog_present=catalog_present):
                if not catalog_present:
                    WowItemVariantSnapshot.objects.all().delete()
                restored = self.resolve(payload, strict=True)
                self.assertEqual(restored['equipment']['head'], snapshot)
                for field, mutation in (('gems', 'type'), ('gems', 'slot'),
                                        ('enchant', 'type'), ('enchant', 'slot'),
                                        ('embellishment', 'type'), ('embellishment', 'slot')):
                    broken = copy.deepcopy(payload)
                    component = broken['e'][0][8][field]
                    if field == 'gems':
                        component = component[0]
                    component['variant']['type' if mutation == 'type' else 'compatible_slots'] = (
                        'drop_equipment' if mutation == 'type' else ['neck'])
                    with self.subTest(field=field, mutation=mutation), self.assertRaises(builder.GearBuilderError):
                        self.resolve(broken, strict=True)

    def test_verified_snapshot_is_preserved_instead_of_refreshed_from_catalog(self):
        snapshot = self.snapshot()
        snapshot['rawValue'] = 'historical-source-value'
        snapshot['resolvedStats'] = {'haste': 123}
        payload = self.payload()
        payload['e'][0].append(snapshot)
        self.hero.stats_json = {'strength': 9999}
        self.hero.save(update_fields=['stats_json'])
        result = self.resolve(payload, strict=True)
        self.assertEqual(result['equipment']['head'], snapshot)

    def test_nullable_snapshot_facts_do_not_accept_invalid_falsy_types(self):
        snapshot = self.snapshot()
        fields = ('item_class_id', 'item_subclass_id', 'inventory_type', 'allowable_class_mask')
        for key in fields:
            snapshot['item'][key] = None
        payload = self.payload()
        payload['e'][0].append(snapshot)
        self.assertEqual(self.resolve(payload, strict=True)['equipment']['head'], snapshot)
        for key in fields:
            for invalid in (False, '', [], {}):
                broken = copy.deepcopy(payload)
                broken['e'][0][8]['item'][key] = invalid
                with self.subTest(key=key, invalid=invalid), self.assertRaises(builder.GearBuilderError):
                    self.resolve(broken, strict=True)
        for key in ('compatible_slots', 'metadata'):
            for invalid in (False, '', 0):
                broken = copy.deepcopy(payload)
                broken['e'][0][8]['variant'][key] = invalid
                with self.subTest(key=key, invalid=invalid), self.assertRaises(builder.GearBuilderError):
                    self.resolve(broken, strict=True)

    def historical_fixture(self):
        from pathlib import Path
        return json.loads((Path(__file__).parent / 'js/fixtures/gear-share-history.json').read_text())

    def seed_historical_snapshot(self, document):
        """Bind captured identities to real, isolated ORM rows, never local DB pks.

        The compact resolved fixture has only projection fields; use its actual
        carrier for missing applicability, retaining all captured IDs and values.
        """
        for raw_slot, equipment in document['equipment'].items():
            slot = builder.canonical_equipment_slot(raw_slot)
            components = [equipment, *equipment.get('gems', [])]
            components += [equipment[key] for key in ('enchant', 'embellishment') if equipment.get(key)]
            for component in components:
                item, variant = component['item'], component['variant']
                metadata = variant.get('metadata') or {}
                branch = variant.get('data_branch') or ('ptr' if variant['key'].startswith('ptr:') else 'retail')
                facts = {**metadata.get('branch_display', {}).get(branch, {}), **item}
                central_item, _ = WowItemSnapshot.objects.get_or_create(item_id=item['item_id'], defaults={
                    'name': item.get('name', ''), 'slot_key': item.get('slot', ''),
                    'catalog_type': variant['type'], 'eligible_specs': facts.get('eligible_specs') or [],
                    **{key: facts.get(key) or 0 for key in (
                        'item_class_id', 'item_subclass_id', 'inventory_type', 'allowable_class_mask')},
                    'metadata': metadata,
                })
                central_variant, created = WowItemVariantSnapshot.objects.get_or_create(pk=variant['id'], defaults={
                    'item': central_item, 'season': self.season, 'batch_key': document['batchKey'],
                    'variant_key': variant['key'], 'variant_type': variant['type'],
                    'item_level': variant['item_level'], 'stats_json': variant.get('stats') or {},
                    'effects_json': variant.get('effects') or [],
                    'compatible_slots': variant.get('compatible_slots') or [slot],
                    'socket_count': variant.get('socket_count', 0),
                    'socket_types': variant.get('socket_types') or [],
                    'metadata': metadata, 'crafting_options': variant.get('crafting_options') or {},
                })
                if not created and slot not in central_variant.compatible_slots:
                    central_variant.compatible_slots.append(slot)
                    central_variant.save(update_fields=['compatible_slots'])

    @staticmethod
    def snapshot_components(snapshot):
        return [snapshot, *snapshot.get('gems', []), *[
            snapshot[field] for field in ('enchant', 'embellishment') if snapshot.get(field)]]

    def sourced_full_snapshot(self):
        payload = self.payload()
        payload['e'][0][1:5] = [self.crafted_item.item_id, self.crafted.variant_key,
            ['crit', 'mastery'], [self.embellishment_item.item_id, self.embellishment.variant_key]]
        payload['e'][0][7] = 1
        snapshot = self.resolve(payload, strict=True)['equipment']['head']
        for component in self.snapshot_components(snapshot):
            component['variant']['batch_key'] = 'test-batch'
        return {'version': 1, 'className': 'Warrior', 'specName': 'Fury',
                'batchKey': 'test-batch', 'equipment': {'head': snapshot}}

    def test_serialized_variant_batch_is_authoritative_not_active_or_metadata_batch(self):
        self.season.gear_batch_key = 'new-active-batch'
        self.season.save(update_fields=['gear_batch_key'])
        for variant in (self.crafted, self.gem, self.enchant, self.embellishment):
            variant.metadata = {**(variant.metadata or {}), 'batch_key': 'untrusted-metadata'}
            with self.subTest(kind=variant.variant_type):
                self.assertEqual(builder.serialize_variant(variant)['batch_key'], 'test-batch')

    def test_captured_old_snapshot_can_use_new_root_with_explicit_component_origins(self):
        document = self.historical_fixture()['v1-full']
        self.seed_historical_snapshot(document)
        old_batch = document['batchKey']
        for snapshot in document['equipment'].values():
            for component in self.snapshot_components(snapshot):
                component['variant']['batch_key'] = old_batch
        document['batchKey'] = 'new-active-batch'
        expected = {builder.canonical_equipment_slot(slot): row
                    for slot, row in document['equipment'].items()}
        response = self.post({**document, 'strict': True})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['equipment'], expected)
        code = self.encode(document)
        saved = storage.save_user_loadout(self.user, name='old-origins', code=code)
        self.assertEqual(storage.get_user_loadout(self.user, saved['id'])['code'], code)

    def test_mixed_old_equipment_new_enhancements_save_and_reimport_exact_snapshot(self):
        document = self.sourced_full_snapshot()
        snapshot = document['equipment']['head']
        document['batchKey'] = 'new-active-batch'
        for component in self.snapshot_components(snapshot)[1:]:
            old = WowItemVariantSnapshot.objects.get(pk=component['variant']['id'])
            old.pk = None
            old.batch_key = document['batchKey']
            old.save()
            component['variant']['id'] = old.pk
            component['variant']['batch_key'] = old.batch_key
        original = copy.deepcopy(document)
        code = self.encode(document)
        saved = storage.save_user_loadout(self.user, name='mixed-origins', code=code)
        stored_code = storage.get_user_loadout(self.user, saved['id'])['code']
        self.assertEqual(stored_code, code)
        _, stored_payload, _ = storage._decode_state(stored_code)
        restored = builder.hydrate_shared_state(
            **builder.share_payload_arguments(stored_payload), strict=True)
        self.assertEqual(restored['equipment'], document['equipment'])
        self.assertEqual(document, original)
        link = storage.create_short_link(self.user, code)
        self.assertEqual(storage.resolve_short_link(link.token)['code'], code)

    def test_each_component_rejects_forged_or_malformed_explicit_batch(self):
        document = self.sourced_full_snapshot()
        for index in range(len(self.snapshot_components(document['equipment']['head']))):
            for batch in ('wrong-batch', '', None, True, 17, [], {}, 'x' * 161):
                broken = copy.deepcopy(document)
                self.snapshot_components(broken['equipment']['head'])[index]['variant']['batch_key'] = batch
                with self.subTest(component=index, batch=batch):
                    response = self.post({**broken, 'strict': True})
                    self.assertEqual(response.status_code, 400, response.content)
                    self.assertNotIn('equipment', response.json())

    def test_legacy_components_without_explicit_batch_remain_bound_to_root(self):
        document = self.sourced_full_snapshot()
        for component in self.snapshot_components(document['equipment']['head']):
            component['variant'].pop('batch_key')
        self.assertEqual(self.post({**document, 'strict': True}).status_code, 200)
        document['batchKey'] = 'new-active-batch'
        self.assertEqual(self.post({**document, 'strict': True}).status_code, 400)
        # Even when every other component declares its origin, an omitted
        # enhancement origin must fall back to root, not to the equipment batch.
        for index in range(len(self.snapshot_components(document['equipment']['head']))):
            broken = self.sourced_full_snapshot()
            broken['batchKey'] = 'new-active-batch'
            self.snapshot_components(broken['equipment']['head'])[index]['variant'].pop('batch_key')
            with self.subTest(component=index):
                self.assertEqual(self.post({**broken, 'strict': True}).status_code, 400)

    def test_key_only_lookup_uses_component_batch_and_keeps_all_identity_checks(self):
        document = self.sourced_full_snapshot()
        document['batchKey'] = 'new-active-batch'
        components = self.snapshot_components(document['equipment']['head'])
        for component in components:
            old = WowItemVariantSnapshot.objects.get(pk=component['variant']['id'])
            # Same item/key in the new root batch must not replace old identity.
            old.pk = None
            old.batch_key = document['batchKey']
            old.item_level += 1
            old.save()
            component['variant']['id'] = None
        response = self.post({**document, 'strict': True})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['equipment'], document['equipment'])
        for index in range(len(components)):
            for field, value in (('item_level', 999), ('type', 'external'), ('data_branch', 'ptr')):
                broken = copy.deepcopy(document)
                self.snapshot_components(broken['equipment']['head'])[index]['variant'][field] = value
                if index == 0 and field == 'item_level':
                    broken['equipment']['head']['itemLevel'] = value
                with self.subTest(component=index, field=field):
                    self.assertEqual(self.post({**broken, 'strict': True}).status_code, 400)

    def test_captured_full_v1_snapshot_with_real_gem_and_enchant_survives_orm_and_deletion(self):
        document = self.historical_fixture()['v1-full']
        self.seed_historical_snapshot(document)
        expected = {builder.canonical_equipment_slot(slot): row for slot, row in document['equipment'].items()}
        self.assertTrue(expected['wrists']['gems'])
        self.assertTrue(expected['shoulders']['enchant'])
        for catalog_present in (True, False):
            if not catalog_present:
                WowItemVariantSnapshot.objects.filter(batch_key=document['batchKey']).delete()
            with self.subTest(catalog_present=catalog_present):
                response = self.post({**document, 'strict': True})
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()['equipment'], expected)
                for compressed in (True, False):
                    code = self.encode(document, compressed)
                    if len(code) > storage.MAX_ENCODED_STATE_LENGTH:
                        # The original uncompressed full snapshot exceeds the
                        # existing encoded limit; compatibility must not lift it.
                        with self.assertRaises(storage.GearBuilderStorageError):
                            storage.save_user_loadout(self.user, name='oversized-v1', code=code)
                        continue
                    saved = storage.save_user_loadout(self.user, name='captured-v1', code=code)
                    self.assertEqual(storage.get_user_loadout(self.user, saved['id'])['code'], code)
                    link = storage.create_short_link(self.user, code)
                    self.assertEqual(storage.resolve_short_link(link.token)['code'], code)

    def test_captured_old_producers_restore_all_sixteen_equipment_and_enhancements(self):
        fixtures = self.historical_fixture()
        self.seed_historical_snapshot(fixtures['resolved'])
        for name in ('v2-aliases', 'v3-aliases', 'v4-aliases', 'v4-equal-18'):
            payload = fixtures[name]
            with self.subTest(codec=name):
                result = self.resolve(payload, strict=True)
                self.assertEqual(set(result['equipment']), dict(builder.EQUIPMENT_SLOTS).keys())
                self.assertEqual(result['issues'], [])
                for slot, expected in fixtures['resolved']['equipment'].items():
                    actual = result['equipment'][slot]
                    self.assertEqual(actual['item']['item_id'], expected['item']['item_id'])
                    self.assertEqual(actual['variant']['id'], expected['variant']['id'])
                    self.assertEqual([g['variant']['id'] for g in actual['gems']],
                                     [g['variant']['id'] for g in expected.get('gems', [])])
                    for field in ('enchant', 'embellishment'):
                        self.assertEqual((actual.get(field) or {}).get('variant', {}).get('id'),
                                         (expected.get(field) or {}).get('variant', {}).get('id'))
                code = self.encode(payload)
                saved = storage.save_user_loadout(self.user, name=name, code=code)
                self.assertEqual(storage.get_user_loadout(self.user, saved['id'])['code'], code)
        for name in ('v4-unresolved', 'v4-conflict'):
            with self.subTest(codec=name), self.assertRaises(builder.GearBuilderError):
                self.resolve(fixtures[name], strict=True)

    def test_storage_keeps_size_and_decompression_limits_and_rejects_duplicate_keys(self):
        for code in ('j' + 'x' * storage.MAX_ENCODED_STATE_LENGTH,
                     self.encode({'padding': 'x' * storage.MAX_DECODED_STATE_LENGTH})):
            with self.assertRaises(storage.GearBuilderStorageError):
                storage._decode_state(code)
        raw = json.dumps(self.payload()).replace('"v": 4', '"v": 4, "v": 4')
        code = 'j' + base64.urlsafe_b64encode(raw.encode()).decode().rstrip('=')
        with self.assertRaises(storage.GearBuilderStorageError):
            storage._decode_state(code)
