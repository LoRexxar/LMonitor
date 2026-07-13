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
            'player_equipment': 'warrior="Valid"\nlevel=90\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222',
            'talent': 'ATTRIBUTE_BUILD',
            'gear_crit': 1000,
            'gear_haste': 2000,
            'gear_mastery': 3000,
            'gear_versatility': 4000,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())

    def test_attribute_profile_create_and_update_reject_malformed_frozen_baseline(self):
        malformed = (
            'not simc',
            'warrior="No gear"\nspec=fury',
            'warrior="Incomplete"\nlevel=90\nspec=fury\nhead=,id=212048',
            'warrior="Unsafe"\nlevel=90\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222\narmory=us,realm,other',
            'warrior="Duplicate"\nlevel=90\nspec=fury\nhead=,id=212048\nhead=,id=299001\nmain_hand=,id=222222',
            'warrior="Alias duplicate"\nlevel=90\nspec=fury\nshoulder=,id=212048\nshoulders=,id=299001\nmain_hand=,id=222222',
        )
        for index, baseline in enumerate(malformed):
            response = self.client.post('/api/simc-profile/', data=json.dumps({
                'name': f'Bad baseline {index}', 'spec': 'fury',
                'player_config_mode': 'attribute_only',
                'player_equipment': baseline, 'talent': 'BUILD',
            }), content_type='application/json')
            self.assertFalse(response.json()['success'], response.json())
            self.assertIn('基线', response.json()['error'])

        profile = SimcProfile.objects.create(
            user_id=self.user.id, name='Historical empty', spec='fury',
            player_config_mode='attribute_only', talent='OLD', player_equipment='',
        )
        response = self.client.put('/api/simc-profile/', data=json.dumps({
            'id': profile.id, 'name': profile.name, 'talent': 'NEW',
        }), content_type='application/json')
        self.assertFalse(response.json()['success'], response.json())
        profile.refresh_from_db()
        self.assertEqual(profile.talent, 'OLD')

    def test_attribute_profile_accepts_plural_simc_slot_aliases(self):
        response = self.client.post('/api/simc-profile/', data=json.dumps({
            'name': 'Plural slots', 'spec': 'fury',
            'player_config_mode': 'attribute_only', 'talent': 'BUILD',
            'player_equipment': (
                'warrior="Plural"\nlevel=90\nspec=fury\n'
                'shoulders=,id=212048\nwrists=,id=212049\nmain_hand=,id=222222'
            ),
        }), content_type='application/json')
        self.assertTrue(response.json()['success'], response.json())

    def test_battlenet_mode_clears_stale_manual_player_block(self):
        response = self.client.post('/api/simc-profile/', data=json.dumps({
            'name': 'Battlenet canonical', 'spec': 'fury',
            'player_config_mode': 'battlenet',
            'battlenet_region': 'eu', 'battlenet_realm': 'Kazzak', 'battlenet_character': 'Tester',
            'player_equipment': 'warrior="Stale"\nlevel=90\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222',
        }), content_type='application/json')
        self.assertTrue(response.json()['success'], response.json())
        profile = SimcProfile.objects.get(name='Battlenet canonical')
        self.assertEqual(profile.player_config_mode, 'battlenet')
        self.assertEqual(profile.player_equipment, '')

        profile.player_config_mode = 'manual_equipment'
        profile.player_equipment = 'warrior="Stale"\nlevel=90\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222'
        profile.battlenet_region = profile.battlenet_realm = profile.battlenet_character = ''
        profile.save()
        response = self.client.put('/api/simc-profile/', data=json.dumps({
            'id': profile.id, 'name': profile.name, 'player_config_mode': 'battlenet',
            'battlenet_region': 'eu', 'battlenet_realm': 'Kazzak', 'battlenet_character': 'Tester',
        }), content_type='application/json')
        self.assertTrue(response.json()['success'], response.json())
        profile.refresh_from_db()
        self.assertEqual(profile.player_config_mode, 'battlenet')
        self.assertEqual(profile.player_equipment, '')

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

    def test_profile_edit_get_returns_all_form_fields(self):
        """The workbench edit form must receive every value it assigns, including strength."""
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Editable profile',
            spec='fury',
            player_config_mode='attribute_only',
            talent='EDIT_BUILD',
            gear_strength=123,
            gear_crit=456,
            gear_haste=789,
            gear_mastery=1011,
            gear_versatility=1213,
        )
        response = self.client.get(f'/api/simc-profile/{profile.id}/')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['gear_strength'], 123)

    def test_attribute_profile_simulate_now_creates_frozen_attribute_task(self):
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Runnable Attribute Profile',
            spec='fury',
            player_config_mode='attribute_only',
            player_equipment='warrior="Runnable"\nlevel=90\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222',
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

    def test_historical_empty_attribute_profile_cannot_simulate_now_or_create_task(self):
        profile = SimcProfile.objects.create(
            user_id=self.user.id, name='Historical empty task', spec='fury',
            player_config_mode='attribute_only', talent='BUILD', player_equipment='',
        )
        response = self.client.post('/api/simc-profile/', data=json.dumps({
            'simulate_now': True, 'profile_id': profile.id,
        }), content_type='application/json')
        self.assertFalse(response.json()['success'], response.json())
        self.assertFalse(SimcTask.objects.exists())
