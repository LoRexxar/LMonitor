import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from botend.models import (
    SimulationRun,

    SimcBackendBinary,
    SimcContentTemplate,
    SimcProfile,
    SimcTask,
    SimcTaskArtifact,
)


class SimcWorkbenchTemplateResourceTests(TestCase):
    BASE_CONTENT = 'iterations=100\n{player_config}\n'

    def setUp(self):
        self.owner = User.objects.create_user(username='wb-owner', password='pwd')
        self.staff = User.objects.create_user(username='wb-staff', password='pwd', is_staff=True)
        self.system = SimcContentTemplate.objects.create(
            owner_user_id=None,
            name='System Base',
            source=SimcContentTemplate.SOURCE_USER,
            spec='default',
            content=self.BASE_CONTENT,
            is_active=True,
        )
        self.client.force_login(self.owner)

    def _post(self, path, payload):
        return self.client.post(path, json.dumps(payload), content_type='application/json')

    def _put(self, path, payload):
        return self.client.put(path, json.dumps(payload), content_type='application/json')

    def test_list_exposes_only_global_base_template(self):
        private = SimcContentTemplate.objects.create(
            owner_user_id=self.owner.id, name='Legacy Private',
            source='user', spec='legacy', content='warrior="Private"', is_active=True,
        )
        default_player = SimcProfile.objects.create(
            user_id=None, name='Default Player', source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury', class_name='warrior',
            spec='warrior_fury', player_config_mode='manual_equipment',
            player_equipment='warrior="Base"', is_active=True,
        )

        payload = self.client.get('/api/simc-workbench/templates/').json()
        self.assertEqual([row['id'] for row in payload['data']], [self.system.id])
        self.assertTrue(payload['data'][0]['read_only'])
        self.assertFalse(payload['can_write'])
        self.assertNotIn(private.id, [row['id'] for row in payload['data']])

        self.client.force_login(self.staff)
        payload = self.client.get('/api/simc-workbench/templates/').json()
        self.assertEqual([row['id'] for row in payload['data']], [self.system.id])
        self.assertFalse(payload['data'][0]['read_only'])
        self.assertTrue(payload['can_write'])

    def test_create_archive_restore_and_delete_are_disabled(self):
        self.client.force_login(self.staff)
        self.assertEqual(self._post('/api/simc-workbench/templates/', {
            'name': 'Extra', 'content': self.BASE_CONTENT,
        }).status_code, 405)
        for action in ('archive', 'restore'):
            self.assertEqual(self._post(
                f'/api/simc-workbench/templates/{self.system.id}/', {'action': action},
            ).status_code, 405)
        self.assertEqual(self.client.delete(
            f'/api/simc-workbench/templates/{self.system.id}/',
        ).status_code, 405)
        self.system.refresh_from_db()
        self.assertTrue(self.system.is_active)

    def test_regular_user_cannot_edit_system_template(self):
        response = self._put(
            f'/api/simc-workbench/templates/{self.system.id}/',
            {'content': 'iterations=200\n{player_config}\n'},
        )
        self.assertEqual(response.status_code, 403)
        self.system.refresh_from_db()
        self.assertEqual(self.system.content, self.BASE_CONTENT)

    def test_staff_can_edit_only_system_template_content(self):
        self.client.force_login(self.staff)
        updated_content = 'iterations=200\n{player_config}\n'
        response = self._put(
            f'/api/simc-workbench/templates/{self.system.id}/',
            {'content': updated_content},
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.system.refresh_from_db()
        self.assertEqual(self.system.content, updated_content)

        for field, value in (
            ('name', 'Renamed'),
            ('spec', 'warrior_fury'),
            ('class_name', 'warrior'),
            ('source', 'simc_upstream'),
        ):
            response = self._put(
                f'/api/simc-workbench/templates/{self.system.id}/',
                {'content': updated_content, field: value},
            )
            self.assertEqual(response.status_code, 400, field)

    def test_internal_or_private_templates_are_not_workbench_resources(self):
        private = SimcContentTemplate.objects.create(
            owner_user_id=self.owner.id, name='Legacy Private',
            source='user', spec='legacy', content='warrior="Private"', is_active=True,
        )
        default_player = SimcProfile.objects.create(
            user_id=None, name='Default Player', source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury', class_name='warrior',
            spec='warrior_fury', player_config_mode='manual_equipment',
            player_equipment='warrior="Base"', is_active=True,
        )
        self.client.force_login(self.staff)
        path = f'/api/simc-workbench/templates/{private.id}/'
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self._put(path, {'content': self.BASE_CONTENT}).status_code, 404)
        self.assertTrue(SimcProfile.objects.filter(pk=default_player.id).exists())

    def test_split_resource_tables_keep_system_base_and_internal_players(self):
        duplicate = SimcContentTemplate.objects.create(
            owner_user_id=None, name='Old Base',
            source='user', spec='legacy', content=self.BASE_CONTENT, is_active=False,
        )
        default_player = SimcProfile.objects.create(
            user_id=None, name='Default Player', source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            system_key='simc_upstream:warrior_fury', class_name='warrior',
            spec='warrior_fury', player_config_mode='manual_equipment',
            player_equipment='warrior="Base"', is_active=True,
        )

        self.assertTrue(SimcContentTemplate.objects.filter(pk=self.system.pk).exists())
        self.assertTrue(SimcContentTemplate.objects.filter(pk=duplicate.pk).exists())
        self.assertTrue(SimcProfile.objects.filter(pk=default_player.pk).exists())


class SimcWorkbenchHistoryResourceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='history-owner', password='pwd')
        self.other = User.objects.create_user(username='history-other', password='pwd')
        self.client.force_login(self.user)
        self.backend = SimcBackendBinary.objects.create(
            identifier='test-history', name='Test History', simc_path='/tmp/simc',
        )
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
        task = SimcTask.objects.create(
            user_id=self.user.id, name='Safe Task', simc_profile_id=0,
            current_status=3, task_type=1, mode='comparison',
            error_detail='SECRET ERROR', ext='{"diagnostic":"SECRET EXT"}',
            result_file='/secret/server/path.html')
        run = SimulationRun.objects.create(
            task=task, sequence=1, candidate_key='safe', candidate_label='Safe candidate',
            status='failed', candidate_params={
                'candidate_type': 'talent', 'is_base': True,
                'request_manifest': 'SECRET MANIFEST', 'apl_override': 'SECRET APL',
            }, resource_manifest={'path': 'SECRET RESOURCE PATH'},
            error_detail='SECRET TRACEBACK', result_summary={'dps': 123, 'raw': 'SECRET RAW'},
        )
        foreign_task = SimcTask.objects.create(
            user_id=self.other.id, name='Foreign Task', simc_profile_id=0, mode='comparison')
        SimulationRun.objects.create(
            task=foreign_task, sequence=1, candidate_label='Foreign Member', status='completed',
            result_summary={'dps': 999999},
        )
        response = self.client.get(f'/api/simc-workbench/tasks/{task.id}/')
        self.assertEqual(response.status_code, 200)
        payload = response.json()['data']
        self.assertEqual([row['id'] for row in payload['runs']], [run.id])
        member = payload['runs'][0]
        for field in ('id', 'sequence', 'status', 'candidate_label', 'result_summary', 'error_summary'):
            self.assertIn(field, member)
        serialized = json.dumps(payload, ensure_ascii=False)
        for secret in ('SECRET MANIFEST', 'SECRET TRACEBACK', 'SECRET ERROR', 'SECRET EXT',
                       'SECRET APL', 'SECRET RESOURCE PATH', 'SECRET RAW', '/secret/server/path.html'):
            self.assertNotIn(secret, serialized)
        self.assertNotIn('Foreign Member', serialized)

        self.assertEqual(self.client.get(
            f'/api/simc-workbench/tasks/{foreign_task.id}/').status_code, 404)

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

    def test_task_detail_parses_summary_from_latest_run_bound_artifact(self):
        task = SimcTask.objects.create(
            user_id=self.user.id, name='Detailed report', simc_profile_id=0,
            backend=self.backend,
            current_status=2, result_file='latest-task-pointer.html')
        old_run = SimulationRun.objects.create(
            task=task, sequence=1, status='completed', result_summary={'dps': 111})
        latest_run = SimulationRun.objects.create(
            task=task, sequence=2, status='completed', result_summary={'dps': 95132})
        SimcTaskArtifact.objects.create(
            task=task, run=old_run, artifact_type='html_report',
            file_path='simc_results/old_run_1.html')
        latest_artifact = SimcTaskArtifact.objects.create(
            task=task, run=latest_run, artifact_type='html_report',
            file_path='simc_results/current_run_2.html')
        report_html = '''<html><body>
          <div id="masthead"><ul class="params">
            <li>Iterations: 1000</li><li>Fight Length: 300</li><li>Fight Style: Patchwerk</li>
          </ul></div>
          <div class="player"><h2>Zornfalte: 95,132 dps</h2><div class="toggle-content"><script type="text/x-deferred-html"><ul class="params">
            <li><b>Race:</b> Orc</li><li><b>Class:</b> Warrior</li><li><b>Spec:</b> Fury</li><li><b>Level:</b> 90</li>
          </ul><table class="sc spec"><tr><th>Talent</th><td>CgEA-test</td></tr></table>
          <table class="sc sort stripetoprow">
            <tr><th>Damage Stats</th><th>DPS</th><th>DPS%</th></tr>
            <tr class="toprow"><td>Rampage</td><td>20,000</td><td>21.0%</td></tr>
          </table></script></div></div>
        </body></html>'''
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / 'current_run_2.html'
            report_path.write_text(report_html, encoding='utf-8')
            with patch(
                'botend.services.simc_artifacts._validated_result',
                return_value=(report_path, 'simc_results/current_run_2.html'),
            ) as validated, patch(
                'botend.dashboard.api.ConvertTextAPIView.bilingual_pairs',
                return_value=([('action', 'rampage', '暴怒')], []),
            ) as bilingual_pairs:
                response = self.client.get(f'/api/simc-workbench/tasks/{task.id}/')

        self.assertEqual(response.status_code, 200)
        payload = response.json()['data']
        detail = payload['report_summary']
        self.assertEqual(detail['dps'], 95132)
        self.assertEqual(detail['character'], {
            'name': 'Zornfalte', 'race': '兽人', 'race_en': 'Orc',
            'class': '战士', 'class_en': 'Warrior',
            'spec': '狂怒', 'spec_en': 'Fury', 'level': '90'})
        self.assertEqual(detail['simulation']['fight_style'], '木桩战')
        self.assertEqual(detail['simulation']['fight_style_en'], 'Patchwerk')
        self.assertEqual(detail['top_abilities'][0]['name'], '暴怒')
        self.assertEqual(detail['top_abilities'][0]['name_en'], 'Rampage')
        bilingual_pairs.assert_called_once_with('warrior_fury')
        validated.assert_called_once_with(task, 'current_run_2.html', run=latest_run)
        self.assertEqual(payload['report_artifact_id'], latest_artifact.id)

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
        csp = response['Content-Security-Policy']
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'unsafe-inline'", csp)
        self.assertIn('sandbox allow-scripts', csp)
        self.assertNotIn('allow-same-origin', csp)
        self.assertNotIn('https:', csp)
        self.assertEqual(
            self.client.get(f'/api/simc-workbench/tasks/{foreign.id}/report-preview/').status_code,
            404,
        )

    def test_run_bound_artifact_preview_allows_only_inline_report_scripts(self):
        task = SimcTask.objects.create(
            user_id=self.user.id, name='Interactive artifact', simc_profile_id=0,
            current_status=2, result_file='interactive_run_1.html')
        run = SimulationRun.objects.create(
            task=task, sequence=1, status='completed', result_summary={'dps': 95132})
        artifact = SimcTaskArtifact.objects.create(
            task=task, run=run, artifact_type='html_report',
            file_path='simc_results/interactive_run_1.html')
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / 'interactive_run_1.html'
            report.write_text('<html><script>window.simcReport = true;</script></html>', encoding='utf-8')
            with patch(
                'botend.services.simc_artifacts._validated_result',
                return_value=(report, 'simc_results/interactive_run_1.html'),
            ):
                response = self.client.get(
                    f'/api/simc-workbench/artifacts/{artifact.id}/preview/')
        self.assertEqual(response.status_code, 200)
        csp = response['Content-Security-Policy']
        self.assertIn("script-src 'unsafe-inline'", csp)
        self.assertIn('sandbox allow-scripts', csp)
        self.assertNotIn('allow-same-origin', csp)
        self.assertNotIn('https:', csp)
