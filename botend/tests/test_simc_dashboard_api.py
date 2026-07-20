import importlib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, RequestFactory, TestCase
from django.utils import timezone

from botend.dashboard.api import SimcAplCandidatesAPIView, SimcBatchTaskAPIView, SimcProfileAPIView, SimcRegularCompareAPIView, SimcTaskAPIView, SimcSpecOptionsAPIView
from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
from botend.management.commands.update_simc_binary import Command as UpdateSimcBinaryCommand
from botend.services.simc_player_config import build_player_config_detail, parse_manual_player_config, parse_manual_simc_candidates, parse_simc_player_profile
from botend.services.simc_composer import SimcComposer
from botend.models import PlayerSpecTopPlayer, SeasonMeta, SimcApl, SimcContentTemplate, SimcProfile, SimcTask, SimcTaskBatch, WowItemSnapshot


class SimcAplCanonicalClassAliasTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='apl_alias_user', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)
        self.base_template = SimcContentTemplate.objects.create(
            name='Generic base template',
            template_type=SimcContentTemplate.TYPE_BASE_TEMPLATE,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='default',
            class_name='',
            content='{player_identity}\n{action_list}',
            is_active=True,
            is_selectable=True,
        )
        self.apl = SimcApl.objects.create(
            name='Default Unholy APL',
            spec='deathknight_unholy',
            class_name='deathknight',
            content='actions=/auto_attack',
            source=SimcApl.SOURCE_SIMC_UPSTREAM,
            is_system=True,
            owner_user_id=None,
            is_active=True,
            is_selectable=True,
        )

    def test_apl_candidates_resolves_death_knight_alias_to_canonical_class(self):
        response = self.client.get(
            '/api/simc-apl-candidates/',
            {'spec': 'unholy', 'class_name': 'death_knight'},
        )

        self.assertEqual(response.status_code, 200, response.json())
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['default_apl_id'], self.apl.id)
        self.assertEqual(payload['default_template_id'], self.base_template.id)
        self.assertEqual([row['id'] for row in payload['data']], [self.apl.id])


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
        """Old frozen/raw tasks are rejected; test that batch claim still works for reference tasks."""
        from botend.models import SimcResourceVersion, SimcProfile, SimcContentTemplate, SimcApl
        import hashlib

        # Create live resources
        profile = SimcProfile.objects.create(
            user_id=801,
            name='Test Profile',
            spec='warrior_fury',
            player_config_mode='manual_equipment',
            player_equipment='warrior=Test\nspec=fury\nhead=,id=1',
        )
        template = SimcContentTemplate.objects.create(
            template_type='base_template',
            source='user',
            spec='warrior_fury',
            name='Test Template',
            content='warrior="T"\n{player_config}\n',
        )
        apl = SimcApl.objects.create(
            name='Test APL',
            spec='warrior_fury',
            content='actions=/auto_attack',
            source='user',
        )

        # Create versions
        profile_payload = {
            "player_config_mode": "manual_equipment",
            "player_equipment": "warrior=Test\nspec=fury\nhead=,id=1",
        }
        profile_version = SimcResourceVersion.objects.create(
            resource_type='profile', resource_id=profile.id,
            content_hash=hashlib.sha256(json.dumps(profile_payload, sort_keys=True).encode()).hexdigest(),
            payload=profile_payload,
        )
        template_payload = {'content': 'warrior="T"\n{player_config}\n'}
        template_version = SimcResourceVersion.objects.create(
            resource_type='template', resource_id=template.id,
            content_hash=hashlib.sha256(json.dumps(template_payload, sort_keys=True).encode()).hexdigest(),
            payload=template_payload,
        )
        apl_payload = {'content': 'actions=/auto_attack'}
        apl_version = SimcResourceVersion.objects.create(
            resource_type='apl', resource_id=apl.id,
            content_hash=hashlib.sha256(json.dumps(apl_payload, sort_keys=True).encode()).hexdigest(),
            payload=apl_payload,
        )

        task = self._task(status=0)
        task.profile_id = profile.id
        task.profile_version_id = profile_version.id
        task.template_id = template.id
        task.template_version_id = template_version.id
        task.apl_id = apl.id
        task.apl_version_id = apl_version.id
        task.save()

        observed_batch_status = []

        def complete_reference_task(simc_task):
            self.batch.refresh_from_db()
            observed_batch_status.append(self.batch.status)
            simc_task.current_status = 2
            simc_task.completed_at = timezone.now()
            simc_task.save(update_fields=['current_status', 'completed_at', 'modified_time'])
            return True

        with patch.object(self.monitor, 'process_reference_task', side_effect=complete_reference_task):
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

    def test_raw_inspection_endpoint_is_removed(self):
        response = self.client.post('/api/simc-profile/inspect-raw/', data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 404)

    def test_raw_simc_task_create_persists_raw_code_in_ext(self):
        """Direct SimC code mode is no longer supported."""
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
        self.assertFalse(payload['success'])
        self.assertIn('不再支持直接 SimC 代码模式', payload['error'])

    def test_raw_simc_attribute_task_is_rejected(self):
        """Direct SimC code mode is no longer supported."""
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
        self.assertIn('不再支持直接 SimC 代码模式', payload['error'])
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
            is_selectable=True,
        )
        self.default_player = SimcContentTemplate.objects.create(
            template_type=SimcContentTemplate.TYPE_DEFAULT_PLAYER,
            source=SimcContentTemplate.SOURCE_SIMC_UPSTREAM,
            spec='warrior_fury',
            name='MID1 Warrior Fury',
            content=(
                'warrior="FrozenArmory"\nlevel=90\nspec=fury\ntalents=BASE\n'
                'head=,id=1\nneck=,id=2\nshoulder=,id=3\nback=,id=4\nchest=,id=5\n'
                'wrist=,id=6\nhands=,id=7\nwaist=,id=8\nlegs=,id=9\nfeet=,id=10\n'
                'finger1=,id=11\nfinger2=,id=12\ntrinket1=,id=13\ntrinket2=,id=14\n'
                'main_hand=,id=15\noff_hand=,id=16'
            ),
            is_active=True,
        )
        self.profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Batch contract Profile',
            spec='warrior_fury',
            player_config_mode='manual_equipment',
            player_equipment=(
                'warrior="Batcher"\nlevel=90\nspec=fury\ntalents=BASE\n'
                'head=,id=212048\nmain_hand=,id=222222'
            ),
            talent='BASE',
            is_active=True,
        )



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

    def test_parse_manual_candidates_splits_real_addon_profile_from_commented_extras(self):
        parsed = parse_simc_player_profile('''
# SimC Addon 12.0.7-01
warrior="炎色雷灬"
level=90
spec=fury
talents=ACTIVE_BUILD
head=,id=249952,enchant_id=8017
neck=,id=249337
main_hand=,id=251078
# Saved Loadout: 团本山丘
# talents=RAID_HILL_BUILD
### Gear from Bags
#
# 盘绕恶意丝带 (285)
# neck=,id=299001,bonus_id=6652/13668
#
# 流光织锦披风 (289)
# back=,id=299002,bonus_id=13440/41
### Weekly Reward Choices
#
# 每周宝库头盔 (298)
# head=,id=299003,bonus_id=13786
### End of Weekly Reward Choices
### Additional Character Info
# upgrade_currencies=c:3347:267
''')

        self.assertEqual(parsed['profile']['identity']['class_name'], 'warrior')
        self.assertEqual(parsed['profile']['identity']['name'], '炎色雷灬')
        self.assertEqual(parsed['profile']['identity']['spec'], 'fury')
        self.assertEqual(parsed['profile']['talents']['build_code'], 'ACTIVE_BUILD')
        self.assertEqual(
            [row['slot'] for row in parsed['profile']['equipment']],
            ['head', 'neck', 'main_hand'],
        )
        self.assertNotIn('Gear from Bags', parsed['profile']['raw_player_block'])
        self.assertNotIn('Saved Loadout', parsed['profile']['raw_player_block'])
        self.assertEqual(
            [(row['slot'], row['item_id'], row['source']) for row in parsed['candidates']['gear']],
            [('neck', 299001, 'bags'), ('back', 299002, 'bags'), ('head', 299003, 'weekly_reward')],
        )
        self.assertEqual(
            parsed['candidates']['talents'],
            [{'name': '团本山丘', 'talent': 'RAID_HILL_BUILD', 'source': 'saved_loadout'}],
        )

    def test_parse_simc_player_profile_splits_current_block_from_commented_candidates(self):
        parsed = parse_simc_player_profile('''
warrior="KBZ"
level=90
spec=fury
talents=BASE_BUILD
head=,id=212048
main_hand=,id=222222
# Saved Loadout: 团本山丘
# talents=RAID_BUILD
### Gear from Bags
# 盘绕恶意丝带 (285)
# neck=,id=249337,bonus_id=6652/13668
### Weekly Reward Choices
# Reward Ring (289)
# finger1=,id=251115
''')
        self.assertEqual(parsed['profile']['identity']['name'], 'KBZ')
        self.assertEqual(parsed['profile']['talents']['build_code'], 'BASE_BUILD')
        self.assertEqual(parsed['profile']['raw_player_block'].count('head='), 1)
        self.assertEqual(parsed['candidates']['talents'], [
            {'name': '团本山丘', 'talent': 'RAID_BUILD', 'source': 'saved_loadout'},
        ])
        self.assertEqual(
            [(row['slot'], row['item_id'], row['source']) for row in parsed['candidates']['gear']],
            [('neck', 249337, 'bags'), ('finger1', 251115, 'weekly_reward')],
        )
        self.assertEqual(parsed['candidates']['gear'][0]['name'], '盘绕恶意丝带')
        self.assertEqual(parsed['candidates']['gear'][0]['item_level'], 285)
        self.assertEqual(parsed['profile']['talents']['saved_loadouts'], [])

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


    def test_auto_attribute_batch_accepts_simc_addon_source_and_freezes_attribute_profile(self):
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'attribute_variants', 'name': 'Fury 即时属性寻优',
            'spec': 'warrior_fury',
            'player_source': {
                'type': 'simc_addon',
                'simc_code': (
                    'warrior="Imported"\nlevel=90\nspec=fury\ntalents=IMPORT_BUILD\n'
                    'gear_crit=1000\ngear_haste=2000\ngear_mastery=3000\ngear_versatility=4000\n'
                    'head=,id=212048\nmain_hand=,id=222222\nactions=/auto_attack'
                ),
            },
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'attribute_step': 50, 'fight_style': 'Patchwerk', 'time': 300, 'target_count': 1,
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        batch = SimcTaskBatch.objects.get(id=response.json()['data']['batch_id'])
        profile = SimcProfile.objects.get(id=json.loads(batch.request_manifest)['profile_id'])
        self.assertEqual(profile.player_config_mode, 'attribute_only')
        self.assertEqual(profile.spec, 'fury')
        self.assertEqual(profile.talent, 'IMPORT_BUILD')
        self.assertEqual(
            [profile.gear_crit, profile.gear_haste, profile.gear_mastery, profile.gear_versatility],
            [1000, 2000, 3000, 4000],
        )
        self.assertNotIn('actions=', profile.player_equipment)
        self.assertEqual(SimcTask.objects.filter(batch=batch).count(), 13)

    @patch('botend.dashboard.api.fetch_battlenet_character_preflight')
    def test_auto_attribute_batch_accepts_battlenet_source_with_frozen_ratings(self, preflight):
        preflight.return_value = {
            'simc_ready': True, 'warnings': [],
            'simc_config': {
                'player_config_mode': 'battlenet', 'battlenet_region': 'eu',
                'battlenet_realm': 'Kazzak', 'battlenet_character': 'Batcher',
                'spec': 'fury', 'talent': '', 'gear_strength': 10000,
                'gear_crit': 1000, 'gear_haste': 2000,
                'gear_mastery': 3000, 'gear_versatility': 4000,
            },
        }
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'attribute_variants', 'name': 'Fury Battle.net 属性寻优',
            'spec': 'warrior_fury',
            'player_source': {'type': 'battlenet', 'region': 'eu', 'realm': 'Kazzak', 'character': 'Batcher'},
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'attribute_step': 50,
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        batch = SimcTaskBatch.objects.get(id=response.json()['data']['batch_id'])
        profile = SimcProfile.objects.get(id=json.loads(batch.request_manifest)['profile_id'])
        self.assertEqual(profile.player_config_mode, 'attribute_only')
        self.assertIn('warrior="FrozenArmory"', profile.player_equipment)
        self.assertEqual([profile.gear_crit, profile.gear_haste, profile.gear_mastery, profile.gear_versatility], [1000, 2000, 3000, 4000])
        tasks = list(SimcTask.objects.filter(batch=batch).order_by('id'))
        self.assertEqual(len(tasks), 13)
        monitor = SimcMonitor(None, None)
        rendered = []
        for task in tasks[:2]:
            request_data = monitor.apply_candidate_overrides({
                'spec': task.profile_version.payload['spec'],
                'fight_style': task.simulation_params.get('fight_style', 'Patchwerk'),
                'time': task.simulation_params.get('max_time', 300),
                'target_count': task.simulation_params.get('desired_targets', 1),
                'player_import_mode': task.profile_version.payload['player_config_mode'],
                'player_equipment': task.profile_version.payload['player_equipment'],
                'talent': task.profile_version.payload['talent'],
                'gear_strength': task.profile_version.payload['gear_strength'],
                'gear_crit': task.profile_version.payload['gear_crit'],
                'gear_haste': task.profile_version.payload['gear_haste'],
                'gear_mastery': task.profile_version.payload['gear_mastery'],
                'gear_versatility': task.profile_version.payload['gear_versatility'],
                'base_template_content': task.template_version.payload['content'],
                'override_action_list': task.apl_version.payload['content'],
                '_result_file_path': task.result_file,
            }, task.mode_params)
            simc_code, _, error = SimcComposer(self.user.id).compose(request_data)
            self.assertFalse(error)
            rendered.append(simc_code)
        self.assertNotEqual(rendered[0], rendered[1])
        self.assertIn('gear_crit_rating=1000', rendered[0])
        self.assertIn('gear_crit_rating=950', rendered[1])
        self.assertNotIn('armory=', rendered[0])

    def test_auto_attribute_batch_rejects_missing_frozen_player_baseline(self):
        self.profile.player_config_mode = 'attribute_only'
        self.profile.player_equipment = ''
        self.profile.save(update_fields=['player_config_mode', 'player_equipment'])
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'attribute_variants', 'name': 'Fury 自动属性比较',
            'simc_profile_id': self.profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'attribute_step': 50, 'fight_style': 'Patchwerk', 'time': 300, 'target_count': 1,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('玩家装备基线', response.json()['error'])
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
        self.assertNotRegex(rendered, r'(?m)^\s*gear_strength\s*=')
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
        self.profile.player_config_mode = 'attribute_only'
        self.profile.save(update_fields=['player_config_mode'])
        results = [
            {'ratings': {'crit': 1000, 'haste': 2000, 'mastery': 3000, 'versatility': 4000}, 'dps': 100000, 'is_center': True},
            {'ratings': {'crit': 950, 'haste': 2050, 'mastery': 3000, 'versatility': 4000}, 'dps': 100100, 'is_center': False},
        ]
        with self.assertRaisesRegex(ValueError, '固定使用 50'):
            SimcBatchTaskAPIView._next_attribute_search_center(results, step=100, min_step=50)
        bad_response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'attribute_variants', 'name': '错误步长',
            'simc_profile_id': self.profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'attribute_step': 100,
        }), content_type='application/json')
        self.assertFalse(bad_response.json()['success'])
        self.assertIn('固定使用 50', bad_response.json()['error'])

        stop = SimcBatchTaskAPIView._attribute_search_stop_reason(
            round_number=20, ratings={'crit': 1200, 'haste': 2000, 'mastery': 3000, 'versatility': 3800},
            step=100, visited_centers=set(), max_rounds=20,
        )
        self.assertEqual(stop, 'max_rounds_reached')




    def test_batch_rejects_unsupported_source_and_oversized_candidate_selection(self):
        base = {
            'name': 'Manual candidate compare',
            'simc_profile_id': self.profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
        }
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
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='FK batch drain all', batch_type='talent_candidates', status=1,
        )
        tasks = [
            SimcTask.objects.create(
                user_id=self.user.id, name=f'candidate {index}', simc_profile_id=0,
                current_status=0, task_type=1, result_file='',
                batch=batch, candidate_label=f'candidate {index}',
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

    def test_attribute_manifest_task_is_rejected_until_reference_architecture(self):
        """Legacy attribute tasks without 6-reference fields are rejected."""
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
        result = monitor.process_simc_task(task)
        self.assertFalse(result)
        task.refresh_from_db()
        self.assertEqual(task.current_status, 3)

    def test_direct_attribute_task_persists_full_manifest_snapshot(self):
        """Task type 2 (old attribute sweep) is now rejected."""
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
        self.assertFalse(payload['success'])
        self.assertIn('旧版属性寻优（task_type=2）已停用', payload['error'])

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
        """raw_simc_code mode is now rejected; verify no secret leaks in error."""
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
        self.assertFalse(payload['success'])
        self.assertIn('不再支持直接 SimC 代码模式', payload['error'])
        self.assertNotIn('create-secret', json.dumps(payload, ensure_ascii=False))


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

    def test_task_detail_uses_workbench_dialog_and_old_modal_is_removed(self):
        main_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        workbench_js = (Path(__file__).resolve().parents[2] / 'static/dashboard/js/simc-workbench.js').read_text(encoding='utf-8')
        self.assertNotIn('function openViewSimcTaskModal(task)', main_js)
        self.assertIn('async function showTaskDetail(resource, id)', workbench_js)
        self.assertIn("window.openSimcWorkbenchDialog(resource === 'batches' ? 'batch-detail' : 'task-detail', null)", workbench_js)
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
        """Test rerun for reference-based tasks creates a new pending task."""
        from botend.models import SimcResourceVersion, SimcProfile, SimcContentTemplate, SimcApl
        import hashlib

        # Create live resources with a different spec to avoid conflicts with setUp
        profile = SimcProfile.objects.create(
            user_id=self.user.id,
            name='Completed Profile',
            spec='warrior_arms',
            player_config_mode='manual_equipment',
            player_equipment='warrior="Snapshot"\nspec=arms\nhead=,id=212048',
        )
        template = SimcContentTemplate.objects.create(
            template_type='base_template',
            source='user',
            spec='warrior_arms',
            name='Rerun Test Template',
            content='warrior="T"\n{player_config}\n',
        )
        apl = SimcApl.objects.create(
            name='Rerun Test APL',
            spec='warrior_arms',
            content='actions=/bloodthirst',
            source='user',
        )

        # Create versions
        profile_payload = {
            "player_config_mode": "manual_equipment",
            "player_equipment": "warrior=\"Snapshot\"\nspec=arms\nhead=,id=212048",
        }
        profile_version = SimcResourceVersion.objects.create(
            resource_type='profile', resource_id=profile.id,
            content_hash=hashlib.sha256(json.dumps(profile_payload, sort_keys=True).encode()).hexdigest(),
            payload=profile_payload,
        )
        template_payload = {'content': 'warrior="T"\n{player_config}\n'}
        template_version = SimcResourceVersion.objects.create(
            resource_type='template', resource_id=template.id,
            content_hash=hashlib.sha256(json.dumps(template_payload, sort_keys=True).encode()).hexdigest(),
            payload=template_payload,
        )
        apl_payload = {'content': 'actions=/bloodthirst'}
        apl_version = SimcResourceVersion.objects.create(
            resource_type='apl', resource_id=apl.id,
            content_hash=hashlib.sha256(json.dumps(apl_payload, sort_keys=True).encode()).hexdigest(),
            payload=apl_payload,
        )

        # Create completed reference task
        original = SimcTask.objects.create(
            user_id=self.user.id,
            name='Completed reference task',
            task_type=1,
            simc_profile_id=0,
            profile_id=profile.id,
            profile_version_id=profile_version.id,
            template_id=template.id,
            template_version_id=template_version.id,
            apl_id=apl.id,
            apl_version_id=apl_version.id,
            current_status=1,
            result_file='simc_task_completed.html',
        )

        rejected = self.client.patch(
            '/api/simc-task/',
            data=json.dumps({'id': original.id, 'action': 'rerun'}),
            content_type='application/json',
        )
        self.assertFalse(rejected.json()['success'])
        self.assertIn('已完成或失败', rejected.json()['error'])
        self.assertEqual(SimcTask.objects.count(), 1)
        original.current_status = 2
        original.save(update_fields=['current_status'])

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

        rerun = SimcTask.objects.get(id=rerun_id)
        self.assertEqual(rerun.current_status, 0)
        self.assertRegex(rerun.result_file, r'^[0-9a-f]{32}\.html$')
        self.assertEqual(rerun.profile_id, profile.id)
        self.assertEqual(rerun.profile_version_id, profile_version.id)
        self.assertEqual(rerun.template_id, template.id)
        self.assertEqual(rerun.template_version_id, template_version.id)
        self.assertEqual(rerun.apl_id, apl.id)
        self.assertEqual(rerun.apl_version_id, apl_version.id)

    def test_direct_attribute_task_rejects_non_50_step(self):
        """Old task_type=2 is rejected; test confirms proper error message."""
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
        self.assertIn('旧版属性寻优（task_type=2）已停用', payload['error'])









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
        self.assertNotRegex(rendered, r'(?m)^\s*gear_strength\s*=')
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
            is_selectable=True,
        )

    def _create_profile(self, name, player_block):
        return SimcProfile.objects.create(
            user_id=self.user.id,
            name=name,
            spec='warrior_fury',
            player_config_mode='manual_equipment',
            player_equipment=player_block,
            talent='ACTIVE_BUILD',
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
        """Batch creation now requires base_template_id and apl_id."""
        player_block = '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
trinket1=,id=111,ilevel=639
# Saved Loadout: Cleave
# talents=CLEAVE_BUILD
'''
        profile = self._create_profile('Talent Replacement Test', player_block)
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'talent_candidates', 'name': 'Fury 天赋对比',
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'candidates': [{'talent': 'CLEAVE_BUILD'}],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        # Verify we have 2 tasks (base + 1 candidate)
        self.assertEqual(SimcTask.objects.count(), 2)
        tasks = list(SimcTask.objects.order_by('id'))
        # Both should have reference fields
        for task in tasks:
            self.assertIsNotNone(task.profile_id)
            self.assertIsNotNone(task.profile_version_id)
            self.assertIsNotNone(task.template_id)
            self.assertIsNotNone(task.template_version_id)
            self.assertIsNotNone(task.apl_id)
            self.assertIsNotNone(task.apl_version_id)

    def test_talent_candidate_batch_accepts_named_manual_build_and_freezes_report_metadata(self):
        profile = self._create_profile('Manual talent Test', '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111,ilevel=639
main_hand=,id=222
''')
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'talent_candidates', 'name': 'Fury 手工天赋对比',
            'include_base': False,
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'candidates': [{
                'name': '手工单体方案', 'talent': 'MANUAL_TALENT_BUILD', 'source': 'manual',
            }],
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        candidate = SimcTask.objects.get()
        self.assertEqual(candidate.candidate_label, '手工单体方案')
        self.assertEqual(candidate.mode_params['candidate_type'], 'talent_override')
        self.assertEqual(candidate.mode_params['talent_override'], 'MANUAL_TALENT_BUILD')
        self.assertEqual(candidate.mode_params['talent_candidate'], {
            'name': '手工单体方案', 'talent': 'MANUAL_TALENT_BUILD', 'source': 'manual',
        })

    def test_talent_candidate_batch_rejects_manual_build_without_name(self):
        profile = self._create_profile('Invalid manual talent Test', '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111
main_hand=,id=222
''')
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'talent_candidates', 'name': 'Fury 手工天赋对比',
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'candidates': [{'name': '', 'talent': 'MANUAL_TALENT_BUILD', 'source': 'manual'}],
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('方案名称', response.json()['error'])

    def test_gear_candidate_batch_rejects_slot_not_in_baseline_block(self):
        player_block = '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111,ilevel=639
### Gear from Bags
# Candidate ring (645)
finger1=,id=222,ilevel=645
'''
        profile = self._create_profile('Gear missing-slot Test', player_block)
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'Fury 装备对比',
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
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
        profile = self._create_profile('Duplicate candidate Test', player_block)
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'Fury 装备对比',
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'candidates': [
                {'slot': 'head', 'item_id': 222, 'source': 'bags'},
                {'slot': 'head', 'item_id': 222, 'source': 'bags'},
            ],
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('不可重复选择', response.json()['error'])

    def test_gear_candidate_batch_accepts_valid_manual_slot_override(self):
        profile = self._create_profile('Manual candidate Test', '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111,ilevel=639
main_hand=,id=222
''')
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'Fury 手工装备对比',
            'include_base': False,
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'candidates': [{
                'slot': 'head', 'item_id': 444, 'source': 'manual',
                'raw_value': ',id=444,ilevel=650', 'name': '手工候选头盔',
            }],
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'], response.json())
        self.assertEqual(SimcTask.objects.count(), 1)
        candidate = SimcTask.objects.get()
        self.assertEqual(candidate.mode_params['gear_swap'], {
            'slot': 'head', 'raw_value': ',id=444,ilevel=650',
            'item_id': 444, 'source': 'manual',
        })

    def test_gear_candidate_batch_rejects_manual_line_for_another_slot(self):
        profile = self._create_profile('Invalid manual candidate Test', '''warrior="Batcher"
spec=fury
talents=ACTIVE_BUILD
head=,id=111
main_hand=,id=222
''')
        response = self.client.post('/api/simc-task/batch/', data=json.dumps({
            'kind': 'gear_candidates', 'name': 'Fury 非法手工装备对比',
            'simc_profile_id': profile.id,
            'base_template_id': self.base_template.id,
            'selected_apl_id': self.default_apl.id,
            'candidates': [{
                'slot': 'head', 'item_id': 444, 'source': 'manual',
                'raw_value': 'neck=,id=444,ilevel=650',
            }],
        }), content_type='application/json')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])
        self.assertIn('槽位', response.json()['error'])

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

    def test_top_players_returns_active_season_spec_top10_for_battlenet_picker(self):
        inactive = SeasonMeta.objects.create(
            season_key='old-season', season_name='旧赛季', is_active=False,
            mplus_zone_id=1, raid_zone_id=1,
        )
        active = SeasonMeta.objects.create(
            season_key='current-season', season_name='当前赛季', is_active=True,
            mplus_zone_id=2, raid_zone_id=2,
        )
        PlayerSpecTopPlayer.objects.create(
            season_id=inactive.id, class_name='Warrior', spec_name='Fury', rank=1,
            score=9999, region='eu', realm='Old Realm', character_name='Oldplayer',
        )
        for index in range(22):
            PlayerSpecTopPlayer.objects.create(
                season_id=active.id,
                class_name='Warrior' if index < 21 else 'Mage',
                spec_name='Protection' if index == 20 else ('Fury' if index % 2 == 0 else 'Arms'),
                rank=index + 1,
                score=5000 - index,
                region='EU' if index % 2 == 0 or index == 20 else 'us',
                realm='Realm 0' if index == 20 else f'Realm {index}',
                character_name='Player0' if index == 20 else f'Player{index}',
            )

        response = self.client.get('/api/simc-battlenet-top-players/?spec=warrior_fury')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['spec'], 'warrior_fury')
        self.assertEqual(payload['season']['id'], active.id)
        self.assertEqual(len(payload['data']), 10)
        self.assertEqual({row['spec'] for row in payload['data']}, {'fury'})
        identities = [(row['region'], row['realm'].casefold(), row['character'].casefold()) for row in payload['data']]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual(payload['data'][0], {
            'id': payload['data'][0]['id'],
            'rank': 1,
            'score': 5000.0,
            'spec': 'fury',
            'region': 'eu',
            'realm': 'Realm 0',
            'character': 'Player0',
            'label': 'Player0 · Realm 0 · EU · Fury',
        })
        self.assertNotIn('Oldplayer', [row['character'] for row in payload['data']])

    def test_top_players_query_does_not_load_large_character_payload_fields(self):
        active = SeasonMeta.objects.create(
            season_key='current-season-lightweight-top10', season_name='当前赛季', is_active=True,
            mplus_zone_id=2, raid_zone_id=2,
        )
        PlayerSpecTopPlayer.objects.create(
            season_id=active.id, class_name='Warrior', spec_name='Fury', rank=1,
            score=5000, region='eu', realm='Kazzak', character_name='Lightweight',
            gear_json=[{'payload': 'large'}], talents_json=[{'payload': 'large'}],
            stats_json={'payload': 'large'}, talent_build_code='LONG_BUILD',
        )

        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get('/api/simc-battlenet-top-players/?spec=warrior_fury')

        self.assertEqual(response.status_code, 200)
        player_queries = [query['sql'].lower() for query in queries.captured_queries
                          if 'wow_spec_top_player' in query['sql'].lower()]
        self.assertEqual(len(player_queries), 1)
        for large_field in ('gear_json', 'talents_json', 'stats_json', 'talent_build_code'):
            self.assertNotIn(large_field, player_queries[0])

    def test_top_players_excludes_cn_characters_from_battlenet_picker(self):
        active = SeasonMeta.objects.create(
            season_key='current-season-cn-filter', season_name='当前赛季', is_active=True,
            mplus_zone_id=2, raid_zone_id=2,
        )
        PlayerSpecTopPlayer.objects.create(
            season_id=active.id, class_name='Warrior', spec_name='Fury', rank=1,
            score=6000, region='cn', realm='国服服务器', character_name='国服角色',
        )
        PlayerSpecTopPlayer.objects.create(
            season_id=active.id, class_name='Warrior', spec_name='Fury', rank=2,
            score=5000, region='eu', realm='Kazzak', character_name='Availableplayer',
        )

        response = self.client.get('/api/simc-battlenet-top-players/?spec=warrior_fury')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([row['character'] for row in payload['data']], ['Availableplayer'])
        self.assertNotIn('cn', [row['region'] for row in payload['data']])

    def test_top_players_rejects_unknown_spec(self):
        response = self.client.get('/api/simc-battlenet-top-players/?spec=warrior_not_a_spec')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])

    def test_preflight_rejects_cn_because_battlenet_cannot_load_cn_characters(self):
        from unittest.mock import patch

        with patch('botend.services.battlenet_preflight.fetch_battlenet_character_preflight') as fetch:
            response = self.client.post('/api/simc-battlenet-preflight/', data=json.dumps({
                'region': 'cn', 'realm': '国服服务器', 'character': '国服角色',
            }), content_type='application/json')

        self.assertEqual(response.status_code, 400)
        self.assertIn('国服角色无法通过 Battle.net 加载', response.json()['error'])
        fetch.assert_not_called()

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

    def test_preflight_freezes_complete_battlenet_player_snapshot(self):
        from botend.services.battlenet_preflight import fetch_battlenet_character_preflight

        profile = {
            'name': 'Snapshotter', 'level': 80, 'race': {'name': 'Orc'},
            'character_class': {'name': 'Warrior'}, 'active_spec': {'name': 'Fury'},
            'realm': {'name': 'Kazzak'},
        }
        equipment = {'equipped_items': [
            {
                'item': {'id': 212048}, 'name': 'Everforged Helm', 'slot': {'type': 'HEAD'},
                'level': {'value': 680}, 'bonus_list': [10255, 10390],
                'enchantments': [{'enchantment_id': 7352, 'display_string': 'Incandescent Essence'}],
                'sockets': [
                    {'item': {'id': 213743}, 'display_string': 'Culminating Blasphemite'},
                    {'item': {'id': 213744}, 'display_string': 'Masterful Ruby'},
                ],
            },
            {'item': {'id': 222222}, 'slot': {'type': 'MAIN_HAND'}, 'level': {'value': 680}},
        ]}
        stats = {'strength': {'effective': 5000}}
        specializations = {
            'active_specialization': {'id': 72},
            'specializations': [{
                'specialization': {'id': 72, 'name': 'Fury'},
                'loadouts': [{'is_active': True, 'talent_loadout_code': 'CwPAAAAAAAAAAAAAAAAAAAAAAMzMzMz'}],
            }],
        }
        with patch('botend.services.battlenet_preflight._token', return_value='token'), patch(
            'botend.services.battlenet_preflight._api_get',
            side_effect=[profile, equipment, stats, specializations],
        ):
            result = fetch_battlenet_character_preflight(
                region='eu', realm='Kazzak', character='Snapshotter', requested_spec='fury',
            )

        snapshot = result['simc_config']['player_equipment']
        self.assertIn('warrior="Snapshotter"', snapshot)
        self.assertIn('level=80', snapshot)
        self.assertIn('race=orc', snapshot)
        self.assertIn('spec=fury', snapshot)
        self.assertIn('head=,id=212048,bonus_id=10255/10390,enchant_id=7352,gem_id=213743/213744', snapshot)
        self.assertIn('main_hand=,id=222222', snapshot)
        self.assertEqual(result['simc_config']['talent'], 'CwPAAAAAAAAAAAAAAAAAAAAAAMzMzMz')
        self.assertNotIn('armory=', snapshot)
        self.assertEqual(result['equipment_summary'], {'count': 2, 'item_level': 680})
        self.assertEqual(result['equipment'][0], {
            'id': 212048,
            'display_name': 'Everforged Helm',
            'slot': 'head',
            'slot_label': '头盔',
            'item_level': 680,
            'enchant': {'id': 7352, 'display_name': 'Incandescent Essence'},
            'gems': [
                {'id': 213743, 'display_name': 'Culminating Blasphemite'},
                {'id': 213744, 'display_name': 'Masterful Ruby'},
            ],
            'bonus_ids': [10255, 10390],
        })
        self.assertEqual(result['equipment'][1]['slot'], 'main_hand')
        self.assertTrue(result['simc_ready'], result)

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

    def test_preflight_rejects_unrecognized_active_spec_for_requested_target(self):
        from botend.services.battlenet_preflight import fetch_battlenet_character_preflight

        profile = {
            'name': 'Unknownspec', 'level': 90,
            'character_class': {'name': 'Warrior'},
            'active_spec': {}, 'realm': {'name': 'Kazzak'},
        }
        equipment = {'equipped_items': [{'level': {'value': 680}}]}
        with patch('botend.services.battlenet_preflight._token', return_value='token'), patch(
            'botend.services.battlenet_preflight._api_get', side_effect=[profile, equipment, {}]
        ):
            result = fetch_battlenet_character_preflight(
                region='eu', realm='Kazzak', character='Unknownspec', requested_spec='fury',
            )

        self.assertFalse(result['simc_ready'])
        self.assertTrue(any('无法识别' in warning for warning in result['warnings']))


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

    def test_workbench_batch_ranking_exposes_named_talent_candidate_details(self):
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Talent report', batch_type='comparison', status=1,
        )
        SimcTask.objects.create(
            user_id=self.user.id, name='Talent candidate', simc_profile_id=0,
            task_type=1, current_status=0, batch=batch, is_active=True,
            candidate_label='手工单体方案', mode_params={
                'candidate_type': 'talent_override', 'is_base': False,
                'talent_override': 'MANUAL_TALENT_BUILD',
                'talent_candidate': {
                    'name': '手工单体方案', 'talent': 'MANUAL_TALENT_BUILD', 'source': 'manual',
                },
            },
        )

        response = self.client.get(f'/api/simc-workbench/batches/{batch.id}/')

        self.assertEqual(response.status_code, 200)
        row = response.json()['data']['ranking'][0]
        self.assertEqual(row['candidate'], {
            'type': 'talent', 'name': '手工单体方案',
            'talent': 'MANUAL_TALENT_BUILD', 'source': 'manual',
        })

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
            ext='{"player_equipment": "secret_config"}',
        )

        response = self.client.get(f'/api/simc-task/batch/?batch_id={batch.id}')
        self.assertEqual(response.status_code, 200)
        payload = response.json()

        # Ensure sensitive fields are not in response
        response_str = json.dumps(payload)
        self.assertNotIn('request_manifest', response_str)
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
        self.assertNotIn('Number(row.pending || 0) > 0', task_js)
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
