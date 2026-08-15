"""Regression contracts for reference-based SimC task inputs.

Tasks reference three persisted resources and immutable SimcResourceVersion rows. They do
not freeze resource bodies in ``ext`` and do not auto-select an APL.
"""
import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.management.commands.import_simc_apl import Command as ImportSimcAplCommand
from botend.management.commands.update_simc_binary import Command as UpdateSimcBinaryCommand
from botend.models import SimcApl, SimcContentTemplate, SimcProfile, SimcTalentString, SimcTask
from botend.services.simc_task_service import initialize_task_runs


BASE_CONTENT = (
    '{simulation_options}\n{player_identity}\n{equipment}\n{talents}\n'
    '{stat_overrides}\n{action_list}\n{output_options}'
)
PLAYER_CONTENT = 'warrior="Player"\nlevel=90\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222'
APL_CONTENT = 'actions=/auto_attack\nactions+=/bloodthirst'


class UpdateSimcBinarySyncContractTests(TestCase):
    def test_import_normalizes_legacy_fury_hero_tree_dispatch(self):
        legacy_apl = '\n'.join([
            'actions+=/run_action_list,name=slayer,if=talent.slayers_dominance&active_enemies=1',
            'actions+=/run_action_list,name=slayer_aoe,if=talent.slayers_dominance&active_enemies>1',
            'actions+=/run_action_list,name=thane,if=talent.lightning_strikes&active_enemies=1',
            'actions+=/run_action_list,name=thane_aoe,if=talent.lightning_strikes&active_enemies>1',
        ])
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, 'warrior_fury.simc').write_text(legacy_apl, encoding='utf-8')
            command = ImportSimcAplCommand()
            command.stdout = SimpleNamespace(write=lambda value: None)

            self.assertEqual(command._process_file(tmpdir, 'warrior_fury.simc', False), 'ok')

        content = SimcApl.objects.get(
            source='simc_upstream', spec='warrior_fury', is_system=True,
        ).content
        self.assertIn('name=slayer,if=hero_tree.slayer&active_enemies=1', content)
        self.assertIn('name=slayer_aoe,if=hero_tree.slayer&active_enemies>1', content)
        self.assertIn('name=thane,if=hero_tree.mountain_thane&active_enemies=1', content)
        self.assertIn('name=thane_aoe,if=hero_tree.mountain_thane&active_enemies>1', content)
        self.assertNotIn('talent.slayers_dominance', content)
        self.assertNotIn('talent.lightning_strikes', content)

    def test_sync_generated_inputs_calls_base_template_then_player_then_apl(self):
        command = UpdateSimcBinaryCommand()
        command.simc_source_dir = '/srv/simc'
        command.stdout = SimpleNamespace(write=lambda x: None)
        command.row = SimpleNamespace(current_version='a' * 40, save=lambda **kwargs: None)
        git_hash = 'a' * 40

        with patch.object(command, '_get_git_hash', return_value=git_hash), \
             patch.object(command, '_set_status'), \
             patch.object(command, '_sync_default_template') as sync_template, \
             patch.object(command, '_publish_system_apl_corpus') as publish_corpus, \
             patch.object(command, '_export_runtime_manifest', return_value='/tmp/test-runtime-manifest.json'), \
             patch('botend.management.commands.update_simc_binary.call_command') as call_cmd:
            command._sync_generated_inputs(wow_build_override='12.0.1.70000')

        sync_template.assert_called_once()
        player_calls = [call for call in call_cmd.call_args_list if call[0][0] == 'import_simc_player_templates']
        self.assertEqual(len(player_calls), 1)
        self.assertEqual(player_calls[0][1]['sync_version'], git_hash)
        self.assertEqual(player_calls[0][1]['source_dir'], '/srv/simc/profiles/MID1')
        apl_calls = [call for call in call_cmd.call_args_list if call[0][0] == 'import_simc_apl']
        self.assertEqual(len(apl_calls), 1)
        self.assertEqual(apl_calls[0][1]['sync_version'], git_hash)
        symbol_calls = [call for call in call_cmd.call_args_list if call[0][0] == 'sync_simc_apl_symbols']
        self.assertEqual(symbol_calls[0][1]['runtime_manifest'], '/tmp/test-runtime-manifest.json')
        publish_corpus.assert_called_once_with(git_hash, '12.0.1.70000', '/tmp/simc', git_hash)


@override_settings(SIMC_APL_CURRENT_IDENTITY=('a' * 40, '12.0.1.70000'))
class SimcTaskReferenceContracts(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='reference_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)
        # Temporary editor bodies must be saved as resources before creating a Task.
        self.template = SimcContentTemplate.objects.create(
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
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=hashlib.sha256(APL_CONTENT.encode()).hexdigest(),
            validation_revision='a' * 40, validation_game_build='12.0.1.70000',
        )
        self.profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Explicit profile name',
            spec='warrior_fury',
            player_config_mode='manual_equipment',
            player_equipment=PLAYER_CONTENT,
            talent='BUILD',
            is_active=True,
        )

    def payload(self, **overrides):
        payload = {
            'name': 'Reference task',
            'simc_profile_id': self.profile.id,
            'base_template_id': self.template.id,
            'selected_apl_id': self.apl.id,
        }
        payload.update(overrides)
        return payload

    def create_task(self, **overrides):
        valid = {
            'valid': True, 'content_hash': hashlib.sha256(APL_CONTENT.encode()).hexdigest(),
            'revision': 'a' * 40, 'game_build': '12.0.1.70000', 'diagnostics': [],
        }
        with patch('botend.services.simc_task_service.current_validation_identity', return_value=('a' * 40, '12.0.1.70000')), \
             patch('botend.services.simc_task_service.validate_apl_for_profile', return_value=valid), \
             patch('botend.dashboard.api.validate_apl_for_profile', return_value=valid):
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

    def test_selected_talent_string_is_filtered_frozen_and_composed(self):
        talent = SimcTalentString.objects.create(
            name='Fury raid build', spec='warrior_fury', talent='SELECTED_BUILD',
            owner_user_id=self.user.id,
        )
        SimcTalentString.objects.create(
            name='Arms build', spec='warrior_arms', talent='OTHER_BUILD',
            owner_user_id=self.user.id,
        )

        candidates = self.client.get(
            '/api/simc-talent-string-candidates/', {'spec': 'warrior_fury'},
        )
        self.assertEqual(candidates.status_code, 200)
        self.assertEqual(
            [row['id'] for row in candidates.json()['data']], [talent.id],
        )

        task = self.create_task(talent_string_id=talent.id)
        self.assertEqual(task.talent_string_id, talent.id)
        self.assertEqual(task.talent_version.resource_type, 'talent')
        self.assertEqual(task.talent_version.payload['talent'], 'SELECTED_BUILD')

        talent.talent = 'CHANGED_BUILD'
        talent.save(update_fields=['talent', 'modified_at'])
        run = initialize_task_runs(task)[0]
        captured = {}

        def compose(request):
            captured.update(request)
            return None, None, 'stop after capturing composer request'

        monitor = SimcMonitor(None, None)
        with patch('botend.controller.plugins.simc.SimcMonitor.SimcComposer.compose', side_effect=compose):
            self.assertFalse(monitor.process_reference_run(task, run))

        self.assertEqual(captured['talent'], 'SELECTED_BUILD')

    def test_normal_task_freezes_explicit_raid_buffs_and_preserves_missing_state(self):
        selected = self.create_task(raid_buffs=['arcane_intellect', 'battle_shout'])
        self.assertEqual(selected.simulation_params['raid_buffs'], ['arcane_intellect', 'battle_shout'])
        initialize_task_runs(selected)
        run = selected.simulation_runs.get()
        self.assertEqual(
            run.task.simulation_params['raid_buffs'],
            ['arcane_intellect', 'battle_shout'],
        )

        missing = self.create_task(name='Missing raid buffs')
        self.assertNotIn('raid_buffs', missing.simulation_params or {})

    def test_normal_task_freezes_class_buff_toggle_with_extra_buffs(self):
        task = self.create_task(
            use_class_raid_buff=True,
            raid_buffs=['bloodlust'],
        )
        self.assertIs(task.simulation_params['use_class_raid_buff'], True)
        self.assertEqual(task.simulation_params['raid_buffs'], ['bloodlust'])

    def test_power_infusion_extra_option_catalog_freezes_and_composes(self):
        catalog = self.client.get('/api/simc-extra-options/options/')
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(catalog.json()['data'][0]['value'], 'power_infusion')

        root = Path(__file__).resolve().parents[2]
        workflow = (root / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        frontend = (root / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        self.assertIn('id="simc-sim-extra-options"', workflow)
        self.assertIn("fetch('/api/simc-extra-options/options/')", frontend)
        self.assertIn('scenario.extra_options', frontend)

        task = self.create_task(extra_options=['power_infusion'])
        self.assertEqual(task.simulation_params['extra_options'], ['power_infusion'])
        run = initialize_task_runs(task)[0]
        captured = {}

        from botend.services.simc_composer import SimcComposer
        original_compose = SimcComposer.compose

        def compose(request):
            captured['extra_options'] = request.get('extra_options')
            content, manifest, error = original_compose(SimcComposer(task.user_id), request)
            captured['content'] = content
            return None, manifest, error or 'stop after capturing composed input'

        monitor = SimcMonitor(None, None)
        with patch('botend.controller.plugins.simc.SimcMonitor.SimcComposer.compose', side_effect=compose):
            self.assertFalse(monitor.process_reference_run(task, run))

        self.assertEqual(captured['extra_options'], ['power_infusion'])
        self.assertIn(
            'external_buffs.power_infusion=0/120/240', captured['content'],
        )
        self.assertLess(
            captured['content'].index('warrior="Player"'),
            captured['content'].index('external_buffs.power_infusion=0/120/240'),
        )
        self.assertNotIn('external_buffs.pool', captured['content'])

    def test_local_worker_passes_frozen_raid_buffs_to_composer(self):
        task = self.create_task(
            use_class_raid_buff=True,
            raid_buffs=['arcane_intellect', 'battle_shout'],
        )
        run = initialize_task_runs(task)[0]
        captured = {}

        def compose(request):
            captured.update(request)
            return None, None, 'stop after capturing composer request'

        monitor = SimcMonitor(None, None)
        with patch('botend.controller.plugins.simc.SimcMonitor.SimcComposer.compose', side_effect=compose):
            self.assertFalse(monitor.process_reference_run(task, run))

        self.assertEqual(
            captured.get('raid_buffs'),
            ['arcane_intellect', 'battle_shout'],
        )
        self.assertIs(captured.get('use_class_raid_buff'), True)

    def test_profile_overrides_are_frozen_and_replace_only_matching_player_fields(self):
        from botend.services.simc_composer import SimcComposer

        self.profile.player_equipment = '\n'.join([
            PLAYER_CONTENT,
            'flask=old_flask',
            'potion=old_potion',
            'food=keep_food',
            'temporary_enchant=main_hand:old_enchant/off_hand:keep_enchant',
            'class_talents=100:1/200:1',
            'spec_talents=300:1',
        ])
        self.profile.talent = ''
        self.profile.save(update_fields=['player_equipment', 'talent'])
        overrides = {
            'flask': 'new_flask',
            'potion': 'new_potion',
            'temporary_enchant': 'main_hand:new_enchant',
            'class_talents': '100:2/400:1',
        }
        task = self.create_task(profile_overrides=overrides)
        self.assertEqual(task.simulation_params['profile_overrides'], overrides)

        run = initialize_task_runs(task)[0]
        captured = {}

        original_compose = SimcComposer.compose

        def compose(request):
            content, manifest, error = original_compose(SimcComposer(task.user_id), request)
            captured['content'] = content
            return None, manifest, error or 'stop after capturing composed input'

        monitor = SimcMonitor(None, None)
        with patch('botend.controller.plugins.simc.SimcMonitor.SimcComposer.compose', side_effect=compose):
            self.assertFalse(monitor.process_reference_run(task, run))

        content = captured['content']
        self.assertIn('flask=new_flask', content)
        self.assertIn('potion=new_potion', content)
        self.assertIn('food=keep_food', content)
        self.assertIn('temporary_enchant=main_hand:new_enchant', content)
        self.assertIn('class_talents=100:2/400:1', content)
        self.assertIn('spec_talents=300:1', content)
        self.assertNotIn('old_flask', content)
        self.assertNotIn('old_potion', content)
        self.assertNotIn('old_enchant', content)

    def test_normal_task_rejects_raid_buff_option_injection(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps(self.payload(raid_buffs=['override.battle_shout=1\nactions=/cancel'])) ,
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])
        self.assertIn('raid_buffs', response.json()['error'])

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

    def test_task_requires_existing_profile_reference(self):
        payload = self.payload()
        payload.pop('simc_profile_id')
        response = self.client.post(
            '/api/simc-task/', data=json.dumps(payload), content_type='application/json'
        )
        self.assertFalse(response.json()['success'])
        self.assertIn('simc_profile_id', response.json()['error'])
        self.assertFalse(SimcTask.objects.exists())
        self.assertEqual(SimcProfile.objects.filter(user_id=self.user.id).count(), 1)
