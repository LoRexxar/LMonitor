"""
TDD tests for SimC API Reference-based Task Creation Contracts.

Tests POST /api/simc-task/ and SimcProfileAPIView simulate_now endpoints
for reference-based task creation with strict validation and transactional safety.

Run with: DJANGO_SETTINGS_MODULE=LMonitor.settings_test_sqlite python manage.py test botend.tests.test_simc_api_reference_contracts
"""
import hashlib
import inspect
import json
from unittest.mock import Mock, patch
from django.test import TestCase, RequestFactory
from django.contrib.auth.models import User
from botend.models import (
    SimcTask,
    SimcProfile,
    SimcApl,
    SimcContentTemplate,
    SimcResourceVersion,
    SimcBackendBinary,
)
from botend.dashboard.api import (
    SimcAplCandidatesAPIView,
    SimcAttributeAnalysisAPIView,
    SimcComparisonTaskAPIView,
    SimcProfileAPIView,
    SimcResultProxyAPIView,
    SimcTaskAPIView,
    SimcTaskPreviewAPIView,
    SimcTaskReportPreviewAPIView,
    SimcWorkbenchAPIView,
)


def mark_apl_current(test_case, apl):
    """Keep API contract fixtures independent from the external SimC validator."""
    identity = ('test-revision', 'test-build')
    content_hash = hashlib.sha256(apl.content.encode('utf-8')).hexdigest()
    SimcApl.objects.filter(pk=apl.pk).update(
        validation_status=SimcApl.VALIDATION_VALID,
        validated_content_hash=content_hash,
        validation_revision=identity[0],
        validation_game_build=identity[1],
    )
    apl.refresh_from_db()
    identity_patcher = patch(
        'botend.services.simc_task_service.current_validation_identity', return_value=identity,
    )
    validation_patcher = patch(
        'botend.services.simc_task_service.validate_apl_for_profile',
        side_effect=lambda profile, selected_apl, **kwargs: {
            'valid': True,
            'content_hash': hashlib.sha256(selected_apl.content.encode('utf-8')).hexdigest(),
            'revision': identity[0],
            'game_build': identity[1],
        },
    )
    identity_patcher.start()
    validation_patcher.start()
    test_case.addCleanup(identity_patcher.stop)
    test_case.addCleanup(validation_patcher.stop)


class SimcComparisonLifecycleOwnershipTests(TestCase):
    def test_comparison_view_does_not_own_attribute_search_continuation(self):
        forbidden_methods = {
            '_continue_attribute_search',
            '_next_attribute_search_center',
            '_attribute_center_signature',
            '_attribute_search_stop_reason',
            '_attribute_search_history',
            '_parse_task_ext',
            '_parse_manifest_round',
            '_safe_error_summary',
            '_has_valid_html_results',
        }

        self.assertFalse(
            forbidden_methods.intersection(vars(SimcComparisonTaskAPIView)),
            '属性续轮和搜索算法必须只由带有效 lease 的后端 Service/Worker 持有',
        )


class SimcTaskAPIReferenceContractsTests(TestCase):
    """Test POST /api/simc-task/ reference-based task creation."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username='testuser', password='testpass')
        self.other_user = User.objects.create_user(username='otheruser', password='otherpass')
        self.backend = SimcBackendBinary.objects.create(
            identifier='test-backend', name='Test backend', is_active=True,
        )

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
        mark_apl_current(self, self.apl)

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
                                       backend=self.backend, task_type=1,
                                       ext=json.dumps({'raw_simc_code': 'warrior="old"'}))
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

    def test_put_rejects_legacy_attribute_task_without_reading_request_task_type(self):
        task = SimcTask.objects.create(
            user_id=self.user.id, name='legacy-attr', simc_profile_id=self.profile.id,
            backend=self.backend, task_type=2, ext='crit_haste',
        )
        request = self.factory.put('/api/simc-task/', data=json.dumps({
            'id': task.id, 'name': 'should-not-update', 'task_type': 2,
            'selected_attributes': 'crit_haste',
        }), content_type='application/json')
        request.user = self.user
        data = json.loads(SimcTaskAPIView().put(request).content)
        self.assertFalse(data['success'])
        self.assertIn('旧版冻结任务', data['error'])

    def test_api_rejects_obsolete_task_type_parameter(self):
        """New task creation is mode-based and must not interpret numeric task types."""
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
        self.assertIn('task_type', data['error'])

    def test_every_task_creating_route_rejects_obsolete_task_type(self):
        cases = [
            (
                SimcTaskAPIView().post,
                self.factory.post('/api/simc-task/', data=json.dumps({
                    'action': 'rerun', 'id': 999999, 'task_type': 1,
                }), content_type='application/json'),
                (),
            ),
            (
                SimcTaskAPIView().patch,
                self.factory.patch('/api/simc-task/', data=json.dumps({
                    'action': 'rerun', 'id': 999999, 'task_type': 1,
                }), content_type='application/json'),
                (),
            ),
            (
                SimcComparisonTaskAPIView().post,
                self.factory.post('/api/simc-comparison-task/', data=json.dumps({
                    'kind': 'talent', 'task_type': 1,
                }), content_type='application/json'),
                (),
            ),
            (
                SimcAplCandidatesAPIView().post,
                self.factory.post('/api/simc-apl-candidates/', data=json.dumps({
                    'task_type': 1,
                }), content_type='application/json'),
                (),
            ),
            (
                SimcWorkbenchAPIView().post,
                self.factory.post('/api/simc-workbench/tasks/999999/', data=json.dumps({
                    'action': 'rerun', 'task_type': 1,
                }), content_type='application/json'),
                ('tasks', 999999),
            ),
        ]
        before = SimcTask.objects.count()
        for handler, request, args in cases:
            with self.subTest(handler=handler.__qualname__):
                request.user = self.user
                response = handler(request, *args)
                data = json.loads(response.content)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(data['success'])
                self.assertIn('task_type', data['error'])
        self.assertEqual(SimcTask.objects.count(), before)

    def test_successful_rerun_routes_publish_mode_without_legacy_fields(self):
        from botend.services.simc_task_service import create_task

        source = create_task(
            user_id=self.user.id,
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
            name='Rerun source',
        )
        cases = [
            (
                SimcTaskAPIView().post,
                self.factory.post('/api/simc-task/', data=json.dumps({
                    'action': 'rerun', 'id': source.id,
                }), content_type='application/json'),
                (),
            ),
            (
                SimcTaskAPIView().patch,
                self.factory.patch('/api/simc-task/', data=json.dumps({
                    'action': 'rerun', 'id': source.id,
                }), content_type='application/json'),
                (),
            ),
            (
                SimcWorkbenchAPIView().post,
                self.factory.post(f'/api/simc-workbench/tasks/{source.id}/', data=json.dumps({
                    'action': 'rerun',
                }), content_type='application/json'),
                ('tasks', source.id),
            ),
        ]
        for handler, request, args in cases:
            with self.subTest(handler=handler.__qualname__):
                request.user = self.user
                response = handler(request, *args)
                data = json.loads(response.content)
                self.assertTrue(data['success'], data)
                self.assertEqual(data['data']['mode'], 'normal')
                self.assertNotIn('task_type', data['data'])
                self.assertNotIn('result_file', data['data'])

        candidate_request = self.factory.post('/api/simc-apl-candidates/', data=json.dumps({
            'profile_id': self.profile.id,
            'base_template_id': self.template.id,
            'selected_apl_id': self.apl.id,
            'candidate_count': 1,
        }), content_type='application/json')
        candidate_request.user = self.user
        candidate_view = SimcAplCandidatesAPIView()
        with patch.object(
            candidate_view,
            '_create_compare_preprocessing_task',
            return_value=(Mock(id=12345, mode='comparison'), []),
        ):
            candidate_response = candidate_view.post(candidate_request)
        candidate_data = json.loads(candidate_response.content)
        self.assertTrue(candidate_data['success'], candidate_data)
        self.assertEqual(candidate_data['data']['mode'], 'comparison')
        self.assertNotIn('task_type', candidate_data['data'])
        self.assertNotIn('result_file', candidate_data['data'])

    def test_result_proxy_does_not_authorize_reference_task_result_file(self):
        from botend.services.simc_task_service import create_task

        task = create_task(
            user_id=self.user.id,
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
            name='Reference report task',
        )
        task.result_file = 'reference-task-report.html'
        task.save(update_fields=['result_file', 'modified_time'])

        request = self.factory.get('/api/simc-result-proxy/', {'file': task.result_file})
        request.user = self.user
        response = SimcResultProxyAPIView().get(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('不存在或无权限', data['error'])

        preview_request = self.factory.get(f'/api/simc-task/{task.id}/report/')
        preview_request.user = self.user
        preview_response = SimcTaskReportPreviewAPIView().get(preview_request, task.id)
        preview_data = json.loads(preview_response.content)
        self.assertEqual(preview_response.status_code, 404)
        self.assertIn('Artifact', preview_data['error'])

        SimcTask.objects.filter(id=task.id).update(task_type=2, mode='normal')
        attribute_request = self.factory.get('/api/simc-attribute-analysis/', {'task_id': task.id})
        attribute_request.user = self.user
        attribute_data = json.loads(SimcAttributeAnalysisAPIView().get(attribute_request).content)
        self.assertFalse(attribute_data['success'])
        self.assertIn('不是属性模拟', attribute_data['error'])

    def test_simc_report_routes_allow_anonymous_read_access(self):
        task = SimcTask.objects.create(
            user_id=self.other_user.id,
            name='Public report task',
            simc_profile_id=self.profile.id,
            backend=self.backend,
            mode='normal',
            task_type=1,
            current_status=2,
            is_active=True,
        )

        page_urls = (
            f'/simc-result/?task_id={task.id}',
            f'/simc-compare/?task_id={task.id}',
            f'/simc-attribute-analysis/?task_id={task.id}',
        )
        for url in page_urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

        response = self.client.get(f'/api/simc-workbench/tasks/{task.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.assertEqual(response.json()['data']['id'], task.id)

        response = self.client.get('/api/simc-task/preview/', {'task_id': task.id})
        self.assertEqual(response.status_code, 200)

        response = self.client.get('/api/simc-task/comparison/', {'task_id': task.id})
        self.assertNotEqual(response.status_code, 302)

        response = self.client.post('/api/simc-task/comparison/', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 302)

        response = self.client.get('/api/simc-regular-compare/', {'task_ids': str(task.id)})
        self.assertNotEqual(response.status_code, 302)

        response = self.client.get('/api/simc-result-proxy/', {'file': 'missing.html'})
        self.assertNotEqual(response.status_code, 302)

        response = self.client.get('/api/simc-workbench/history/')
        self.assertEqual(response.status_code, 302)

    def test_task_api_source_does_not_read_task_type_from_new_requests(self):
        source = inspect.getsource(SimcTaskAPIView)
        self.assertNotIn("data.get('task_type'", source)

    def test_task_list_and_preview_responses_do_not_publish_task_type(self):
        task = SimcTask.objects.create(
            user_id=self.user.id, name='Visible task', simc_profile_id=self.profile.id,
            backend=self.backend, mode='normal', task_type=1,
        )
        list_request = self.factory.get('/api/simc-task/')
        list_request.user = self.user
        list_data = json.loads(SimcTaskAPIView().get(list_request).content)['data'][0]
        self.assertEqual(list_data['reference'], {})
        self.assertNotIn('task_type', list_data)

        preview_request = self.factory.get('/api/simc-task/preview/', {'task_id': task.id})
        preview_request.user = self.user
        preview_data = json.loads(SimcTaskPreviewAPIView().get(preview_request).content)['data']
        self.assertNotIn('task_type', preview_data)

    def test_workbench_task_row_publishes_mode_without_task_type(self):
        task = SimcTask.objects.create(
            user_id=self.user.id, name='Workbench task', simc_profile_id=self.profile.id,
            backend=self.backend, mode='comparison', task_type=1,
        )
        row = SimcWorkbenchAPIView._task_row(task)
        self.assertEqual(row['mode'], 'comparison')
        self.assertNotIn('task_type', row)

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
        """API creates a Task from three existing resource references."""
        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Test Task',
                'simc_profile_id': self.profile.id,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertTrue(data['success'], data)
        self.assertEqual(data['data']['mode'], 'normal')
        self.assertNotIn('task_type', data['data'])
        self.assertNotIn('result_file', data['data'])

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

        list_request = self.factory.get('/api/simc-task/')
        list_request.user = self.user
        list_row = json.loads(SimcTaskAPIView().get(list_request).content)['data'][0]
        self.assertNotIn('result_file', list_row)

        preview_request = self.factory.get('/api/simc-task/preview/', {'task_id': task.id})
        preview_request.user = self.user
        preview = json.loads(SimcTaskPreviewAPIView().get(preview_request).content)['data']
        self.assertNotIn('result_file', preview)

    def test_api_does_not_call_composer_at_creation(self):
        """RED: API creation should NOT call SimcComposer.compose."""
        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'simc_profile_id': self.profile.id,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
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

    def test_api_allows_cross_user_template_for_explicit_simulation(self):
        other_template = SimcContentTemplate.objects.create(
            name="Other Template",
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
                'simc_profile_id': self.profile.id,
                'base_template_id': other_template.id,
                'selected_apl_id': self.apl.id,
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertTrue(data['success'], data)

    def test_api_rejects_inactive_template(self):
        """RED: API should reject is_active=False template."""
        self.template.is_active = False
        self.template.save()

        request = self.factory.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Task',
                'simc_profile_id': self.profile.id,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
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
                'simc_profile_id': self.profile.id,
                'base_template_id': self.template.id,
                'selected_apl_id': self.apl.id,
            }),
            content_type='application/json',
        )
        request.user = self.user

        response = SimcTaskAPIView().post(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertIn('not selectable', data['error'].lower())

    def test_api_does_not_update_existing_profile(self):
        """Task creation selects an existing Profile and never mutates it."""
        original_equipment = self.profile.player_equipment
        original_talent = self.profile.talent
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

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.player_equipment, original_equipment)
        self.assertEqual(self.profile.talent, original_talent)

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

    def test_api_does_not_update_profile_name_when_profile_name_is_supplied(self):
        """Task payload cannot rename the referenced Profile."""
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

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.name, 'Test Profile')

    def test_api_requires_existing_profile_reference(self):
        """The run form cannot create a Profile implicitly."""
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
        self.assertIn('simc_profile_id', data['error'])
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

    def _reference_task(self, **overrides):
        versions = {}
        for resource_type, resource in (
            ('profile', self.profile), ('template', self.template), ('apl', self.apl),
        ):
            versions[f'{resource_type}_version'] = SimcResourceVersion.objects.create(
                resource_type=resource_type,
                resource_id=resource.id,
                content_hash=f'{resource_type}-{resource.id}',
                payload={'content': 'snapshot'},
            )
        values = {
            'user_id': self.user.id,
            'name': 'pending-task',
            'simc_profile_id': self.profile.id,
            'profile': self.profile,
            'template': self.template,
            'apl': self.apl,
            'backend': self.backend,
            'current_status': 0,
            **versions,
        }
        values.update(overrides)
        return SimcTask.objects.create(**values)

    def test_put_can_cancel_pending_task(self):
        task = self._reference_task()
        request = self.factory.put('/api/simc-task/', data=json.dumps({
            'id': task.id, 'name': task.name, 'current_status': 5,
        }), content_type='application/json')
        request.user = self.user

        response = SimcTaskAPIView().put(request)
        data = json.loads(response.content)

        self.assertTrue(data['success'])
        task.refresh_from_db()
        self.assertEqual(task.current_status, 5)

    def test_put_can_mark_pending_task_failed(self):
        task = self._reference_task()
        request = self.factory.put('/api/simc-task/', data=json.dumps({
            'id': task.id, 'name': task.name, 'current_status': 3,
        }), content_type='application/json')
        request.user = self.user

        response = SimcTaskAPIView().put(request)
        data = json.loads(response.content)

        self.assertTrue(data['success'])
        task.refresh_from_db()
        self.assertEqual(task.current_status, 3)

    def test_put_cannot_change_running_task_status(self):
        task = self._reference_task(current_status=1)
        request = self.factory.put('/api/simc-task/', data=json.dumps({
            'id': task.id, 'name': task.name, 'current_status': 5,
        }), content_type='application/json')
        request.user = self.user

        response = SimcTaskAPIView().put(request)
        data = json.loads(response.content)

        self.assertFalse(data['success'])
        self.assertEqual(response.status_code, 409)
        task.refresh_from_db()
        self.assertEqual(task.current_status, 1)

    def test_put_rejects_arbitrary_target_status(self):
        task = self._reference_task()
        request = self.factory.put('/api/simc-task/', data=json.dumps({
            'id': task.id, 'name': task.name, 'current_status': 2,
        }), content_type='application/json')
        request.user = self.user

        response = SimcTaskAPIView().put(request)

        self.assertEqual(response.status_code, 400)
        task.refresh_from_db()
        self.assertEqual(task.current_status, 0)

    def test_put_cannot_change_another_users_task_status(self):
        task = self._reference_task()
        other_user = User.objects.create_user(username='other-user', password='testpass')
        request = self.factory.put('/api/simc-task/', data=json.dumps({
            'id': task.id, 'name': task.name, 'current_status': 5,
        }), content_type='application/json')
        request.user = other_user

        response = SimcTaskAPIView().put(request)

        self.assertEqual(response.status_code, 404)
        task.refresh_from_db()
        self.assertEqual(task.current_status, 0)


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
        mark_apl_current(self, self.apl)

    def test_profile_api_rejects_obsolete_task_type_parameter(self):
        request = self.factory.post(
            '/api/simc-profile/',
            data=json.dumps({'simc_profile_id': self.profile.id, 'simulate_now': True, 'task_type': 1}),
            content_type='application/json',
        )
        request.user = self.user
        response = SimcProfileAPIView().post(request)
        data = json.loads(response.content)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(data['success'])
        self.assertIn('task_type', data['error'])

        before = SimcTask.objects.count()
        patch_request = self.factory.patch(
            f'/api/simc-profile/{self.profile.id}/simulate/',
            data=json.dumps({'task_type': 1}),
            content_type='application/json',
        )
        patch_request.user = self.user
        patch_response = SimcProfileAPIView().patch(patch_request, self.profile.id)
        patch_data = json.loads(patch_response.content)
        self.assertEqual(patch_response.status_code, 400)
        self.assertFalse(patch_data['success'])
        self.assertIn('task_type', patch_data['error'])
        self.assertEqual(SimcTask.objects.count(), before)

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
