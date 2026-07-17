"""
TDD tests for SimC API Reference-based Task Creation Contracts.

Tests POST /api/simc-task/ and SimcProfileAPIView simulate_now endpoints
for reference-based task creation with strict validation and transactional safety.

Run with: DJANGO_SETTINGS_MODULE=LMonitor.settings_test_sqlite python manage.py test botend.tests.test_simc_api_reference_contracts
"""
import json
from django.test import TestCase, RequestFactory
from django.contrib.auth.models import User
from botend.models import SimcTask, SimcProfile, SimcApl, SimcContentTemplate, SimcResourceVersion
from botend.dashboard.api import SimcTaskAPIView, SimcProfileAPIView


class SimcTaskAPIReferenceContractsTests(TestCase):
    """Test POST /api/simc-task/ reference-based task creation."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.other_user = User.objects.create_user(username='otheruser', password='otherpass')

        # Create resources
        self.profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name="Test Profile",
            spec="warrior_fury",
            player_config_mode="manual_equipment",
            player_equipment="warrior=\"Test\"\nlevel=80",
            talent="BQEAAAAAAAAAAAAAAAAAAAAAAAAAAAAg",
            is_active=True,
        )

        self.template = SimcContentTemplate.objects.create(
            name="Base Template",
            template_type="base_template",
            spec="warrior_fury",
            content="iterations=1000\ntarget_error=0.1",
            is_active=True,
            is_selectable=True,
        )

        self.apl = SimcApl.objects.create(
            name="Test APL",
            spec="warrior_fury",
            content="actions=/auto_attack",
            is_active=True,
            is_selectable=True,
            owner_user_id=self.user.id,
        )

    def test_api_rejects_raw_simc_code(self):
        """RED: API should reject raw_simc_code and require base_template_id + selected_apl_id."""
        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'raw_simc_code': 'warrior="Test"\nlevel=80',
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('不再支持直接 SimC 代码模式', data['error'])

    def test_put_rejects_legacy_task_instead_of_rebuilding_old_ext(self):
        task = SimcTask.objects.create(user_id=self.user.id, name='legacy', simc_profile_id=self.profile.id,
                                       task_type=1, ext=json.dumps({'raw_simc_code': 'warrior="old"'}))
        request = self.factory.put('/api/simc-task/', data=json.dumps({
            'id': task.id, 'name': 'should-not-update', 'simc_profile_id': self.profile.id,
            'task_type': 1, 'ext': task.ext,
        }), content_type='application/json')
        request.user = self.user
        data = json.loads(SimcTaskAPIView().put(request).content)
        self.assertFalse(data['success'])
        self.assertIn('旧版冻结任务', data['error'])
        task.refresh_from_db()
        self.assertEqual(task.name, 'legacy')

    def test_put_keeps_task_type_2_rejection_boundary(self):
        task = SimcTask.objects.create(
            user_id=self.user.id, name='legacy-attr', simc_profile_id=self.profile.id,
            task_type=2, ext='crit_haste',
        )
        request = self.factory.put('/api/simc-task/', data=json.dumps({
            'id': task.id, 'name': 'should-not-update', 'task_type': 2,
            'selected_attributes': 'crit_haste',
        }), content_type='application/json')
        request.user = self.user
        data = json.loads(SimcTaskAPIView().put(request).content)
        self.assertFalse(data['success'])
        self.assertIn('task_type=2', data['error'])

    def test_api_rejects_task_type_2(self):
        """RED: API should reject task_type=2 (legacy attribute optimization)."""
        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'task_type': 2,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('已停用', data['error'])

    def test_api_rejects_base_template_content(self):
        """RED: API should reject base_template_content temporary text."""
        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'base_template_content': 'iterations=1000',
                'selected_apl_id': self.apl.id,
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('不再支持 base_template_content', data['error'])

    def test_api_rejects_override_action_list(self):
        """RED: API should reject override_action_list temporary text."""
        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'base_template_id': self.template.id,
                'override_action_list': 'actions=/custom',
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('不再支持 override_action_list', data['error'])

    def test_api_requires_base_template_id(self):
        """RED: API should require base_template_id."""
        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'selected_apl_id': self.apl.id,
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('必须提供 base_template_id', data['error'])

    def test_api_requires_selected_apl_id(self):
        """RED: API should require selected_apl_id."""
        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'base_template_id': self.template.id,
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('必须提供 selected_apl_id', data['error'])

    def test_api_creates_task_with_complete_references(self):
        """RED: API should create task with profile/template/apl + version FKs."""
        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Test Task',
                'profile_name': 'New Test Profile',
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
                'spec': 'warrior_fury',
                'player_equipment': 'warrior="New"\nlevel=80',
                'talent': 'ABC',
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertTrue(data['success'])

        task = SimcTask.objects.get(pk=data['data']['id'])

        # Verify complete references
        self.assertIsNotNone(task.profile_id)
        self.assertIsNotNone(task.template_id)
        self.assertIsNotNone(task.apl_id)
        self.assertIsNotNone(task.profile_version_id)
        self.assertIsNotNone(task.template_version_id)
        self.assertIsNotNone(task.apl_version_id)

        # Verify live FKs match
        self.assertEqual(task.template_id, self.template.id)
        self.assertEqual(task.apl_id, self.apl.id)

    def test_api_does_not_call_composer_at_creation(self):
        """RED: API creation should NOT call SimcComposer.compose."""
        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'profile_name': 'Composer-free Profile',
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
                'spec': 'warrior_fury',
                'player_equipment': 'warrior="Test"\nlevel=80',
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertTrue(data['success'])

        task = SimcTask.objects.get(pk=data['data']['id'])

        # Task should not have frozen content attributes
        self.assertFalse(hasattr(task, 'final_simc_content'))
        self.assertFalse(hasattr(task, 'input_hash'))
        self.assertFalse(hasattr(task, 'fragment_manifest'))

    def test_api_rejects_cross_user_template(self):
        """RED: API should reject template owned by other user."""
        other_template = SimcContentTemplate.objects.create(
            name="Other Template",
            template_type="base_template",
            spec="warrior_fury",
            content="iterations=2000",
            is_active=True,
            is_selectable=True,
            owner_user_id=self.other_user.id,
        )

        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'profile_name': 'Cross-user Test Profile',
                'base_template_id': other_template.id,
                'selected_apl_id': self.apl.id,
                'spec': 'warrior_fury',
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('belongs to user', data['error'].lower())

    def test_api_rejects_inactive_template(self):
        """RED: API should reject is_active=False template."""
        self.template.is_active = False
        self.template.save()

        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'profile_name': 'Inactive-template Test Profile',
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
                'spec': 'warrior_fury',
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('not active', data['error'].lower())

    def test_api_rejects_unselectable_apl(self):
        """RED: API should reject is_selectable=False APL."""
        self.apl.is_selectable = False
        self.apl.save()

        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'profile_name': 'Unselectable-APL Test Profile',
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
                'spec': 'warrior_fury',
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('not selectable', data['error'].lower())

    def test_api_updates_existing_profile(self):
        """RED: API should update existing profile when simc_profile_id provided."""
        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'simc_profile_id': self.profile.id,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
                'spec': 'warrior_fury',
                'player_equipment': 'warrior="UPDATED"\nlevel=85',
                'talent': 'XYZ',
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertTrue(data['success'])

        # Verify profile was updated
        self.profile.refresh_from_db()
        self.assertIn('UPDATED', self.profile.player_equipment)
        self.assertEqual(self.profile.talent, 'XYZ')

    def test_api_preserves_profile_name_when_no_explicit_profile_name(self):
        """RED: API should preserve Profile.name when profile_name not explicitly provided."""
        original_name = self.profile.name

        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task Name Should Not Overwrite Profile Name',
                'simc_profile_id': self.profile.id,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
                'spec': 'warrior_fury',
                'player_equipment': 'warrior="Test"\nlevel=80',
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertTrue(data['success'])

        # Verify profile name was NOT changed
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.name, original_name)

    def test_api_updates_profile_name_when_explicit_profile_name(self):
        """RED: API should update Profile.name only when profile_name explicitly provided."""
        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'simc_profile_id': self.profile.id,
                'profile_name': 'New Profile Name',
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
                'spec': 'warrior_fury',
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertTrue(data['success'])

        # Verify profile name was updated
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.name, 'New Profile Name')

    def test_api_requires_profile_name_for_new_profile(self):
        """API must not silently reuse the task name as a new Profile name."""
        initial_profile_count = SimcProfile.objects.count()
        initial_version_count = SimcResourceVersion.objects.count()
        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
                'spec': 'warrior_fury',
                'player_equipment': 'warrior="Test"\nlevel=80',
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('profile_name', data['error'])
        self.assertEqual(SimcTask.objects.count(), 0)
        self.assertEqual(SimcProfile.objects.count(), initial_profile_count)
        self.assertEqual(SimcResourceVersion.objects.count(), initial_version_count)

    def test_api_transaction_rollback_on_resource_validation_failure(self):
        """RED: API should rollback profile update if resource validation fails."""
        original_equipment = self.profile.player_equipment

        # Try to update profile but with invalid (inactive) APL
        self.apl.is_active = False
        self.apl.save()

        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'simc_profile_id': self.profile.id,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
                'spec': 'warrior_fury',
                'player_equipment': 'warrior="SHOULD_ROLLBACK"\nlevel=80',
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])

        # Verify profile update was rolled back
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.player_equipment, original_equipment)
        self.assertNotIn('SHOULD_ROLLBACK', self.profile.player_equipment)

    def test_api_does_not_create_task_on_profile_update_failure(self):
        """RED: API should not create task if profile update fails."""
        initial_task_count = SimcTask.objects.count()

        # Try to update non-existent profile
        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'simc_profile_id': 99999,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
                'spec': 'warrior_fury',
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])

        # Verify no task was created
        self.assertEqual(SimcTask.objects.count(), initial_task_count)


class SimcProfileAPISimulateNowContractsTests(TestCase):
    """Test SimcProfileAPIView simulate_now endpoint for reference-based task creation."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username='testuser', password='testpass')

        self.profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name="Test Profile",
            spec="warrior_fury",
            player_config_mode="manual_equipment",
            player_equipment="warrior=\"Test\"\nlevel=80",
            is_active=True,
        )

        self.template = SimcContentTemplate.objects.create(
            name="Base Template",
            template_type="base_template",
            spec="warrior_fury",
            content="iterations=1000",
            is_active=True,
            is_selectable=True,
        )

        self.apl = SimcApl.objects.create(
            name="Test APL",
            spec="warrior_fury",
            content="actions=/auto",
            is_active=True,
            is_selectable=True,
            owner_user_id=self.user.id,
        )

    def test_simulate_now_requires_explicit_template_and_apl(self):
        """RED: simulate_now should require explicit base_template_id and selected_apl_id."""
        request = self.factory.post(
            '/api/simc-profile/',
            data=json.dumps({
                'simc_profile_id': self.profile.id,
                'simulate_now': True,
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcProfileAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('必须提供', data['error'])

    def test_simulate_now_creates_reference_task(self):
        """RED: simulate_now should create reference task with complete FKs."""
        request = self.factory.post(
            '/api/simc-profile/',
            data=json.dumps({
                'simc_profile_id': self.profile.id,
                'simulate_now': True,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcProfileAPIView().post(request)
        data = json.loads(response.content)

        self.assertTrue(data['success'])

        # Verify task was created with complete references
        self.assertIn('task_data', data)
        task = SimcTask.objects.get(pk=data['task_data']['id'])

        self.assertIsNotNone(task.profile_id)
        self.assertIsNotNone(task.template_id)
        self.assertIsNotNone(task.apl_id)
        self.assertIsNotNone(task.profile_version_id)
        self.assertIsNotNone(task.template_version_id)
        self.assertIsNotNone(task.apl_version_id)

    def test_simulate_now_uses_existing_profile_without_update(self):
        """RED: simulate_now should use existing profile as-is without updating it."""
        original_equipment = self.profile.player_equipment

        request = self.factory.post(
            '/api/simc-profile/',
            data=json.dumps({
                'simc_profile_id': self.profile.id,
                'simulate_now': True,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcProfileAPIView().post(request)
        data = json.loads(response.content)

        self.assertTrue(data['success'])

        # Verify profile was NOT updated
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.player_equipment, original_equipment)

        # Verify task references the original profile snapshot
        task = SimcTask.objects.get(pk=data['task_data']['id'])
        profile_version = SimcResourceVersion.objects.get(pk=task.profile_version_id)
        self.assertEqual(profile_version.payload['player_equipment'], original_equipment)

    def test_simulate_now_preserves_profile_name(self):
        """RED: simulate_now should use existing profile as-is."""
        original_name = self.profile.name

        request = self.factory.post(
            '/api/simc-profile/',
            data=json.dumps({
                'simc_profile_id': self.profile.id,
                'simulate_now': True,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcProfileAPIView().post(request)
        data = json.loads(response.content)

        self.assertTrue(data['success'])

        # Verify profile name was preserved
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.name, original_name)

    def test_simulate_now_rollback_on_resource_failure(self):
        """RED: simulate_now should fail gracefully if resource validation fails."""
        # Use inactive APL to trigger failure
        self.apl.is_active = False
        self.apl.save()

        request = self.factory.post(
            '/api/simc-profile/',
            data=json.dumps({
                'simc_profile_id': self.profile.id,
                'simulate_now': True,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcProfileAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('not active', data['error'].lower())

    def test_new_profile_simulate_now_rolls_back_profile_when_task_validation_fails(self):
        """Saving a new Profile and creating its Task is one atomic operation."""
        self.apl.is_active = False
        self.apl.save()
        initial_profile_count = SimcProfile.objects.count()

        request = self.factory.post(
            '/api/simc-profile/',
            data=json.dumps({
                'name': 'Atomic Profile',
                'simulate_now': True,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
                'spec': 'warrior_fury',
                'player_config_mode': 'manual_equipment',
                'player_equipment': 'warrior="Atomic"\nlevel=80',
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcProfileAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('not active', data['error'].lower())
        self.assertEqual(SimcProfile.objects.count(), initial_profile_count)
        self.assertEqual(SimcTask.objects.count(), 0)
