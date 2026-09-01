from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase

from botend.models import (
    DashboardUserGroup,
    DashboardUserGroupMembership,
    GearBuilderShareLink,
    GearBuilderUserLoadout,
)


User = get_user_model()
ROOT = Path(__file__).resolve().parents[2]


class DashboardGearBuilderManagementTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            username='gear-dashboard-admin',
            email='gear-dashboard-admin@example.com',
            password='Admin-pass-123!',
        )
        self.owner = User.objects.create_user(username='gear-owner', password='Owner-pass-123!')
        self.viewer = User.objects.create_user(username='gear-viewer', password='Viewer-pass-123!')
        self.loadout = GearBuilderUserLoadout.objects.create(
            user=self.owner,
            name='团本单体',
            encoded_state='z-loadout-code',
            state_hash='a' * 64,
            class_name='Warrior',
            spec_name='Fury',
            batch_key='season-1-build-1',
        )
        self.share = GearBuilderShareLink.objects.create(
            user=self.owner,
            token='GearShort001',
            encoded_state='z-share-code',
            state_hash='b' * 64,
            class_name='Warrior',
            spec_name='Fury',
            batch_key='season-1-build-1',
            access_count=7,
        )

    def grant_viewer_permission(self):
        group = DashboardUserGroup.objects.create(
            name='职业配装管理员',
            permission_codes=['gear-builder.manage'],
            is_active=True,
        )
        DashboardUserGroupMembership.objects.create(user=self.viewer, group=group)

    def test_dashboard_section_uses_existing_permission_catalog(self):
        self.client.force_login(self.viewer)
        self.assertEqual(
            self.client.get('/dashboard/?section=gear-builder-management').status_code,
            403,
        )

        self.grant_viewer_permission()
        response = self.client.get('/dashboard/?section=gear-builder-management')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['dashboard_default_section'], 'gear-builder-management')
        self.assertContains(response, '职业配装管理')
        self.assertContains(response, 'gear-builder.manage')

    def test_management_api_requires_matching_dashboard_permission(self):
        self.client.force_login(self.viewer)
        response = self.client.get('/api/dashboard/gear-builder/loadouts/')
        self.assertEqual(response.status_code, 403)

        self.grant_viewer_permission()
        response = self.client.get('/api/dashboard/gear-builder/loadouts/')
        self.assertEqual(response.status_code, 200)

    def test_lists_both_resources_without_returning_encoded_state(self):
        self.client.force_login(self.admin)
        response = self.client.get('/api/dashboard/gear-builder/loadouts/?q=gear-owner')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['pagination']['total'], 1)
        self.assertNotIn('code', payload['records'][0])
        self.assertEqual(payload['records'][0]['name'], '团本单体')
        self.assertEqual(payload['summary'], {'loadouts': 1, 'shares': 1, 'active_shares': 1})

        response = self.client.get('/api/dashboard/gear-builder/shares/?q=GearShort')
        payload = response.json()
        self.assertEqual(payload['records'][0]['short_path'], '/g/GearShort001/')
        self.assertEqual(payload['records'][0]['access_count'], 7)
        self.assertNotIn('code', payload['records'][0])

    def test_detail_returns_code_only_on_demand(self):
        self.client.force_login(self.admin)
        loadout = self.client.get(
            f'/api/dashboard/gear-builder/loadouts/{self.loadout.id}/'
        ).json()['record']
        share = self.client.get(
            f'/api/dashboard/gear-builder/shares/{self.share.id}/'
        ).json()['record']
        self.assertEqual(loadout['code'], 'z-loadout-code')
        self.assertEqual(share['code'], 'z-share-code')
        self.assertEqual(share['token'], 'GearShort001')

    def test_delete_loadout_and_disable_share(self):
        self.client.force_login(self.admin)
        response = self.client.delete(
            f'/api/dashboard/gear-builder/loadouts/{self.loadout.id}/'
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(GearBuilderUserLoadout.objects.filter(pk=self.loadout.id).exists())

        response = self.client.delete(
            f'/api/dashboard/gear-builder/shares/{self.share.id}/'
        )
        self.assertEqual(response.status_code, 200)
        self.share.refresh_from_db()
        self.assertFalse(self.share.is_active)
        self.assertTrue(GearBuilderShareLink.objects.filter(pk=self.share.id).exists())

        inactive = self.client.get('/api/dashboard/gear-builder/shares/?active=inactive').json()
        self.assertEqual(inactive['pagination']['total'], 1)

    def test_unknown_resource_and_invalid_page_are_rejected(self):
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get('/api/dashboard/gear-builder/unknown/').status_code,
            404,
        )
        self.assertEqual(
            self.client.get('/api/dashboard/gear-builder/loadouts/?page=bad').status_code,
            400,
        )


class DashboardGearBuilderFrontendContractTests(TestCase):
    def test_dashboard_contains_dedicated_tab_and_management_script(self):
        template = (ROOT / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        javascript = (ROOT / 'static/dashboard/js/gear_builder_management.js').read_text(encoding='utf-8')
        main = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')

        self.assertIn('data-section="gear-builder-management"', template)
        self.assertIn('data-gear-builder-resource="loadouts"', template)
        self.assertIn('data-gear-builder-resource="shares"', template)
        self.assertIn("dashboard/js/gear_builder_management.js", template)
        self.assertIn('window.loadGearBuilderManagement = loadRecords', javascript)
        self.assertIn('navigator.clipboard.writeText(state.code)', javascript)
        self.assertIn("sectionId === 'gear-builder-management'", main)
