import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from botend.models import (
    SimcAplKeywordPair,
    SimcContentTemplate,
    SimcProfile,
    SimcTask,
    SimcTaskArtifact,
    SimcTaskBatch,
)


class SimcWorkbenchTemplateResourceTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='wb-owner', password='pwd')
        self.other = User.objects.create_user(username='wb-other', password='pwd')
        self.staff = User.objects.create_user(username='wb-staff', password='pwd', is_staff=True)
        self.client.force_login(self.owner)

    def _template(self, *, owner_id, name, template_type='custom_apl', source='user', content='actions=/wait'):
        return SimcContentTemplate.objects.create(
            owner_user_id=owner_id, name=name, template_type=template_type,
            source=source, spec=name.lower().replace(' ', '_'), content=content,
            is_active=True,
        )

    def _post(self, path, payload):
        return self.client.post(path, json.dumps(payload), content_type='application/json')

    def _put(self, path, payload):
        return self.client.put(path, json.dumps(payload), content_type='application/json')

    def test_row_permissions_and_owner_isolation(self):
        own = self._template(owner_id=self.owner.id, name='Own')
        foreign = self._template(owner_id=self.other.id, name='Foreign')
        system = self._template(owner_id=None, name='System')
        upstream = self._template(owner_id=None, name='Upstream', source='simc_upstream')
        default_player = self._template(
            owner_id=None, name='Default Player', template_type='default_player', content='warrior="Base"')

        payload = self.client.get('/api/simc-workbench/templates/').json()
        rows = {row['id']: row for row in payload['data']}
        self.assertFalse(rows[own.id]['read_only'])
        self.assertTrue(rows[system.id]['read_only'])
        self.assertTrue(rows[upstream.id]['read_only'])
        self.assertTrue(rows[default_player.id]['read_only'])
        self.assertNotIn(foreign.id, rows)
        self.assertTrue(payload['can_write'])

        self.client.force_login(self.staff)
        rows = {row['id']: row for row in self.client.get('/api/simc-workbench/templates/').json()['data']}
        self.assertFalse(rows[system.id]['read_only'])
        self.assertTrue(rows[upstream.id]['read_only'])
        self.assertTrue(rows[default_player.id]['read_only'])
        self.assertNotIn(foreign.id, rows)
        self.assertEqual(self._put(
            f'/api/simc-workbench/templates/{foreign.id}/', {'name': 'stolen'}).status_code, 404)

    def test_regular_owner_create_edit_archive_restore_and_type_validation(self):
        response = self._post('/api/simc-workbench/templates/', {
            'name': 'Personal Base', 'template_type': 'base_template',
            'spec': 'fury', 'content': 'iterations=100\n{player_config}\n',
            'owner_user_id': None,
        })
        self.assertEqual(response.status_code, 200, response.content)
        template = SimcContentTemplate.objects.get(id=response.json()['data']['id'])
        self.assertEqual(template.owner_user_id, self.owner.id)
        self.assertEqual(template.source, SimcContentTemplate.SOURCE_USER)

        invalid = self._put(f'/api/simc-workbench/templates/{template.id}/', {
            'template_type': 'base_template', 'content': 'iterations=200',
        })
        self.assertEqual(invalid.status_code, 400)
        template.refresh_from_db()
        self.assertIn('{player_config}', template.content)

        update = self._put(f'/api/simc-workbench/templates/{template.id}/', {
            'name': 'Personal APL', 'template_type': 'custom_apl', 'content': 'actions=/charge',
        })
        self.assertEqual(update.status_code, 200, update.content)
        template.refresh_from_db()
        self.assertEqual(template.template_type, SimcContentTemplate.TYPE_CUSTOM_APL)
        self.assertEqual(template.content, 'actions=/charge')

        for action, expected in (('archive', False), ('restore', True)):
            response = self._post(
                f'/api/simc-workbench/templates/{template.id}/', {'action': action})
            self.assertEqual(response.status_code, 200, response.content)
            template.refresh_from_db()
            self.assertEqual(template.is_active, expected)

    def test_protected_templates_and_invalid_types_are_rejected(self):
        owned = self._template(owner_id=self.owner.id, name='Mutable')
        upstream = self._template(owner_id=self.owner.id, name='Readonly Upstream', source='simc_upstream')
        default_player = self._template(
            owner_id=self.owner.id, name='Readonly Player', template_type='default_player', content='warrior="X"')

        for template in (upstream, default_player):
            self.assertEqual(self._put(
                f'/api/simc-workbench/templates/{template.id}/', {'content': 'changed'}).status_code, 403)
            self.assertEqual(self._post(
                f'/api/simc-workbench/templates/{template.id}/', {'action': 'archive'}).status_code, 403)

        self.assertEqual(self._post('/api/simc-workbench/templates/', {
            'name': 'Forbidden', 'template_type': 'default_player', 'content': 'warrior="X"'}).status_code, 403)
        self.assertEqual(self._put(f'/api/simc-workbench/templates/{owned.id}/', {
            'template_type': 'default_player', 'content': 'warrior="X"'}).status_code, 403)
        self.assertEqual(self._put(f'/api/simc-workbench/templates/{owned.id}/', {
            'template_type': 'not-real', 'content': 'actions=/x'}).status_code, 400)

    def test_staff_can_manage_global_but_not_private_template(self):
        system = self._template(owner_id=None, name='Managed System')
        private = self._template(owner_id=self.other.id, name='Private')
        self.client.force_login(self.staff)
        self.assertEqual(self._put(
            f'/api/simc-workbench/templates/{system.id}/', {'content': 'actions=/system'}).status_code, 200)
        self.assertEqual(self._post(
            f'/api/simc-workbench/templates/{system.id}/', {'action': 'archive'}).status_code, 200)
        self.assertEqual(self._put(
            f'/api/simc-workbench/templates/{private.id}/', {'content': 'actions=/stolen'}).status_code, 404)

    def test_foreign_protected_templates_are_indistinguishable_from_missing(self):
        foreign_upstream = self._template(
            owner_id=self.other.id, name='Foreign Upstream', source='simc_upstream')
        foreign_default_player = self._template(
            owner_id=self.other.id, name='Foreign Default Player',
            template_type='default_player', content='warrior="Foreign"')

        for template in (foreign_upstream, foreign_default_player):
            path = f'/api/simc-workbench/templates/{template.id}/'
            self.assertEqual(self._put(path, {'content': 'actions=/stolen'}).status_code, 404)
            self.assertEqual(self._post(path, {'action': 'archive'}).status_code, 404)

        self.client.force_login(self.staff)
        for template in (foreign_upstream, foreign_default_player):
            path = f'/api/simc-workbench/templates/{template.id}/'
            self.assertEqual(self._put(path, {'content': 'actions=/stolen'}).status_code, 404)
            self.assertEqual(self._post(path, {'action': 'archive'}).status_code, 404)


class SimcWorkbenchKeywordResourceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='kw-user', password='pwd')
        self.staff = User.objects.create_user(username='kw-staff', password='pwd', is_staff=True)
        self.keyword = SimcAplKeywordPair.objects.create(
            apl_keyword='actions=/old', cn_keyword='旧', description='old')

    def test_read_for_all_but_only_staff_can_write_and_apl_keyword_is_immutable(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get('/api/simc-workbench/apl-keywords/').status_code, 200)
        self.assertEqual(self.client.get(
            f'/api/simc-workbench/apl-keywords/{self.keyword.id}/').status_code, 200)
        denied = self.client.put(
            f'/api/simc-workbench/apl-keywords/{self.keyword.id}/',
            json.dumps({'cn_keyword': '改'}), content_type='application/json')
        self.assertEqual(denied.status_code, 403)

        self.client.force_login(self.staff)
        immutable = self.client.put(
            f'/api/simc-workbench/apl-keywords/{self.keyword.id}/',
            json.dumps({'apl_keyword': 'actions=/new', 'cn_keyword': '新'}),
            content_type='application/json')
        self.assertEqual(immutable.status_code, 400)
        self.keyword.refresh_from_db()
        self.assertEqual(self.keyword.apl_keyword, 'actions=/old')
        self.assertEqual(self.keyword.cn_keyword, '旧')

        updated = self.client.put(
            f'/api/simc-workbench/apl-keywords/{self.keyword.id}/',
            json.dumps({'cn_keyword': '已更新', 'description': 'new'}),
            content_type='application/json')
        self.assertEqual(updated.status_code, 200)
        self.keyword.refresh_from_db()
        self.assertEqual(self.keyword.cn_keyword, '已更新')


class SimcWorkbenchHistoryResourceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='history-owner', password='pwd')
        self.other = User.objects.create_user(username='history-other', password='pwd')
        self.client.force_login(self.user)
        self.profile = SimcProfile.objects.create(
            user_id=self.user.id, name='Inactive Profile', spec='fury', is_active=False)

    def test_inactive_profile_remains_visible_to_owner_and_cannot_execute(self):
        detail = self.client.get(f'/api/simc-workbench/profiles/{self.profile.id}/')
        self.assertEqual(detail.status_code, 200)
        self.assertFalse(detail.json()['data']['is_active'])
        ids = [row['id'] for row in self.client.get('/api/simc-workbench/profiles/').json()['data']]
        self.assertIn(self.profile.id, ids)
        execute = self.client.post('/api/simc-task/', json.dumps({
            'name': 'must fail', 'simc_profile_id': self.profile.id,
        }), content_type='application/json')
        self.assertFalse(execute.json()['success'])
        self.assertFalse(SimcTask.objects.filter(name='must fail').exists())

    def test_batch_detail_has_only_safe_owned_member_summaries(self):
        batch = SimcTaskBatch.objects.create(
            user_id=self.user.id, name='Owned Batch', request_manifest='SECRET MANIFEST',
            error_detail='SECRET TRACEBACK', status=1)
        task = SimcTask.objects.create(
            user_id=self.user.id, batch=batch, name='Safe Task', simc_profile_id=0,
            current_status=3, task_type=2, final_simc_content='SECRET SIMC',
            error_detail='SECRET ERROR', ext='{"raw_simc":"SECRET RAW"}',
            result_file='/secret/server/path.html')
        foreign_member = SimcTask.objects.create(
            user_id=self.other.id, batch=batch, name='Foreign Member', simc_profile_id=0)
        response = self.client.get(f'/api/simc-workbench/batches/{batch.id}/')
        self.assertEqual(response.status_code, 200)
        payload = response.json()['data']
        self.assertEqual([row['id'] for row in payload['tasks']], [task.id])
        member = payload['tasks'][0]
        for field in ('id', 'name', 'status', 'status_label', 'task_type', 'updated_at', 'can_view'):
            self.assertIn(field, member)
        serialized = json.dumps(payload, ensure_ascii=False)
        for secret in ('SECRET MANIFEST', 'SECRET TRACEBACK', 'SECRET SIMC', 'SECRET ERROR', 'SECRET RAW', '/secret/server/path.html'):
            self.assertNotIn(secret, serialized)
        self.assertNotIn(foreign_member.id, [row['id'] for row in payload['tasks']])

        foreign_batch = SimcTaskBatch.objects.create(user_id=self.other.id, name='Foreign Batch')
        self.assertEqual(self.client.get(
            f'/api/simc-workbench/batches/{foreign_batch.id}/').status_code, 404)

    def test_artifact_list_is_paginated_filtered_and_owner_isolated(self):
        owner_task = SimcTask.objects.create(
            user_id=self.user.id, name='Owner Task', simc_profile_id=0)
        other_task = SimcTask.objects.create(
            user_id=self.other.id, name='Other Task', simc_profile_id=0)
        artifacts = []
        for index, artifact_type in enumerate(('html_report', 'json_stats', 'html_report', 'log', 'html_report')):
            artifacts.append(SimcTaskArtifact.objects.create(
                task=owner_task, artifact_type=artifact_type,
                file_path=f'simc_results/private-{index}.html', file_size=index))
        foreign = SimcTaskArtifact.objects.create(
            task=other_task, artifact_type='html_report',
            file_path='simc_results/foreign-secret.html')

        first = self.client.get('/api/simc-workbench/artifacts/?page=1&page_size=2').json()
        self.assertEqual(first['pagination'], {
            'page': 1, 'page_size': 2, 'total': 5, 'total_pages': 3,
        })
        self.assertEqual(len(first['data']), 2)
        second = self.client.get('/api/simc-workbench/artifacts/?page=2&page_size=2').json()
        self.assertEqual(len(second['data']), 2)
        self.assertTrue({row['id'] for row in first['data']}.isdisjoint(
            {row['id'] for row in second['data']}))

        filtered = self.client.get(
            f'/api/simc-workbench/artifacts/?task_id={owner_task.id}&artifact_type=html_report&page_size=50').json()
        self.assertEqual(filtered['pagination']['total'], 3)
        self.assertEqual({row['artifact_type'] for row in filtered['data']}, {'html_report'})
        self.assertTrue(all(row['can_preview'] for row in filtered['data']))
        self.assertTrue(all('preview_url' in row for row in filtered['data']))
        all_rows = first['data'] + second['data']
        non_html_rows = [row for row in all_rows if row['artifact_type'] != 'html_report']
        self.assertTrue(non_html_rows)
        self.assertTrue(all(row['can_preview'] is False for row in non_html_rows))
        self.assertTrue(all('preview_url' not in row for row in non_html_rows))
        serialized = json.dumps(filtered, ensure_ascii=False)
        self.assertNotIn('file_path', serialized)
        self.assertNotIn('foreign-secret', serialized)
        self.assertNotIn(str(foreign.id), [str(row['id']) for row in filtered['data']])

        foreign_filter = self.client.get(
            f'/api/simc-workbench/artifacts/?task_id={other_task.id}').json()
        self.assertEqual(foreign_filter['pagination']['total'], 0)
        self.assertEqual(self.client.get(
            f'/api/simc-workbench/artifacts/{foreign.id}/').status_code, 404)

    def test_artifact_rejects_invalid_pagination_and_clamps_page_size(self):
        self.assertEqual(self.client.get(
            '/api/simc-workbench/artifacts/?page=nope').status_code, 400)
        payload = self.client.get(
            '/api/simc-workbench/artifacts/?page_size=999').json()
        self.assertEqual(payload['pagination']['page_size'], 50)

    def test_archived_task_report_remains_available_to_owner(self):
        task = SimcTask.objects.create(
            user_id=self.user.id,
            name='Archived report',
            simc_profile_id=0,
            result_file='archived-report.html',
            is_active=False,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / 'archived-report.html'
            report_path.write_text('<html>archived</html>', encoding='utf-8')
            with patch(
                'botend.services.simc_artifacts._validated_result',
                return_value=(report_path, 'simc_results/archived-report.html'),
            ):
                response = self.client.get(
                    f'/api/simc-workbench/tasks/{task.id}/report-preview/'
                )
        self.assertEqual(response.status_code, 200)

    def test_legacy_task_report_preview_is_owner_scoped_and_sandbox_safe(self):
        task = SimcTask.objects.create(
            user_id=self.user.id, name='Legacy report', simc_profile_id=0,
            current_status=2, result_file='simc_task_42.html')
        foreign = SimcTask.objects.create(
            user_id=self.other.id, name='Foreign report', simc_profile_id=0,
            current_status=2, result_file='simc_task_99.html')
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'simc_task_42.html'
            report.write_text('<html><body>123 DPS</body></html>', encoding='utf-8')
            with patch('botend.services.simc_artifacts._validated_result', return_value=(report, 'simc_results/simc_task_42.html')):
                response = self.client.get(f'/api/simc-workbench/tasks/{task.id}/report-preview/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/html; charset=utf-8')
        self.assertIn("default-src 'none'", response['Content-Security-Policy'])
        self.assertEqual(
            self.client.get(f'/api/simc-workbench/tasks/{foreign.id}/report-preview/').status_code,
            404,
        )
