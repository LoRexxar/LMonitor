"""Task stores references; Run remains the sole result-body owner."""
import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from botend.models import SimcBackendBinary, SimcTask, SimulationRun
from botend.services.simc_task_result_storage import (
    candidate_references, result_reference, resolve_task_result, resolve_task_analysis,
    compact_task_results,
)


class TaskResultStorageTests(TestCase):
    def setUp(self):
        self.backend = SimcBackendBinary.objects.create(identifier='storage', name='storage')
        self.task = SimcTask.objects.create(user_id=1, name='storage', simc_profile_id=0,
                                           backend=self.backend, mode='comparison', current_status=2)
        self.runs = [SimulationRun.objects.create(
            task=self.task, sequence=n, candidate_key=str(n), candidate_label=str(n),
            status='completed', result_summary={'dps': n * 100, 'evidence': ['fact' * 1000]},
        ) for n in (1, 2)]

    def legacy(self):
        self.task.result_summary = json.dumps({'runs': [r.result_summary for r in self.runs]})
        self.task.analysis_result = {'attribute_search': {'stop_reason': 'complete'},
                                    'candidates': [dict(run_id=r.pk, status=r.status,
                                                        result_summary=r.result_summary) for r in self.runs]}
        self.task.save(update_fields=['result_summary', 'analysis_result'])
        return json.loads(self.task.result_summary), self.task.analysis_result

    def test_references_rehydrate_exactly_and_do_not_embed_bodies(self):
        self.task.result_summary = json.dumps(result_reference(self.runs))
        self.task.analysis_result = {'candidates': candidate_references(self.runs)}
        self.assertNotIn('evidence', self.task.result_summary)
        self.assertNotIn('result_summary', self.task.analysis_result['candidates'][0])
        self.assertEqual(resolve_task_result(self.task), {'runs': [r.result_summary for r in self.runs]})
        self.assertEqual(resolve_task_analysis(self.task)['candidates'][0]['result_summary'], self.runs[0].result_summary)

    def test_single_result_and_legacy_without_runs_remain_compatible(self):
        self.task.result_summary = json.dumps(result_reference(self.runs[:1]))
        self.assertEqual(resolve_task_result(self.task), self.runs[0].result_summary)
        self.task.result_summary = '{"dps":123,"legacy":"preserved"}'
        self.assertEqual(resolve_task_result(self.task), {'dps': 123, 'legacy': 'preserved'})

    def test_foreign_or_missing_run_is_not_silently_resolved(self):
        self.task.result_summary = json.dumps({'_run_result_refs': 1, 'shape': 'single', 'run_ids': [999999]})
        with self.assertRaises(ValueError):
            resolve_task_result(self.task)

    def test_history_dry_run_apply_repeat_and_restore(self):
        result, analysis = self.legacy()
        dry = compact_task_results(self.task.pk, apply=False)
        self.assertEqual(dry['status'], 'would_compact')
        self.task.refresh_from_db()
        self.assertEqual(json.loads(self.task.result_summary), result)
        done = compact_task_results(self.task.pk, apply=True)
        self.assertEqual(done['status'], 'compacted')
        self.task.refresh_from_db()
        self.assertEqual(resolve_task_result(self.task), result)
        self.assertEqual(resolve_task_analysis(self.task), analysis)
        self.assertEqual(compact_task_results(self.task.pk, apply=True)['status'], 'unchanged')
        self.assertEqual(compact_task_results(self.task.pk, apply=True, restore=True)['status'], 'restored')
        self.task.refresh_from_db()
        self.assertEqual(json.loads(self.task.result_summary), result)
        self.assertEqual(self.task.analysis_result, analysis)

    def test_mismatched_history_and_active_task_are_not_modified(self):
        self.legacy()
        self.task.analysis_result['candidates'][0]['result_summary'] = {'dps': -1}
        self.task.save(update_fields=['analysis_result'])
        self.assertEqual(compact_task_results(self.task.pk, apply=True)['status'], 'unresolved')
        self.task.current_status = 1
        self.task.save(update_fields=['current_status'])
        self.assertEqual(compact_task_results(self.task.pk, apply=True)['status'], 'active')

    def test_terminal_local_worker_and_agent_store_only_references(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        from botend.services.simc_run_control import _finalize_task
        self.task.current_status = 0
        self.task.save(update_fields=['current_status'])
        self.assertTrue(SimcMonitor(None, None).process_reference_task(self.task))
        self.task.refresh_from_db()
        self.assertNotIn('evidence', self.task.result_summary)
        self.assertNotIn('result_summary', self.task.analysis_result['candidates'][0])
        self.assertEqual(resolve_task_result(self.task), {'runs': [r.result_summary for r in self.runs]})
        _finalize_task(self.task, timezone.now())
        self.task.refresh_from_db()
        self.assertNotIn('evidence', self.task.result_summary)
        self.assertEqual(resolve_task_result(self.task), {'runs': [r.result_summary for r in self.runs]})

    def test_public_task_projection_unchanged_after_compaction(self):
        from botend.dashboard.api import SimcWorkbenchAPIView
        self.legacy()
        before = SimcWorkbenchAPIView._task_row(self.task)
        compact_task_results(self.task.pk, apply=True)
        self.task.refresh_from_db()
        self.assertEqual(SimcWorkbenchAPIView._task_row(self.task), before)
        self.assertEqual(before['result_summary']['dps'], 200)

    def test_management_command_defaults_to_dry_run(self):
        result, _ = self.legacy()
        out = StringIO()
        call_command('compact_simc_task_results', task_id=self.task.pk, stdout=out)
        self.task.refresh_from_db()
        self.assertEqual(json.loads(self.task.result_summary), result)
        self.assertEqual(json.loads(out.getvalue())['results'][0]['status'], 'would_compact')
