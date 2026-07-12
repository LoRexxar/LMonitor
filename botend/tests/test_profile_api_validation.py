"""
Regression tests for SimC Profile API validation requirements.
Run BEFORE implementing fixes to see expected failures.
"""
import json
from django.contrib.auth.models import User
from django.test import Client, TestCase
from botend.models import SimcProfile, SimcTask


class SimcProfileAPIValidationTests(TestCase):
    """Test Profile API validation for battlenet/manual_equipment/attribute_only modes."""

    def setUp(self):
        self.user = User.objects.create_user(username='profile_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_battlenet_profile_requires_region_realm_character(self):
        """Requirement 2: battlenet mode must validate region, realm, character."""
        # Missing region
        response = self.client.post('/api/simc-profile/', data=json.dumps({
            'name': 'Test Battlenet Missing Region',
            'spec': 'fury',
            'player_config_mode': 'battlenet',
            'battlenet_region': '',
            'battlenet_realm': 'Kazzak',
            'battlenet_character': 'TestChar',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('region', response.json()['error'].lower())

        # Invalid region
        response = self.client.post('/api/simc-profile/', data=json.dumps({
            'name': 'Test Battlenet Invalid Region',
            'spec': 'fury',
            'player_config_mode': 'battlenet',
            'battlenet_region': 'invalid',
            'battlenet_realm': 'Kazzak',
            'battlenet_character': 'TestChar',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('region', response.json()['error'].lower())

        # Missing realm
        response = self.client.post('/api/simc-profile/', data=json.dumps({
            'name': 'Test Battlenet Missing Realm',
            'spec': 'fury',
            'player_config_mode': 'battlenet',
            'battlenet_region': 'eu',
            'battlenet_realm': '',
            'battlenet_character': 'TestChar',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('realm', response.json()['error'].lower())

        # Missing character
        response = self.client.post('/api/simc-profile/', data=json.dumps({
            'name': 'Test Battlenet Missing Character',
            'spec': 'fury',
            'player_config_mode': 'battlenet',
            'battlenet_region': 'eu',
            'battlenet_realm': 'Kazzak',
            'battlenet_character': '',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('character', response.json()['error'].lower())

        # Valid battlenet profile
        response = self.client.post('/api/simc-profile/', data=json.dumps({
            'name': 'Test Battlenet Valid',
            'spec': 'fury',
            'player_config_mode': 'battlenet',
            'battlenet_region': 'eu',
            'battlenet_realm': 'Kazzak',
            'battlenet_character': 'TestChar',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())

    def test_manual_equipment_profile_requires_player_equipment(self):
        """Requirement 2: manual_equipment mode must validate player_equipment non-empty."""
        response = self.client.post('/api/simc-profile/', data=json.dumps({
            'name': 'Test Manual Equipment Empty',
            'spec': 'fury',
            'player_config_mode': 'manual_equipment',
            'player_equipment': '',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('equipment', response.json()['error'].lower())

        # Valid manual_equipment profile
        response = self.client.post('/api/simc-profile/', data=json.dumps({
            'name': 'Test Manual Equipment Valid',
            'spec': 'fury',
            'player_config_mode': 'manual_equipment',
            'player_equipment': 'talents=TEST\nhead=,id=212048',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())

    def test_attribute_only_profile_requires_talent(self):
        """Requirement 2: attribute_only mode must validate talent non-empty."""
        response = self.client.post('/api/simc-profile/', data=json.dumps({
            'name': 'Test Attribute Only Missing Talent',
            'spec': 'fury',
            'player_config_mode': 'attribute_only',
            'talent': '',
            'gear_crit': 1000,
            'gear_haste': 2000,
            'gear_mastery': 3000,
            'gear_versatility': 4000,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('talent', response.json()['error'].lower())

        # Valid attribute_only profile
        response = self.client.post('/api/simc-profile/', data=json.dumps({
            'name': 'Test Attribute Only Valid',
            'spec': 'fury',
            'player_config_mode': 'attribute_only',
            'talent': 'ATTRIBUTE_BUILD',
            'gear_crit': 1000,
            'gear_haste': 2000,
            'gear_mastery': 3000,
            'gear_versatility': 4000,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())

    def test_profile_update_validates_same_rules(self):
        """Requirement 2: Update must apply same validation as create."""
        # Create a valid profile
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Original Profile',
            spec='fury',
            player_config_mode='battlenet',
            battlenet_region='eu',
            battlenet_realm='Kazzak',
            battlenet_character='TestChar',
        )

        # Try to update to invalid battlenet
        response = self.client.put('/api/simc-profile/', data=json.dumps({
            'id': profile.id,
            'name': 'Updated Profile',
            'spec': 'fury',
            'player_config_mode': 'battlenet',
            'battlenet_region': 'invalid',
            'battlenet_realm': 'Kazzak',
            'battlenet_character': 'TestChar',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])

        # Try to update to invalid manual_equipment
        response = self.client.put('/api/simc-profile/', data=json.dumps({
            'id': profile.id,
            'name': 'Updated Profile',
            'spec': 'fury',
            'player_config_mode': 'manual_equipment',
            'player_equipment': '',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])

    def test_attribute_profile_simulate_now_creates_frozen_attribute_task(self):
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Runnable Attribute Profile',
            spec='fury',
            player_config_mode='attribute_only',
            talent='SIMULATE_BUILD',
            gear_strength=0,
            gear_crit=1000,
            gear_haste=2000,
            gear_mastery=3000,
            gear_versatility=4000,
        )
        response = self.client.post('/api/simc-profile/', data=json.dumps({
            'simulate_now': True,
            'profile_id': profile.id,
            'task_type': 2,
            'selected_attributes': 'crit_haste',
            'attribute_step': 50,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['task_id'], simc_profile_id=profile.id, user_id=self.user.id)
        ext = json.loads(task.ext)
        self.assertEqual(task.task_type, 2)
        self.assertEqual(task.result_file, '')
        self.assertEqual(ext['player_config_mode'], 'attribute_only')
        self.assertEqual(ext['talent'], 'SIMULATE_BUILD')
        self.assertEqual(ext['gear_strength'], 0)
        self.assertEqual(ext['selected_attributes'], 'crit_haste')
        self.assertEqual(ext['attribute_step'], 50)
