import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from botend.models import SimcApl, SimcContentTemplate, SimcProfile, SimcTask


DEFAULT_PLAYER = '''warrior="Default Fury"
level=90
race=orc
spec=fury
talents=DEFAULT_BUILD
head=,id=212048,ilevel=639
neck=,id=212049,ilevel=639
shoulders=,id=212050,ilevel=639
back=,id=212051,ilevel=639
chest=,id=212052,ilevel=639
wrists=,id=212053,ilevel=639
hands=,id=212054,ilevel=639
waist=,id=212055,ilevel=639
legs=,id=212056,ilevel=639
feet=,id=212057,ilevel=639
finger1=,id=212058,ilevel=639
finger2=,id=212059,ilevel=639
trinket1=,id=212060,ilevel=639
trinket2=,id=212061,ilevel=639
main_hand=,id=222222,ilevel=639
'''


class SimcHomeCreationResourceContractTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='home-flow', password='pwd')
        self.client.force_login(self.user)

    def _apl(self, name='Default', **overrides):
        values = {
            'name': name, 'spec': 'warrior_fury', 'class_name': 'warrior',
            'content': 'actions=/bloodthirst', 'source': SimcApl.SOURCE_SIMC_UPSTREAM,
            'is_system': True, 'owner_user_id': None, 'is_active': True, 'is_selectable': True,
        }
        values.update(overrides)
        return SimcApl.objects.create(**values)

    def _template(self, name='Base', **overrides):
        values = {
            'name': name, 'template_type': SimcContentTemplate.TYPE_BASE_TEMPLATE,
            'source': SimcContentTemplate.SOURCE_SIMC_UPSTREAM, 'spec': 'warrior_fury',
            'class_name': 'warrior', 'content': '{player_config}\n{apl}\n',
            'is_active': True, 'is_selectable': True, 'owner_user_id': None,
        }
        values.update(overrides)
        return SimcContentTemplate.objects.create(**values)

    def test_candidates_mark_only_the_unique_system_default_and_resolve_template(self):
        default = self._apl()
        personal = self._apl(
            name='Personal', source=SimcApl.SOURCE_USER, is_system=False,
            owner_user_id=self.user.id,
        )
        template = self._template()
        response = self.client.get('/api/simc-apl-candidates/?spec=fury&class_name=warrior')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['default_template_id'], template.id)
        by_id = {row['id']: row for row in payload['data']}
        self.assertTrue(by_id[default.id]['is_default'])
        self.assertFalse(by_id[personal.id]['is_default'])

    def test_candidates_fail_when_default_apl_is_missing(self):
        self._template()
        response = self.client.get('/api/simc-apl-candidates/?spec=fury&class_name=warrior')
        self.assertEqual(response.status_code, 409)
        self.assertIn('默认 APL', response.json()['error'])

    def test_candidates_fail_when_default_template_is_missing(self):
        self._apl()
        response = self.client.get('/api/simc-apl-candidates/?spec=fury&class_name=warrior')
        self.assertEqual(response.status_code, 409)
        self.assertIn('基础模板', response.json()['error'])

    def test_candidates_fail_when_multiple_matching_templates_exist(self):
        self._apl()
        self._template(spec='default')
        self._template(name='All', spec='all')
        response = self.client.get('/api/simc-apl-candidates/?spec=fury&class_name=warrior')
        self.assertEqual(response.status_code, 409)
        self.assertIn('多个', response.json()['error'])

    def _task_resources(self):
        template = self._template(content='{player_config}\n{apl}\n')
        apl = self._apl()
        SimcContentTemplate.objects.create(
            name='Default Fury player',
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', class_name='warrior', content=DEFAULT_PLAYER,
            is_active=True, is_selectable=False,
        )
        return template, apl

    def test_task_default_source_is_frozen_without_preexisting_profile(self):
        template, apl = self._task_resources()
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Default source task', 'spec': 'fury',
            'player_source': {'type': 'default'},
            'base_template_id': template.id, 'selected_apl_id': apl.id,
            'fight_style': 'Patchwerk', 'time': 300, 'target_count': 1,
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.select_related('profile', 'profile_version').get(id=response.json()['data']['id'])
        self.assertEqual(task.profile.player_config_mode, 'manual_equipment')
        self.assertEqual(task.profile.spec, 'fury')
        self.assertIn('warrior="Default Fury"', task.profile_version.payload['player_equipment'])
        self.assertEqual(SimcProfile.objects.filter(user_id=self.user.id).count(), 1)

    def test_task_addon_source_ignores_actions_and_freezes_player_block(self):
        template, apl = self._task_resources()
        addon = DEFAULT_PLAYER + '\nactions=/malicious_override\n'
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Addon source task', 'spec': 'fury',
            'player_source': {'type': 'simc_addon', 'simc_code': addon},
            'base_template_id': template.id, 'selected_apl_id': apl.id,
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.select_related('profile_version', 'apl_version').get(id=response.json()['data']['id'])
        self.assertNotIn('actions=', task.profile_version.payload['player_equipment'])
        self.assertEqual(task.apl_version.payload['content'], 'actions=/bloodthirst')

    def test_task_rejects_source_spec_conflict_without_persisting_profile(self):
        template, apl = self._task_resources()
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Conflicting source', 'spec': 'arms',
            'player_source': {'type': 'simc_addon', 'simc_code': DEFAULT_PLAYER},
            'base_template_id': template.id, 'selected_apl_id': apl.id,
        }), content_type='application/json')

        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn('专精', response.json()['error'])
        self.assertFalse(SimcProfile.objects.filter(user_id=self.user.id).exists())
        self.assertFalse(SimcTask.objects.exists())

    def test_task_battlenet_source_is_frozen_without_preexisting_profile(self):
        template, apl = self._task_resources()
        preflight = {
            'simc_ready': True, 'warnings': [],
            'simc_config': {
                'player_config_mode': 'battlenet',
                'battlenet_region': 'eu', 'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr', 'spec': 'fury', 'talent': 'BN_BUILD',
                'gear_strength': 5000, 'gear_crit': 1000, 'gear_haste': 2000,
                'gear_mastery': 3000, 'gear_versatility': 4000,
            },
        }
        with patch('botend.dashboard.api.fetch_battlenet_character_preflight', return_value=preflight):
            response = self.client.post('/api/simc-task/', data=json.dumps({
                'name': 'Battle.net source task', 'spec': 'fury',
                'player_source': {'type': 'battlenet', 'region': 'eu', 'realm': 'Kazzak', 'character': 'Bloodmastêr'},
                'base_template_id': template.id, 'selected_apl_id': apl.id,
            }), content_type='application/json')

        self.assertEqual(response.status_code, 200, response.content)
        task = SimcTask.objects.select_related('profile_version').get(id=response.json()['data']['id'])
        self.assertEqual(task.profile_version.payload['battlenet_character'], 'Bloodmastêr')
        self.assertEqual(task.profile_version.payload['gear_crit'], 1000)

    def test_generic_base_template_is_valid_for_target_spec_task(self):
        template, apl = self._task_resources()
        template.spec = 'default'
        template.save(update_fields=['spec'])
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Generic base template task', 'spec': 'fury',
            'player_source': {'type': 'default'},
            'base_template_id': template.id, 'selected_apl_id': apl.id,
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['success'], response.json())
        self.assertTrue(SimcTask.objects.filter(id=response.json()['data']['id']).exists())

    def test_instant_profile_rolls_back_when_task_resource_validation_fails(self):
        self._task_resources()
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Atomic rollback', 'spec': 'fury',
            'player_source': {'type': 'default'},
            'base_template_id': 999999, 'selected_apl_id': 999999,
        }), content_type='application/json')

        self.assertFalse(response.json()['success'])
        self.assertFalse(SimcProfile.objects.filter(user_id=self.user.id).exists())
        self.assertFalse(SimcTask.objects.exists())
