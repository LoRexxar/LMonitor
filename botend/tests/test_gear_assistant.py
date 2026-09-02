import json

from django.contrib.auth import get_user_model
from django.test import TestCase

from botend.models import GearBuilderOwnedItem, WowItemSnapshot, WowItemVariantSnapshot
from botend.tests.test_gear_builder import GearBuilderTestDataMixin


class GearAssistantTests(GearBuilderTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username='gear-helper', password='test-password')
        self.dungeon_item = WowItemSnapshot.objects.create(
            item_id=10101,
            name='Dungeon Helm',
            name_zh='地城头盔',
            catalog_type='equipment',
            slot_key='head',
            item_class_id=4,
            item_subclass_id=4,
            inventory_type=1,
            armor_type='板甲',
            eligible_specs=['Warrior:Fury'],
        )
        self.dungeon_variant = WowItemVariantSnapshot.objects.create(
            item=self.dungeon_item,
            season=self.season,
            batch_key='test-batch',
            variant_key='dungeon-hero-6',
            variant_type=WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
            item_level=730,
            upgrade_track='hero',
            track_rank=6,
            track_max_rank=6,
            compatible_slots=['head'],
            stats_json={'strength': 1100, 'crit': 500, 'mastery': 260},
            source_json=[{'type': 'mythic_plus', 'instance_zh': '测试地城'}],
        )

    def test_page_and_entry_require_login(self):
        response = self.client.get('/portal/gear-assistant/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/auth/login/', response['Location'])
        anonymous_header = self.client.get('/portal/gear-builder/')
        self.assertNotContains(anonymous_header, '/portal/gear-assistant/')

        self.client.force_login(self.user)
        response = self.client.get('/portal/gear-assistant/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '辅助配装')
        self.assertContains(response, '生成三套配装方案')
        builder = self.client.get('/portal/gear-builder/')
        self.assertContains(builder, 'id="gear-mode-owned"')
        self.assertContains(builder, 'id="gear-open-assistant"')

    def test_login_returns_to_assistant_and_rejects_external_next(self):
        response = self.client.post(
            '/auth/login/',
            data=json.dumps({'username': 'gear-helper', 'password': 'test-password', 'next': '/portal/gear-assistant/'}),
            content_type='application/json',
        )
        self.assertEqual(response.json()['redirect_url'], '/portal/gear-assistant/')
        self.client.logout()
        unsafe = self.client.post(
            '/auth/login/',
            data=json.dumps({'username': 'gear-helper', 'password': 'test-password', 'next': 'https://example.com/steal'}),
            content_type='application/json',
        )
        self.assertEqual(unsafe.json()['redirect_url'], '/dashboard/')

    def test_owned_items_are_private_and_reuse_variant(self):
        anonymous = self.client.get('/portal/api/gear-builder/owned-items/')
        self.assertEqual(anonymous.status_code, 401)
        self.client.force_login(self.user)
        created = self.client.post(
            '/portal/api/gear-builder/owned-items/',
            data=json.dumps({'variant_id': self.hero.id, 'slot': 'head', 'source': 'manual'}),
            content_type='application/json',
        )
        self.assertEqual(created.status_code, 200, created.content)
        owned = GearBuilderOwnedItem.objects.get(user=self.user)
        self.assertEqual(owned.variant_id, self.hero.id)
        self.assertEqual(owned.item_id, self.helm.item_id)
        bulk_body = {'items': [{'variant_id': self.hero.id, 'slot': 'head', 'source': 'simc_equipped'}]}
        self.client.post('/portal/api/gear-builder/owned-items/', data=json.dumps(bulk_body), content_type='application/json')
        self.client.post('/portal/api/gear-builder/owned-items/', data=json.dumps(bulk_body), content_type='application/json')
        owned.refresh_from_db()
        self.assertEqual(owned.quantity, 1)

        other = get_user_model().objects.create_user(username='other-helper', password='test-password')
        self.client.force_login(other)
        self.assertEqual(self.client.get('/portal/api/gear-builder/owned-items/').json()['items'], [])

    def test_simc_bag_items_are_returned_for_owned_import(self):
        profile = '\n'.join((
            'warrior="Test"',
            'spec=fury',
            'head=rift_helm,id=10001,ilevel=710',
            '### Gear from Bags',
            '# Dungeon Helm (730)',
            '# head=dungeon_helm,id=10101,ilevel=730,bonus_id=11/22,gem_id=10004,enchant_id=8001',
        ))
        response = self.client.post(
            '/portal/api/gear-builder/import-simc/',
            data=json.dumps({'profile': profile}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(len(payload['equipment']), 1)
        self.assertEqual(len(payload['bag_equipment']), 1)
        self.assertEqual(payload['bag_equipment'][0]['variant_id'], self.dungeon_variant.id)
        self.assertEqual(payload['bag_equipment'][0]['bonus_ids'], [11, 22])
        self.assertEqual(payload['bag_equipment'][0]['import_source'], 'simc_bag')

    def test_optimizer_returns_three_deterministic_plans(self):
        self.client.force_login(self.user)
        self.client.post(
            '/portal/api/gear-builder/owned-items/',
            data=json.dumps({'variant_id': self.hero.id, 'slot': 'head'}),
            content_type='application/json',
        )
        response = self.client.post(
            '/portal/api/gear-assistant/optimize/',
            data=json.dumps({
                'class_name': 'Warrior',
                'spec_name': 'Fury',
                'equipment': {},
                'target': {'crit': 12, 'haste': 8, 'mastery': 14, 'versatility': 0},
                'include_gems': True,
                'include_enchants': True,
                'flask': 'auto',
                'use_ai': False,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        plans = {row['key']: row for row in response.json()['plans']}
        self.assertEqual(set(plans), {'prefer_owned', 'all', 'dungeon'})
        self.assertEqual(plans['prefer_owned']['equipment']['head']['variant']['id'], self.hero.id)
        self.assertEqual(plans['prefer_owned']['owned_count'], 1)
        self.assertEqual(plans['dungeon']['equipment']['head']['variant']['id'], self.dungeon_variant.id)
        self.assertIn('测试地城', plans['dungeon']['missing_items'][0]['source'])
        self.assertIn(plans['all']['flask']['key'], {'none', 'crit', 'haste', 'mastery'})

    def test_fixed_slot_is_preserved_and_not_reported_missing(self):
        self.client.force_login(self.user)
        response = self.client.post(
            '/portal/api/gear-assistant/optimize/',
            data=json.dumps({
                'class_name': 'Warrior', 'spec_name': 'Fury',
                'equipment': {'head': {
                    'variant': {'id': self.hero.id}, 'selectedStats': [],
                    'gems': [{'variant': {'id': self.gem.id}}],
                    'enchant': {'variant': {'id': self.enchant.id}},
                }},
                'target': {'crit': 20, 'haste': 15, 'mastery': 20, 'versatility': 5},
                'include_gems': False, 'include_enchants': False, 'flask': 'none', 'use_ai': False,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        for plan in response.json()['plans']:
            self.assertEqual(plan['equipment']['head']['variant']['id'], self.hero.id)
            self.assertEqual(plan['equipment']['head']['gems'][0]['variant']['id'], self.gem.id)
            self.assertEqual(plan['equipment']['head']['enchant']['variant']['id'], self.enchant.id)
            self.assertNotIn('head', [row['slot'] for row in plan['missing_items']])

    def test_unlocked_gems_and_enchants_can_be_replaced(self):
        crit_gem_item = WowItemSnapshot.objects.create(
            item_id=10111, name_zh='暴击测试宝石', catalog_type='gem', quality=4,
        )
        crit_gem = WowItemVariantSnapshot.objects.create(
            item=crit_gem_item, season=self.season, batch_key='test-batch',
            variant_key='crit-gem-q3', variant_type=WowItemVariantSnapshot.TYPE_GEM,
            crafting_quality=3, compatible_slots=['head'], stats_json={'crit': 1000},
            metadata={'simc_name': 'critical_test_gem_3'},
        )
        haste_enchant_item = WowItemSnapshot.objects.create(
            item_id=10112, name_zh='急速测试附魔', catalog_type='enchant', quality=4,
        )
        haste_enchant = WowItemVariantSnapshot.objects.create(
            item=haste_enchant_item, season=self.season, batch_key='test-batch',
            variant_key='haste-enchant-q3', variant_type=WowItemVariantSnapshot.TYPE_ENCHANT,
            crafting_quality=3, compatible_slots=['head'], stats_json={'haste': 1000},
            metadata={'simc_name': 'haste_test_enchant_3'},
        )
        self.client.force_login(self.user)
        response = self.client.post(
            '/portal/api/gear-assistant/optimize/',
            data=json.dumps({
                'class_name': 'Warrior', 'spec_name': 'Fury',
                'equipment': {'head': {
                    'variant': {'id': self.hero.id},
                    'gems': [{'variant': {'id': self.gem.id}}],
                    'enchant': {'variant': {'id': self.enchant.id}},
                }},
                'target': {'crit': 35, 'haste': 30, 'mastery': 11.2, 'versatility': 0},
                'include_gems': True, 'lock_gems': False,
                'include_enchants': True, 'lock_enchants': False,
                'flask': 'none', 'use_ai': False,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        entry = response.json()['plans'][0]['equipment']['head']
        self.assertEqual(entry['gems'][0]['variant']['id'], crit_gem.id)
        self.assertEqual(entry['enchant']['variant']['id'], haste_enchant.id)


class GearAssistantFrontendContractTests(TestCase):
    def test_frontend_assets_expose_owned_and_three_plan_flow(self):
        template = open('templates/portal/gear_assistant.html', encoding='utf-8').read()
        script = open('static/portal/js/gear_assistant.js', encoding='utf-8').read()
        builder = open('static/portal/js/gear_builder.js', encoding='utf-8').read()
        self.assertIn('data-optimize-url', template)
        self.assertIn('生成三套配装方案', template)
        self.assertIn('data-apply-plan', script)
        self.assertIn('生成三套配装方案', script)
        self.assertIn('lock_gems', script)
        self.assertIn('lock_enchants', script)
        self.assertIn('owned_equipment', builder)
        self.assertIn('wowdaily:gear-assistant:draft:v1', builder)
