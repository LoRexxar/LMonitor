"""Regression contracts for reference-based SimC task inputs.

Tasks reference three persisted resources and immutable SimcResourceVersion rows. They do
not freeze resource bodies in ``ext`` and do not auto-select an APL.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase

from botend.management.commands.update_simc_binary import Command as UpdateSimcBinaryCommand
from botend.models import SimcApl, SimcContentTemplate, SimcProfile, SimcTask


BASE_CONTENT = (
    '{simulation_options}\n{player_identity}\n{equipment}\n{talents}\n'
    '{stat_overrides}\n{action_list}\n{output_options}'
)
PLAYER_CONTENT = 'warrior="Player"\nlevel=90\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222'
APL_CONTENT = 'actions=/auto_attack\nactions+=/bloodthirst'


class UpdateSimcBinarySyncContractTests(TestCase):
    def test_sync_generated_inputs_calls_base_template_then_player_then_apl(self):
        command = UpdateSimcBinaryCommand()
        command.simc_source_dir = '/srv/simc'
        command.stdout = SimpleNamespace(write=lambda x: None)
        command.row = SimpleNamespace(save=lambda **kwargs: None)
        git_hash = 'abc123def'

        with patch.object(command, '_get_git_hash', return_value=git_hash), \
             patch.object(command, '_set_status'), \
             patch.object(command, '_sync_default_template') as sync_template, \
             patch('botend.management.commands.update_simc_binary.call_command') as call_cmd:
            command._sync_generated_inputs()

        sync_template.assert_called_once()
        player_calls = [call for call in call_cmd.call_args_list if call[0][0] == 'import_simc_player_templates']
        self.assertEqual(len(player_calls), 1)
        self.assertEqual(player_calls[0][1]['sync_version'], git_hash)
        self.assertEqual(player_calls[0][1]['source_dir'], '/srv/simc/profiles/MID1')
        self.assertEqual(len([call for call in call_cmd.call_args_list if call[0][0] == 'import_simc_apl']), 1)


class SimcTaskReferenceContracts(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='reference_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)
        # Temporary editor bodies must be saved as resources before creating a Task.
        self.template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            owner_user_id=self.user.id,
            spec='warrior_fury', name='Saved edited template', content=BASE_CONTENT,
            is_active=True, is_selectable=True,
        )
        self.apl = SimcApl.objects.create(
            source=SimcApl.SOURCE_USER,
            owner_user_id=self.user.id,
            spec='warrior_fury', name='Saved edited APL', content=APL_CONTENT,
            is_active=True, is_selectable=True,
        )

    def payload(self, **overrides):
        payload = {
            'name': 'Reference task',
            'profile_name': 'Explicit profile name',
            'task_type': 1,
            'base_template_id': self.template.id,
            'selected_apl_id': self.apl.id,
            'player_import_mode': 'manual_equipment',
            'player_equipment': PLAYER_CONTENT,
            'spec': 'warrior_fury',
            'talent': 'BUILD',
        }
        payload.update(overrides)
        return payload

    def create_task(self, **overrides):
        response = self.client.post(
            '/api/simc-task/', data=json.dumps(self.payload(**overrides)),
            content_type='application/json',
        )
        self.assertTrue(response.json()['success'], response.json())
        return SimcTask.objects.select_related(
            'profile', 'template', 'apl', 'profile_version', 'template_version', 'apl_version'
        ).get(id=response.json()['data']['id'])

    def test_task_stores_resource_fks_and_immutable_version_payloads(self):
        task = self.create_task()

        self.assertEqual(task.profile.name, 'Explicit profile name')
        self.assertEqual(task.template_id, self.template.id)
        self.assertEqual(task.apl_id, self.apl.id)
        self.assertEqual(task.profile_version.resource_id, task.profile_id)
        self.assertEqual(task.profile_version.resource_type, 'profile')
        self.assertEqual(task.template_version.resource_id, self.template.id)
        self.assertEqual(task.template_version.resource_type, 'template')
        self.assertEqual(task.apl_version.resource_id, self.apl.id)
        self.assertEqual(task.apl_version.resource_type, 'apl')
        self.assertEqual(task.profile_version.payload['player_equipment'], PLAYER_CONTENT)
        self.assertEqual(task.template_version.payload['content'], BASE_CONTENT)
        self.assertEqual(task.apl_version.payload['content'], APL_CONTENT)

        ext = json.loads(task.ext or '{}')
        self.assertNotIn('base_template_content', ext)
        self.assertNotIn('override_action_list', ext)
        self.assertNotIn('player_equipment', ext)

    def test_version_payloads_do_not_change_when_live_resources_change(self):
        task = self.create_task()
        version_ids = (task.profile_version_id, task.template_version_id, task.apl_version_id)

        task.profile.player_equipment = 'warrior="Changed"\nspec=fury'
        task.profile.save(update_fields=['player_equipment'])
        self.template.content = 'iterations=999999'
        self.template.save(update_fields=['content'])
        self.apl.content = 'actions=/whirlwind'
        self.apl.save(update_fields=['content'])

        task.refresh_from_db()
        self.assertEqual(
            (task.profile_version_id, task.template_version_id, task.apl_version_id), version_ids
        )
        self.assertEqual(task.profile_version.payload['player_equipment'], PLAYER_CONTENT)
        self.assertEqual(task.template_version.payload['content'], BASE_CONTENT)
        self.assertEqual(task.apl_version.payload['content'], APL_CONTENT)

    def test_temporary_template_or_apl_body_is_rejected(self):
        for field, body in (
            ('base_template_content', BASE_CONTENT + '\niterations=12345'),
            ('override_action_list', 'actions=/execute'),
        ):
            with self.subTest(field=field):
                response = self.client.post(
                    '/api/simc-task/', data=json.dumps(self.payload(**{field: body})),
                    content_type='application/json',
                )
                self.assertFalse(response.json()['success'])
                self.assertIn(field, response.json()['error'])
        self.assertFalse(SimcTask.objects.exists())

    def test_missing_explicit_apl_is_rejected_instead_of_auto_selecting(self):
        SimcApl.objects.create(
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='Another enabled APL', content='actions=/execute',
            is_system=True, is_active=True, is_selectable=True,
        )
        response = self.client.post(
            '/api/simc-task/', data=json.dumps(self.payload(selected_apl_id=None)),
            content_type='application/json',
        )
        self.assertFalse(response.json()['success'])
        self.assertIn('selected_apl_id', response.json()['error'])
        self.assertFalse(SimcTask.objects.exists())

    def test_new_profile_requires_explicit_profile_name(self):
        payload = self.payload()
        payload.pop('profile_name')
        response = self.client.post(
            '/api/simc-task/', data=json.dumps(payload), content_type='application/json'
        )
        self.assertFalse(response.json()['success'])
        self.assertIn('profile_name', response.json()['error'])
        self.assertFalse(SimcTask.objects.exists())
        self.assertFalse(SimcProfile.objects.filter(user_id=self.user.id).exists())
