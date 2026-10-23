"""毒液石旧分享引用的定向装等兼容与完整快照身份校验。"""
import base64
import copy
import gzip
import json

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import WowItemSnapshot, WowItemVariantSnapshot
from botend.services import gear_builder as builder, gear_builder_storage as storage
from botend.tests.test_gear_builder import GearBuilderTestDataMixin


class VenomstoneShareHistoryTests(GearBuilderTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.gem.compatible_slots.append('neck')
        self.gem.save(update_fields=['compatible_slots'])
        item = WowItemSnapshot.objects.create(item_id=280001, name_zh='毒液石项链',
            inventory_type=2, slot_key='neck', eligible_specs=['Warrior:Fury'])
        self.rows = []
        for kind, key, level in (
                ('crafted_equipment', 'crafted-myth-q5-331-venomstone', 340),
                ('drop_equipment', 'raid-myth-6-334-venomstone', 340),
                ('drop_equipment', 'raid-hero-6-321-venomstone', 328),
                ('crafted_equipment', 'crafted-hero-q5-318-venomstone', 328)):
            self.rows.append(WowItemVariantSnapshot.objects.create(item=item, season=self.season,
                batch_key='test-batch', variant_type=kind, variant_key=key, item_level=level,
                upgrade_track='hero' if level == 328 else 'myth', track_rank=8, track_max_rank=6,
                crafting_quality=5 if kind == 'crafted_equipment' else 0,
                crafting_options={'stat_count': 2, 'stat_pool': ['crit', 'haste'], 'secondary_total': 200}
                if kind == 'crafted_equipment' else {}, compatible_slots=['neck'],
                socket_count=1, socket_types=['prismatic'], stats_json={'stamina': 100},
                effects_json=[{'description_zh': '历史效果。'}],
                metadata={'venomstone': {'item_id': 280562}}))

    def payload(self, row, version=3):
        def ref(value, equipment=False):
            if version == 2:
                return value.pk
            if version == 3:
                return [value.pk, value.item.item_id, value.variant_key, value.item_level]
            return value.variant_key if equipment else [value.item.item_id, value.variant_key]
        return {'v': version, 'c': 'Warrior', 's': 'Fury', 'b': 'test-batch', 'l': ['neck'],
                'e': [['neck', row.item.item_id, ref(row, True),
                       ['crit', 'haste'] if row.variant_type == 'crafted_equipment' else [],
                       0, [ref(self.gem)], 0, 0]]}

    def resolve(self, payload, **kwargs):
        return builder.hydrate_shared_state(**builder.share_payload_arguments(payload), strict=True, **kwargs)

    def upgrade(self, row, level):
        row.item_level = level
        row.stats_json = {'stamina': 200}
        row.effects_json = [{'description_zh': '新效果。'}]
        if row.variant_type == 'crafted_equipment':
            row.crafting_options['secondary_total'] = 400
        row.save()

    def test_all_reference_versions_refresh_in_place_without_writing_or_changing_input(self):
        for row, level in zip(self.rows, (337, 341, 328, 328)):
            payloads = [self.payload(row, version) for version in (2, 3, 4)]
            original = copy.deepcopy(payloads)
            self.upgrade(row, level)
            for payload in payloads:
                with self.subTest(level=level, version=payload['v']), CaptureQueriesContext(connection) as queries:
                    result = self.resolve(payload)
                    entry = result['equipment']['neck']
                    self.assertEqual(entry['itemLevel'], level)
                    self.assertEqual(entry['variant']['id'], row.pk)
                    self.assertEqual(entry['variant']['stats']['stamina'], 200)
                    self.assertEqual(entry['gems'][0]['variant']['id'], self.gem.pk)
                    self.assertFalse(entry['addedSocket'])
                    self.assertEqual(entry['selectedStats'], payload['e'][0][3])
                    if level == 337:
                        self.assertEqual(entry['resolvedStats']['crit'] + entry['resolvedStats']['haste'], 400)
                self.assertFalse(any(q['sql'].lstrip().split()[0].upper() in
                    {'UPDATE', 'INSERT', 'DELETE'} for q in queries))
            self.assertEqual(payloads, original)

    def test_all_reference_versions_refresh_across_batches_but_current_false_keeps_old(self):
        for row, level in zip(self.rows, (337, 341, 328, 328)):
            payloads = [self.payload(row, version) for version in (2, 3, 4)]
            newer = copy.copy(row)
            newer.pk = None
            newer.batch_key = 'new-batch'
            self.upgrade(newer, level)
            self.season.gear_batch_key = 'new-batch'
            self.season.save(update_fields=['gear_batch_key'])
            for payload in payloads:
                with self.subTest(level=level, version=payload['v']):
                    self.assertEqual(self.resolve(payload)['equipment']['neck']['variant']['id'], newer.pk)
                    self.assertEqual(self.resolve(payload, current=False)['equipment']['neck']['itemLevel'], row.item_level)
            row.refresh_from_db()
            self.assertEqual(row.item_level, 328 if level == 328 else 340)

    def test_complete_snapshots_and_stored_codes_remain_exact_after_in_place_update(self):
        user = get_user_model().objects.create_user(username='毒液石历史快照')
        for row, level in zip(self.rows, (337, 341, 328, 328)):
            payloads = [self.payload(row, version) for version in (2, 3, 4)]
            snapshot = self.resolve(payloads[0])['equipment']['neck']
            for payload in payloads:
                payload['e'][0].append(copy.deepcopy(snapshot))
            payloads.append({'version': 1, 'className': 'Warrior', 'specName': 'Fury',
                'batchKey': 'test-batch', 'equipment': {'neck': copy.deepcopy(snapshot)}, 'lockedSlots': ['neck']})
            self.upgrade(row, level)
            for payload in payloads:
                with self.subTest(level=level, version=payload.get('v', 1)):
                    original = copy.deepcopy(payload)
                    self.assertEqual(self.resolve(payload)['equipment']['neck'], snapshot)
                    response = self.client.post('/portal/api/gear-builder/resolve-share/',
                        data=json.dumps({**payload, 'strict': True}), content_type='application/json')
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertEqual(response.json()['equipment']['neck'], snapshot)
                    code = 'z' + base64.urlsafe_b64encode(gzip.compress(json.dumps(payload).encode())).decode().rstrip('=')
                    saved = storage.save_user_loadout(user, name='毒液石旧快照', code=code)
                    self.assertEqual(storage.get_user_loadout(user, saved['id'])['code'], code)
                    link = storage.create_short_link(user, code)
                    self.assertEqual(storage.resolve_short_link(link.token)['code'], code)
                    self.assertEqual(payload, original)

    def test_v3_invalid_identity_and_other_levels_still_rejected(self):
        row = self.rows[0]
        payload = self.payload(row)
        self.upgrade(row, 337)
        for index, value in ((0, 999999), (1, 999999), (2, 'wrong-key'), (3, 339), (3, 328), (3, 341)):
            broken = copy.deepcopy(payload)
            broken['e'][0][2][index] = value
            with self.subTest(index=index, value=value), self.assertRaises(builder.GearBuilderError):
                self.resolve(broken)
        broken = copy.deepcopy(payload)
        broken['b'] = 'wrong-batch'
        with self.assertRaises(builder.GearBuilderError):
            self.resolve(broken)

    def test_snapshot_exemption_does_not_disable_other_identity_checks(self):
        row = self.rows[1]
        payload = self.payload(row)
        payload['e'][0].append(self.resolve(payload)['equipment']['neck'])
        self.upgrade(row, 341)
        for field, value in (('key', 'wrong-key'), ('type', 'crafted_equipment'),
                ('data_branch', 'ptr'), ('batch_key', 'wrong-batch'), ('item_level', 339), ('item_id', 999999)):
            broken = copy.deepcopy(payload)
            broken['e'][0][8]['variant'][field] = value
            with self.subTest(field=field), self.assertRaises(builder.GearBuilderError):
                self.resolve(broken)
        row.metadata = {}
        row.save(update_fields=['metadata'])
        # 分享内保留毒液石标记也不能代替中央身份。
        with self.assertRaises(builder.GearBuilderError):
            self.resolve(payload)

    def test_cross_batch_alias_requires_same_branch_key_type_and_central_marker(self):
        old = self.rows[1]
        newer = copy.copy(old)
        newer.pk = None
        newer.batch_key = 'new-batch'
        self.upgrade(newer, 341)
        self.season.gear_batch_key = 'new-batch'
        self.season.save(update_fields=['gear_batch_key'])
        for field, value in (('metadata', {'data_branch': 'ptr', 'venomstone': {'item_id': 280562}}),
                ('metadata', {}), ('variant_key', 'other-myth-6-334-venomstone'),
                ('variant_type', 'crafted_equipment'), ('item_level', 337), ('track_rank', 6)):
            original = getattr(newer, field)
            setattr(newer, field, value)
            newer.save()
            with self.subTest(field=field, value=value):
                self.assertEqual(builder.current_variants([old])[old.pk].pk, old.pk)
            setattr(newer, field, original)
            newer.save()
        self.assertEqual(builder.current_variants([old])[old.pk].pk, newer.pk)

    def test_ordinary_or_328_identity_never_accepts_340_as_alias(self):
        for row in self.rows:
            payload = self.payload(row)
            payload['e'][0][2][3] = 340
            if row.item_level != 328:
                self.upgrade(row, 337 if row.variant_type == 'crafted_equipment' else 341)
                row.variant_key = row.variant_key.removesuffix('-venomstone')
                row.save(update_fields=['variant_key'])
                payload['e'][0][2][2] = row.variant_key
            with self.subTest(key=row.variant_key), self.assertRaises(builder.GearBuilderError):
                self.resolve(payload)

    def test_branch_prefixed_keys_accept_only_the_matching_central_identity(self):
        for row, level, branch in zip(self.rows, (337, 341), ('retail', 'ptr')):
            row.variant_key = f'{branch}:{row.variant_key}'
            row.metadata['data_branch'] = branch
            row.save()
            payload = self.payload(row)
            self.upgrade(row, level)
            self.assertEqual(self.resolve(payload)['equipment']['neck']['itemLevel'], level)
            row.item_level = 340
            row.save(update_fields=['item_level'])
            newer = copy.copy(row)
            newer.pk = None
            newer.batch_key = 'new-batch'
            self.upgrade(newer, level)
            self.season.gear_batch_key = 'new-batch'
            self.season.save(update_fields=['gear_batch_key'])
            self.assertEqual(self.resolve(payload)['equipment']['neck']['variant']['id'], newer.pk)
