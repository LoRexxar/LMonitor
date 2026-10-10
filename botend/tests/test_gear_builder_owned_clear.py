"""Account-scoped owned-item clearing through the real HTTP/ORM boundary."""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from botend.models import (
    GearBuilderOwnedItem,
    GearBuilderUserLoadout,
    SeasonMeta,
    WowItemSnapshot,
    WowItemVariantSnapshot,
)


class GearBuilderOwnedClearAPITests(TestCase):
    url = '/portal/api/gear-builder/owned-items/'

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username='owned-clear-user')
        cls.other = get_user_model().objects.create_user(username='owned-clear-other')
        season = SeasonMeta.objects.create(
            season_key='owned-clear-season', season_name='Clear test',
            mplus_zone_id=1, raid_zone_id=2,
        )
        item = WowItemSnapshot.objects.create(item_id=90001, name='Retained helm', slot_key='head')
        variant = WowItemVariantSnapshot.objects.create(
            item=item, season=season, batch_key='current', variant_key='helm',
            variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
            stats_json={'crit': 123},
        )
        cls.owned = GearBuilderOwnedItem.objects.create(
            user=cls.user, fingerprint='current-head', item_id=item.item_id,
            variant=variant, slot_key='head', batch_key='current', quantity=7,
        )
        GearBuilderOwnedItem.objects.create(
            user=cls.user, fingerprint='historical-ring', item_id=90002,
            slot_key='finger1', batch_key='historical',
            source=GearBuilderOwnedItem.SOURCE_SIMC_BAG,
        )
        cls.other_owned = GearBuilderOwnedItem.objects.create(
            user=cls.other, fingerprint='current-head', item_id=item.item_id,
            variant=variant, slot_key='head', batch_key='current', quantity=3,
        )
        for user in (cls.user, cls.other):
            GearBuilderUserLoadout.objects.create(
                user=user, name='Saved loadout', encoded_state='retained-state',
                state_hash='retained-hash', class_name='Warrior', spec_name='Fury',
                batch_key='current',
            )

    def snapshot(self, model, **filters):
        return list(model.objects.filter(**filters).order_by('pk').values())

    def delete_collection(self, payload, **kwargs):
        return self.client.delete(
            self.url, data=json.dumps(payload), content_type='application/json', **kwargs,
        )

    def test_anonymous_clear_is_unauthorized_and_preserves_rows(self):
        before = self.snapshot(GearBuilderOwnedItem)
        for payload in ({'confirm_clear': True}, {}):
            with self.subTest(payload=payload):
                response = self.delete_collection(payload)
                self.assertEqual(response.status_code, 401)
                self.assertIs(response.json()['success'], False)
                self.assertEqual(self.snapshot(GearBuilderOwnedItem), before)

    def test_clear_requires_explicit_json_boolean_true(self):
        self.client.force_login(self.user)
        before = self.snapshot(GearBuilderOwnedItem)
        bodies = [
            '', '{}', '{"confirm_clear": false}', '{"confirm_clear": "true"}',
            '{"confirm_clear": 1}', '{"confirm_clear": 0}', '{"confirm_clear": null}',
            '{"confirm_clear": []}', '{"confirm_clear": {}}',
            '[]', 'true', 'null', '{broken',
        ]
        for body in bodies:
            with self.subTest(body=body):
                response = self.client.delete(
                    self.url + '?confirm_clear=true', data=body, content_type='application/json',
                )
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIs(response.json()['success'], False)
                self.assertEqual(self.snapshot(GearBuilderOwnedItem), before)

    def test_clear_is_account_wide_isolated_and_idempotent(self):
        self.client.force_login(self.user)
        retained = {
            model: self.snapshot(model)
            for model in (GearBuilderUserLoadout, WowItemSnapshot, WowItemVariantSnapshot)
        }
        other_before = self.snapshot(GearBuilderOwnedItem, user=self.other)
        # Client-supplied identities/filters must never change the authenticated scope.
        response = self.client.delete(
            self.url + f'?user_id={self.other.pk}&slot=head&batch_key=current',
            data=json.dumps({'confirm_clear': True, 'user_id': self.other.pk}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {'success': True, 'deleted_count': 2})
        self.assertFalse(GearBuilderOwnedItem.objects.filter(user=self.user).exists())
        self.assertEqual(self.client.get(self.url).json(), {'success': True, 'items': []})
        repeated = self.delete_collection({'confirm_clear': True})
        self.assertEqual(repeated.status_code, 200, repeated.content)
        self.assertEqual(repeated.json(), {'success': True, 'deleted_count': 0})
        self.assertEqual(self.snapshot(GearBuilderOwnedItem, user=self.other), other_before)
        for model, before in retained.items():
            self.assertEqual(self.snapshot(model), before)

    def test_single_delete_keeps_existing_contract_and_owner_isolation(self):
        self.client.force_login(self.user)
        before = self.snapshot(GearBuilderOwnedItem)
        forbidden = self.client.delete(f'{self.url}{self.other_owned.pk}/')
        self.assertEqual(forbidden.status_code, 404)
        self.assertIs(forbidden.json()['success'], False)
        self.assertEqual(self.snapshot(GearBuilderOwnedItem), before)
        response = self.client.delete(f'{self.url}{self.owned.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'success': True})
        self.assertFalse(GearBuilderOwnedItem.objects.filter(pk=self.owned.pk).exists())
        self.assertEqual(GearBuilderOwnedItem.objects.filter(user=self.user).count(), 1)
        missing = self.client.delete(f'{self.url}{self.owned.pk}/')
        self.assertEqual(missing.status_code, 404)
