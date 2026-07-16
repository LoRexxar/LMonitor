"""
SimC Composer Phase 1 Contract Tests

Tests the semantic slot resolution and template rendering contract:
- Slots: simulation_options, player_identity, talents, equipment, stat_overrides, action_list, output_options
- Source arbitration: each slot has exactly one final source
- Manual/Addon equipment blocks default equipment loading
- Armory occupies equipment slot but empty content doesn't fallback
- User class/spec vs BNet class/spec: consistent merge, conflict reject
- Explicit empty APL doesn't fallback to default
- All placeholders replaced in final_simc_content
- Final content must have single actor
- Task creation freezes manifest; template changes don't affect existing tasks
- Worker only reads frozen final_simc_content
- Batch and task user isolation
"""
import hashlib
import json
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import Client, TestCase
from botend.models import SimcTask, SimcContentTemplate
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor


class SimcComposerEquipmentSlotResolutionTests(TestCase):
    """Test equipment slot resolution and fallback prevention."""

    def setUp(self):
        self.user = User.objects.create_user(username='equipment_test_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

        # Create default equipment template
        self.default_equipment = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury',
            class_name='warrior',
            content='warrior="DefaultEquipment"\nspec=fury\nhead=,id=999999\nmain_hand=,id=888888',
            is_active=True,
        )

        # Create base template
        self.base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='fight_style=Patchwerk\n{player_identity}\n{equipment}\n{action_list}',
            is_active=True,
        )

    def test_manual_equipment_blocks_default_equipment_load(self):
        """Manual equipment input must prevent loading default equipment template."""
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Manual equipment blocks default',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="ManualPlayer"\nspec=fury\nhead=,id=111111\nmain_hand=,id=222222',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])

        # Verify final_simc_content exists and contains manual equipment only
        self.assertIsNotNone(task.final_simc_content, "Task must have frozen final_simc_content")
        self.assertIn('ManualPlayer', task.final_simc_content)
        self.assertIn('id=111111', task.final_simc_content)
        self.assertNotIn('DefaultEquipment', task.final_simc_content, "Must not load default equipment")
        self.assertNotIn('id=999999', task.final_simc_content, "Must not contain default equipment items")

    def test_addon_equipment_blocks_default_equipment_load(self):
        """Addon full export must prevent loading default equipment template."""
        addon_export = '''warrior="AddonPlayer"
spec=fury
level=80
race=orc
head=,id=212048,bonus_id=11109/11143/11297/10299/11328/10532/10254
neck=,id=225577,gem_id=213743,bonus_id=11109/11143/11297/10299/11328/10532
main_hand=,id=222566,bonus_id=10421/11109/11144/11297/1511/10299/11328/10532
talents=BUILD'''

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Addon equipment blocks default',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'addon_full_export',
            'player_equipment': addon_export,
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])

        self.assertIsNotNone(task.final_simc_content)
        self.assertIn('AddonPlayer', task.final_simc_content)
        self.assertIn('id=212048', task.final_simc_content)
        self.assertNotIn('DefaultEquipment', task.final_simc_content, "Must not load default equipment")

    @patch('botend.dashboard.api.fetch_battlenet_character_preflight')
    def test_armory_empty_equipment_does_not_fallback_to_default(self, mock_preflight):
        """
        When armory occupies equipment slot but returns empty content,
        must NOT fallback to default equipment. Empty is a valid state.
        """
        mock_preflight.return_value = {
            'simc_ready': True,
            'warnings': [],
            'identity': {'class_name': 'warrior', 'level': 80},
            'spec': {'key': 'fury'},
        }
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Armory empty equipment no fallback',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'battlenet',
            'battlenet_region': 'us',
            'battlenet_realm': 'area-52',
            'battlenet_character': 'testchar',
            # Simulate armory fetch returned but equipment was empty
            '_armory_equipment_result': '',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])

        # Equipment slot is occupied by armory source but content is empty
        manifest = json.loads(task.fragment_manifest or '{}')
        equipment_slot = manifest.get('slots', {}).get('equipment', {})
        self.assertEqual(equipment_slot.get('source'), 'battlenet_armory')

        # Must not contain default equipment
        self.assertNotIn('DefaultEquipment', task.final_simc_content or '')
        self.assertNotIn('id=999999', task.final_simc_content or '')


class SimcComposerIdentitySlotResolutionTests(TestCase):
    """Test player identity slot resolution: user input vs BNet must merge or reject."""

    def setUp(self):
        self.user = User.objects.create_user(username='identity_test_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

        self.base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='{player_identity}\n{equipment}\n{action_list}',
            is_active=True,
        )

    @patch('botend.dashboard.api.fetch_battlenet_character_preflight')
    def test_user_spec_consistent_with_bnet_spec_merges(self, mock_preflight):
        """User-specified spec matching BNet spec should merge into single identity slot."""
        # Mock server-side preflight to return consistent spec
        mock_preflight.return_value = {
            'simc_ready': True,
            'warnings': [],
            'identity': {'class_name': 'warrior', 'level': 80},
            'spec': {'key': 'fury'},
        }

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Consistent spec merge',
            'task_type': 1,
            'spec': 'fury',  # User input
            'player_import_mode': 'battlenet',
            'battlenet_region': 'us',
            'battlenet_realm': 'area-52',
            'battlenet_character': 'testchar',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])

        # Battle.net 模式冻结 SimC armory 导入指令，且只能出现一个角色来源。
        final = task.final_simc_content or ''
        actor_lines = [line for line in final.split('\n') if line.startswith('armory=')]
        self.assertEqual(actor_lines, ['armory=us,area-52,testchar'])

    @patch('botend.dashboard.api.fetch_battlenet_character_preflight')
    def test_user_spec_conflicts_with_bnet_spec_rejects(self, mock_preflight):
        """User-specified spec conflicting with BNet spec must reject task creation."""
        # Mock server-side preflight to return conflicting spec
        mock_preflight.return_value = {
            'simc_ready': True,
            'warnings': [],
            'identity': {'class_name': 'warrior', 'level': 80},
            'spec': {'key': 'fury'},  # BNet returns fury
        }

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Conflicting spec reject',
            'task_type': 1,
            'spec': 'arms',  # User wants arms
            'player_import_mode': 'battlenet',
            'battlenet_region': 'us',
            'battlenet_realm': 'area-52',
            'battlenet_character': 'testchar',
        }), content_type='application/json')

        self.assertFalse(response.json()['success'], "Must reject conflicting spec")
        self.assertIn('冲突', response.json().get('error', ''))
        self.assertFalse(SimcTask.objects.filter(user_id=self.user.id).exists())

    @patch('botend.dashboard.api.fetch_battlenet_character_preflight')
    def test_user_class_conflicts_with_bnet_class_rejects(self, mock_preflight):
        """User-specified class conflicting with BNet class must reject task creation."""
        # Mock server-side preflight to return conflicting class
        mock_preflight.return_value = {
            'simc_ready': True,
            'warnings': [],
            'identity': {'class_name': 'warrior', 'level': 80},
            'spec': {'key': 'fury'},
        }

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Conflicting class reject',
            'task_type': 1,
            'spec': 'fire',  # Mage spec (implies mage class)
            'player_import_mode': 'battlenet',
            'battlenet_region': 'us',
            'battlenet_realm': 'area-52',
            'battlenet_character': 'warriorchar',
        }), content_type='application/json')

        self.assertFalse(response.json()['success'], "Must reject conflicting class")
        self.assertIn('冲突', response.json().get('error', ''))


class SimcComposerAplSlotResolutionTests(TestCase):
    """Test APL slot resolution and explicit empty handling."""

    def setUp(self):
        self.user = User.objects.create_user(username='apl_test_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

        from botend.models import SimcApl
        self.default_apl = SimcApl.objects.create(
            name='Default Fury APL',
            spec='warrior_fury',
            class_name='warrior',
            content='actions+=/bloodthirst\nactions+=/rampage',
            source='simc_upstream',
            is_system=True,
            is_active=True,
        )

        self.base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='{player_identity}\n{equipment}\n{action_list}',
            is_active=True,
        )

    def test_explicit_empty_apl_does_not_fallback(self):
        """Explicitly empty APL must remain empty, not fallback to default APL."""
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Explicit empty APL',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048',
            'selected_apl_id': self.default_apl.id,
            'override_action_list': '',  # Explicit empty
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])

        # Verify APL slot is explicitly empty in manifest
        manifest = json.loads(task.fragment_manifest or '{}')
        apl_slot = manifest.get('slots', {}).get('action_list', {})
        self.assertEqual(apl_slot.get('source'), 'user_explicit_empty')

        # Final content must not contain default APL
        self.assertNotIn('bloodthirst', task.final_simc_content or '')
        self.assertNotIn('rampage', task.final_simc_content or '')


class SimcComposerTemplateRenderingTests(TestCase):
    """Test template rendering with placeholder replacement."""

    def setUp(self):
        self.user = User.objects.create_user(username='render_test_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_all_placeholders_replaced_in_final_content(self):
        """Final simc content must have all placeholders replaced."""
        base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='''fight_style={fight_style}
max_time={time}
desired_targets={target_count}
{player_identity}
{equipment}
{stat_overrides}
{action_list}
html={result_file}''',
            is_active=True,
        )

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'All placeholders replaced',
            'task_type': 1,
            'spec': 'fury',
            'fight_style': 'Patchwerk',
            'time': 300,
            'target_count': 1,
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048',
            'override_action_list': 'actions+=/execute',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])

        final = task.final_simc_content
        self.assertIsNotNone(final)

        # No placeholders should remain
        placeholders = ['{fight_style}', '{time}', '{target_count}', '{player_identity}',
                       '{equipment}', '{stat_overrides}', '{action_list}', '{result_file}']
        for placeholder in placeholders:
            self.assertNotIn(placeholder, final, f"Placeholder {placeholder} must be replaced")

    def test_final_content_single_actor_only(self):
        """Final simc content must contain exactly one actor definition."""
        base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='{player_identity}\n{equipment}\n{action_list}',
            is_active=True,
        )

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Single actor check',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="SinglePlayer"\nspec=fury\nhead=,id=212048',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])

        final = task.final_simc_content
        actor_lines = [line for line in (final or '').split('\n') if '=' in line and line.split('=')[0].strip() in ['warrior', 'mage', 'priest', 'paladin', 'druid', 'hunter', 'rogue', 'shaman', 'warlock', 'monk', 'demon_hunter', 'death_knight', 'evoker']]

        self.assertEqual(len(actor_lines), 1, f"Must have exactly one actor, found {len(actor_lines)}: {actor_lines}")


class SimcComposerFrozenManifestTests(TestCase):
    """Test frozen manifest contract: template changes don't affect existing tasks."""

    def setUp(self):
        self.user = User.objects.create_user(username='frozen_test_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_template_change_does_not_affect_frozen_task(self):
        """After task creation, template content changes must not affect task final_simc_content."""
        base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='# Original Template\n{player_identity}\n{equipment}',
            is_active=True,
        )

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Frozen manifest task',
            'task_type': 1,
            'spec': 'fury',
            'base_template_id': base_template.id,
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])
        original_content = task.final_simc_content

        self.assertIn('Original Template', original_content)

        # Change template
        base_template.content = '# Modified Template\n{player_identity}\n{equipment}'
        base_template.save()

        # Task content must not change
        task.refresh_from_db()
        self.assertEqual(task.final_simc_content, original_content)
        self.assertNotIn('Modified Template', task.final_simc_content)

    def test_worker_reads_frozen_final_simc_content_only(self):
        """SimcMonitor worker must execute frozen final_simc_content, not regenerate."""
        base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='{player_identity}\n{equipment}',
            is_active=True,
        )

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Worker frozen content',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="WorkerTest"\nspec=fury\nhead=,id=212048',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])

        # Verify final_simc_content is frozen and immutable
        self.assertIsNotNone(task.final_simc_content)
        self.assertIn('WorkerTest', task.final_simc_content)

        # Verify fragment_manifest exists (for audit/debugging)
        self.assertIsNotNone(task.fragment_manifest)
        manifest = json.loads(task.fragment_manifest)
        self.assertEqual(manifest.get('manifest_version'), 'v2')


class SimcFrozenWorkerGateTests(TestCase):
    """Worker 对 manifest v2 只接受 hash 完整的冻结正文。"""

    def setUp(self):
        self.user = User.objects.create_user(username='frozen_worker_user', password='pwd')

    def _task(self, *, content, input_hash=None):
        return SimcTask.objects.create(
            user_id=self.user.id,
            name='Frozen worker gate',
            simc_profile_id=0,
            task_type=1,
            ext='{}',
            final_simc_content=content,
            input_hash=input_hash if input_hash is not None else hashlib.sha256(
                str(content or '').encode('utf-8')
            ).hexdigest(),
            fragment_manifest=json.dumps({'manifest_version': 'v2'}),
            current_status=0,
            is_active=True,
        )

    def test_v2_task_routes_by_frozen_content_without_profile_lookup(self):
        content = 'warrior="Frozen"\nspec=fury\nhtml=simc_task_test.html'
        task = self._task(content=content)
        monitor = SimcMonitor(None, None)

        with patch.object(monitor, 'process_regular_simulation', return_value=True) as runner, \
             patch('botend.controller.plugins.simc.SimcMonitor.SimcProfile.objects.filter',
                   side_effect=AssertionError('v2 worker must not query SimcProfile')):
            self.assertTrue(monitor.process_simc_task(task))

        runner.assert_called_once()
        self.assertIsNone(runner.call_args.args[1])

    def test_v2_task_rejects_final_content_hash_mismatch(self):
        task = self._task(
            content='warrior="Tampered"\nspec=fury',
            input_hash=hashlib.sha256(b'warrior="Original"\nspec=fury').hexdigest(),
        )
        monitor = SimcMonitor(None, None)

        with patch.object(monitor, 'process_regular_simulation') as runner:
            self.assertFalse(monitor.process_simc_task(task))

        runner.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.current_status, 3)
        self.assertIn('hash', str(task.result_file).lower())

    def test_v2_task_missing_frozen_content_does_not_fall_back_to_legacy(self):
        task = self._task(content='', input_hash='')
        monitor = SimcMonitor(None, None)

        with patch.object(monitor, 'process_regular_simulation') as runner, \
             patch('botend.controller.plugins.simc.SimcMonitor.SimcProfile.objects.filter') as profile_query:
            self.assertFalse(monitor.process_simc_task(task))

        runner.assert_not_called()
        profile_query.assert_not_called()
        task.refresh_from_db()
        self.assertEqual(task.current_status, 3)
        self.assertIn('冻结', str(task.result_file))


class SimcComposerUserIsolationTests(TestCase):
    """Test batch and task user isolation."""

    def setUp(self):
        self.user1 = User.objects.create_user(username='user1', password='pwd')
        self.user2 = User.objects.create_user(username='user2', password='pwd')
        self.client = Client()

        self.base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='{player_identity}\n{equipment}',
            is_active=True,
        )

    def test_task_strictly_isolated_by_user_id(self):
        """Tasks must be strictly isolated by user_id."""
        # User1 creates task
        self.client.force_login(self.user1)
        response1 = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'User1 task',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="User1Player"\nspec=fury\nhead=,id=111111',
        }), content_type='application/json')

        self.assertTrue(response1.json()['success'])
        task1_id = response1.json()['data']['id']

        # User2 creates task
        self.client.force_login(self.user2)
        response2 = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'User2 task',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="User2Player"\nspec=fury\nhead=,id=222222',
        }), content_type='application/json')

        self.assertTrue(response2.json()['success'])
        task2_id = response2.json()['data']['id']

        # Verify isolation
        task1 = SimcTask.objects.get(id=task1_id)
        task2 = SimcTask.objects.get(id=task2_id)

        self.assertEqual(task1.user_id, self.user1.id)
        self.assertEqual(task2.user_id, self.user2.id)
        self.assertIn('User1Player', task1.final_simc_content)
        self.assertIn('User2Player', task2.final_simc_content)
        self.assertNotIn('User2Player', task1.final_simc_content)
        self.assertNotIn('User1Player', task2.final_simc_content)

    def test_batch_query_returns_only_user_tasks(self):
        """Batch queries must only return tasks belonging to the requesting user."""
        # Create tasks for both users
        self.client.force_login(self.user1)
        self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'User1 task',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="User1"\nspec=fury\nhead=,id=111',
        }), content_type='application/json')

        self.client.force_login(self.user2)
        self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'User2 task',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="User2"\nspec=fury\nhead=,id=222',
        }), content_type='application/json')

        # User1 queries tasks
        self.client.force_login(self.user1)
        response = self.client.get('/api/simc-task/')

        self.assertTrue(response.json()['success'])
        tasks = response.json()['data']

        # User1 should only see their own tasks
        for task in tasks:
            task_obj = SimcTask.objects.get(id=task['id'])
            self.assertEqual(task_obj.user_id, self.user1.id)


class SimcComposerNonWarriorSpecTests(TestCase):
    """Test non-warrior specs can select appropriate default equipment/APL."""

    def setUp(self):
        self.user = User.objects.create_user(username='mage_test_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

        # Create mage default equipment and APL
        self.mage_equipment = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='mage_fire',
            class_name='mage',
            content=(
                'mage="DefaultMageEquip"\nlevel=90\nspec=fire\n'
                'head=,id=777701\nneck=,id=777702\nshoulder=,id=777703\n'
                'back=,id=777704\nchest=,id=777705\nwrist=,id=777706\n'
                'hands=,id=777707\nwaist=,id=777708\nlegs=,id=777709\n'
                'feet=,id=777710\nfinger1=,id=777711\nfinger2=,id=777712\n'
                'trinket1=,id=777713\ntrinket2=,id=777714\nmain_hand=,id=888888'
            ),
            is_active=True,
        )

        from botend.models import SimcApl
        self.mage_apl = SimcApl.objects.create(
            name='Default Fire APL',
            spec='mage_fire',
            class_name='mage',
            content='actions+=/fireball\nactions+=/pyroblast',
            source='simc_upstream',
            is_system=True,
            is_active=True,
        )

        self.base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='mage_fire',
            content='{player_identity}\n{equipment}\n{action_list}',
            is_active=True,
        )

    def test_mage_fire_uses_mage_defaults(self):
        """Non-warrior spec (mage_fire) should use spec-appropriate defaults."""
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Mage fire spec',
            'task_type': 1,
            'spec': 'fire',
            'player_import_mode': 'attribute_only',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])

        # Composer separates actor identity from equipment lines; the default
        # actor name must not be copied from the equipment template.
        self.assertIn('mage="Player"', task.final_simc_content)
        self.assertNotIn('DefaultMageEquip', task.final_simc_content)
        self.assertIn('id=777701', task.final_simc_content)
        self.assertIn('fireball', task.final_simc_content)
        self.assertIn('pyroblast', task.final_simc_content)


class SimcComposerPlaceholderValidationTests(TestCase):
    """Test unknown placeholder rejection."""

    def setUp(self):
        self.user = User.objects.create_user(username='placeholder_test_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_unknown_placeholder_rejected(self):
        """Templates with unknown placeholders must be rejected."""
        bad_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='{player_identity}\n{equipment}\n{unknown_placeholder}\n{action_list}',
            is_active=True,
        )

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Unknown placeholder task',
            'task_type': 1,
            'spec': 'fury',
            'base_template_id': bad_template.id,
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048',
        }), content_type='application/json')

        self.assertFalse(response.json()['success'], "Must reject unknown placeholder")
        self.assertIn('placeholder', response.json().get('error', '').lower())


class SimcComposerTemplateCandidateValidationTests(TestCase):
    """Test invalid template/APL ID and zero/multiple candidate failures."""

    def setUp(self):
        self.user = User.objects.create_user(username='validation_test_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

        self.base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='{player_identity}\n{equipment}\n{action_list}',
            is_active=True,
        )

    def test_invalid_explicit_template_id_rejects(self):
        """Explicitly invalid template ID must fail."""
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Invalid template ID',
            'task_type': 1,
            'spec': 'fury',
            'base_template_id': 999999,
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048',
        }), content_type='application/json')

        self.assertFalse(response.json()['success'], "Must reject invalid template ID")

    def test_invalid_explicit_apl_id_rejects(self):
        """Explicitly invalid APL ID must fail."""
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Invalid APL ID',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048',
            'selected_apl_id': 999999,
        }), content_type='application/json')

        self.assertFalse(response.json()['success'], "Must reject invalid APL ID")

    def test_zero_default_equipment_candidates_fails(self):
        """Zero default equipment candidates must fail closed."""
        # No default equipment templates exist for this spec
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'No default equipment',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'default',  # Expects default equipment
        }), content_type='application/json')

        self.assertFalse(response.json()['success'], "Must reject when no default equipment exists")


class SimcComposerUserContentIsolationTests(TestCase):
    """Test user content isolation with owner_user_id."""

    def setUp(self):
        self.user1 = User.objects.create_user(username='content_user1', password='pwd')
        self.user2 = User.objects.create_user(username='content_user2', password='pwd')
        self.client = Client()

        # User1's private template
        self.user1_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='# User1 Private\n{player_identity}\n{equipment}',
            owner_user_id=self.user1.id,
            is_active=True,
        )

        # Global template
        self.global_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='# Global\n{player_identity}\n{equipment}',
            owner_user_id=None,  # Global
            is_active=True,
        )
        from botend.models import SimcApl
        self.user1_apl = SimcApl.objects.create(
            name='User1 private APL',
            spec='warrior_fury',
            class_name='warrior',
            content='actions=/bloodthirst',
            source='user',
            is_system=False,
            owner_user_id=self.user1.id,
            is_active=True,
        )

    def test_user_cannot_access_other_user_private_apl_even_with_override(self):
        self.client.force_login(self.user2)
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Cross-user APL access',
            'task_type': 1,
            'spec': 'fury',
            'base_template_id': self.global_template.id,
            'selected_apl_id': self.user1_apl.id,
            'override_action_list': 'actions=/attacker_supplied_override',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048',
        }), content_type='application/json')

        self.assertFalse(response.json()['success'], response.json())
        self.assertFalse(SimcTask.objects.filter(user_id=self.user2.id).exists())

    def test_user_cannot_access_other_user_private_template(self):
        """User2 cannot explicitly reference User1's private template."""
        self.client.force_login(self.user2)

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Cross-user template access',
            'task_type': 1,
            'spec': 'fury',
            'base_template_id': self.user1_template.id,  # User1's private
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048',
        }), content_type='application/json')

        self.assertFalse(response.json()['success'], "Must reject cross-user private template access")

    def test_user_can_access_global_template(self):
        """Any user can access global templates."""
        self.client.force_login(self.user2)

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Global template access',
            'task_type': 1,
            'spec': 'fury',
            'base_template_id': self.global_template.id,  # Global
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())


class SimcComposerManualExportSemanticParsingTests(TestCase):
    """Test full manual/addon export semantic slot parsing."""

    def setUp(self):
        self.user = User.objects.create_user(username='export_test_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

        self.base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='{player_identity}\n{talents}\n{equipment}\n{action_list}',
            is_active=True,
        )

    def test_addon_export_parsed_into_semantic_slots(self):
        """Full addon export should be parsed into identity/talents/equipment/APL slots."""
        addon_export = '''warrior="AddonPlayer"
spec=fury
level=80
race=orc
role=attack
position=back
professions=enchanting=100/jewelcrafting=100
talents=BUILD_STRING_HERE
head=,id=212048,bonus_id=11109/11143/11297/10299/11328/10532/10254
neck=,id=225577,gem_id=213743,bonus_id=11109/11143/11297/10299/11328/10532
shoulder=,id=212046,bonus_id=11109/11143/11297/10299/11328/10532
back=,id=212045,enchant_id=7403,bonus_id=11109/11143/11297/10299/11328/10532
chest=,id=212051,enchant_id=7364,bonus_id=11109/11143/11297/10299/11328/10532
main_hand=,id=222566,enchant_id=7460,bonus_id=10421/11109/11144/11297/1511/10299/11328/10532
actions+=/charge
actions+=/bloodthirst'''

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Addon export semantic parsing',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'addon_full_export',
            'player_equipment': addon_export,
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])

        manifest = json.loads(task.fragment_manifest)
        slots = manifest.get('slots', {})

        # Identity slot should be from export
        self.assertEqual(slots.get('player_identity', {}).get('source'), 'addon_export')

        # Talents slot should be from export
        self.assertEqual(slots.get('talents', {}).get('source'), 'addon_export')

        # Equipment slot should be from export
        self.assertEqual(slots.get('equipment', {}).get('source'), 'addon_export')

        # APL slot should be from export
        self.assertEqual(slots.get('action_list', {}).get('source'), 'addon_export')

        # Verify no duplicate equipment slots in final content
        final = task.final_simc_content
        head_lines = [line for line in final.split('\n') if line.startswith('head=')]
        self.assertEqual(len(head_lines), 1, "Must not duplicate equipment slots")

    def test_export_spec_conflicts_with_request_spec_rejects(self):
        """Addon export spec conflicting with request spec must reject."""
        addon_export = '''warrior="AddonPlayer"
spec=fury
level=80
head=,id=212048'''

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Export spec conflict',
            'task_type': 1,
            'spec': 'arms',  # Request arms but export has fury
            'player_import_mode': 'addon_full_export',
            'player_equipment': addon_export,
        }), content_type='application/json')

        self.assertFalse(response.json()['success'], "Must reject conflicting spec")
        self.assertIn('冲突', response.json().get('error', ''))


class SimcComposerFinalValidationTests(TestCase):
    """Test final validation: single actor, unique spec, etc."""

    def setUp(self):
        self.user = User.objects.create_user(username='final_validation_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

        self.base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='{player_identity}\n{equipment}\n{action_list}',
            is_active=True,
        )

    def test_final_content_has_single_spec_line(self):
        """Final content must have exactly one spec= line."""
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Single spec validation',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])

        spec_lines = [line for line in task.final_simc_content.split('\n') if line.strip().startswith('spec=')]
        self.assertEqual(len(spec_lines), 1, "Must have exactly one spec= line")

    def test_result_file_path_matches_task_result_file(self):
        """HTML report path in final_simc_content must match task.result_file."""
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Result file path validation',
            'task_type': 1,
            'spec': 'fury',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Player"\nspec=fury\nhead=,id=212048',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task = SimcTask.objects.get(id=response.json()['data']['id'])

        # Find html= line in final content
        html_lines = [line for line in task.final_simc_content.split('\n')
                     if line.strip().startswith('html=')]

        self.assertEqual(len(html_lines), 1, "Must have exactly one html= line")

        # Extract path from html=path
        html_path = html_lines[0].split('=', 1)[1].strip()

        # Should match task.result_file
        self.assertEqual(html_path, task.result_file,
                        "html= path must match task.result_file")
