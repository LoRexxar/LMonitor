import importlib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, RequestFactory, TestCase
from django.utils import timezone

from botend.dashboard.api import SimcAplCandidatesAPIView, SimcBatchTaskAPIView, SimcProfileAPIView, SimcRegularCompareAPIView, SimcTaskAPIView, SimcSpecOptionsAPIView, inspect_raw_simc_code
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.management.commands.update_simc_binary import Command as UpdateSimcBinaryCommand
from botend.services.simc_player_config import build_player_config_detail, parse_manual_player_config, parse_manual_simc_candidates
from botend.models import SimcApl, SimcContentTemplate, SimcProfile, SimcTask, SimcTaskBatch, WowItemSnapshot


class SimcWorkerBatchLifecycleTests(TestCase):
    def setUp(self):
        self.monitor = SimcMonitor(None, None)
        self.batch = SimcTaskBatch.objects.create(
            user_id=801,
            name='worker lifecycle',
            batch_type='comparison',
            status=0,
        )

    def _task(self, status=0, batch='default', ext=None):
        task_batch = self.batch if batch == 'default' else batch
        return SimcTask.objects.create(
            user_id=801,
            name=f'task-{status}',
            simc_profile_id=0,
            task_type=1,
            current_status=status,
            batch=task_batch,
            ext=json.dumps(ext or {}, ensure_ascii=False),
            is_active=True,
        )

    def test_batch_stays_running_until_all_fk_members_succeed(self):
        first = self._task(status=2)
        second = self._task(status=0)

        self.monitor.sync_batch_lifecycle(self.batch.id)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, 1)
        self.assertIsNone(self.batch.completed_at)

        second.current_status = 2
        second.save(update_fields=['current_status', 'modified_time'])
        self.monitor.sync_batch_lifecycle(self.batch.id)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, 2)
        self.assertIsNotNone(self.batch.completed_at)
        first.refresh_from_db()
        self.assertEqual(first.current_status, 2)

    def test_batch_failure_has_priority_and_completed_at_is_idempotent(self):
        failed = self._task(status=3)
        self._task(status=2)

        self.monitor.sync_batch_lifecycle(self.batch.id)
        self.batch.refresh_from_db()
        completed_at = self.batch.completed_at
        self.assertEqual(self.batch.status, 3)
        self.assertIsNotNone(completed_at)

        self.monitor.sync_batch_lifecycle(self.batch.id)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, 3)
        self.assertEqual(self.batch.completed_at, completed_at)
        failed.refresh_from_db()
        self.assertEqual(failed.current_status, 3)

    def test_appending_pending_task_reopens_completed_batch(self):
        self._task(status=2)
        self.monitor.sync_batch_lifecycle(self.batch.id)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, 2)

        self._task(status=0)
        self.monitor.sync_batch_lifecycle(self.batch.id)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, 1)
        self.assertIsNone(self.batch.completed_at)

    def test_sync_uses_fk_and_ignores_forged_legacy_batch_id(self):
        other = SimcTaskBatch.objects.create(
            user_id=801, name='other', batch_type='comparison', status=0,
        )
        self._task(status=2, ext={'batch_compare': {'batch_id': other.id}})

        self.monitor.sync_batch_lifecycle(self.batch.id)
        self.batch.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.batch.status, 2)
        self.assertEqual(other.status, 0)

    def test_mark_task_failed_updates_fk_batch_without_legacy_lookup(self):
        task = self._task(status=0)
        legacy_only = self._task(
            status=0,
            batch=None,
            ext={'batch_compare': {'batch_id': self.batch.id}},
        )

        self.monitor.mark_task_failed(task, 'expected failure')
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, 3)
        self.assertIsNotNone(self.batch.completed_at)

        isolated_batch = SimcTaskBatch.objects.create(
            user_id=801, name='isolated', batch_type='comparison', status=0,
        )
        legacy_only.ext = json.dumps({'batch_compare': {'batch_id': isolated_batch.id}})
        legacy_only.save(update_fields=['ext', 'modified_time'])
        self.monitor.mark_task_failed(legacy_only, 'legacy failure')
        isolated_batch.refresh_from_db()
        self.assertEqual(isolated_batch.status, 0)

    def test_soft_deleted_tasks_do_not_block_or_fail_batch_lifecycle(self):
        self._task(status=2)
        deleted_pending = self._task(status=0)
        deleted_failed = self._task(status=3)
        SimcTask.objects.filter(id__in=[deleted_pending.id, deleted_failed.id]).update(is_active=False)

        self.monitor.sync_batch_lifecycle(self.batch.id)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, 2)
        self.assertIsNotNone(self.batch.completed_at)

    def test_soft_delete_reconciles_real_batch_immediately(self):
        succeeded = self._task(status=2)
        pending = self._task(status=0)
        self.monitor.sync_batch_lifecycle(self.batch.id)
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, 1)

        user = User.objects.create_user(username='batch_delete_user', password='pwd')
        self.batch.user_id = user.id
        self.batch.save(update_fields=['user_id', 'updated_at'])
        SimcTask.objects.filter(id__in=[succeeded.id, pending.id]).update(user_id=user.id)
        client = Client()
        client.force_login(user)
        response = client.delete(
            '/api/simc-task/',
            data=json.dumps({'id': pending.id}),
            content_type='application/json',
        )

        self.assertTrue(response.json()['success'], response.json())
        self.batch.refresh_from_db()
        self.assertEqual(self.batch.status, 2)
        self.assertIsNotNone(self.batch.completed_at)

    def test_claim_promotes_batch_before_execution_and_finally_completes_it(self):
        task = self._task(
            status=0,
            ext={'raw_simc_code': 'warrior="Batch"\nlevel=80'},
        )
        observed_batch_status = []

        def complete_task(simc_task, _profile):
            self.batch.refresh_from_db()
            observed_batch_status.append(self.batch.status)
            simc_task.current_status = 2
            simc_task.completed_at = timezone.now()
            simc_task.save(update_fields=['current_status', 'completed_at', 'modified_time'])
            return True

        with patch.object(self.monitor, 'process_regular_simulation', side_effect=complete_task):
            self.assertTrue(self.monitor.process_simc_task(task))

        self.batch.refresh_from_db()
        self.assertEqual(observed_batch_status, [1])
        self.assertEqual(self.batch.status, 2)
        self.assertIsNotNone(self.batch.completed_at)


class SimcTemplateAPIViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='template_user', password='pwd', is_staff=True)
        self.client = Client()
        self.client.force_login(self.user)

    def test_list_returns_metadata_preview_without_apl_source(self):
        """The legacy template list must not mix full APL source into its base-template view."""
        base = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='fury', name='基础模板', content='warrior="Template"', is_active=True,
        )
        apl = SimcApl.objects.create(
            name='默认 APL',
            spec='warrior_fury',
            content='actions+=/bloodthirst',
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
            is_active=True,
        )
        response = self.client.get('/api/simc-template/?template_type=base_template')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual([row['id'] for row in payload['templates']], [base.id])
        self.assertEqual(payload['templates'][0]['template_type'], 'base_template')
        self.assertNotIn('template_content', payload['templates'][0])
        self.assertNotIn('content', payload['templates'][0])
        self.assertTrue(SimcApl.objects.filter(id=apl.id).exists())
        self.assertTrue(SimcContentTemplate.objects.filter(id=base.id).exists())

    def test_default_player_cannot_create_or_mutate_identity_fields(self):
        """default_player 不允许通过 API 创建或改变 template_type/source/spec 身份字段。"""
        protected = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='protected', content='secret baseline',
            is_active=True, is_selectable=False,
        )
        forbidden_attempts = [
            self.client.put(f'/api/simc-template/?id={protected.id}', data=json.dumps({
                'content': 'changed', 'template_type': 'base_template',
            }), content_type='application/json'),
            self.client.put(f'/api/simc-template/?id={protected.id}', data=json.dumps({
                'content': 'changed', 'source': 'user',
            }), content_type='application/json'),
            self.client.put(f'/api/simc-template/?id={protected.id}', data=json.dumps({
                'content': 'changed', 'spec': 'warrior_arms',
            }), content_type='application/json'),
            self.client.post('/api/simc-template/', data=json.dumps({
                'content': 'forged', 'template_type': 'default_player',
                'source': 'simc_upstream', 'spec': 'warrior_fury',
            }), content_type='application/json'),
        ]
        for response in forbidden_attempts:
            self.assertEqual(response.status_code, 403, response.content)
            self.assertFalse(response.json()['success'])
        protected.refresh_from_db()
        self.assertEqual(protected.content, 'secret baseline')
        self.assertEqual(protected.template_type, SimcContentTemplate.TYPE_DEFAULT_PLAYER)
        self.assertEqual(protected.source, SimcContentTemplate.SOURCE_SIMC_UPSTREAM)
        self.assertEqual(protected.spec, 'warrior_fury')

    def test_default_player_upstream_content_and_metadata_are_read_only(self):
        """simc_upstream 的 default_player 对 staff 也完全只读。"""
        protected = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='Baseline', content='warrior="Fury"\nlevel=80',
            is_active=True, is_selectable=False,
        )
        response = self.client.put(f'/api/simc-template/?id={protected.id}', data=json.dumps({
            'content': 'warrior="Fury"\nlevel=80\nrace=orc',
            'name': 'Updated Baseline',
            'is_selectable': True,
            'is_active': False,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()['success'])
        protected.refresh_from_db()
        self.assertNotIn('race=orc', protected.content)
        self.assertEqual(protected.name, 'Baseline')
        self.assertFalse(protected.is_selectable)
        self.assertTrue(protected.is_active)

    def test_base_template_rejects_actor_lines(self):
        """base_template 必须恰好一个 {player_config} 占位符，不允许 actor= 行。"""
        valid = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='', name='Valid', content='fight_style=Patchwerk\n{player_config}\n',
            is_active=True,
        )
        response = self.client.put(f'/api/simc-template/?id={valid.id}', data=json.dumps({
            'content': 'fight_style=Patchwerk\nactor="Bad"\n{player_config}\n',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('actor', response.json()['error'].lower())

        response = self.client.put(f'/api/simc-template/?id={valid.id}', data=json.dumps({
            'content': 'fight_style=Patchwerk\n{player_config}\n{player_config}\n',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('player_config', response.json()['error'].lower())

        response = self.client.put(f'/api/simc-template/?id={valid.id}', data=json.dumps({
            'content': 'fight_style=Patchwerk\nmax_time=300\n',
        }), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('player_config', response.json()['error'].lower())


    def test_delete_rejects_default_player(self):
        """DELETE 不允许删除 default_player 类型。"""
        protected = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury', name='Baseline', content='warrior="Fury"',
            is_active=True,
        )
        response = self.client.delete(f'/api/simc-template/?id={protected.id}')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(response.json()['success'])
        self.assertTrue(SimcContentTemplate.objects.filter(id=protected.id).exists())

    def test_delete_allows_user_content(self):
        """DELETE 允许删除用户创建的 base_template/custom_apl/default_apl。"""
        user_base = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='', name='My Base', content='fight_style=Patchwerk\n{player_config}',
        )
        user_apl = SimcApl.objects.create(
            name='My APL',
            spec='warrior_fury',
            content='actions=/auto_attack',
            source=SimcApl.SOURCE_USER,
            owner_user_id=self.user.id,
        )
        response = self.client.delete(f'/api/simc-template/?id={user_base.id}')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        self.assertFalse(SimcContentTemplate.objects.filter(id=user_base.id).exists())

        response = self.client.delete(f'/api/simc-workbench/apls/{user_apl.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SimcApl.objects.filter(id=user_apl.id).exists())
    def test_non_staff_cannot_mutate_system_template(self):
        user = User.objects.create_user(username='readonly_template_user', password='pwd')
        self.client.force_login(user)
        system_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='', name='System Base', content='fight_style=Patchwerk\n{player_config}',
        )

        update = self.client.put(
            f'/api/simc-template/?id={system_template.id}',
            data=json.dumps({'content': 'fight_style=HecticAddCleave\n{player_config}'}),
            content_type='application/json',
        )
        delete = self.client.delete(f'/api/simc-template/?id={system_template.id}')

        self.assertEqual(update.status_code, 403)
        self.assertEqual(delete.status_code, 403)
        system_template.refresh_from_db()
        self.assertEqual(system_template.content, 'fight_style=Patchwerk\n{player_config}')


class SimcAplCanonicalSpecPermissionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='apl-owner', password='pwd')
        self.admin = User.objects.create_user(username='lorexxar', password='pwd', is_staff=True)
        self.client.force_login(self.user)
        self.system_apl = SimcApl.objects.create(
            name='System Fury', spec='warrior_fury', class_name='warrior',
            content='actions+=/bloodthirst', source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True, is_active=True,
        )
        self.other_apl = SimcApl.objects.create(
            name='Other Fury', spec='warrior_fury', class_name='warrior',
            content='actions+=/whirlwind', source=SimcApl.SOURCE_USER,
            owner_user_id=self.admin.id, is_system=False, is_active=True,
        )

    def test_spec_options_are_canonical_and_include_midnight_devourer(self):
        response = self.client.get('/api/simc-spec-options/')
        self.assertEqual(response.status_code, 200)
        values = {row['value'] for row in response.json()['data']}
        self.assertIn('warrior_fury', values)
        self.assertIn('demonhunter_devourer', values)
        self.assertNotIn('demon_hunter_devourer', values)

    def test_apl_update_rejects_unknown_spec(self):
        self.client.force_login(self.admin)
        response = self.client.put(
            f'/api/simc-workbench/apls/{self.system_apl.id}/',
            data=json.dumps({'spec': 'not_a_real_spec'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.system_apl.refresh_from_db()
        self.assertEqual(self.system_apl.spec, 'warrior_fury')

    def test_admin_can_update_and_delete_system_and_other_user_apl(self):
        self.client.force_login(self.admin)
        update = self.client.put(
            f'/api/simc-workbench/apls/{self.system_apl.id}/',
            data=json.dumps({'name': 'Updated Fury', 'spec': 'warrior_fury', 'content': 'actions+=/raging_blow'}),
            content_type='application/json',
        )
        self.assertEqual(update.status_code, 200)
        delete = self.client.delete(f'/api/simc-workbench/apls/{self.other_apl.id}/')
        self.assertEqual(delete.status_code, 200)
        self.assertFalse(SimcApl.objects.filter(id=self.other_apl.id).exists())


class SimcBackendUpdateSafetyTests(TestCase):
    def test_tracked_source_changes_are_autocommitted_before_rebase_pull(self):
        command = UpdateSimcBinaryCommand()
        command.simc_source_dir = '/srv/simc'
        command._run = __import__('unittest').mock.Mock()

        with patch('botend.management.commands.update_simc_binary.subprocess.run') as run:
            run.return_value = SimpleNamespace(returncode=0, stdout=' M tracked.simc\n', stderr='')
            command._preserve_tracked_changes_before_pull()

        self.assertEqual(
            command._run.call_args_list,
            [
                __import__('unittest').mock.call(
                    ['git', 'add', '-u'], cwd='/srv/simc', timeout=30,
                    status='保存本地 SimC 源码改动', progress=8,
                ),
                __import__('unittest').mock.call(
                    ['git', 'commit', '-m', __import__('unittest').mock.ANY], cwd='/srv/simc', timeout=60,
                    status='提交本地 SimC 源码改动', progress=9,
                ),
            ],
        )
        commit_message = command._run.call_args_list[1].args[0][-1]
        self.assertIn('auto-save local changes before upstream sync', commit_message)

    def test_auto_update_failure_keeps_usable_binary_available_for_tasks(self):
        monitor = SimcMonitor(None, None)
        row = SimpleNamespace(simc_path=monitor.simc_path, auto_update=True, last_checked_at=None, is_updating=False)

        with patch.object(monitor, '_get_backend_row', return_value=row), \
             patch.object(monitor, '_validate_local_simc_binary', side_effect=[(True, ''), (True, '')]), \
             patch.object(monitor, '_get_git_hash', return_value='old123'), \
             patch.object(monitor, '_get_git_upstream_hash', return_value='new456'), \
             patch('django.core.management.call_command', side_effect=RuntimeError('compile failed')), \
             patch.object(monitor, '_set_update_status') as set_status, \
             patch('botend.controller.plugins.simc.SimcMonitor.upsert_system_alert'):
            self.assertTrue(monitor.ensure_local_simc_backend_current())

        self.assertTrue(any(
            kwargs.get('status') == '自动更新失败，继续使用现有 SimC 二进制'
            for _, kwargs in set_status.call_args_list
        ))


class SimcRawInspectTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='simc_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_inspect_raw_simc_code_detects_profile_and_default_apl(self):
        SimcApl.objects.create(
            name='默认APL hunter_beast_mastery',
            spec='hunter_beast_mastery',
            class_name='hunter',
            content='actions+=/kill_command',
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
            is_active=True,
            is_selectable=True,
        )
        payload = inspect_raw_simc_code('''
hunter="Bloodmastêr"
level=80
race=orc
role=attack
spec=beast_mastery
''')

        self.assertEqual(payload['character_name'], 'Bloodmastêr')
        self.assertEqual(payload['class'], 'hunter')
        self.assertEqual(payload['spec'], 'beast_mastery')
        self.assertEqual(payload['spec_key'], 'hunter_beast_mastery')
        self.assertTrue(payload['default_apl_available'])
        self.assertEqual(payload['plans'][0]['id'], 'regular')
        self.assertTrue(payload['plans'][0]['enabled'])
        self.assertFalse(payload['plans'][1]['enabled'])

    def test_inspect_raw_endpoint_returns_plans(self):
        response = self.client.post(
            '/api/simc-profile/inspect-raw/',
            data=json.dumps({'raw_simc_code': 'warrior="Foo"\nspec=fury\n'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['data']['class'], 'warrior')
        self.assertEqual(payload['data']['spec'], 'fury')
        self.assertEqual(payload['data']['plans'][0]['task_type'], 1)

    def test_raw_simc_task_create_persists_raw_code_in_ext(self):
        raw_code = 'mage="Arcaneone"\nspec=arcane\n'
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Arcaneone arcane 常规模拟',
                'task_type': 1,
                'simc_profile_id': 0,
                'raw_simc_code': raw_code,
                'regular_time': 300,
                'regular_target_count': 1,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        self.assertEqual(task.simc_profile_id, 0)
        self.assertEqual(task.task_type, 1)
        ext = json.loads(task.ext)
        self.assertEqual(ext['raw_simc_code'], raw_code)
        self.assertEqual(ext['regular_time'], 300)
        self.assertEqual(ext['regular_target_count'], 1)

    def test_raw_simc_attribute_task_is_rejected(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'bad attribute raw',
                'task_type': 2,
                'simc_profile_id': 0,
                'raw_simc_code': 'paladin="Foo"\nspec=retribution\n',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('不支持属性模拟', payload['error'])
        self.assertFalse(SimcTask.objects.exists())


class SimcBatchVariableCompareTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='batch_compare_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)
        self.base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury',
            name='Batch contract base',
            content=(
                '{simulation_options}\n{player_config}\n'
                '{stat_overrides}\n{action_list}\n{output_options}\n'
            ),
            is_active=True,
        )
        self.default_apl = SimcApl.objects.create(
            name='Batch contract APL',
            spec='warrior_fury',
            content='actions=/auto_attack',
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
            is_active=True,
        )

    def test_general_attribute_task_stays_on_attribute_executor_until_split_into_atoms(self):
        profile = SimcProfile.objects.create(
            user_id=self.user.id, name='Attribute source', spec='fury', talent='BUILD',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Attribute"\nspec=fury\nmain_hand=,id=222222',
            gear_crit=1000, gear_haste=2000, gear_mastery=3000, gear_versatility=4000,
            is_active=True,
        )
        with patch('botend.dashboard.api.SimcComposer.compose') as compose:
            response = self.client.post('/api/simc-task/', data=json.dumps({
                'name': 'legacy attribute sweep',
                'task_type': 2,
                'simc_profile_id': profile.id,
                'player_config_mode': 'manual_equipment',
                'player_equipment': profile.player_equipment,
                'spec': 'fury',
                'talent': 'BUILD',
                'selected_attributes': 'crit_haste',
                'attribute_step': 50,
            }), content_type='application/json')

        payload = response.json()
        self.assertTrue(payload['success'], payload)
        compose.assert_not_called()
        task = SimcTask.objects.get(id=payload['data']['id'])
        self.assertIsNone(task.fragment_manifest)
        self.assertFalse(task.final_simc_content)

        monitor = SimcMonitor(None, None)
        with patch.object(monitor, 'process_attribute_simulation', return_value=True) as execute:
            self.assertTrue(monitor.process_simc_task(task))
        execute.assert_called_once()
        self.assertIsNone(execute.call_args.args[1])

    def test_batch_creates_database_batch_and_frozen_v2_atoms(self):
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'Frozen gear batch', 'spec': 'fury',
            'player_config_mode': 'manual_equipment',
            'player_equipment': (
                'warrior="Batcher"\nlevel=90\nspec=fury\ntalents=BASE\n'
                'head=,id=212048\nmain_hand=,id=222222\n'
                '### Gear from Bags\nhead=,id=299001'
            ),
            'candidates': [{'slot': 'head', 'item_id': 299001, 'source': 'bags'}],
        }), content_type='application/json')

        payload = response.json()
        self.assertTrue(payload['success'], payload)
        batch = SimcTaskBatch.objects.get(id=payload['data']['batch_id'])
        self.assertEqual(batch.user_id, self.user.id)
        self.assertEqual(batch.batch_type, 'gear_candidates')
        tasks = list(SimcTask.objects.filter(batch=batch).order_by('id'))
        self.assertEqual(len(tasks), 2)
        self.assertEqual([task.candidate_label for task in tasks], ['基准配置', 'head #299001'])
        for task in tasks:
            manifest = json.loads(task.fragment_manifest)
            self.assertEqual(manifest['manifest_version'], 'v2')
            self.assertTrue(task.final_simc_content.strip())
            self.assertEqual(
                task.input_hash,
                hashlib.sha256(task.final_simc_content.encode('utf-8')).hexdigest(),
            )
            self.assertIn(f'html={task.result_file}', task.final_simc_content)

    def test_batch_rolls_back_when_one_candidate_cannot_be_composed(self):
        with patch('botend.dashboard.api.SimcComposer.compose', side_effect=[
            ('warrior="ok"\nhtml=ok.html', SimpleNamespace(to_json=lambda: '{"manifest_version":"v2"}'), None),
            (None, None, '候选内容冲突'),
        ]) as compose:
            response = self.client.post('/api/simc-task/batch/', data=json.dumps({
                'kind': 'gear_candidates', 'name': 'Rollback batch', 'spec': 'fury',
                'player_config_mode': 'manual_equipment',
                'player_equipment': (
                    'warrior="Batcher"\nspec=fury\ntalents=BASE\nhead=,id=212048\n'
                    '### Gear from Bags\nhead=,id=299001'
                ),
                'candidates': [{'slot': 'head', 'item_id': 299001, 'source': 'bags'}],
            }), content_type='application/json')

        self.assertFalse(response.json()['success'])
        self.assertEqual(compose.call_count, 2)
        self.assertFalse(SimcTaskBatch.objects.exists())
        self.assertFalse(SimcTask.objects.exists())

    def test_parse_manual_candidates_canonicalizes_plural_slot_aliases(self):
        candidates = parse_manual_simc_candidates('''
warrior="Batcher"
level=90
spec=fury
shoulders=,id=212048
main_hand=,id=222222
### Gear from Bags
shoulders=,id=299001
''')
        self.assertEqual(candidates['gear_candidates'][0]['slot'], 'shoulder')

    def test_plural_equipped_slot_can_be_replaced_by_canonical_candidate(self):
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'plural shoulder', 'spec': 'fury',
            'player_config_mode': 'manual_equipment',
            'player_equipment': (
                'warrior="Batcher"\nlevel=90\nspec=fury\ntalents=BASE\n'
                'shoulders=,id=212048\nmain_hand=,id=222222\n'
                '### Gear from Bags\nshoulders=,id=299001'
            ),
            'candidates': [{'slot': 'shoulder', 'item_id': 299001, 'source': 'bags'}],
        }), content_type='application/json')
        self.assertTrue(response.json()['success'], response.json())
        tasks = list(SimcTask.objects.order_by('id'))
        self.assertEqual(len(tasks), 2)
        candidate_ext = json.loads(tasks[1].ext)
        self.assertIn('shoulders=,id=299001', candidate_ext['player_equipment'])

    def test_singular_talent_baseline_can_create_talent_candidate_batch(self):
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'talent_candidates', 'name': 'singular talent', 'spec': 'fury',
            'player_config_mode': 'manual_equipment',
            'player_equipment': (
                'warrior="Batcher"\nlevel=90\nspec=fury\ntalent=BASE\n'
                'head=,id=212048\nmain_hand=,id=222222\n'
                '# Saved Loadout: Candidate\n# talents=NEW'
            ),
            'candidates': [{'talent': 'NEW', 'source': 'saved_loadout'}],
        }), content_type='application/json')
        self.assertTrue(response.json()['success'], response.json())
        tasks = list(SimcTask.objects.order_by('id'))
        self.assertEqual(len(tasks), 2)
        candidate_ext = json.loads(tasks[1].ext)
        self.assertIn('talents=NEW', candidate_ext['player_equipment'])
        self.assertNotIn('talent=BASE', candidate_ext['player_equipment'])

    def test_parse_manual_candidates_keeps_equipped_baseline_separate_from_bag_and_loadout_choices(self):
        candidates = parse_manual_simc_candidates('''
warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=212048,ilevel=639
### Gear from Bags
# Bag helm (650)
head=,id=299001,ilevel=650
# Saved Loadout: Cleave
# talents=CLEAVE_BUILD
### Weekly Reward Choices
# Weekly ring (655)
finger1=,id=299002,ilevel=655
''')
        self.assertEqual(candidates['base_talent'], 'ACTIVE_BUILD')
        self.assertEqual(candidates['gear_candidates'][0]['slot'], 'head')
        self.assertEqual(candidates['gear_candidates'][0]['item_id'], 299001)
        self.assertEqual(candidates['gear_candidates'][0]['source'], 'bags')
        self.assertEqual(candidates['gear_candidates'][1]['source'], 'weekly_reward')
        self.assertEqual(candidates['talent_candidates'][0]['talent'], 'CLEAVE_BUILD')
        self.assertEqual(parse_manual_player_config('head=,id=212048\n### Gear from Bags\nhead=,id=299001', 'fury')['equipment'][0]['id'], 212048)

    def test_auto_attribute_batch_creates_complete_50_rating_pairwise_neighborhood(self):
        base = {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}
        rows = SimcBatchTaskAPIView._attribute_variants(base, 50)
        self.assertEqual(len(rows), 13)
        self.assertEqual(sum(is_base for _, _, is_base, _ in rows), 1)
        moves = [candidate['move'] for _, _, is_base, candidate in rows if not is_base]
        self.assertEqual(
            {(move['from'], move['to'], move['transfer']) for move in moves},
            {(source, target, 50) for source in base for target in base if source != target},
        )
        for _, ratings, _, candidate in rows:
            self.assertEqual(sum(ratings.values()), sum(base.values()))
            self.assertTrue(all(value >= 0 for value in ratings.values()))
            if candidate['move'].get('type') != 'baseline':
                self.assertEqual(candidate['move']['transfer'], 50)

    def test_auto_attribute_batch_omits_sub_50_source_without_projecting_non_grid_move(self):
        base = {'crit': 49, 'haste': 50, 'mastery': 100, 'versatility': 0}
        rows = SimcBatchTaskAPIView._attribute_variants(base, 50)
        moves = [candidate['move'] for _, _, is_base, candidate in rows if not is_base]
        self.assertEqual(len(rows), 7)  # centre + (haste/mastery) * 3 valid targets
        self.assertTrue(all(move['from'] != 'crit' for move in moves))
        self.assertTrue(all(move['transfer'] == 50 for move in moves))

    def test_auto_attribute_batch_creates_base_and_limited_variants_with_one_batch_id(self):
        frozen_player = 'warrior="Batcher"\nlevel=90\nspec=fury\ntalents=ATTRIBUTE_BUILD\nhead=,id=212048,ilevel=639\nmain_hand=,id=222222,ilevel=639'
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'attribute_variants', 'name': 'Fury 自动属性比较', 'spec': 'fury',
            'player_config_mode': 'attribute_only', 'talent': 'ATTRIBUTE_BUILD',
            'player_equipment': frozen_player,
            'gear_strength': 5000,
            'gear_crit': 1000, 'gear_haste': 2000, 'gear_mastery': 3000, 'gear_versatility': 4000,
            'attribute_step': 50, 'fight_style': 'Patchwerk', 'time': 300, 'target_count': 1,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        # 首轮覆盖四属性之间全部有向 50 绿字转移：中心 + 12 个合法邻居。
        self.assertEqual(payload['data']['accepted'], 13)
        ext_rows = [json.loads(task.ext) for task in SimcTask.objects.order_by('id')]
        self.assertEqual(len(ext_rows), 13)
        self.assertEqual(len({row['batch_compare']['batch_id'] for row in ext_rows}), 1)
        self.assertEqual({row['batch_compare']['kind'] for row in ext_rows}, {'attribute_variants'})
        self.assertEqual(sum(row['batch_compare']['is_base'] for row in ext_rows), 1)
        self.assertEqual({row['player_config_mode'] for row in ext_rows}, {'attribute_only'})
        self.assertEqual({row['player_equipment'] for row in ext_rows}, {frozen_player})
        self.assertEqual({row['talent'] for row in ext_rows}, {'ATTRIBUTE_BUILD'})
        self.assertEqual({row['gear_strength'] for row in ext_rows}, {5000})
        candidates = [row['batch_compare']['candidate'] for row in ext_rows]
        self.assertEqual(candidates[0]['algorithm'], 'four_stat_pairwise_hill_climb')
        self.assertEqual(candidates[0]['algorithm_version'], 2)
        self.assertEqual(candidates[0]['round'], 1)
        base_total = sum((1000, 2000, 3000, 4000))
        gears = [{stat: row[f'gear_{stat}'] for stat in ('crit', 'haste', 'mastery', 'versatility')} for row in ext_rows]
        self.assertTrue(all(sum(gear.values()) == base_total for gear in gears))
        changed_stats = {stat for gear in gears[1:] for stat, value in gear.items() if value != {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}[stat]}
        self.assertEqual(changed_stats, {'crit', 'haste', 'mastery', 'versatility'})

    def test_auto_attribute_batch_rejects_missing_frozen_player_baseline(self):
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'attribute_variants', 'name': 'Fury 自动属性比较', 'spec': 'fury',
            'player_config_mode': 'attribute_only', 'talent': 'ATTRIBUTE_BUILD',
            'gear_strength': 5000,
            'gear_crit': 1000, 'gear_haste': 2000, 'gear_mastery': 3000, 'gear_versatility': 4000,
            'attribute_step': 50, 'fight_style': 'Patchwerk', 'time': 300, 'target_count': 1,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('玩家装备基线', response.json()['error'])
        self.assertFalse(SimcTask.objects.exists())

    def test_attribute_search_ui_submits_the_visible_frozen_player_baseline(self):
        main_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        start = main_js.index('function simcAttributeSearchRequestBody()')
        end = main_js.index('async function submitSimcAttributeSearch', start)
        request_builder = main_js[start:end]

        self.assertIn("document.getElementById('simc-sim-equipment')", request_builder)
        self.assertIn('player_equipment:', request_builder)
        self.assertIn('玩家装备基线', request_builder)

    def test_player_detail_refresh_submits_attribute_frozen_player_baseline(self):
        main_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        start = main_js.index('async function refreshSimcPlayerDetail()')
        end = main_js.index('function simcAttributeSearchRequestBody()', start)
        refresh = main_js[start:end]
        self.assertIn('requestBody.player_equipment', refresh)
        self.assertIn("document.getElementById('simc-sim-equipment')", refresh)

    def test_regular_attribute_task_rejects_missing_default_player_template(self):
        response = self.client.post('/api/simc-task/', data=json.dumps({
            'name': 'Fury 属性基准', 'task_type': 1, 'spec': 'fury',
            'player_config_mode': 'attribute_only', 'talent': 'ATTRIBUTE_BUILD',
            'gear_crit': 1000, 'gear_haste': 2000,
            'gear_mastery': 3000, 'gear_versatility': 4000,
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('默认玩家装备模板', response.json()['error'])
        self.assertFalse(SimcTask.objects.exists())

    def test_auto_attribute_batch_projects_anchor_direction_to_boundary_instead_of_dropping_it(self):
        # 50-rating 离散搜索不允许把不足一步的余额投影成 100 等非网格转移。
        base = {'crit': 400, 'haste': 1100, 'mastery': 1140, 'versatility': 100}
        rows = SimcBatchTaskAPIView._attribute_variants(base, 50)
        self.assertEqual(len(rows), 13)
        self.assertTrue(all(sum(ratings.values()) == sum(base.values()) for _, ratings, _, _ in rows))
        self.assertTrue(all(candidate['move'].get('type') == 'baseline' or candidate['move']['transfer'] == 50 for _, _, _, candidate in rows))

        chosen = SimcBatchTaskAPIView._next_attribute_search_center([
            {'ratings': {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}, 'dps': 100000, 'is_center': True},
            {'ratings': {'crit': 950, 'haste': 2050, 'mastery': 3000, 'versatility': 4000}, 'dps': 101500},
        ], step=50, min_step=50)
        self.assertEqual(chosen['ratings'], {'crit': 950, 'haste': 2050, 'mastery': 3000, 'versatility': 4000})
        self.assertEqual(chosen['step'], 50)
        self.assertFalse(chosen['converged'])

        local_optimum = SimcBatchTaskAPIView._next_attribute_search_center([
            {'ratings': {'crit': 950, 'haste': 2050, 'mastery': 3000, 'versatility': 4000}, 'dps': 102000, 'is_center': True},
            {'ratings': {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}, 'dps': 101800},
        ], step=50, min_step=50)
        self.assertTrue(local_optimum['converged'])
        self.assertEqual(local_optimum['stop_reason'], 'local_optimum_50_pairwise')

    def test_next_attribute_round_preserves_budget_and_marks_new_center(self):
        base = {'crit': 1200, 'haste': 2000, 'mastery': 3000, 'versatility': 3800}
        rows = SimcBatchTaskAPIView._attribute_variants(base, 50, round_number=2, mark_base=True)
        self.assertEqual(len(rows), 13)
        self.assertTrue(rows[0][2])
        self.assertEqual(rows[0][3]['round'], 2)
        self.assertTrue(all(sum(ratings.values()) == 10000 for _, ratings, _, _ in rows))

    def test_continue_attribute_search_preserves_exact_frozen_player_baseline(self):
        frozen_player = 'warrior="Batcher"\nlevel=90\nspec=fury\ntalents=ATTRIBUTE_BUILD\nhead=,id=212048,ilevel=639\nmain_hand=,id=222222,ilevel=639'
        batch_id = 'frozen-player-batch'
        ratings_rows = SimcBatchTaskAPIView._attribute_variants(
            {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}, 50,
        )
        for index, (label, ratings, is_base, candidate) in enumerate(ratings_rows):
            base_ext = {'batch_compare': {
                'version': 2, 'batch_id': batch_id, 'kind': 'attribute_variants',
                'index': index, 'label': label, 'is_base': is_base, 'candidate': candidate,
            }}
            ext = SimcTaskAPIView()._build_task_ext(
                task_type=1, ext=base_ext, fight_style='Patchwerk', time=300, target_count=1,
                player_config_mode='attribute_only', player_equipment=frozen_player,
                spec='fury', talent='ATTRIBUTE_BUILD', gear_strength=5000,
                gear_crit=ratings['crit'], gear_haste=ratings['haste'],
                gear_mastery=ratings['mastery'], gear_versatility=ratings['versatility'],
            )
            SimcTask.objects.create(
                user_id=self.user.id, simc_profile_id=0,
                name=f'Fury 自动属性比较 · {label}', current_status=2,
                result_file=f'simc_task_{index}.html', task_type=1, ext=ext,
            )

        request = RequestFactory().post('/api/simc-task/batch/', data='{}', content_type='application/json')
        request.user = self.user
        dps_values = iter([100000, 101500] + [100100] * 11)
        with patch.object(SimcRegularCompareAPIView, '_get_result_file_content', return_value='<html></html>'), \
                patch.object(SimcRegularCompareAPIView, '_parse_regular_result', side_effect=lambda _html: {'dps': next(dps_values)}):
            result = SimcBatchTaskAPIView()._continue_attribute_search(request, {}, batch_id)

        self.assertEqual(result['accepted'], 13)
        next_round_ext = [json.loads(task.ext) for task in SimcTask.objects.filter(id__in=result['task_ids'])]
        self.assertEqual({row['player_equipment'] for row in next_round_ext}, {frozen_player})
        self.assertEqual({row['batch_compare']['candidate']['round'] for row in next_round_ext}, {2})

    def test_continue_attribute_search_preserves_template_and_explicit_empty_apl(self):
        frozen_player = 'warrior="Batcher"\nlevel=90\nspec=fury\ntalents=ATTRIBUTE_BUILD\nhead=,id=212048,ilevel=639\nmain_hand=,id=222222,ilevel=639'
        self.default_apl.is_active = False
        self.default_apl.save()
        template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='fury', name='Frozen base', content='warrior="DB_CHANGED"', is_active=True,
        )
        apl = SimcApl.objects.create(
            name='Frozen APL',
            spec='warrior_fury',
            content='actions=/DB_CHANGED',
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
            is_active=True,
        )
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Frozen input batch',
            batch_type='attribute_sweep', status=1,
            request_manifest=json.dumps({'version': 2}),
        )
        batch_id = str(batch.id)
        ratings_rows = SimcBatchTaskAPIView._attribute_variants(
            {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}, 50,
        )
        for index, (label, ratings, is_base, candidate) in enumerate(ratings_rows):
            ext = SimcTaskAPIView()._build_task_ext(
                task_type=1,
                ext={'batch_compare': {'version': 2, 'batch_id': batch_id, 'kind': 'attribute_variants', 'index': index, 'label': label, 'is_base': is_base, 'candidate': candidate}},
                fight_style='Patchwerk', time=300, target_count=1,
                player_config_mode='attribute_only', player_equipment=frozen_player,
                spec='fury', talent='ATTRIBUTE_BUILD', gear_strength=5000,
                gear_crit=ratings['crit'], gear_haste=ratings['haste'],
                gear_mastery=ratings['mastery'], gear_versatility=ratings['versatility'],
                base_template_id=template.id,
                base_template_content='{player_config}\n{action_list}',
                selected_apl_id=apl.id, override_action_list='', override_action_list_provided=True,
            )
            SimcTask.objects.create(
                user_id=self.user.id, simc_profile_id=0,
                name=f'Fury 自动属性比较 · {label}', current_status=2,
                result_file=f'simc_task_{100 + index}.html', task_type=1, ext=ext,
                batch=batch, candidate_label=label,
            )

        template.content = 'warrior="NEW_DB"'
        template.save(update_fields=['content'])
        apl.content = 'actions=/NEW_DB'
        apl.save(update_fields=['content'])
        request = RequestFactory().post('/api/simc-task/batch/', data='{}', content_type='application/json')
        request.user = self.user
        dps_values = iter([100000, 101500] + [100100] * 11)
        with patch.object(SimcRegularCompareAPIView, '_get_result_file_content', return_value='<html></html>'), \
                patch.object(SimcRegularCompareAPIView, '_parse_regular_result', side_effect=lambda _html: {'dps': next(dps_values)}):
            result = SimcBatchTaskAPIView()._continue_attribute_search(request, {}, batch_id)

        next_round_ext = [json.loads(task.ext) for task in SimcTask.objects.filter(id__in=result['task_ids'])]
        self.assertEqual({row['base_template_content'] for row in next_round_ext}, {'{player_config}\n{action_list}'})
        self.assertEqual({row['override_action_list'] for row in next_round_ext}, {''})
        next_round_tasks = list(SimcTask.objects.filter(id__in=result['task_ids']))
        self.assertEqual({task.batch_id for task in next_round_tasks}, {batch.id})
        self.assertEqual({json.loads(task.fragment_manifest)['manifest_version'] for task in next_round_tasks}, {'v2'})
        self.assertTrue(all(task.final_simc_content for task in next_round_tasks))
        self.assertTrue(all(
            task.input_hash == hashlib.sha256(task.final_simc_content.encode('utf-8')).hexdigest()
            for task in next_round_tasks
        ))

    def test_saved_profile_simulate_now_freezes_all_execution_inputs(self):
        self.base_template.is_active = False
        self.base_template.save()
        self.default_apl.is_active = False
        self.default_apl.save()
        template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='fury', name='Fury base', content=(
                '{simulation_options}\n{player_config}\n{stat_overrides}\n'
                '{action_list}\n{output_options}\n'
            ), is_active=True,
        )
        apl = SimcApl.objects.create(
            name='Fury APL',
            spec='warrior_fury',
            content='actions=/bloodthirst',
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
            is_active=True,
        )
        profile = SimcProfile.objects.create(
            user_id=self.user.id, name='Saved fury', spec='fury', talent='BUILD',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Saved"\nspec=fury\nmain_hand=,id=222222',
            is_active=True,
        )

        result = SimcProfileAPIView()._create_simulation_task(self.user.id, profile)

        self.assertTrue(result['success'], result)
        ext = json.loads(SimcTask.objects.get(id=result['data']['id']).ext)
        self.assertEqual(ext['base_template_id'], template.id)
        self.assertEqual(ext['base_template_content'], template.content)
        self.assertEqual(ext['player_equipment'], profile.player_equipment)
        self.assertEqual(ext['selected_apl_id'], apl.id)
        self.assertEqual(ext['override_action_list'], apl.content)

        task = SimcTask.objects.get(id=result['data']['id'])
        manifest = json.loads(task.fragment_manifest)
        self.assertEqual(manifest['manifest_version'], 'v2')
        self.assertTrue(task.final_simc_content.strip())
        self.assertEqual(
            task.input_hash,
            hashlib.sha256(task.final_simc_content.encode('utf-8')).hexdigest(),
        )
        self.assertIn(f'html={task.result_file}', task.final_simc_content)
        self.assertIn('actions=/bloodthirst', task.final_simc_content)

        frozen_content = task.final_simc_content
        profile.player_equipment = 'warrior="Changed"\nspec=fury\nmain_hand=,id=999999'
        profile.save(update_fields=['player_equipment'])
        template.content = '{player_config}\n# changed template\n{output_options}'
        template.save(update_fields=['content'])
        apl.content = 'actions=/changed'
        apl.save(update_fields=['content'])

        monitor = SimcMonitor(None, None)
        with patch.object(monitor, 'process_regular_simulation', return_value=True) as execute:
            self.assertTrue(monitor.process_simc_task(task))
        execute.assert_called_once()
        executed_task, executed_profile = execute.call_args.args
        self.assertIsNone(executed_profile)
        self.assertEqual(executed_task.final_simc_content, frozen_content)

    def test_attribute_detail_parses_frozen_player_and_overlays_requested_ratings(self):
        detail = build_player_config_detail(
            'attribute_only', 'fury',
            player_equipment='warrior="Batcher"\nlevel=90\nspec=fury\ntalents=BASE\nhead=,id=212048,ilevel=639',
            talent='ATTRIBUTE_BUILD', gear_strength=5000,
            gear_crit=1000, gear_haste=2000, gear_mastery=3000, gear_versatility=4000,
        )

        self.assertEqual(detail['identity']['name'], 'Batcher')
        self.assertEqual(detail['identity']['level'], 90)
        self.assertEqual(detail['equipment'][0]['id'], 212048)
        self.assertEqual(detail['talents']['build_code'], 'ATTRIBUTE_BUILD')
        self.assertEqual(detail['stats']['secondary']['crit']['rating'], 1000)
        self.assertNotIn('无装备', detail['source']['label'])

    def test_battlenet_template_selection_accepts_playerless_default_template(self):
        monitor = SimcMonitor(None, None)
        default_template = SimpleNamespace(
            id=1,
            spec='default',
            content='fight_style={fight_style}\n{player_config}\n{action_list}',
        )
        selected = monitor._select_template_from_queryset(
            [default_template], 'blood', player_config_mode='battlenet'
        )
        self.assertIs(selected, default_template)

    def test_template_selection_ignores_non_executable_probe_template(self):
        monitor = SimcMonitor(None, None)
        probe = SimpleNamespace(id=1, spec='default', content='spec={spec}\n{player_config}\n')
        executable = SimpleNamespace(
            id=2,
            spec='default',
            content='warrior="Template"\nspec={spec}\n',
        )
        selected = monitor._select_template_from_queryset([probe, executable], 'fury')
        self.assertIs(selected, executable)

    def test_incomplete_base_template_is_not_executable(self):
        probe = SimpleNamespace(id=1, content='spec=fury\n{player_config}\n')
        self.assertFalse(SimcMonitor._is_executable_base_template(probe))

    def test_simc_error_details_keep_attribute_batch_execution_context(self):
        monitor = SimcMonitor(None, None)
        manifest = {
            'player_config_mode': 'attribute_only',
            'spec': 'fury',
            'talent': 'ATTRIBUTE_BUILD',
            'gear_crit': 1000,
            'gear_haste': 2000,
            'gear_mastery': 3000,
            'gear_versatility': 4000,
            'selected_apl_id': 42,
            'batch_compare': {'batch_id': 'batch-1', 'candidate': {'round': 1}},
        }
        task = SimpleNamespace(ext=json.dumps(manifest), id=99)

        monitor.save_simc_error_details(task, 'SimC未生成预期结果文件', stderr_text='x' * 20000)

        stored = json.loads(task.ext)
        for key, value in manifest.items():
            self.assertEqual(stored[key], value)
        self.assertEqual(stored['simc_error_summary'], 'SimC未生成预期结果文件')
        self.assertIn('simc_error_native', stored)

    def test_attribute_batch_task_renders_its_own_explicit_html_result_file(self):
        monitor = SimcMonitor(None, None)
        rendered = monitor.apply_template(
            'warrior="LMonitor"\n{player_config}\nhtml={result_file}\n{action_list}',
            {
                'player_config_mode': 'attribute_only',
                'player_equipment': 'warrior="Frozen"\nlevel=90\nspec=fury\ntalents=OLD\nhead=,id=212048\nmain_hand=,id=222222',
                'talent': 'BUILD',
                'gear_strength': 5000,
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
                'result_file': 'simc_task_42.html',
            },
        )
        self.assertIn('html=simc_task_42.html', rendered)
        self.assertIn('warrior="Frozen"', rendered)
        self.assertIn('head=,id=212048', rendered)
        self.assertNotIn('warrior="LMonitor"', rendered)
        self.assertIn('gear_strength=5000', rendered)
        self.assertIn('gear_crit_rating=1000', rendered)
        self.assertIn('gear_haste_rating=2000', rendered)
        self.assertIn('gear_mastery_rating=3000', rendered)
        self.assertIn('gear_versatility_rating=4000', rendered)
        self.assertNotIn('\ncrit_rating=1000', rendered)
        self.assertNotIn('{result_file}', rendered)

    def test_attribute_batch_task_appends_explicit_html_when_base_template_has_no_placeholder(self):
        monitor = SimcMonitor(None, None)
        rendered = monitor.apply_template(
            'warrior="LMonitor"\n{player_config}\n{action_list}',
            {
                'player_config_mode': 'attribute_only',
                'player_equipment': 'warrior="Frozen"\nlevel=90\nspec=fury\ntalents=OLD\nhead=,id=212048\nmain_hand=,id=222222',
                'talent': 'BUILD',
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
                'result_file': 'simc_task_43.html',
            },
        )
        self.assertTrue(rendered.endswith('html=simc_task_43.html'))
        self.assertEqual(rendered.count('html='), 1)

    def test_attribute_render_replaces_singular_talent_with_selected_build(self):
        rendered = SimcMonitor(None, None).apply_template(
            '{player_config}\n{action_list}',
            {
                'player_config_mode': 'attribute_only',
                'player_equipment': 'warrior="Frozen"\nlevel=90\nspec=fury\ntalent=OLD\nhead=,id=212048\nmain_hand=,id=222222',
                'talent': 'NEW',
            },
        )
        self.assertIn('talents=NEW', rendered)
        self.assertNotIn('talent=OLD', rendered)

    def test_attribute_render_drops_executable_bag_and_weekly_alternatives(self):
        monitor = SimcMonitor(None, None)
        rendered = monitor.apply_template(
            'warrior="Template"\n{player_config}\n{action_list}',
            {
                'player_config_mode': 'attribute_only', 'talent': 'BUILD',
                'player_equipment': (
                    'warrior="Frozen"\nlevel=90\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222\n'
                    '### Gear from Bags\nhead=,id=299001\n'
                    '### Weekly Reward Choices\nfinger1=,id=299002'
                ),
            },
        )
        self.assertIn('head=,id=212048', rendered)
        self.assertNotIn('299001', rendered)
        self.assertNotIn('299002', rendered)
        self.assertNotIn('Gear from Bags', rendered)

    def test_attribute_task_rejects_nonempty_baseline_without_actor_or_equipped_slot(self):
        for baseline in ('head=,id=212048', 'warrior="No gear"\nspec=fury'):
            response = self.client.post('/api/simc-task/', data=json.dumps({
                'name': 'Malformed baseline', 'task_type': 1, 'spec': 'fury',
                'player_config_mode': 'attribute_only', 'player_equipment': baseline,
                'talent': 'BUILD', 'gear_crit': 1, 'gear_haste': 2,
                'gear_mastery': 3, 'gear_versatility': 4,
            }), content_type='application/json')
            self.assertFalse(response.json()['success'], response.json())
        self.assertFalse(SimcTask.objects.exists())

    def test_result_file_directive_replaces_existing_html_output(self):
        rendered = SimcMonitor.ensure_result_file_directive(
            'warrior="LMonitor"\nhtml=stale_report.html\n',
            'simc_task_44.html',
        )
        self.assertEqual(rendered.count('html='), 1)
        self.assertTrue(rendered.endswith('html=simc_task_44.html'))
        self.assertNotIn('stale_report.html', rendered)

    def test_attribute_search_stops_when_it_revisits_same_center_and_step(self):
        ratings = {'crit': 1200, 'haste': 2000, 'mastery': 3000, 'versatility': 3800}
        stop = SimcBatchTaskAPIView._attribute_search_stop_reason(
            round_number=4, ratings=ratings, step=200,
            visited_centers={(tuple(ratings[stat] for stat in SimcBatchTaskAPIView.ATTRIBUTE_STATS), 200)},
            max_rounds=20,
        )
        self.assertEqual(stop, 'cycle_detected')

    def test_execute_simc_command_passes_absolute_task_result_path(self):
        from unittest.mock import patch
        import tempfile
        import os
        monitor = object.__new__(SimcMonitor)
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor.simc_path = '/opt/simc'
            monitor.result_path = tmpdir
            task = SimpleNamespace(id=88, result_file='simc_task_88.html', ext='{}', save=lambda **kwargs: None)
            expected = os.path.join(tmpdir, task.result_file)
            with patch('botend.controller.plugins.simc.SimcMonitor.subprocess.run') as run:
                run.return_value = SimpleNamespace(
                    returncode=0,
                    stdout='Player: Audit warrior fury 90\n  DPS=60000.0\n    bloodthirst Count=40 pDPS=5000\n',
                    stderr='',
                )
                with patch('botend.interface.ossupload.ossUpload', return_value=True):
                    with open(expected, 'w', encoding='utf-8') as report:
                        report.write('<html></html>')
                    self.assertTrue(monitor.execute_simc_command('/tmp/input.simc', task, task.result_file))
            self.assertEqual(run.call_args.args[0], ['/opt/simc', '/tmp/input.simc', f'html={expected}'])

    def test_execute_simc_command_rejects_auto_attack_only_semantic_result(self):
        from unittest.mock import patch
        import tempfile
        import os
        monitor = object.__new__(SimcMonitor)
        stdout = '''Player: Audit warrior fury 90
  DPS=2422.9 DPS-Error=9.1/0.38%
    auto_attack_mh Count=112.6 pDPS=1618
    auto_attack_oh Count=110.4 pDPS=803
    charge_impact Count=1.0 pDPS=2
'''
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor.simc_path = '/opt/simc'
            monitor.result_path = tmpdir
            task = SimpleNamespace(
                id=89, result_file='simc_task_89.html',
                ext=json.dumps({'spec': 'fury'}),
                save=lambda **kwargs: None,
            )
            expected = os.path.join(tmpdir, task.result_file)
            with open(expected, 'w', encoding='utf-8') as report:
                report.write('<html></html>')
            with patch('botend.controller.plugins.simc.SimcMonitor.subprocess.run') as run:
                run.return_value = SimpleNamespace(returncode=0, stdout=stdout, stderr='')
                self.assertFalse(monitor.execute_simc_command('/tmp/input.simc', task, task.result_file))
        stored = json.loads(task.ext)
        self.assertIn('只有自动攻击', stored['simc_error_summary'])
        self.assertEqual(stored['semantic_validation']['valid'], False)

    def test_semantic_validation_identifies_unresolved_talent_apl_dispatch(self):
        stdout = '''Player: Audit warrior fury 90
  DPS=2499.2 DPS-Error=20/0.82%
  Priorities (actions.default):
    auto_attack/charge,if=time<=0.5
    run_action_list,name=slayer,if=talent.slayers_dominance&active_enemies=1
    run_action_list,name=thane,if=talent.lightning_strikes&active_enemies=1
  Actions:
    auto_attack_mh Count=48.7 pDPS=1672
    auto_attack_oh Count=47.5 pDPS=823
    charge_impact Count=1.0 pDPS=5
'''
        validation = SimcMonitor.validate_simulation_semantics(stdout)
        self.assertFalse(validation['valid'])
        self.assertEqual(validation['failure_type'], 'talent_apl_dispatch')
        self.assertEqual(validation['unresolved_action_lists'], ['slayer', 'thane'])
        self.assertIn('英雄天赋', validation['reason'])
        self.assertIn('slayer', validation['reason'])
        self.assertIn('thane', validation['reason'])

    def test_semantic_validation_identifies_single_unresolved_talent_dispatch(self):
        stdout = '''Player: Audit warrior fury 90
  DPS=2499.2 DPS-Error=20/0.82%
  Priorities (actions.default):
    auto_attack
    run_action_list,name=hero,if=talent.hero_root
  Actions:
    auto_attack_mh Count=48.7 pDPS=1672
'''
        validation = SimcMonitor.validate_simulation_semantics(stdout)
        self.assertEqual(validation['failure_type'], 'talent_apl_dispatch')
        self.assertEqual(validation['unresolved_action_lists'], ['hero'])

    def test_semantic_validation_does_not_misclassify_when_a_talent_dispatch_is_active(self):
        stdout = '''Player: Audit warrior fury 90
  DPS=2499.2 DPS-Error=20/0.82%
  Priorities (actions.default):
    auto_attack
    run_action_list,name=slayer,if=talent.slayers_dominance
    run_action_list,name=thane,if=talent.lightning_strikes
  Priorities (actions.slayer):
    bloodthirst,if=0
  Actions:
    auto_attack_mh Count=48.7 pDPS=1672
'''
        validation = SimcMonitor.validate_simulation_semantics(stdout)
        self.assertEqual(validation['failure_type'], 'auto_attack_only')
        self.assertEqual(validation['unresolved_action_lists'], ['thane'])

    def test_semantic_validation_accepts_core_skill_damage(self):
        stdout = '''Player: Audit warrior fury 90
  DPS=62453.0 DPS-Error=150/0.24%
  Priorities (actions.slayer):
    auto_attack_mh Count=144.6 pDPS=3390
    bloodthirst Count=43.3 pDPS=4976
    rampage1 Count=79.6 pDPS=2295
'''
        validation = SimcMonitor.validate_simulation_semantics(stdout)
        self.assertTrue(validation['valid'])
        self.assertGreater(validation['non_auto_dps'], 0)

    def test_attribute_search_rejects_any_non_50_step(self):
        results = [
            {'ratings': {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}, 'dps': 100000, 'is_center': True},
            {'ratings': {'crit': 950, 'haste': 2050, 'mastery': 3000, 'versatility': 4000}, 'dps': 100100, 'is_center': False},
        ]
        with self.assertRaisesRegex(ValueError, '固定使用 50'):
            SimcBatchTaskAPIView._next_attribute_search_center(results, step=100, min_step=50)
        bad_response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'attribute_variants', 'name': '错误步长', 'spec': 'fury',
            'player_config_mode': 'attribute_only', 'talent': 'ATTRIBUTE_BUILD',
            'player_equipment': 'warrior="Frozen"\nlevel=90\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222',
            'gear_crit': 1000, 'gear_haste': 2000, 'gear_mastery': 3000, 'gear_versatility': 4000,
            'attribute_step': 100,
        }), content_type='application/json')
        self.assertFalse(bad_response.json()['success'])
        self.assertIn('固定使用 50', bad_response.json()['error'])

        stop = SimcBatchTaskAPIView._attribute_search_stop_reason(
            round_number=20, ratings={'crit': 1200, 'haste': 2000, 'mastery': 3000, 'versatility': 3800},
            step=100, visited_centers=set(), max_rounds=20,
        )
        self.assertEqual(stop, 'max_rounds_reached')

    def test_attribute_round_manifest_parser_defaults_invalid_values_to_first_round(self):
        self.assertEqual(SimcBatchTaskAPIView._parse_manifest_round({}), 1)
        self.assertEqual(SimcBatchTaskAPIView._parse_manifest_round({'candidate': {'round': 'bad'}}), 1)
        self.assertEqual(SimcBatchTaskAPIView._parse_manifest_round({'candidate': {'round': 3}}), 3)

    def test_candidate_comparison_ui_allows_independent_trinket_gear_and_talent_selection(self):
        """候选入口按变更维度分批，不能把饰品、其他装备和天赋混成同一排名。"""
        main_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        ui_start = main_js.index('function renderSimcComparisonCandidates(comparison)')
        ui_end = main_js.index('async function preflightSimcBattlenet()', ui_start)
        candidate_ui = main_js[ui_start:ui_end]

        self.assertIn('simc-comparison-kind="trinket_candidates"', candidate_ui)
        self.assertIn('simc-comparison-kind="gear_candidates"', candidate_ui)
        self.assertIn('simc-comparison-kind="talent_candidates"', candidate_ui)
        self.assertIn('startSelectedSimcCandidateComparisons', candidate_ui)
        self.assertIn("kind === 'trinket_candidates'", candidate_ui)
        self.assertIn("kind === 'gear_candidates'", candidate_ui)
        self.assertIn('for (const kind of selectedKinds)', candidate_ui)
        self.assertIn("kind: requestKind", candidate_ui)
        self.assertIn("category: kind", candidate_ui)
        self.assertIn('const completed = await pollSimcCandidateComparison', candidate_ui)
        self.assertIn('if (!completed || !isCurrentSimcCandidateControl(control)) return;', candidate_ui)
        self.assertIn('finish(true);', candidate_ui)
        self.assertIn("switchSimcWorkbenchL1Tab('history')", candidate_ui)
        self.assertIn("'/api/simc-regular-compare/?batch_id='", candidate_ui)
        self.assertIn("switchSimcWorkbenchTab('artifacts')", candidate_ui)
        self.assertNotIn("'/simc-compare/?batch_id='", candidate_ui)
        self.assertNotIn("window.open(", candidate_ui)
        self.assertIn('resolve(completed);', candidate_ui)

    def test_batch_marks_trinket_category_without_changing_gear_candidate_validation(self):
        base = {
            'name': 'Trinket compare', 'spec': 'fury', 'player_config_mode': 'manual_equipment',
            'player_equipment': '''warrior="Batcher"
spec=fury
talents=BASE
trinket1=,id=212048
### Gear from Bags
# Alternate trinket (650)
trinket1=,id=299001,ilevel=650
''',
        }
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            **base, 'kind': 'gear_candidates', 'category': 'trinket_candidates',
            'candidates': [{'slot': 'trinket1', 'item_id': 299001, 'source': 'bags'}],
        }), content_type='application/json')

        self.assertTrue(response.json()['success'], response.json())
        manifests = [json.loads(task.ext)['batch_compare'] for task in SimcTask.objects.order_by('id')]
        self.assertEqual({manifest['kind'] for manifest in manifests}, {'gear_candidates'})
        self.assertEqual({manifest['category'] for manifest in manifests}, {'trinket_candidates'})

    def test_batch_rejects_unsupported_source_and_oversized_candidate_selection(self):
        base = {'name': 'Manual candidate compare', 'spec': 'fury', 'player_config_mode': 'manual_equipment',
                'player_equipment': 'warrior="Batcher"\nspec=fury\ntalents=BASE\nhead=,id=212048'}
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({**base, 'kind': 'gear_candidates', 'candidates': [{'slot': 'head', 'item_id': 1, 'source': 'external'}]}), content_type='application/json')
        self.assertFalse(response.json()['success'])
        self.assertIn('来源', response.json()['error'])
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({**base, 'kind': 'gear_candidates', 'candidates': [{'slot': 'head', 'item_id': 200000 + i, 'source': 'bags'} for i in range(8)]}), content_type='application/json')
        self.assertFalse(response.json()['success'])
        self.assertIn('最多', response.json()['error'])


    def test_legacy_two_stat_scan_honors_50_rating_steps_and_keeps_baseline(self):
        monitor = SimcMonitor(None, None)
        points = monitor.build_attribute_test_points(total_value=4000, base_value=1700, requested_step=50)
        self.assertEqual(points[0], 0)
        self.assertEqual(points[-1], 4000)
        self.assertIn(1700, points)
        self.assertEqual(points, list(range(0, 4001, 50)))
        self.assertEqual(points, sorted(set(points)))

    def test_attribute_batch_report_returns_real_dps_rankings_path_and_local_optimum(self):
        batch_id = 'batch-attribute-report'
        base = {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}
        variants = SimcBatchTaskAPIView._attribute_variants(base, 50)
        reports = {}
        for index, (label, ratings, is_base, candidate) in enumerate(variants):
            dps = 100000 if is_base else 99900
            task = SimcTask.objects.create(
                user_id=self.user.id, name=f'attribute {label}', simc_profile_id=0,
                current_status=2, task_type=1, result_file=f'attribute_{index}.html',
                ext=json.dumps({
                    'player_config_mode': 'attribute_only', 'spec': 'fury', 'talent': 'BUILD',
                    **{f'gear_{stat}': ratings[stat] for stat in SimcBatchTaskAPIView.ATTRIBUTE_STATS},
                    'batch_compare': {
                        'version': 2, 'batch_id': batch_id, 'kind': 'attribute_variants',
                        'index': index, 'label': label, 'is_base': is_base, 'candidate': candidate,
                    },
                }),
            )
            reports[task.result_file] = f'<h2>Fury: {dps:,} dps</h2>'

        def result_content(_self, result_file):
            return reports.get(result_file)

        with patch.object(SimcRegularCompareAPIView, '_get_result_file_content', result_content):
            response = self.client.get('/api/simc-regular-compare/?batch_id=' + batch_id)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        report = payload['data']['attribute_report']
        self.assertEqual(report['algorithm'], 'four_stat_pairwise_hill_climb')
        self.assertEqual(report['step'], 50)
        self.assertEqual(report['total_rating'], 10000)
        self.assertEqual(report['rounds_completed'], 1)
        self.assertEqual(report['recommendation']['ratings'], base)
        self.assertEqual(report['stop_reason'], 'local_optimum_50_pairwise')
        self.assertEqual(len(report['candidates']), 13)
        self.assertEqual(report['candidates'][0]['dps'], 100000)
        self.assertTrue(all('result_file' not in row for row in report['candidates']))
        self.assertTrue(all('result_file' not in row for row in report['search_path']))

    def test_regular_candidate_batch_returns_only_safe_parsed_summary(self):
        batch_id = 'batch-regular-report'
        tasks = []
        for index, (label, dps) in enumerate((('基准配置', 1744), ('候选天赋', 1801))):
            tasks.append(SimcTask.objects.create(
                user_id=self.user.id, name=label, simc_profile_id=0,
                current_status=2, task_type=1, result_file=f'simc_task_{800 + index}.html',
                ext=json.dumps({'batch_compare': {
                    'version': 1, 'batch_id': batch_id, 'kind': 'talent_candidates',
                    'index': index, 'label': label, 'is_base': index == 0,
                    'candidate': {'type': 'base' if index == 0 else 'talent'},
                }}),
            ))
        reports = {
            task.result_file: f'<h2>Fury: {dps:,} dps</h2>'
            for task, (_, dps) in zip(tasks, (('基准配置', 1744), ('候选天赋', 1801)))
        }
        with patch.object(SimcRegularCompareAPIView, '_get_result_file_content', lambda _self, filename: reports.get(filename)):
            response = self.client.get('/api/simc-regular-compare/?batch_id=' + batch_id)

        payload = response.json()
        self.assertTrue(payload['success'], payload)
        rows = payload['data']['tasks']
        self.assertEqual([row['dps'] for row in rows], [1744, 1801])
        self.assertTrue(all(set(row) == {
            'id', 'name', 'label', 'rank', 'dps', 'delta_dps', 'delta_percent'
        } for row in rows))

    def test_database_batch_relation_is_authoritative_and_result_read_has_no_lifecycle_side_effect(self):
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Authoritative batch',
            batch_type='comparison', status=1,
            request_manifest=json.dumps({'version': 2}),
        )
        reports = {}
        for index, (label, dps) in enumerate((('基准配置', 100000), ('候选配置', 101000))):
            task = SimcTask.objects.create(
                user_id=self.user.id, name=label, simc_profile_id=0,
                current_status=2, task_type=1, result_file=f'authoritative_{index}.html',
                batch=batch, candidate_label=label,
                ext=json.dumps({'batch_compare': {
                    'version': 2, 'batch_id': str(batch.id), 'kind': 'talent_candidates',
                    'index': index, 'label': label, 'is_base': index == 0,
                }}),
            )
            reports[task.result_file] = f'<h2>Fury: {dps:,} dps</h2>'
        SimcTask.objects.create(
            user_id=self.user.id, name='伪造 legacy 同号任务', simc_profile_id=0,
            current_status=2, task_type=1, result_file='unrelated.html',
            ext=json.dumps({'batch_compare': {
                'version': 1, 'batch_id': str(batch.id), 'kind': 'talent_candidates',
                'index': 99, 'label': '不应混入', 'is_base': False,
            }}),
        )

        with patch.object(SimcRegularCompareAPIView, '_get_result_file_content', lambda _self, filename: reports.get(filename)):
            payload = self.client.get(f'/api/simc-regular-compare/?batch_id={batch.id}').json()

        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['data']['batch']['total'], 2)
        self.assertEqual([row['label'] for row in payload['data']['tasks']], ['基准配置', '候选配置'])
        batch.refresh_from_db()
        self.assertEqual(batch.status, 1)
        self.assertIsNone(batch.completed_at)

    def test_batch_compare_query_is_isolated_and_reports_pending_progress(self):
        batch_id, other_id = 'batch-isolated', 'batch-other'
        def create_task(name, bid, index, status=0):
            return SimcTask.objects.create(user_id=self.user.id, name=name, simc_profile_id=0, current_status=status, task_type=1, result_file='', ext=json.dumps({'batch_compare': {'version': 1, 'batch_id': bid, 'kind': 'attribute_variants', 'index': index, 'is_base': index == 0, 'label': name}}))
        create_task('baseline', batch_id, 0)
        create_task('crit +200', batch_id, 1, 1)
        create_task('unrelated', other_id, 0, 2)
        response = self.client.get('/api/simc-regular-compare/?batch_id=' + batch_id)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['data']['batch']['batch_id'], batch_id)
        self.assertEqual(payload['data']['batch']['total'], 2)
        self.assertEqual(payload['data']['batch']['pending'], 1)
        self.assertEqual(payload['data']['batch']['running'], 1)
        self.assertEqual(payload['data']['batch']['succeeded'], 0)

    def test_scan_real_batch_uses_fk_and_does_not_mix_forged_legacy_id(self):
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='real drain', batch_type='comparison', status=0,
        )
        real_tasks = [
            SimcTask.objects.create(
                user_id=self.user.id, name=f'real {index}', simc_profile_id=0,
                current_status=0, task_type=1, batch=batch,
                ext=json.dumps({'batch_compare': {
                    'version': 2, 'batch_id': str(batch.id), 'kind': 'talent_candidates',
                    'index': index, 'label': f'real {index}',
                }}),
            )
            for index in range(2)
        ]
        forged = SimcTask.objects.create(
            user_id=self.user.id, name='forged legacy', simc_profile_id=0,
            current_status=0, task_type=1,
            ext=json.dumps({'batch_compare': {
                'version': 1, 'batch_id': str(batch.id), 'kind': 'talent_candidates',
                'index': 99, 'label': 'forged',
            }}),
        )
        monitor = SimcMonitor(None, None)

        with patch.object(monitor, 'ensure_local_simc_backend_current', return_value=True), \
             patch('botend.controller.plugins.simc.SimcMonitor.os.path.exists', return_value=True), \
             patch('botend.controller.plugins.simc.SimcMonitor.os.path.isfile', return_value=True), \
             patch.object(monitor, 'process_simc_task', return_value=True) as process:
            self.assertTrue(monitor.scan())

        self.assertEqual([call.args[0].id for call in process.call_args_list], [task.id for task in real_tasks])
        self.assertNotIn(forged.id, [call.args[0].id for call in process.call_args_list])

    def test_scan_drains_all_pending_batch_candidates_in_one_dispatch(self):
        batch_id = 'batch-drain-all'
        tasks = [
            SimcTask.objects.create(
                user_id=self.user.id, name=f'candidate {index}', simc_profile_id=0,
                current_status=0, task_type=1, result_file='',
                ext=json.dumps({'batch_compare': {
                    'version': 1, 'batch_id': batch_id, 'kind': 'talent_candidates',
                    'index': index, 'label': f'candidate {index}', 'is_base': index == 0,
                }}),
            )
            for index in range(3)
        ]
        monitor = SimcMonitor(None, None)

        def finish(task):
            SimcTask.objects.filter(id=task.id).update(current_status=2, result_file=f'simc_task_{task.id}.html')
            return True

        with patch.object(monitor, 'ensure_local_simc_backend_current', return_value=True), \
             patch('botend.controller.plugins.simc.SimcMonitor.os.path.exists', return_value=True), \
             patch('botend.controller.plugins.simc.SimcMonitor.os.path.isfile', return_value=True), \
             patch.object(monitor, 'process_simc_task', side_effect=finish) as process:
            self.assertTrue(monitor.scan())

        self.assertEqual([call.args[0].id for call in process.call_args_list], [task.id for task in tasks])
        self.assertEqual(SimcTask.objects.filter(id__in=[task.id for task in tasks], current_status=2).count(), 3)

    def test_compare_response_ranks_candidates_against_explicit_baseline(self):
        batch_id = 'batch-ranked-summary'
        reports = {}
        for index, (label, dps) in enumerate((('基准配置', 100000), ('候选 A', 103000), ('候选 B', 99000))):
            task = SimcTask.objects.create(
                user_id=self.user.id, name=label, simc_profile_id=0,
                current_status=2, task_type=1, result_file=f'ranked_{index}.html',
                ext=json.dumps({'batch_compare': {
                    'version': 1, 'batch_id': batch_id, 'kind': 'talent_candidates',
                    'index': index, 'label': label, 'is_base': index == 0,
                }}),
            )
            reports[task.result_file] = f'<div class="player"><h2>Fury: {dps:,} dps</h2></div>'

        with patch.object(SimcRegularCompareAPIView, '_get_result_file_content', lambda _self, filename: reports.get(filename)):
            payload = self.client.get('/api/simc-regular-compare/?batch_id=' + batch_id).json()

        rows = payload['data']['tasks']
        self.assertEqual([(row['label'], row['rank']) for row in rows], [('基准配置', 2), ('候选 A', 1), ('候选 B', 3)])
        self.assertEqual(rows[0]['delta_dps'], 0)
        self.assertEqual(rows[0]['delta_percent'], 0.0)
        self.assertEqual(rows[1]['delta_dps'], 3000)
        self.assertEqual(rows[1]['delta_percent'], 3.0)
        self.assertEqual(rows[2]['delta_dps'], -1000)
        self.assertEqual(rows[2]['delta_percent'], -1.0)
        self.assertEqual(payload['data']['comparison']['baseline']['label'], '基准配置')
        self.assertEqual(payload['data']['comparison']['winner']['label'], '候选 A')


class SimcNewConfigModeTests(TestCase):
    """测试新版工作台任务配置：只输入玩家信息，战斗/APL 由选项控制。"""

    def setUp(self):
        self.user = User.objects.create_user(username='newmode_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)
        self.base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='{player_identity}\n{equipment}\n{action_list}\n{simulation_options}\n{stat_overrides}\n{output_options}',
            is_active=True,
        )
        self.default_apl = SimcApl.objects.create(
            name='Default APL',
            spec='warrior_fury',
            content='actions=/auto_attack\nactions+=/bloodthirst',
            source=SimcApl.SOURCE_USER,
            owner_user_id=self.user.id,
            is_active=True,
        )

    def test_attribute_manifest_task_routes_to_attribute_runner_without_profile_lookup(self):
        task = SimcTask.objects.create(
            user_id=self.user.id,
            name='Manifest attribute snapshot',
            task_type=2,
            simc_profile_id=0,
            ext=json.dumps({
                'player_config_mode': 'attribute_only',
                'spec': 'fury',
                'talent': 'SNAPSHOT_BUILD',
                'selected_attributes': 'crit_haste',
                'attribute_step': 50,
                'gear_strength': 0,
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
            }),
            current_status=0,
            is_active=True,
        )
        monitor = SimcMonitor(None, None)
        with patch.object(monitor, 'process_attribute_simulation', return_value=True) as attribute_runner, \
             patch.object(monitor, 'process_regular_simulation') as regular_runner:
            self.assertTrue(monitor.process_simc_task(task))

        attribute_runner.assert_called_once()
        self.assertIsNone(attribute_runner.call_args.args[1])
        regular_runner.assert_not_called()

    def test_direct_attribute_task_persists_full_manifest_snapshot(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Direct attribute snapshot',
                'task_type': 2,
                'player_import_mode': 'attribute_only',
                'player_equipment': 'warrior="Frozen"\nlevel=90\nspec=fury\ntalents=SNAPSHOT_BUILD\nhead=,id=212048\nmain_hand=,id=222222',
                'spec': 'fury',
                'talent': 'SNAPSHOT_BUILD',
                'selected_attributes': 'crit_haste',
                'attribute_step': 50,
                'gear_strength': 0,
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
                'fight_style': 'DungeonSlice',
                'time': 180,
                'target_count': 5,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        ext = json.loads(task.ext)
        self.assertEqual(ext['player_config_mode'], 'attribute_only')
        self.assertEqual(ext['spec'], 'fury')
        self.assertEqual(ext['talent'], 'SNAPSHOT_BUILD')
        self.assertEqual(ext['gear_strength'], 0)
        self.assertEqual(ext['gear_crit'], 1000)
        self.assertEqual(ext['gear_versatility'], 4000)
        self.assertEqual(ext['fight_style'], 'DungeonSlice')
        self.assertEqual(ext['time'], 180)
        self.assertEqual(ext['target_count'], 5)

    def test_task_list_does_not_expose_raw_simc_code(self):
        task = SimcTask.objects.create(
            user_id=self.user.id,
            name='private raw code',
            task_type=1,
            simc_profile_id=0,
            ext=json.dumps({'raw_simc_code': 'warrior="secret"\nspec=fury\n', 'spec': 'fury'}),
        )

        response = self.client.get('/api/simc-task/')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        listed = next(row for row in payload['data'] if row['id'] == task.id)
        self.assertNotIn('ext', listed)
        self.assertNotIn('raw_simc_code', listed['ext_detail'])
        self.assertNotIn('secret', json.dumps(payload, ensure_ascii=False))

    def test_task_create_response_does_not_expose_raw_simc_code(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'new private raw code',
                'task_type': 1,
                'simc_profile_id': 0,
                'raw_simc_code': 'warrior="create-secret"\nspec=fury\n',
                'regular_time': 300,
                'regular_target_count': 1,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertNotIn('ext', payload['data'])
        self.assertNotIn('create-secret', json.dumps(payload, ensure_ascii=False))

    def test_task_management_uses_new_inline_safe_history_ui(self):
        main_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        workbench_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/simc-workbench.js').read_text(encoding='utf-8')
        self.assertNotIn('function displaySimcTaskData(tasks)', main_js)
        self.assertNotIn('function openViewSimcTaskModal(task)', main_js)
        self.assertIn('async function showTaskDetail(resource, id)', workbench_js)
        self.assertIn("window.openSimcWorkbenchDialog('task-detail'", workbench_js)
        self.assertIn("document.getElementById('simc-dialog-body')", workbench_js)
        self.assertNotIn('raw_simc_code', workbench_js)
        self.assertNotIn('candidate_reason', workbench_js)
        self.assertNotIn('preprocess_reasoning', workbench_js)
        self.assertNotIn('simc_error_native', workbench_js)

    def test_task_ext_summary_drops_raw_simc_code_from_browser_response(self):
        summary = SimcTaskAPIView()._task_ext_summary(1, json.dumps({
            'raw_simc_code': 'warrior="secret"\nspec=fury\n',
            'metadata': {'raw_simc_code': 'nested-secret'},
            'player_equipment': 'warrior="equipment-secret"',
            'override_action_list': 'actions=secret_action',
            'spec': 'fury',
            'time': 300,
        }))

        self.assertNotIn('raw_simc_code', summary)
        self.assertNotIn('metadata', summary)
        self.assertNotIn('player_equipment', summary)
        self.assertNotIn('override_action_list', summary)
        self.assertNotIn('simc_error_native', summary)
        self.assertNotIn('secret', json.dumps(summary, ensure_ascii=False))
        self.assertEqual(summary['spec'], 'fury')
        self.assertEqual(summary['time'], 300)

    def test_task_ext_summary_keeps_safe_apl_context_without_apl_source(self):
        summary = SimcTaskAPIView()._task_ext_summary(1, json.dumps({
            'selected_apl_id': 42,
            'apl_compare': {
                'batch_id': 'batch-42',
                'candidate_index': 2,
                'candidate_name': '候选方案2',
                'candidate_reason': '交换技能优先级',
                'is_base': False,
                'preprocess_stage': 'ready',
                'preprocess_error': '无',
                'preprocess_reasoning': '包含不应进入浏览器的推理全文',
                'apl_list': 'actions=/secret_action',
            },
        }))

        self.assertEqual(summary['selected_apl_id'], 42)
        self.assertEqual(summary['apl_compare'], {
            'batch_id': 'batch-42',
            'candidate_index': 2,
            'is_base': False,
            'preprocess_stage': 'ready',
        })
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn('candidate_name', serialized)
        self.assertNotIn('candidate_reason', serialized)
        self.assertNotIn('preprocess_error', serialized)
        self.assertNotIn('preprocess_reasoning', serialized)
        self.assertNotIn('secret_action', serialized)

    def test_task_list_hides_failed_native_result_output(self):
        task = SimcTask.objects.create(
            user_id=self.user.id,
            name='failed raw task',
            task_type=1,
            simc_profile_id=0,
            current_status=3,
            result_file='SimC执行失败\\n错误输出: warrior="result-secret"',
        )

        response = self.client.get('/api/simc-task/')
        self.assertEqual(response.status_code, 200)
        listed = next(row for row in response.json()['data'] if row['id'] == task.id)
        self.assertEqual(listed['result_file'], '')
        self.assertNotIn('result-secret', json.dumps(response.json(), ensure_ascii=False))

    def test_attribute_analysis_ssr_parses_task_owned_attribute_report(self):
        task = SimcTask.objects.create(
            user_id=self.user.id,
            name='SSR attribute report',
            task_type=2,
            simc_profile_id=0,
            current_status=2,
            result_file='77_gear_crit_850_gear_haste_979.html',
        )
        result_file = f'{task.id}_gear_crit_850_gear_haste_979.html'
        task.result_file = result_file
        task.save(update_fields=['result_file'])
        response_mock = SimpleNamespace(status_code=200, text='Bloodmastêr: 123,456 dps')

        with patch('botend.dashboard.dashboard.settings.OSS_CONFIG', {'base_url': 'https://oss.example/'}, create=True), \
             patch('requests.get', return_value=response_mock):
            response = self.client.get(f'/simc-attribute-analysis-ssr/?task_id={task.id}')

        self.assertEqual(response.status_code, 200, response.content.decode())
        self.assertContains(response, '123456')
        self.assertContains(response, 'gear_crit')

    def test_attribute_analysis_api_parses_only_current_task_owned_reports(self):
        task = SimcTask.objects.create(
            user_id=self.user.id,
            name='API attribute report',
            task_type=2,
            simc_profile_id=0,
            current_status=2,
        )
        owned_file = f'{task.id}_gear_crit_850_gear_haste_979.html'
        foreign_file = f'{task.id + 1}_gear_crit_900_gear_haste_929.html'
        task.result_file = f'{owned_file},{foreign_file},legacy_crit_1_haste_2.html'
        task.save(update_fields=['result_file'])
        response_mock = SimpleNamespace(status_code=200, text='Bloodmastêr: 123,456 dps')

        with patch('botend.dashboard.api.settings.OSS_CONFIG', {'base_url': 'https://oss.example/'}, create=True), \
             patch('requests.get', return_value=response_mock):
            response = self.client.get(f'/api/simc-attribute-analysis/?task_id={task.id}')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(len(payload['data']['results']), 1)
        self.assertEqual(payload['data']['results'][0]['file_name'], owned_file)
        self.assertEqual(payload['data']['results'][0]['attr1_name'], 'gear_crit')
        self.assertEqual(payload['data']['results'][0]['attr1_value'], 850)
        self.assertEqual(payload['data']['results'][0]['dps'], 123456)

    def test_preview_returns_only_current_users_manifest_snapshot(self):
        task = SimcTask.objects.create(
            user_id=self.user.id,
            name='Preview manifest task',
            task_type=1,
            simc_profile_id=0,
            current_status=2,
            result_file='preview-task.html',
            ext=json.dumps({
                'player_config_mode': 'battlenet',
                'battlenet_region': 'eu',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr',
                'spec': 'blood',
                'fight_style': 'Patchwerk',
                'time': 300,
                'target_count': 1,
                'gear_strength': 0,
                'gear_crit': 850,
                'gear_haste': 979,
                'gear_mastery': 641,
                'gear_versatility': 69,
            }),
        )

        response = self.client.get(f'/api/simc-task/preview/?task_id={task.id}')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['data']['id'], task.id)
        self.assertEqual(payload['data']['spec'], 'blood')
        self.assertEqual(payload['data']['gear']['strength'], 0)
        self.assertEqual(payload['data']['gear']['haste'], 979)
        self.assertNotIn('raw_simc_code', payload['data'])

        other = User.objects.create_user(username='preview_other_user', password='pwd')
        self.client.force_login(other)
        forbidden = self.client.get(f'/api/simc-task/preview/?task_id={task.id}')
        self.assertEqual(forbidden.status_code, 200)
        self.assertFalse(forbidden.json()['success'])
        self.assertIn('无权限', forbidden.json()['error'])

    def test_task_detail_is_inline_and_old_modal_is_removed(self):
        main_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        workbench_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/simc-workbench.js').read_text(encoding='utf-8')
        self.assertNotIn('function openViewSimcTaskModal(task)', main_js)
        self.assertIn('async function showTaskDetail(resource, id)', workbench_js)
        self.assertIn("host.classList.remove('hidden')", workbench_js)
        self.assertNotIn('modal.style.display', workbench_js)

    def test_dashboard_sections_stay_inside_main_content(self):
        from bs4 import BeautifulSoup

        template = (Path(__file__).resolve().parents[2] / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        soup = BeautifulSoup(template, 'html.parser')
        main_content = soup.select_one('.main-content')

        self.assertIsNotNone(main_content)
        for section_id in ('dashboard-home', 'simc-workbench', 'tools', 'database-tables'):
            section = soup.select_one(f'#{section_id}')
            self.assertIsNotNone(section, section_id)
            self.assertIs(section.parent, main_content, section_id)

    def test_simc_workbench_panels_are_grouped_by_l1_information_architecture(self):
        from bs4 import BeautifulSoup

        template = (Path(__file__).resolve().parents[2] / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        soup = BeautifulSoup(template, 'html.parser')
        expected_groups = {
            'simc-l1-workflow-panel': (
                'simc-workbench-import-panel', 'simc-workbench-profiles-panel',
                'simc-workbench-templates-panel', 'simc-workbench-apl-panel',
            ),
            'simc-l1-history-panel': ('simc-workbench-tasks-panel',),
            'simc-l1-advanced-panel': (
                'simc-workbench-backend-panel', 'simc-workbench-rules-panel',
            ),
        }
        for group_id, panel_ids in expected_groups.items():
            group = soup.select_one(f'#{group_id}')
            self.assertIsNotNone(group, group_id)
            for panel_id in panel_ids:
                panel = soup.select_one(f'#{panel_id}')
                self.assertIsNotNone(panel, panel_id)
                self.assertIn(group, panel.parents, panel_id)

    def test_template_list_uses_dedicated_api_preview_and_inline_detail(self):
        main_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        workbench_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/simc-workbench.js').read_text(encoding='utf-8')
        self.assertNotIn('function displayTemplateList(templates)', main_js)
        self.assertIn('async function loadTemplates()', workbench_js)
        self.assertIn('async function showTemplateDetail(id)', workbench_js)
        self.assertIn('row.preview', workbench_js)
        self.assertNotIn('row.content', workbench_js[workbench_js.index('async function loadTemplates()'):workbench_js.index('function renderTemplateForm')])

    def test_task_history_uses_shared_safe_dialog_instead_of_raw_config_modal(self):
        template = (Path(__file__).resolve().parents[2] / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        main_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        workbench_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/simc-workbench.js').read_text(encoding='utf-8')

        self.assertNotIn('id="simc-wb-task-detail"', template)
        self.assertIn('id="simc-workbench-dialog"', template)
        self.assertIn("openSimcWorkbenchDialog('task-detail'", workbench_js)
        self.assertNotIn('查看SimC代码', template)
        self.assertNotIn('生成的SimC代码', template)
        self.assertNotIn('copy-simc-code', template)
        self.assertNotIn('view-simc-task-code', main_js)
        self.assertIn('async function showTaskDetail(resource, id)', workbench_js)
        self.assertIn('状态：', workbench_js)
        self.assertIn('更新时间：', workbench_js)
        self.assertNotIn('final_simc_content', workbench_js)

    def test_final_execution_config_validation_summarizes_rendered_simc_without_raw_content(self):
        rendered = '''warrior="AuditActor"
spec=fury
talents=SECRET_BUILD
head=,id=123
actions=auto_attack
actions+=/bloodthirst
html=simc_task_99.html
'''

        summary = SimcMonitor.build_final_config_validation(rendered)

        self.assertEqual(summary['actor_count'], 1)
        self.assertEqual(summary['spec_count'], 1)
        self.assertEqual(summary['talents_count'], 1)
        self.assertEqual(summary['equipment_count'], 1)
        self.assertEqual(summary['action_count'], 2)
        self.assertEqual(summary['html_output_count'], 1)
        self.assertEqual(summary['placeholder_count'], 0)
        self.assertEqual(len(summary['sha256']), 64)
        self.assertNotIn('SECRET_BUILD', json.dumps(summary))

    def test_worker_persists_final_execution_validation_in_task_manifest(self):
        task = SimcTask.objects.create(
            user_id=self.user.id,
            name='Worker audit',
            simc_profile_id=0,
            task_type=1,
            current_status=1,
            ext=json.dumps({'spec': 'fury', 'override_action_list': 'actions=SECRET'}),
        )

        SimcMonitor.persist_final_config_validation(task, 'warrior="A"\nspec=fury\nactions=auto_attack\nhtml=x.html')

        task.refresh_from_db()
        manifest = json.loads(task.ext)
        self.assertEqual(manifest['final_config_validation']['actor_count'], 1)
        self.assertNotIn('SECRET', json.dumps(manifest['final_config_validation']))
        self.assertEqual(manifest['override_action_list'], 'actions=SECRET')

    def test_task_preview_returns_persisted_final_execution_validation(self):
        validation = {
            'char_count': 12000, 'line_count': 280, 'sha256': 'a' * 64,
            'actor_count': 1, 'spec_count': 1, 'talents_count': 1,
            'equipment_count': 14, 'action_count': 112,
            'html_output_count': 1, 'placeholder_count': 0,
        }
        task = SimcTask.objects.create(
            user_id=self.user.id,
            name='Validated task',
            simc_profile_id=0,
            task_type=1,
            current_status=2,
            ext=json.dumps({'spec': 'fury', 'final_config_validation': validation}),
        )

        response = self.client.get(f'/api/simc-task/preview/?task_id={task.id}')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['final_config_validation'], validation)

    def test_rerun_creates_pending_task_without_mutating_completed_manifest_task(self):
        manifest = {
            'player_config_mode': 'manual_equipment',
            'spec': 'fury',
            'player_equipment': 'warrior="Snapshot"\nspec=fury\nhead=,id=212048',
            'fight_style': 'Patchwerk',
            'time': 300,
            'target_count': 1,
        }
        original = SimcTask.objects.create(
            user_id=self.user.id,
            name='Completed frozen snapshot',
            simc_profile_id=0,
            task_type=1,
            current_status=2,
            result_file='simc_task_completed.html',
            ext=json.dumps(manifest),
        )

        response = self.client.patch(
            '/api/simc-task/',
            data=json.dumps({'id': original.id, 'action': 'rerun'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        rerun_id = payload['data']['id']
        self.assertNotEqual(rerun_id, original.id)
        self.assertEqual(SimcTask.objects.count(), 2)

        original.refresh_from_db()
        self.assertEqual(original.current_status, 2)
        self.assertEqual(original.result_file, 'simc_task_completed.html')
        self.assertEqual(json.loads(original.ext), manifest)

        rerun = SimcTask.objects.get(id=rerun_id)
        self.assertEqual(rerun.current_status, 0)
        self.assertEqual(rerun.result_file, '')
        self.assertEqual(json.loads(rerun.ext), manifest)

    def test_direct_attribute_task_rejects_non_50_step(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Bad direct attribute step',
                'task_type': 2,
                'player_import_mode': 'attribute_only',
                'player_equipment': 'warrior="Frozen"\nlevel=90\nspec=fury\ntalents=SNAPSHOT_BUILD\nhead=,id=212048\nmain_hand=,id=222222',
                'spec': 'fury',
                'talent': 'SNAPSHOT_BUILD',
                'selected_attributes': 'crit_haste',
                'attribute_step': 25,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'], payload)
        self.assertIn('50', payload['error'])

    def test_attribute_render_uses_manifest_snapshot_instead_of_changed_profile(self):
        monitor = SimcMonitor(None, None)
        monitor.select_template_by_spec = lambda spec: SimpleNamespace(
            content='warrior="LMonitor"\\nspec={spec}\\n{player_config}\\nhtml={result_file}'
        )
        profile = SimpleNamespace(
            spec='arms', talent='CHANGED_PROFILE_BUILD', player_config_mode='attribute_only',
            player_import_mode='attribute_only', player_equipment='changed=1',
            battlenet_region='us', battlenet_realm='Changed', battlenet_character='Changed',
        )
        rendered = monitor.generate_attribute_simc_code(profile, {
            'gear_strength': 0, 'gear_crit': 1000, 'gear_haste': 2000,
            'gear_mastery': 3000, 'gear_versatility': 4000,
        }, '77_gear_crit_1000_gear_haste_2000.html', {
            'player_config_mode': 'attribute_only', 'spec': 'fury',
            'player_equipment': 'warrior="Snapshot"\nlevel=90\nspec=fury\ntalents=OLD\nhead=,id=212048\nmain_hand=,id=222222',
            'talent': 'SNAPSHOT_BUILD', 'gear_strength': 0,
        })

        self.assertIn('spec=fury', rendered)
        self.assertIn('talents=SNAPSHOT_BUILD', rendered)
        self.assertNotIn('CHANGED_PROFILE_BUILD', rendered)
        self.assertNotIn('spec=arms', rendered)
        self.assertIn('gear_strength=0', rendered)

    def test_create_task_with_manual_equipment_mode(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Fury Manual Equipment',
                'task_type': 1,
                'player_import_mode': 'manual_equipment',
                'player_equipment': 'talents=TEST\nhead=,id=212048',
                'fight_style': 'Patchwerk',
                'time': 300,
                'target_count': 1,
                'spec': 'fury',
                'talent': 'TEST',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        self.assertTrue(task.result_file.endswith('.html'))
        self.assertIn(f'html={task.result_file}', task.final_simc_content)
        ext = json.loads(task.ext)
        self.assertEqual(ext['player_config_mode'], 'manual_equipment')
        self.assertEqual(ext['player_import_mode'], 'manual_equipment')
        self.assertEqual(ext['player_equipment'], 'talents=TEST\nhead=,id=212048')
        self.assertEqual(ext['fight_style'], 'Patchwerk')
        self.assertEqual(ext['time'], 300)
        self.assertEqual(ext['target_count'], 1)

    def test_create_task_with_dungeon_preset_values_persists_exact_combat_combination(self):
        """战斗组合预设只是前端预填，任务端必须按选择后的精确值固化。"""
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Fury DungeonSlice 300s 5目标',
                'task_type': 1,
                'player_import_mode': 'attribute_only',
                'player_equipment': 'warrior="Frozen"\nlevel=90\nspec=fury\ntalents=DUNGEON_BUILD\nhead=,id=212048\nmain_hand=,id=222222',
                'spec': 'fury',
                'talent': 'DUNGEON_BUILD',
                'gear_crit': 400,
                'gear_haste': 1100,
                'gear_mastery': 1140,
                'gear_versatility': 100,
                'fight_style': 'DungeonSlice',
                'time': 300,
                'target_count': 5,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        ext = json.loads(task.ext)
        self.assertEqual(ext['fight_style'], 'DungeonSlice')
        self.assertEqual(ext['time'], 300)
        self.assertEqual(ext['target_count'], 5)
        self.assertEqual(ext['spec'], 'fury')
        self.assertEqual(ext['player_config_mode'], 'attribute_only')

    def test_create_task_with_legacy_equipment_alias_maps_to_manual_equipment(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Legacy Equipment Alias',
                'task_type': 1,
                'player_config_mode': 'equipment',
                'player_equipment': 'talents=TEST\nneck=,id=224433',
                'spec': 'fury',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        ext = json.loads(task.ext)
        self.assertEqual(ext['player_config_mode'], 'manual_equipment')
        self.assertEqual(ext['player_import_mode'], 'manual_equipment')

    @patch('botend.dashboard.api.fetch_battlenet_character_preflight')
    def test_create_task_with_battlenet_mode(self, preflight):
        preflight.return_value = {
            'identity': {'class_name': 'warrior', 'level': 80},
            'spec': {'key': 'fury'},
            'simc_ready': True,
            'warnings': [],
        }
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Fury Battle.net Import',
                'task_type': 1,
                'player_import_mode': 'battlenet',
                'battlenet_region': 'EU',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr',
                'fight_style': 'Patchwerk',
                'time': 300,
                'target_count': 1,
                'spec': 'fury',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        task = SimcTask.objects.get(id=payload['data']['id'])
        self.assertTrue(task.result_file.endswith('.html'))
        self.assertIn(f'html={task.result_file}', task.final_simc_content)
        self.assertIn('armory=eu,Kazzak,Bloodmastêr', task.final_simc_content)
        self.assertNotIn('warrior="Bloodmastêr"', task.final_simc_content)
        ext = json.loads(task.ext)
        self.assertEqual(ext['player_config_mode'], 'battlenet')
        self.assertEqual(ext['player_import_mode'], 'battlenet')
        self.assertEqual(ext['battlenet_region'], 'eu')
        self.assertEqual(ext['battlenet_realm'], 'Kazzak')
        self.assertEqual(ext['battlenet_character'], 'Bloodmastêr')

    def test_manual_equipment_requires_player_block(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'No Equipment',
                'task_type': 1,
                'player_import_mode': 'manual_equipment',
                'player_equipment': '',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('玩家装备配置不能为空', payload['error'])

    def test_battlenet_requires_region_realm_character(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Bad Battlenet',
                'task_type': 1,
                'player_import_mode': 'battlenet',
                'battlenet_region': 'eu',
                'battlenet_realm': '',
                'battlenet_character': 'Bloodmastêr',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('Battle.net 导入需要提供', payload['error'])

    def test_stats_mode_is_rejected_in_new_workbench(self):
        response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Stats Not Allowed',
                'task_type': 1,
                'player_config_mode': 'stats',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload['success'])
        self.assertIn('玩家信息导入方式必须是', payload['error'])

    def test_apply_template_builds_battlenet_armory_player_block(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        monitor = object.__new__(SimcMonitor)
        rendered = monitor.apply_template(
            'deathknight="LMonitor_Base"\nspec={spec}\nfight_style={fight_style}\n{player_config}\n{action_list}',
            {
                'fight_style': 'Patchwerk',
                'player_import_mode': 'battlenet',
                'battlenet_region': 'eu',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr',
                'spec': 'fury',
                'override_action_list': 'actions=auto_attack',
            },
        )
        self.assertNotIn('Bloodmast_r', rendered)
        self.assertNotIn('deathknight="LMonitor_Base"', rendered)
        self.assertNotIn('\nspec=fury', rendered)
        self.assertIn('armory=eu,Kazzak,Bloodmastêr', rendered)
        self.assertIn('actions=auto_attack', rendered)

    def test_apply_template_battlenet_does_not_override_imported_player(self):
        monitor = object.__new__(SimcMonitor)
        template = '\n'.join([
            'deathknight="LMonitor_Base"',
            'source=default',
            'spec={spec}',
            'level=80',
            'race=mechagnome',
            'role=attack',
            'position=back',
            'fight_style={fight_style}',
            'max_time={time}',
            'desired_targets={target_count}',
            'talents={talent}',
            'potion=tempered_potion_3',
            'gear_crit_rating={gear_crit}',
            '{player_config}',
            '{action_list}',
        ])

        rendered = monitor.apply_template(template, {
            'player_import_mode': 'battlenet',
            'battlenet_region': 'eu',
            'battlenet_realm': 'Kazzak',
            'battlenet_character': 'Bloodmastêr',
            'spec': 'blood',
            'fight_style': 'Patchwerk',
            'time': 300,
            'target_count': 1,
            'override_action_list': 'actions=auto_attack',
        })

        self.assertIn('armory=eu,Kazzak,Bloodmastêr', rendered)
        self.assertIn('fight_style=Patchwerk', rendered)
        self.assertIn('max_time=300', rendered)
        self.assertIn('desired_targets=1', rendered)
        self.assertIn('actions=auto_attack', rendered)
        for player_option in (
            'deathknight=', 'source=', 'spec=', 'level=', 'race=', 'role=',
            'position=', 'talents=', 'potion=', 'gear_crit_rating=',
        ):
            self.assertNotIn(player_option, rendered)

    def test_apply_template_manual_equipment_replaces_template_actor_instead_of_creating_two_players(self):
        monitor = object.__new__(SimcMonitor)
        template = 'warrior="LMonitor_Base"\nspec=fury\ntalents=TEMPLATE\n{player_config}\n{action_list}'
        player = 'warrior="Real_Player"\nspec=fury\ntalents=CANDIDATE\nhead=,id=212048'
        rendered = monitor.apply_template(template, {
            'spec': 'fury', 'talent': 'CANDIDATE',
            'player_import_mode': 'manual_equipment',
            'player_equipment': player,
            'override_action_list': 'actions=auto_attack',
        })
        self.assertNotIn('warrior="LMonitor_Base"', rendered)
        self.assertEqual(rendered.count('warrior="Real_Player"'), 1)
        self.assertEqual(rendered.count('\nspec=fury'), 1)
        self.assertEqual(rendered.count('\ntalents=CANDIDATE'), 1)
        self.assertNotIn('talents=TEMPLATE', rendered)

    def test_standard_raid_buff_migration_updates_all_base_templates(self):
        self.default_apl.is_active = False
        self.default_apl.save()
        migration = importlib.import_module(
            'botend.migrations.0103_enable_standard_simc_raid_buffs'
        )
        first = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='default',
            content='fight_style=Patchwerk\noptimal_raid=0\n{player_config}',
        )
        second = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='fury',
            content='optimal_raid=0\noverride.battle_shout=1',
        )
        apl = SimcApl.objects.create(
            name='Migration APL',
            spec='warrior_fury',
            content='# optimal_raid=0 must not alter APL content',
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
        )
        historical_model = SimpleNamespace(objects=SimcContentTemplate.objects)
        apps = SimpleNamespace(get_model=lambda *args: historical_model)

        migration.enable_standard_raid_buffs(apps, None)

        first.refresh_from_db()
        second.refresh_from_db()
        apl.refresh_from_db()
        self.assertIn('optimal_raid=1', first.content)
        self.assertIn('optimal_raid=1', second.content)
        self.assertNotIn('optimal_raid=0', first.content)
        self.assertEqual(apl.content, '# optimal_raid=0 must not alter APL content')

    def test_apply_template_manual_equipment_preserves_template_runtime_options(self):
        monitor = object.__new__(SimcMonitor)
        template = '\n'.join([
            'warrior="LMonitor_Base"',
            'spec={spec}',
            'fight_style={fight_style}',
            'max_time={time}',
            'desired_targets={target_count}',
            'optimal_raid=0',
            'override.battle_shout=1',
            'potion=tempered_potion_3',
            'shoulders=TEMPLATE_SHOULDERS,id=1',
            'wrists=TEMPLATE_WRISTS,id=2',
            '{player_config}',
            '{action_list}',
        ])
        player = '\n'.join([
            'warrior="Real_Player"',
            'spec=fury',
            'talents=CANDIDATE',
            'head=,id=212048',
            'shoulders=,id=212050',
            'wrists=,id=211999',
            'main_hand=,id=224638',
        ])

        rendered = monitor.apply_template(template, {
            'spec': 'fury', 'talent': 'CANDIDATE',
            'fight_style': 'Patchwerk', 'time': 300, 'target_count': 1,
            'player_import_mode': 'manual_equipment',
            'player_equipment': player,
            'override_action_list': 'actions=auto_attack',
        })

        self.assertEqual(rendered.count('warrior="Real_Player"'), 1)
        self.assertNotIn('warrior="LMonitor_Base"', rendered)
        self.assertIn('fight_style=Patchwerk', rendered)
        self.assertIn('max_time=300', rendered)
        self.assertIn('desired_targets=1', rendered)
        self.assertIn('optimal_raid=0', rendered)
        self.assertIn('override.battle_shout=1', rendered)
        self.assertIn('potion=tempered_potion_3', rendered)
        self.assertNotIn('TEMPLATE_SHOULDERS', rendered)
        self.assertNotIn('TEMPLATE_WRISTS', rendered)
        self.assertEqual(rendered.count('shoulders=,id=212050'), 1)
        self.assertEqual(rendered.count('wrists=,id=211999'), 1)

    def test_apply_template_manual_equipment_removes_template_player_fields_after_placeholder(self):
        monitor = object.__new__(SimcMonitor)
        template = '\n'.join([
            '{player_config}',
            'spec={spec}',
            'talents=TEMPLATE',
            'shoulders=TEMPLATE_SHOULDERS',
            'fight_style={fight_style}',
            '{action_list}',
            'html={result_file}',
        ])
        rendered = monitor.apply_template(template, {
            'spec': 'fury',
            'fight_style': 'Patchwerk',
            'player_import_mode': 'manual_equipment',
            'player_equipment': '\n'.join([
                'warrior="Real_Player"',
                'spec=fury',
                'talents=CANDIDATE',
                'shoulders=,id=212050',
            ]),
            'override_action_list': 'actions=auto_attack',
            'result_file': 'simc_task_101.html',
        })
        self.assertNotIn('talents=TEMPLATE', rendered)
        self.assertNotIn('TEMPLATE_SHOULDERS', rendered)
        self.assertEqual(rendered.count('\nspec=fury'), 1)
        self.assertIn('fight_style=Patchwerk', rendered)

    def test_apply_template_manual_equipment_requires_exactly_one_player_placeholder(self):
        monitor = object.__new__(SimcMonitor)
        config = {
            'spec': 'fury',
            'player_import_mode': 'manual_equipment',
            'player_equipment': 'warrior="Real_Player"\nspec=fury\ntalents=CANDIDATE',
            'override_action_list': 'actions=auto_attack',
        }

        with self.assertRaisesRegex(ValueError, 'player_config.*恰好一个'):
            monitor.apply_template('warrior="Template"\nspec={spec}\n{action_list}', config)
        with self.assertRaisesRegex(ValueError, 'player_config.*恰好一个'):
            monitor.apply_template('{player_config}\n{player_config}\n{action_list}', config)

    def test_apply_template_inserts_manual_equipment_player_block(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        monitor = object.__new__(SimcMonitor)
        rendered = monitor.apply_template(
            'fight_style={fight_style}\n{player_config}\n{action_list}',
            {
                'fight_style': 'Patchwerk',
                'player_import_mode': 'manual_equipment',
                'player_equipment': 'talents=TEST\nhead=,id=212048',
                'override_action_list': 'actions=auto_attack',
            },
        )
        self.assertIn('talents=TEST', rendered)
        self.assertIn('head=,id=212048', rendered)
        self.assertIn('actions=auto_attack', rendered)
    def test_apply_template_manual_equipment_truncates_exported_alternative_sections(self):
        monitor = object.__new__(SimcMonitor)
        rendered = monitor.apply_template(
            'fight_style=Patchwerk\n{player_config}\n{action_list}',
            {
                'player_import_mode': 'manual_equipment',
                'player_equipment': 'warrior="Real"\nlevel=90\nspec=fury\nhead=,id=212048\nmain_hand=,id=222222\n### Gear from Bags\nhead=,id=299001',
                'override_action_list': 'actions=auto_attack',
            },
        )
        self.assertIn('head=,id=212048', rendered)
        self.assertNotIn('299001', rendered)

    def test_apply_template_inserts_attribute_only_frozen_player_and_rating_overrides(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        monitor = object.__new__(SimcMonitor)
        rendered = monitor.apply_template(
            'spec={spec}\n{player_config}\n{gear_crit}\n{gear_haste}\n{gear_mastery}\n{gear_versatility}\n{action_list}',
            {
                'spec': 'fury',
                'player_config_mode': 'attribute_only',
                'player_equipment': 'warrior="Frozen"\nlevel=90\nspec=fury\ntalents=OLD\nhead=,id=212048\nmain_hand=,id=222222',
                'talent': 'ATTRIBUTE_BUILD',
                'gear_strength': 5000,
                'gear_crit': 1000,
                'gear_haste': 2000,
                'gear_mastery': 3000,
                'gear_versatility': 4000,
                'override_action_list': 'actions=auto_attack',
            },
        )
        self.assertIn('talents=ATTRIBUTE_BUILD', rendered)
        self.assertIn('gear_strength=5000', rendered)
        self.assertIn('crit_rating=1000', rendered)
        self.assertIn('haste_rating=2000', rendered)
        self.assertIn('mastery_rating=3000', rendered)
        self.assertIn('versatility_rating=4000', rendered)
        self.assertNotIn('{gear_', rendered)
        self.assertNotIn('armory=', rendered)
        self.assertIn('head=,id=212048', rendered)
        self.assertIn('actions=auto_attack', rendered)


class SimcPlayerConfigDetailTests(TestCase):
    """玩家详情只解析当前输入与本地快照，不渲染完整 SimC 执行配置。"""

    def setUp(self):
        self.user = User.objects.create_user(username='player_detail_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)
        self.base_template = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_USER,
            spec='warrior_fury',
            content='{player_identity}\n{equipment}\n{action_list}\n{simulation_options}\n{stat_overrides}\n{output_options}',
            is_active=True,
        )
        self.default_apl = SimcApl.objects.create(
            name='Player Detail APL',
            spec='warrior_fury',
            content='actions=/auto_attack\nactions+=/bloodthirst',
            source=SimcApl.SOURCE_USER,
            owner_user_id=self.user.id,
            is_active=True,
        )

    def test_player_config_detail_returns_structured_manual_player_detail_with_items_and_stats(self):
        WowItemSnapshot.objects.create(item_id=212048, name='Helm of Tests', name_zh='测试头盔', icon='inv_helmet_01')
        WowItemSnapshot.objects.create(item_id=71543, name='Swift Enchant', name_zh='迅捷附魔')
        WowItemSnapshot.objects.create(item_id=213479, name='Test Gem', name_zh='测试宝石')
        from botend.models import SimcSecondaryStatRule
        SimcSecondaryStatRule.objects.update_or_create(
            class_name='warrior',
            defaults={
                'crit_per_percent': 46, 'haste_per_percent': 44,
                'mastery_per_percent': 46, 'versatility_per_percent': 54,
            },
        )
        response = self.client.post(
            '/api/simc-player-config-detail/',
            data=json.dumps({
                'spec': 'fury',
                'player_config_mode': 'manual_equipment',
                'player_equipment': '\n'.join([
                    'warrior="Previewer"',
                    'level=80',
                    'race=orc',
                    'region=cn',
                    'server=死亡之翼',
                    'spec=fury',
                    'talents=BUILDCODE',
                    'head=,id=212048,ilevel=639,enchant_id=71543,gems=213479/213480',
                    'main_hand=,id=224638,ilevel=646',
                    'crit_rating=10730',
                    'haste_rating=18641',
                    'mastery_rating=21785',
                    'versatility_rating=6757',
                ]),
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        detail = payload['data']
        self.assertEqual(detail['source']['type'], 'manual_equipment')
        self.assertEqual(detail['identity']['name'], 'Previewer')
        self.assertEqual(detail['identity']['race'], 'orc')
        self.assertEqual(detail['identity']['region'], 'cn')
        self.assertEqual(detail['identity']['realm'], '死亡之翼')
        self.assertEqual(detail['talents']['build_code'], 'BUILDCODE')
        self.assertEqual(detail['equipment'][0]['slot'], 'head')
        self.assertEqual(detail['equipment'][0]['display_name'], '测试头盔')
        self.assertEqual(detail['equipment'][0]['item_level'], 639)
        self.assertEqual(detail['equipment'][0]['enchant']['display_name'], '迅捷附魔')
        self.assertEqual(detail['equipment'][0]['gems'][0]['display_name'], '测试宝石')
        self.assertEqual(detail['stats']['secondary']['crit']['rating'], 10730)
        self.assertAlmostEqual(detail['stats']['secondary']['crit']['percent'], 233.26, places=2)
        self.assertEqual(SimcTask.objects.count(), 0)

    def test_player_config_detail_exposes_only_parsed_comparison_candidates(self):
        player_block = '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
trinket1=,id=111,ilevel=639
# Saved Loadout: Cleave
# talents=CLEAVE_BUILD
### Gear from Bags
# Candidate Trinket (645)
trinket1=,id=222,ilevel=645
### Weekly Reward Choices
# Candidate Ring (646)
finger1=,id=333,ilevel=646
'''
        response = self.client.post(
            '/api/simc-player-config-detail/',
            data=json.dumps({
                'spec': 'fury', 'player_config_mode': 'manual_equipment',
                'player_equipment': player_block,
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        candidates = payload['data']['comparison_candidates']
        self.assertEqual(candidates['max_selectable'], 7)
        self.assertEqual(
            [(row['slot'], row['item_id'], row['source']) for row in candidates['gear']],
            [('trinket1', 222, 'bags'), ('finger1', 333, 'weekly_reward')],
        )
        self.assertEqual(candidates['talents'], [{'name': 'Cleave', 'talent': 'CLEAVE_BUILD', 'source': 'saved_loadout'}])

    def test_talent_candidate_batch_replaces_player_block_talent_before_execution(self):
        player_block = '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
trinket1=,id=111,ilevel=639
# Saved Loadout: Cleave
# talents=CLEAVE_BUILD
'''
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'talent_candidates', 'name': 'Fury 天赋对比', 'spec': 'fury',
            'player_config_mode': 'manual_equipment', 'player_equipment': player_block,
            'candidates': [{'talent': 'CLEAVE_BUILD'}],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        ext_rows = [json.loads(task.ext) for task in SimcTask.objects.order_by('id')]
        self.assertEqual(len(ext_rows), 2)
        candidate = next(row for row in ext_rows if not row['batch_compare']['is_base'])
        self.assertIn('talents=CLEAVE_BUILD', candidate['player_equipment'])
        self.assertNotIn('talents=ACTIVE_BUILD', candidate['player_equipment'])

    def test_gear_candidate_batch_rejects_slot_not_in_baseline_block(self):
        player_block = '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111,ilevel=639
### Gear from Bags
# Candidate ring (645)
finger1=,id=222,ilevel=645
'''
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'Fury 装备对比', 'spec': 'fury',
            'player_config_mode': 'manual_equipment', 'player_equipment': player_block,
            'candidates': [{'slot': 'finger1', 'item_id': 222, 'source': 'bags'}],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('未包含可替换的装备槽位', response.json()['error'])

    def test_candidate_batch_rejects_duplicate_candidates(self):
        player_block = '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111,ilevel=639
### Gear from Bags
# Candidate helm (645)
head=,id=222,ilevel=645
# Saved Loadout: Cleave
# talents=CLEAVE_BUILD
'''
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'Fury 装备对比', 'spec': 'fury',
            'player_config_mode': 'manual_equipment', 'player_equipment': player_block,
            'candidates': [
                {'slot': 'head', 'item_id': 222, 'source': 'bags'},
                {'slot': 'head', 'item_id': 222, 'source': 'bags'},
            ],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('不可重复选择', response.json()['error'])

    def test_real_simc_export_keeps_main_gear_names_and_excludes_bag_choices(self):
        config = '''# 炎色雷灬 - Fury - 2026-07-10 02:37 - CN/死亡之翼
warrior="炎色雷灬"
level=90
race=orc
region=cn
server=死亡之翼
role=attack
professions=enchanting=100/jewelcrafting=100
spec=fury
talents=ACTIVE_BUILD
# Saved Loadout: 团本屠戮
# talents=SAVED_BUILD
omnium_talents=136817:1/136819:1
# 终夜者的獠牙头盔 (289)
head=,id=249952,enchant_id=8017,gem_id=240892,bonus_id=6652/13534
# 腐沼的孢子之心 (298)
neck=,id=268291,gem_id=240983,bonus_id=6652/13668
# 信徒的流丝罩袍 (285)
back=,id=239656,bonus_id=12214/13667,content_tuning=3615,crafted_stats=32/36,crafting_quality=5
# 旋风虚空裂斧 (298)
main_hand=,id=251117,enchant_id=8041,bonus_id=13440/6652
### Gear from Bags
# 盘绕恶意丝带 (285)
# neck=,id=249337,bonus_id=6652/13668
'''
        detail = parse_manual_player_config(config, 'fury')

        self.assertEqual(detail['identity']['name'], '炎色雷灬')
        self.assertEqual(detail['identity']['region'], 'cn')
        self.assertEqual(detail['identity']['realm'], '死亡之翼')
        self.assertEqual(detail['identity']['role'], 'attack')
        self.assertEqual(detail['identity']['professions'], {'enchanting': 100, 'jewelcrafting': 100})
        self.assertEqual(detail['talents']['build_code'], 'ACTIVE_BUILD')
        self.assertEqual(detail['talents']['saved_loadouts'], [{'name': '团本屠戮', 'build_code': 'SAVED_BUILD'}])
        self.assertEqual(len(detail['equipment']), 4)
        self.assertEqual(detail['equipment'][0]['display_name'], '终夜者的獠牙头盔')
        self.assertEqual(detail['equipment'][0]['item_level'], 289)
        self.assertEqual(detail['equipment'][0]['gems'][0]['id'], 240892)
        self.assertEqual(detail['equipment'][2]['crafted_stats'], ['精通', '全能'])
        self.assertEqual(detail['equipment'][2]['crafting_quality'], 5)
        self.assertEqual(detail['omnium_talents'], [{'id': 136817, 'rank': 1}, {'id': 136819, 'rank': 1}])

    def test_player_config_detail_returns_battlenet_identity_and_explicit_missing_detail(self):
        response = self.client.post(
            '/api/simc-player-config-detail/',
            data=json.dumps({
                'spec': 'fury',
                'player_import_mode': 'battlenet',
                'battlenet_region': 'EU',
                'battlenet_realm': 'Kazzak',
                'battlenet_character': 'Bloodmastêr',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        detail = payload['data']
        self.assertEqual(detail['source']['type'], 'battlenet')
        self.assertEqual(detail['identity']['region'], 'eu')
        self.assertEqual(detail['identity']['realm'], 'Kazzak')
        self.assertEqual(detail['identity']['name'], 'Bloodmastêr')
        self.assertEqual(detail['equipment'], [])
        self.assertTrue(detail['missing_fields'])
        self.assertIn('未保存角色装备快照', detail['missing_fields'][0])

    def test_attribute_only_profile_preserves_legacy_data_but_rejects_new_task_without_baseline(self):
        from botend.models import SimcMasteryCoefficient, SimcSecondaryStatRule

        SimcSecondaryStatRule.objects.update_or_create(
            class_name='warrior',
            defaults={
                'crit_per_percent': 46,
                'haste_per_percent': 44,
                'mastery_per_percent': 46,
                'versatility_per_percent': 54,
            },
        )
        SimcMasteryCoefficient.objects.update_or_create(
            spec='fury', defaults={'mastery_coefficient': 1.4}
        )
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Legacy fury stats',
            spec='fury',
            # 历史记录曾因字段默认值被写成 battlenet，但没有任何角色/装备数据；
            # 读取时必须仍按属性型配置处理。
            player_config_mode='battlenet',
            player_equipment='',
            battlenet_region='',
            battlenet_realm='',
            battlenet_character='',
            talent='LEGACY_BUILD',
            gear_crit=1000,
            gear_haste=2000,
            gear_mastery=3000,
            gear_versatility=4000,
        )

        detail_response = self.client.get(f'/api/simc-profile/{profile.id}/')
        self.assertEqual(detail_response.status_code, 200)
        detail_payload = detail_response.json()
        self.assertTrue(detail_payload['success'], detail_payload)
        self.assertEqual(detail_payload['player_config_mode'], 'attribute_only')
        self.assertEqual(detail_payload['player_equipment'], '')
        self.assertEqual(detail_payload['battlenet_region'], '')

        update_response = self.client.put(
            '/api/simc-profile/',
            data=json.dumps({
                'id': profile.id,
                'name': 'Legacy fury stats updated',
                'spec': 'fury',
                'player_config_mode': 'attribute_only',
                'talent': 'UPDATED_BUILD',
                'gear_crit': 1100,
                'gear_haste': 2200,
                'gear_mastery': 3300,
                'gear_versatility': 4400,
            }),
            content_type='application/json',
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertFalse(update_response.json()['success'], update_response.json())
        self.assertIn('基线', update_response.json()['error'])
        profile.refresh_from_db()
        self.assertEqual(profile.player_config_mode, 'battlenet')
        self.assertEqual(profile.talent, 'LEGACY_BUILD')
        self.assertEqual(profile.player_equipment, '')
        self.assertEqual(profile.battlenet_character, '')

        detail_response = self.client.post(
            '/api/simc-player-config-detail/',
            data=json.dumps({
                'spec': 'fury', 'player_config_mode': 'attribute_only',
                'talent': profile.talent, 'gear_strength': 5000,
                'gear_crit': profile.gear_crit, 'gear_haste': profile.gear_haste,
                'gear_mastery': profile.gear_mastery, 'gear_versatility': profile.gear_versatility,
            }), content_type='application/json',
        )
        self.assertEqual(detail_response.status_code, 200)
        detail_payload = detail_response.json()
        self.assertTrue(detail_payload['success'], detail_payload)
        detail = detail_payload['data']
        self.assertEqual(detail['source']['type'], 'attribute_only')
        self.assertEqual(detail['talents']['build_code'], 'LEGACY_BUILD')
        self.assertEqual(detail['stats']['primary']['strength'], 5000)
        self.assertEqual(detail['stats']['secondary']['crit']['rating'], 1000)
        self.assertAlmostEqual(detail['stats']['secondary']['crit']['percent'], 21.74, places=2)
        self.assertAlmostEqual(detail['stats']['secondary']['mastery']['percent'], 91.30, places=2)
        self.assertEqual(detail['equipment'], [])
        self.assertIn('历史配置未保存冻结玩家装备基线', detail['missing_fields'][0])

        task_response = self.client.post(
            '/api/simc-task/',
            data=json.dumps({
                'name': 'Legacy fury attributes',
                'task_type': 1,
                'spec': 'fury',
                'player_config_mode': 'attribute_only',
                'talent': profile.talent,
                'gear_crit': profile.gear_crit,
                'gear_haste': profile.gear_haste,
                'gear_mastery': profile.gear_mastery,
                'gear_versatility': profile.gear_versatility,
            }),
            content_type='application/json',
        )
        self.assertEqual(task_response.status_code, 200)
        task_payload = task_response.json()
        self.assertFalse(task_payload['success'], task_payload)
        self.assertIn('玩家装备基线', task_payload['error'])
        self.assertFalse(SimcTask.objects.exists())

    def test_attribute_only_profile_load_contract_keeps_equipment_empty(self):
        """工作台加载历史属性配置时，属性只能进入专用字段，不能污染隐藏装备框。"""
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Legacy workbench load contract',
            spec='fury',
            player_config_mode='battlenet',  # 新字段迁移时的错误历史默认值。
            player_equipment='',
            battlenet_region='',
            battlenet_realm='',
            battlenet_character='',
            talent='WORKBENCH_BUILD',
            gear_crit=401,
            gear_haste=1100,
            gear_mastery=1140,
            gear_versatility=100,
        )

        response = self.client.get(f'/api/simc-profile/{profile.id}/')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['player_config_mode'], 'attribute_only')
        self.assertEqual(payload['talent'], 'WORKBENCH_BUILD')
        self.assertEqual(payload['player_equipment'], '')
        self.assertFalse(payload['battlenet_region'])
        self.assertFalse(payload['battlenet_realm'])


class SimcBattlenetPreflightTests(TestCase):
    """Battle.net 提交前预检必须真实获取角色信息，而不是只回显 armory 三元组。"""

    def setUp(self):
        self.user = User.objects.create_user(username='battlenet_preflight_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_preflight_returns_fetched_character_and_simc_readiness(self):
        from unittest.mock import patch

        fetched = {
            'identity': {
                'name': 'Bloodmastêr', 'realm': 'Kazzak', 'region': 'eu',
                'class_name': 'warrior', 'level': 80,
            },
            'spec': {'key': 'fury', 'name': 'Fury'},
            'equipment': {'count': 15, 'item_level': 680},
            'stats': {'secondary': {'crit': {'rating': 1000}}},
            'simc_ready': True,
            'warnings': [],
        }
        with patch('botend.services.battlenet_preflight.fetch_battlenet_character_preflight', return_value=fetched) as fetch:
            response = self.client.post('/api/simc-battlenet-preflight/', data=json.dumps({
                'region': 'EU', 'realm': 'Kazzak', 'character': 'Bloodmastêr', 'spec': 'fury',
            }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertTrue(payload['data']['simc_ready'])
        self.assertEqual(payload['data']['identity']['name'], 'Bloodmastêr')
        self.assertEqual(payload['data']['spec']['key'], 'fury')
        fetch.assert_called_once_with(region='eu', realm='Kazzak', character='Bloodmastêr', requested_spec='fury')

    def test_preflight_service_parses_live_stats_and_rejects_missing_talent(self):
        from botend.services.battlenet_preflight import fetch_battlenet_character_preflight

        profile = {
            'name': 'Bloodmastêr', 'level': 80,
            'character_class': {'name': 'Warrior'},
            'active_spec': {'name': 'Fury'},
            'realm': {'name': 'Kazzak'},
        }
        equipment = {'equipped_items': [{'level': {'value': 680}}]}
        stats = {
            'strength': {'effective': 5000},
            'melee_crit': {'rating': 1000, 'value': 20.0},
            'melee_haste': {'rating': 2000, 'value': 15.0},
            'mastery': {'rating': 3000, 'value': 30.0},
            'versatility': {'rating': 4000, 'damageDoneBonus': 10.0},
        }
        with patch('botend.services.battlenet_preflight._token', return_value='token'), patch(
            'botend.services.battlenet_preflight._api_get', side_effect=[profile, equipment, stats]
        ):
            result = fetch_battlenet_character_preflight(
                region='eu', realm='Kazzak', character='Bloodmastêr', requested_spec='fury',
            )

        self.assertTrue(result['simc_ready'], result)
        self.assertEqual(result['stats']['primary']['strength'], 5000)
        self.assertEqual(result['stats']['secondary']['crit']['rating'], 1000)
        self.assertEqual(result['simc_config']['gear_strength'], 5000)
        self.assertEqual(result['simc_config']['gear_versatility'], 4000)
        self.assertEqual(result['simc_config']['talent'], '')
        self.assertEqual(result['warnings'], [])

    def test_preflight_normalizes_spaced_battlenet_class_name(self):
        from botend.services.battlenet_preflight import fetch_battlenet_character_preflight

        profile = {
            'name': 'Bloodmastêr', 'level': 90,
            'character_class': {'name': 'Death Knight'},
            'active_spec': {'name': 'Blood'},
            'realm': {'name': 'Kazzak'},
        }
        equipment = {'equipped_items': [{'level': {'value': 292}}]}
        with patch('botend.services.battlenet_preflight._token', return_value='token'), patch(
            'botend.services.battlenet_preflight._api_get', side_effect=[profile, equipment, {}]
        ):
            result = fetch_battlenet_character_preflight(
                region='eu', realm='Kazzak', character='Bloodmastêr', requested_spec='blood',
            )

        self.assertEqual(result['identity']['class_name'], 'deathknight')
        self.assertTrue(result['simc_ready'], result)
        self.assertEqual(result['warnings'], [])


class SimcBatchTaskAPIViewGetTests(TestCase):
    """Test GET endpoint for SimcBatchTaskAPIView - list batches and batch details."""

    def setUp(self):
        self.user = User.objects.create_user(username='batch_get_user', password='pwd')
        self.other_user = User.objects.create_user(username='other_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_get_list_returns_recent_20_batches_with_status_counts(self):
        # Create 25 batches (reverse order so 24 is most recent)
        batches_created = []
        for i in range(25):
            batch = SimcTaskBatch.objects.create(
                user_id=self.user.id,
                name=f'Batch {i}',
                batch_type='comparison',
                status=1 if i < 5 else 2,
            )
            batches_created.append(batch)

        # Add tasks to the last 3 batches created (22, 23, 24)
        for i in [22, 23, 24]:
            batch = batches_created[i]
            SimcTask.objects.create(
                user_id=self.user.id, name=f'Task {i}-0', simc_profile_id=0,
                task_type=1, current_status=0, batch=batch, is_active=True,
            )
            SimcTask.objects.create(
                user_id=self.user.id, name=f'Task {i}-1', simc_profile_id=0,
                task_type=1, current_status=1, batch=batch, is_active=True,
            )
            SimcTask.objects.create(
                user_id=self.user.id, name=f'Task {i}-2', simc_profile_id=0,
                task_type=1, current_status=2, batch=batch, is_active=True,
            )
            SimcTask.objects.create(
                user_id=self.user.id, name=f'Task {i}-3', simc_profile_id=0,
                task_type=1, current_status=3, batch=batch, is_active=True,
            )

        response = self.client.get('/api/simc-task/batch/')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        batches = payload['data']
        self.assertEqual(len(batches), 20)  # Only 20 most recent

        # Find first batch with tasks (should be Batch 24)
        first = batches[0]
        self.assertEqual(first['name'], 'Batch 24')
        self.assertIn('batch_id', first)
        self.assertIn('batch_type', first)
        self.assertIn('status', first)
        self.assertIn('status_counts', first)
        self.assertIn('created_at', first)
        self.assertEqual(first['status_counts']['pending'], 1)
        self.assertEqual(first['status_counts']['running'], 1)
        self.assertEqual(first['status_counts']['completed'], 1)
        self.assertEqual(first['status_counts']['failed'], 1)

    def test_get_detail_returns_batch_with_task_list(self):
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id,
            name='Detail Test Batch',
            batch_type='attribute_sweep',
            status=2,
            completed_at=timezone.now(),
        )
        task1 = SimcTask.objects.create(
            user_id=self.user.id, name='Task 1', simc_profile_id=0,
            task_type=1, current_status=2, batch=batch, is_active=True,
            candidate_label='基准配置', result_file='simc_task_1.html',
        )
        task2 = SimcTask.objects.create(
            user_id=self.user.id, name='Task 2', simc_profile_id=0,
            task_type=1, current_status=3, batch=batch, is_active=True,
            candidate_label='候选A', error_detail='Test error message\nwith traceback',
        )

        response = self.client.get(f'/api/simc-task/batch/?batch_id={batch.id}')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        data = payload['data']

        self.assertEqual(data['batch_id'], batch.id)
        self.assertEqual(data['name'], 'Detail Test Batch')
        self.assertEqual(data['batch_type'], 'attribute_sweep')
        self.assertEqual(data['status'], 2)
        self.assertIn('report_url', data)
        # No report_url because task2 has status=3 (failed)
        self.assertEqual(data['report_url'], '')

        # Check tasks
        tasks = data['tasks']
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]['task_id'], task1.id)
        self.assertEqual(tasks[0]['candidate_label'], '基准配置')
        self.assertEqual(tasks[0]['status'], 2)

        # Error summary should not contain traceback/sensitive info
        self.assertEqual(tasks[1]['task_id'], task2.id)
        self.assertEqual(tasks[1]['status'], 3)
        # _safe_error_summary returns fixed message for error_detail with traceback
        self.assertEqual(tasks[1]['error_summary'], '任务执行失败')

    def test_get_detail_enforces_user_isolation(self):
        other_batch = SimcTaskBatch.objects.create(
            user_id=self.other_user.id,
            name='Other User Batch',
            batch_type='comparison',
            status=1,
        )

        response = self.client.get(f'/api/simc-task/batch/?batch_id={other_batch.id}')
        self.assertEqual(response.status_code, 404)
        payload = response.json()
        self.assertFalse(payload['success'])

    def test_get_detail_prohibits_sensitive_data_leakage(self):
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id,
            name='Security Test',
            batch_type='comparison',
            status=2,
            request_manifest='{"secret": "sensitive_data"}',
        )
        SimcTask.objects.create(
            user_id=self.user.id, name='Task', simc_profile_id=0,
            task_type=1, current_status=2, batch=batch, is_active=True,
            final_simc_content='spec=fury\nclass=warrior',
            ext='{"player_equipment": "secret_config"}',
        )

        response = self.client.get(f'/api/simc-task/batch/?batch_id={batch.id}')
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        # Ensure sensitive fields are not in response
        response_str = json.dumps(payload)
        self.assertNotIn('request_manifest', response_str)
        self.assertNotIn('final_simc_content', response_str)
        self.assertNotIn('player_equipment', response_str)
        self.assertNotIn('secret', response_str)

    def test_get_list_filters_inactive_batches_and_tasks(self):
        active_batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Active', batch_type='comparison',
            status=1, is_active=True,
        )
        SimcTask.objects.create(
            user_id=self.user.id, name='Active Task', simc_profile_id=0,
            task_type=1, current_status=2, batch=active_batch, is_active=True,
        )
        SimcTask.objects.create(
            user_id=self.user.id, name='Inactive Task', simc_profile_id=0,
            task_type=1, current_status=2, batch=active_batch, is_active=False,
        )

        inactive_batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Inactive', batch_type='comparison',
            status=1, is_active=False,
        )

        response = self.client.get('/api/simc-task/batch/')
        payload = response.json()
        batches = payload['data']

        # Only active batch should be returned
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]['batch_id'], active_batch.id)
        # Only active task should be counted
        self.assertEqual(batches[0]['status_counts']['completed'], 1)

    def test_get_detail_safe_error_summary_from_ext(self):
        """Test that error summary comes from ext.simc_error_summary safely"""
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Error Test', batch_type='comparison', status=1,
        )
        # Task with safe summary in ext
        safe_task = SimcTask.objects.create(
            user_id=self.user.id, name='Safe', simc_profile_id=0,
            task_type=1, current_status=3, batch=batch, is_active=True,
            ext='{"simc_error_summary": "配置解析失败"}',
        )
        # Task with sensitive patterns in ext summary - should be blocked
        unsafe_task = SimcTask.objects.create(
            user_id=self.user.id, name='Unsafe', simc_profile_id=0,
            task_type=1, current_status=3, batch=batch, is_active=True,
            ext='{"simc_error_summary": "Traceback file /path/to/file"}',
        )
        # Task with error_detail (not ext) - should use fixed message
        detail_task = SimcTask.objects.create(
            user_id=self.user.id, name='Detail', simc_profile_id=0,
            task_type=1, current_status=3, batch=batch, is_active=True,
            error_detail='Raw error from stderr',
        )

        response = self.client.get(f'/api/simc-task/batch/?batch_id={batch.id}')
        payload = response.json()
        tasks = payload['data']['tasks']

        # Safe summary should be returned (truncated to 200 chars)
        self.assertEqual(tasks[0]['error_summary'], '配置解析失败')
        # Unsafe summary with sensitive patterns should return fixed message
        self.assertEqual(tasks[1]['error_summary'], '任务执行失败')
        # error_detail is ignored, fixed message returned
        self.assertEqual(tasks[2]['error_summary'], '任务执行失败')

    def test_get_detail_no_report_url_when_tasks_incomplete(self):
        """Test report_url only appears when all tasks succeed with valid HTML"""
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Incomplete', batch_type='comparison', status=1,
        )
        SimcTask.objects.create(
            user_id=self.user.id, name='T1', simc_profile_id=0,
            task_type=1, current_status=2, batch=batch, is_active=True,
            result_file='simc_task_1.html',
        )
        SimcTask.objects.create(
            user_id=self.user.id, name='T2', simc_profile_id=0,
            task_type=1, current_status=1, batch=batch, is_active=True,  # still running
        )

        response = self.client.get(f'/api/simc-task/batch/?batch_id={batch.id}')
        payload = response.json()
        # No report_url because not all tasks are status=2
        self.assertEqual(payload['data']['report_url'], '')

    def test_get_detail_no_report_url_when_no_valid_html(self):
        """Test report_url blocked when result_file doesn't pass validation"""
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Invalid', batch_type='comparison', status=2,
        )
        SimcTask.objects.create(
            user_id=self.user.id, name='T1', simc_profile_id=0,
            task_type=1, current_status=2, batch=batch, is_active=True,
            result_file='../../etc/passwd',  # Invalid path
        )

        response = self.client.get(f'/api/simc-task/batch/?batch_id={batch.id}')
        payload = response.json()
        # No report_url because result_file fails validation
        self.assertEqual(payload['data']['report_url'], '')

    def test_get_detail_no_report_url_for_invalid_attribute_result(self):
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Invalid attribute result',
            batch_type='attribute_sweep', status=2,
        )
        SimcTask.objects.create(
            user_id=self.user.id, name='T1', simc_profile_id=0,
            task_type=2, current_status=2, batch=batch, is_active=True,
            result_file='../../secret.html',
        )

        response = self.client.get(f'/api/simc-task/batch/?batch_id={batch.id}')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['report_url'], '')

    def test_batch_frontend_polling_and_safe_detail_contract(self):
        workbench_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/simc-workbench.js').read_text(encoding='utf-8')
        start = workbench_js.index('async function loadTasks(')
        end = workbench_js.index('function renderPagination(', start)
        task_js = workbench_js[start:end]

        self.assertNotIn('setInterval(', task_js)
        self.assertIn('taskFetchInFlight', task_js)
        self.assertIn('taskRequestSerial', task_js)
        self.assertIn('setTimeout(', task_js)
        self.assertIn('[0, 1, 4].includes(Number(row.status))', task_js)
        self.assertIn('Number(row.pending || 0) > 0', task_js)
        self.assertIn('scheduleTaskRefresh(hasActive)', task_js)
        self.assertIn('暂无记录', task_js)

    def test_get_detail_report_url_only_when_all_valid(self):
        """Test report_url appears only when all tasks succeed with valid HTML"""
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Valid', batch_type='comparison', status=2,
        )
        SimcTask.objects.create(
            user_id=self.user.id, name='T1', simc_profile_id=0,
            task_type=1, current_status=2, batch=batch, is_active=True,
            result_file='simc_task_123.html',
        )
        SimcTask.objects.create(
            user_id=self.user.id, name='T2', simc_profile_id=0,
            task_type=1, current_status=2, batch=batch, is_active=True,
            result_file='a1b2c3d4e5f6789012345678901234ab.html',
        )

        response = self.client.get(f'/api/simc-task/batch/?batch_id={batch.id}')
        payload = response.json()
        # report_url should be present
        self.assertEqual(payload['data']['report_url'], f'/simc-compare/?batch_id={batch.id}')

    def test_status_4_counted_as_running(self):
        """Test that status=4 tasks are counted as 'running'"""
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Status4', batch_type='comparison', status=1,
        )
        SimcTask.objects.create(
            user_id=self.user.id, name='T1', simc_profile_id=0,
            task_type=1, current_status=4, batch=batch, is_active=True,
        )
        SimcTask.objects.create(
            user_id=self.user.id, name='T2', simc_profile_id=0,
            task_type=1, current_status=1, batch=batch, is_active=True,
        )

        response = self.client.get(f'/api/simc-task/batch/?batch_id={batch.id}')
        payload = response.json()
        # Both status=4 and status=1 should count as running
        self.assertEqual(payload['data']['status_counts']['running'], 2)
