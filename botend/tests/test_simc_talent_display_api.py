"""Dashboard title projections must not flow back into executable inputs."""
import hashlib
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from botend.guide_models import ClassGuide
from botend.models import SimcBackendBinary, SimcResourceVersion, SimcTalentString, SimcTask
from botend.services.simc_task_service import _build_talent_payload, _compute_content_hash

CODE = 'C8DAAAAAAAAAAAAAAAAAAAAAAYGGLzMzsgZGZmZGAAAMLzYmZZWAAAALmZYbmZmZsBAAAAgNzMmZAAwMDzMmZmZWAmZANjxYwMMGA'


class TalentDisplayApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(username='talent_display_api', password='test-only')
        cls.guide = ClassGuide.objects.create(
            slug='dashboard-talent-display', title='Fire guide', spec_id=63,
            class_name='Mage', spec_name='Fire', game_version='12.1.5',
            source_url='https://www.icy-veins.com/wow/fire-mage-pve-dps-guide',
            source_markdown='## 大秘境\n\n:::talents Mythic+ Build\n```json\n'
            + json.dumps({'code': CODE}) + '\n```\n:::\n',
        )
        digest = hashlib.sha256(CODE.encode()).hexdigest()
        cls.talent = SimcTalentString.objects.create(
            name='[Icy Veins] ![](https://example.test/icon.webp) Mythic+ Build',
            spec='mage_fire', talent=CODE, hero_talent_names=['霜火'],
            system_key=f'guide:icy-veins:{cls.guide.pk}:mage_fire:{digest}',
            is_system=True,
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_read_surfaces_and_unchanged_editor_put_preserve_talent_hash(self):
        original = _build_talent_payload(self.talent)
        signature = _compute_content_hash(original)
        version = SimcResourceVersion.objects.create(
            resource_type='talent', resource_id=self.talent.pk,
            payload=original, content_hash=signature,
        )
        display_names = []
        for url in ('/api/simc-talent-string/', '/api/simc-talent-string-candidates/'):
            response = self.client.get(url, {'spec': self.talent.spec})
            self.assertEqual(response.status_code, 200)
            row = next(item for item in response.json()['data'] if item['id'] == self.talent.pk)
            self.assertEqual(row['name'], original['name'])
            self.assertEqual(row['talent'], original['talent'])
            display_names.append(row['display_name'])
        detail = self.client.get(f'/api/simc-talent-string/{self.talent.pk}/')
        self.assertEqual(detail.status_code, 200)
        row = detail.json()['data']
        self.assertEqual(row['name'], original['name'])
        display_names.append(row['display_name'])
        options = self.client.get('/api/simc-benchmarks/options/')
        self.assertEqual(options.status_code, 200)
        option = next(item for item in options.json()['data']['resources']['talent_strings'] if item['id'] == self.talent.pk)
        display_names.append(option['display_name'])
        self.assertEqual(option['name'], original['name'])
        self.assertEqual(option['build_code'], original['talent'])
        self.assertEqual(len(set(display_names)), 1)
        self.assertIn('大秘境', display_names[0])
        self.assertNotIn('https://', display_names[0])

        # Decoder behavior is independent: this test verifies the actual PUT
        # route, ORM save and immutable payload/hash contract, not DB2 parsing.
        with patch('botend.dashboard.api._validate_simc_talent_hero_tree', return_value=['霜火']):
            saved = self.client.put('/api/simc-talent-string/', data=json.dumps({
                key: row[key] for key in ('id', 'name', 'spec', 'talent')
            }), content_type='application/json')
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json()['success'])
        self.talent.refresh_from_db()
        self.assertEqual(_build_talent_payload(self.talent), original)
        self.assertEqual(_compute_content_hash(_build_talent_payload(self.talent)), signature)
        version.refresh_from_db()
        self.assertEqual(version.payload, original)
        self.assertEqual(version.content_hash, signature)

    def test_history_displays_frozen_name_not_changed_live_name_or_code(self):
        original = _build_talent_payload(self.talent)
        signature = _compute_content_hash(original)
        version = SimcResourceVersion.objects.create(resource_type='talent', resource_id=self.talent.pk,
            payload=original, content_hash=signature)
        backend = SimcBackendBinary.objects.create(identifier='talent-title-test', name='test', simc_path='/not-executed')
        task = SimcTask.objects.create(user_id=self.user.pk, name='History', simc_profile_id=0,
            backend=backend, talent_string=self.talent, talent_version=version, current_status=2)
        # A later resource edit cannot supply a different build's title to history.
        SimcTalentString.objects.filter(pk=self.talent.pk).update(name='CURRENT LIVE NAME', talent='NEW_CODE')
        response = self.client.get('/api/simc-workbench/history/')
        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.json()['data'] if item['id'] == task.pk)
        self.assertEqual(row['talent_name'], original['name'])
        self.assertEqual(row['talent_display_name'], '[Icy Veins] Mythic+ Build')
        version.refresh_from_db()
        self.assertEqual(version.payload, original)
        self.assertEqual(version.content_hash, signature)

    def test_non_guide_legacy_hero_name_is_normalized_only_for_display(self):
        talent = SimcTalentString.objects.create(name='法术投射者 Build', spec='mage_frost',
            talent='LEGACY_CODE', owner_user_id=self.user.pk)
        response = self.client.get(f'/api/simc-talent-string/{talent.pk}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['name'], '法术投射者 Build')
        self.assertEqual(response.json()['data']['display_name'], '疾咒师 Build')
        talent.refresh_from_db()
        self.assertEqual(talent.name, '法术投射者 Build')
