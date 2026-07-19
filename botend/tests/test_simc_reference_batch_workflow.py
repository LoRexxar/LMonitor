"""Reference-based SimC Batch workflow contracts.

Each test is introduced RED-first. Batch candidates must remain reference tasks;
Task rows never store a composed/frozen SimC body.
"""
import json

from django.contrib.auth.models import User
from unittest.mock import patch

from django.test import Client, RequestFactory, TestCase

from botend.models import (
    SimcApl,
    SimcContentTemplate,
    SimcProfile,
    SimcTask,
    SimcTaskBatch,
)
from botend.services.simc_task_service import create_task
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.services.simc_composer import SimcComposer


class ReferenceBatchTaskCreationServiceTests(TestCase):
    def setUp(self):
        self.user_id = 2301
        self.profile = SimcProfile.objects.create(
            user_id=self.user_id,
            name='Batch Fury Profile',
            spec='fury',
            player_config_mode='manual_equipment',
            player_equipment=(
                'warrior="Batch"\nlevel=80\nspec=fury\ntalents=BASE\n'
                'head=,id=1001\nmain_hand=,id=2001'
            ),
            talent='BASE',
            gear_strength=5000,
            gear_crit=1000,
            gear_haste=2000,
            gear_mastery=3000,
            gear_versatility=4000,
            is_active=True,
        )
        self.template = SimcContentTemplate.objects.create(
            name='Batch Base Template',
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec='fury',
            content='{simulation_options}\n{player_config}\n{stat_overrides}\n{action_list}\n{output_options}',
            is_active=True,
            is_selectable=True,
        )
        self.apl = SimcApl.objects.create(
            name='Batch Fury APL',
            spec='fury',
            content='actions=/auto_attack',
            is_system=True,
            is_active=True,
            is_selectable=True,
        )
        self.batch = SimcTaskBatch.objects.create(
            user_id=self.user_id,
            name='Reference comparison',
            batch_type='gear_candidates',
        )

    def test_comparison_task_uses_complete_references_and_keeps_candidate_params(self):
        task = create_task(
            user_id=self.user_id,
            name='Reference comparison · helm',
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
            mode='comparison',
            simulation_params={
                'fight_style': 'Patchwerk',
                'max_time': 300,
                'desired_targets': 1,
            },
            mode_params={
                'candidate_type': 'gear_swap',
                'is_base': False,
                'batch_index': 1,
                'gear_swap': {
                    'slot': 'head',
                    'raw_value': ',id=299001,ilevel=650',
                    'item_id': 299001,
                    'source': 'bags',
                },
                'untrusted_extra': 'drop-me',
            },
            candidate_label='head #299001',
            batch_id=self.batch.id,
        )

        self.assertEqual(task.mode, 'comparison')
        self.assertEqual(task.batch_id, self.batch.id)
        self.assertEqual(task.profile_id, self.profile.id)
        self.assertEqual(task.template_id, self.template.id)
        self.assertEqual(task.apl_id, self.apl.id)
        self.assertIsNotNone(task.profile_version_id)
        self.assertIsNotNone(task.template_version_id)
        self.assertIsNotNone(task.apl_version_id)
        self.assertEqual(task.mode_params['candidate_type'], 'gear_swap')
        self.assertEqual(task.mode_params['gear_swap']['slot'], 'head')
        self.assertNotIn('untrusted_extra', task.mode_params)

    def test_attribute_sweep_task_keeps_only_candidate_ratings_and_metadata(self):
        task = create_task(
            user_id=self.user_id,
            name='Reference attributes · crit -50 / haste +50',
            profile_id=self.profile.id,
            template_id=self.template.id,
            apl_id=self.apl.id,
            mode='attribute_sweep',
            mode_params={
                'candidate_type': 'attribute_ratings',
                'is_base': False,
                'batch_index': 2,
                'attribute_ratings': {
                    'crit': 950,
                    'haste': 2050,
                    'mastery': 3000,
                    'versatility': 4000,
                },
                'search': {'round': 1, 'step': 50},
            },
            candidate_label='crit -50 / haste +50',
            batch_id=self.batch.id,
        )

        self.assertEqual(task.mode, 'attribute_sweep')
        self.assertEqual(task.mode_params['attribute_ratings']['crit'], 950)
        self.assertEqual(task.mode_params['search'], {'round': 1, 'step': 50})


class ReferenceBatchAPIViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='reference_batch_api', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)
        self.profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='API Batch Profile',
            spec='fury',
            player_config_mode='manual_equipment',
            player_equipment=(
                'warrior="Batcher"\nlevel=90\nspec=fury\ntalents=BASE\n'
                'head=,id=212048\nmain_hand=,id=222222\n'
                '### Gear from Bags\nhead=,id=299001'
            ),
            talent='BASE',
            is_active=True,
        )
        self.template = SimcContentTemplate.objects.create(
            name='API Batch Base Template',
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            spec='fury',
            content='{simulation_options}\n{player_config}\n{stat_overrides}\n{action_list}\n{output_options}',
            is_active=True,
            is_selectable=True,
        )
        self.apl = SimcApl.objects.create(
            name='API Batch Fury APL',
            spec='fury',
            content='actions=/auto_attack',
            is_system=True,
            is_active=True,
            is_selectable=True,
        )

    def test_gear_batch_api_creates_shared_reference_tasks(self):
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'gear_candidates',
            'name': 'Reference gear batch',
            'simc_profile_id': self.profile.id,
            'candidates': [{'slot': 'head', 'item_id': 299001, 'source': 'bags'}],
            'base_template_id': self.template.id,
            'selected_apl_id': self.apl.id,
        }), content_type='application/json')

        payload = response.json()
        self.assertTrue(payload['success'], payload)
        batch = SimcTaskBatch.objects.get(id=payload['data']['batch_id'])
        tasks = list(SimcTask.objects.filter(batch=batch).order_by('id'))
        self.assertEqual(len(tasks), 2)
        self.assertEqual({task.mode for task in tasks}, {'comparison'})
        self.assertEqual(len({task.profile_id for task in tasks}), 1)
        self.assertEqual(len({task.profile_version_id for task in tasks}), 1)
        self.assertEqual(len({task.template_version_id for task in tasks}), 1)
        self.assertEqual(len({task.apl_version_id for task in tasks}), 1)
        self.assertTrue(all(task.profile_id and task.template_id and task.apl_id for task in tasks))
        self.assertEqual(tasks[0].mode_params['candidate_type'], 'base')
        self.assertEqual(tasks[1].mode_params['candidate_type'], 'gear_swap')
        self.assertEqual(tasks[1].mode_params['gear_swap']['item_id'], 299001)
        self.profile.refresh_from_db()
        self.assertIn('### Gear from Bags', self.profile.player_equipment)
        frozen_player_block = tasks[0].profile_version.payload['player_equipment']
        self.assertNotIn('### Gear from Bags', frozen_player_block)
        self.assertNotIn('id=299001', frozen_player_block)
        self.assertIn('head=,id=212048', frozen_player_block)
        self.assertEqual(tasks[0].profile_version_id, tasks[1].profile_version_id)
        self.assertFalse(any(hasattr(task, 'final_simc_content') for task in tasks))

    def test_attribute_continuation_reuses_exact_resource_versions_without_ext(self):
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Attribute continuation profile',
            spec='fury',
            player_config_mode='manual_equipment',
            player_equipment=(
                'warrior="Batcher"\nlevel=90\nspec=fury\ntalents=BASE\n'
                'head=,id=212048\nmain_hand=,id=222222'
            ),
            talent='BASE',
            gear_strength=5000,
            gear_crit=1000,
            gear_haste=2000,
            gear_mastery=3000,
            gear_versatility=4000,
            is_active=True,
        )
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id,
            name='Attribute reference batch',
            batch_type='attribute_sweep',
            status=1,
        )
        api = __import__('botend.dashboard.api', fromlist=['SimcBatchTaskAPIView']).SimcBatchTaskAPIView()
        rows = api._attribute_variants(
            {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000},
            50,
        )
        source_tasks = []
        for index, (label, ratings, is_base, candidate) in enumerate(rows):
            task = create_task(
                user_id=self.user.id,
                name=f'Attribute reference batch · {label}',
                profile_id=profile.id,
                template_id=self.template.id,
                apl_id=self.apl.id,
                mode='attribute_sweep',
                simulation_params={
                    'fight_style': 'Patchwerk', 'max_time': 300, 'desired_targets': 1,
                },
                mode_params={
                    'candidate_type': 'attribute_ratings',
                    'is_base': is_base,
                    'batch_index': index,
                    'attribute_ratings': ratings,
                    'search': candidate,
                },
                candidate_label=label,
                batch_id=batch.id,
            )
            task.current_status = 2
            task.result_file = f'attribute_{task.id}.html'
            task.save(update_fields=['current_status', 'result_file'])
            source_tasks.append(task)

        source_profile_version_id = source_tasks[0].profile_version_id
        source_template_version_id = source_tasks[0].template_version_id
        source_apl_version_id = source_tasks[0].apl_version_id
        profile.player_equipment = profile.player_equipment.replace('id=212048', 'id=999999')
        profile.save(update_fields=['player_equipment'])
        self.template.content = '# changed after round one\n{player_config}\n{action_list}'
        self.template.save(update_fields=['content'])
        self.apl.content = 'actions=/changed_after_round_one'
        self.apl.save(update_fields=['content'])

        request = RequestFactory().post('/api/simc-task/batch/', data='{}', content_type='application/json')
        request.user = self.user
        dps_values = iter([100000, 101500] + [100100] * (len(source_tasks) - 2))
        with patch('botend.dashboard.api.SimcRegularCompareAPIView._get_result_file_content', return_value='<html></html>'), \
                patch('botend.dashboard.api.SimcRegularCompareAPIView._parse_regular_result', side_effect=lambda _html: {'dps': next(dps_values)}):
            result = api._continue_attribute_search(request, {}, str(batch.id))

        next_tasks = list(SimcTask.objects.filter(id__in=result['task_ids']).order_by('id'))
        self.assertEqual(result['accepted'], len(rows))
        self.assertEqual({task.batch_id for task in next_tasks}, {batch.id})
        self.assertEqual({task.mode for task in next_tasks}, {'attribute_sweep'})
        self.assertEqual({task.profile_id for task in next_tasks}, {profile.id})
        self.assertEqual({task.profile_version_id for task in next_tasks}, {source_profile_version_id})
        self.assertEqual({task.template_version_id for task in next_tasks}, {source_template_version_id})
        self.assertEqual({task.apl_version_id for task in next_tasks}, {source_apl_version_id})
        self.assertEqual({task.mode_params['search']['round'] for task in next_tasks}, {2})
        self.assertTrue(all(task.ext in (None, '') for task in next_tasks))

        # Only the highest round gates continuation. A historical failed row must
        # neither poison the current round nor enter its DPS recommendation.
        source_tasks[0].current_status = 3
        source_tasks[0].save(update_fields=['current_status'])
        for task in next_tasks:
            task.current_status = 2
            task.result_file = f'attribute_{task.id}.html'
            task.save(update_fields=['current_status', 'result_file'])
        round_two_dps = iter([100000, 101500] + [100100] * (len(next_tasks) - 2))
        with patch('botend.dashboard.api.SimcRegularCompareAPIView._get_result_file_content', return_value='<html></html>'), \
                patch('botend.dashboard.api.SimcRegularCompareAPIView._parse_regular_result', side_effect=lambda _html: {'dps': next(round_two_dps)}):
            third = api._continue_attribute_search(request, {}, str(batch.id))
        self.assertEqual({
            task.mode_params['search']['round']
            for task in SimcTask.objects.filter(id__in=third['task_ids'])
        }, {3})

    def test_attribute_continuation_requires_success_parseable_dps_and_consistent_current_versions(self):
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Guarded continuation', batch_type='attribute_sweep', status=1,
        )
        api = __import__('botend.dashboard.api', fromlist=['SimcBatchTaskAPIView']).SimcBatchTaskAPIView()
        rows = api._attribute_variants(
            {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}, 50,
        )[:2]
        tasks = []
        for index, (label, ratings, is_base, candidate) in enumerate(rows):
            task = create_task(
                user_id=self.user.id, name=label, profile_id=self.profile.id,
                template_id=self.template.id, apl_id=self.apl.id,
                mode='attribute_sweep', batch_id=batch.id, candidate_label=label,
                mode_params={'candidate_type': 'attribute_ratings', 'is_base': is_base,
                             'batch_index': index, 'attribute_ratings': ratings, 'search': candidate},
            )
            task.current_status = 2
            task.result_file = f'attribute_{task.id}.html'
            task.save(update_fields=['current_status', 'result_file'])
            tasks.append(task)
        request = RequestFactory().post('/api/simc-task/batch/', data='{}', content_type='application/json')
        request.user = self.user

        tasks[1].current_status = 3
        tasks[1].save(update_fields=['current_status'])
        with self.assertRaisesRegex(ValueError, '全部成功'):
            api._continue_attribute_search(request, {}, str(batch.id))
        tasks[1].current_status = 2
        tasks[1].save(update_fields=['current_status'])

        with patch('botend.dashboard.api.SimcRegularCompareAPIView._get_result_file_content', return_value='<html></html>'), \
                patch('botend.dashboard.api.SimcRegularCompareAPIView._parse_regular_result', return_value={}):
            with self.assertRaisesRegex(ValueError, 'DPS'):
                api._continue_attribute_search(request, {}, str(batch.id))

        tasks[1].profile_version_id = tasks[0].template_version_id
        tasks[1].save(update_fields=['profile_version'])
        with self.assertRaisesRegex(ValueError, '资源版本不一致'):
            api._continue_attribute_search(request, {}, str(batch.id))

    def test_complete_reference_task_put_only_renames_and_cannot_reset_status_or_inputs(self):
        task = create_task(
            user_id=self.user.id, name='Immutable run', profile_id=self.profile.id,
            template_id=self.template.id, apl_id=self.apl.id,
            simulation_params={'iterations': 100},
        )
        task.current_status = 2
        task.save(update_fields=['current_status'])

        response = self.client.put('/api/simc-task/', data=json.dumps({
            'id': task.id, 'name': 'Renamed only', 'current_status': 0,
            'simc_profile_id': 0, 'task_type': 2, 'ext': 'tampered',
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        task.refresh_from_db()
        self.assertEqual(task.name, 'Renamed only')
        self.assertEqual(task.current_status, 2)
        self.assertEqual(task.profile_id, self.profile.id)
        self.assertEqual(task.simulation_params, {'iterations': 100})

    def test_complete_reference_task_post_rerun_creates_new_edited_execution(self):
        task = create_task(
            user_id=self.user.id, name='Immutable source', profile_id=self.profile.id,
            template_id=self.template.id, apl_id=self.apl.id,
            simulation_params={'iterations': 100},
        )
        task.current_status = 2
        task.save(update_fields=['current_status'])

        response = self.client.post('/api/simc-task/', data=json.dumps({
            'id': task.id, 'action': 'rerun', 'name': 'Edited rerun',
            'simulation_params': {'iterations': 200},
        }), content_type='application/json')

        payload = response.json()
        self.assertTrue(payload['success'], payload)
        rerun = SimcTask.objects.get(id=payload['data']['id'])
        self.assertNotEqual(rerun.id, task.id)
        self.assertEqual(rerun.source_task_id, task.id)
        self.assertEqual(rerun.simulation_params, {'iterations': 200})
        self.assertEqual(rerun.profile_version_id, task.profile_version_id)
        self.assertEqual(rerun.template_version_id, task.template_version_id)
        self.assertEqual(rerun.apl_version_id, task.apl_version_id)
        task.refresh_from_db()
        self.assertEqual(task.simulation_params, {'iterations': 100})

    def test_task_preview_uses_reference_versions_and_never_ext_body(self):
        """Reference task detail exposes component refs/params, not frozen manifest text."""
        from botend.services.simc_task_service import create_task
        from botend.dashboard.api import SimcTaskPreviewAPIView

        profile = SimcProfile.objects.create(
            user_id=self.user.id, name='Preview Profile', spec='warrior_fury',
            player_config_mode='manual_equipment', player_equipment='warrior="Preview"',
            is_active=True,
        )
        template = SimcContentTemplate.objects.create(
            name='Preview Template', template_type='base_template', spec='warrior_fury',
            content='iterations=100', is_active=True, is_selectable=True,
        )
        apl = SimcApl.objects.create(
            name='Preview APL', spec='warrior_fury', content='actions=/auto_attack',
            is_active=True, is_selectable=True, owner_user_id=self.user.id,
        )
        task = create_task(
            user_id=self.user.id, name='Reference preview', profile_id=profile.id,
            template_id=template.id, apl_id=apl.id, mode='normal',
            simulation_params={'iterations': 100}, mode_params={'candidate_type': 'base'},
        )
        request = RequestFactory().get(f'/api/simc-task/preview/?task_id={task.id}')
        request.user = self.user
        response = SimcTaskPreviewAPIView().get(request)
        payload = json.loads(response.content)
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['data']['profile_id'], profile.id)
        self.assertEqual(payload['data']['profile_version_id'], task.profile_version_id)
        self.assertEqual(payload['data']['template_version_id'], task.template_version_id)
        self.assertEqual(payload['data']['apl_version_id'], task.apl_version_id)
        self.assertEqual(payload['data']['simulation_params']['iterations'], 100)
        self.assertNotIn('content', payload['data'])

    def test_reference_task_rerun_accepts_component_overrides_without_mutating_source(self):
        """Task rerun is a new task with validated component/parameter overrides."""
        from botend.services.simc_task_service import create_task
        from botend.services.task_rerun import create_rerun
        profile = SimcProfile.objects.create(
            user_id=self.user.id, name='Rerun Profile', spec='warrior_fury',
            player_config_mode='manual_equipment', player_equipment='warrior="Rerun"', is_active=True,
        )
        template = SimcContentTemplate.objects.create(
            name='Rerun Template', template_type='base_template', spec='warrior_fury',
            content='iterations=100', is_active=True, is_selectable=True,
        )
        apl = SimcApl.objects.create(
            name='Rerun APL', spec='warrior_fury', content='actions=/auto_attack',
            is_active=True, is_selectable=True, owner_user_id=self.user.id,
        )
        source = create_task(
            user_id=self.user.id, name='Source', profile_id=profile.id,
            template_id=template.id, apl_id=apl.id, mode='normal',
            simulation_params={'iterations': 100}, mode_params={'candidate_type': 'base'},
        )
        source.current_status = 2
        source.save(update_fields=['current_status'])
        rerun = create_rerun(source.id, self.user.id, {
            'name': 'Edited rerun', 'simulation_params': {'iterations': 200},
            'mode_params': {'candidate_type': 'base', 'search': {'round': 2}},
        })
        source.refresh_from_db()
        self.assertNotEqual(rerun.id, source.id)
        self.assertEqual(rerun.profile_version_id, source.profile_version_id)
        self.assertEqual(rerun.template_version_id, source.template_version_id)
        self.assertEqual(rerun.apl_version_id, source.apl_version_id)
        self.assertEqual(rerun.simulation_params['iterations'], 200)
        self.assertEqual(source.simulation_params['iterations'], 100)
class ReferenceBatchWorkerOverrideTests(TestCase):
    def test_candidate_composition_preserves_addon_omnium_metadata(self):
        """Saved Loadouts share Addon's required omnium metadata with the active build."""
        baseline = {
            'player_import_mode': 'manual_equipment',
            'player_equipment': (
                'warrior="Batcher"\nlevel=90\nspec=fury\n'
                'talents=BASE\n'
                'omnium_talents=136817:1/136819:1/136822:1\n'
                'head=,id=212048\nmain_hand=,id=222222'
            ),
            'talent': 'BASE',
        }
        request = SimcMonitor.apply_candidate_overrides(
            baseline, {
                'candidate_type': 'talent_override',
                'talent_override': 'CANDIDATE',
            }
        )
        composer = SimcComposer(2301)
        parsed = composer._parse_player_export(request['player_equipment'])
        self.assertEqual(
            parsed['talents'],
            'talents=CANDIDATE\nomnium_talents=136817:1/136819:1/136822:1',
        )
        self.assertIn(
            'omnium_talents=136817:1/136819:1/136822:1',
            request['player_equipment'],
        )

    def test_worker_applies_candidate_differences_to_runtime_request_only(self):
        baseline = {
            'player_equipment': (
                'warrior="Batcher"\nspec=fury\ntalents=BASE\n'
                'head=,id=212048\nmain_hand=,id=222222'
            ),
            'talent': 'BASE',
            'gear_crit': 1000,
            'gear_haste': 2000,
            'gear_mastery': 3000,
            'gear_versatility': 4000,
        }

        gear_request = SimcMonitor.apply_candidate_overrides(baseline, {
            'candidate_type': 'gear_swap',
            'gear_swap': {'slot': 'head', 'raw_value': ',id=299001,ilevel=650'},
        })
        self.assertIn('head=,id=299001,ilevel=650', gear_request['player_equipment'])
        self.assertNotIn('head=,id=212048', gear_request['player_equipment'])

        talent_request = SimcMonitor.apply_candidate_overrides(baseline, {
            'candidate_type': 'talent_override',
            'talent_override': 'NEW_BUILD',
        })
        self.assertEqual(talent_request['talent'], 'NEW_BUILD')
        self.assertIn('talents=NEW_BUILD', talent_request['player_equipment'])
        self.assertNotIn('talents=BASE', talent_request['player_equipment'])

        attribute_request = SimcMonitor.apply_candidate_overrides(baseline, {
            'candidate_type': 'attribute_ratings',
            'attribute_ratings': {
                'crit': 950, 'haste': 2050, 'mastery': 3000, 'versatility': 4000,
            },
        })
        self.assertEqual(attribute_request['gear_crit'], 950)
        self.assertEqual(attribute_request['gear_haste'], 2050)
        self.assertEqual(baseline['gear_crit'], 1000)
