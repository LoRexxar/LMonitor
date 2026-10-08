"""Backfill bulk writes must refresh the file-backed selected result projection."""
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.db import DatabaseError, transaction
from django.test import TransactionTestCase, override_settings

from botend.models import (
    SimcBenchmarkCandidate, SimcBenchmarkExecution, SimcBenchmarkPanel,
    SimcBenchmarkResult, SimcTask, SimulationRun, WowItemSnapshot,
)
from botend.services import simc_benchmark_result_snapshot as snapshots


class BackfillDisplayMetadataSnapshotTests(TransactionTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        settings = override_settings(SIMC_BENCHMARK_RESULT_SNAPSHOT_ROOT=temporary.name)
        settings.enable()
        self.addCleanup(settings.disable)
        # Only APL validation at the binary boundary is stubbed by this fixture.
        # ORM, bulk_update, commit hooks, projection and file IO remain real.
        from botend.tests.test_simc_benchmark_execution import SimcBenchmarkExecutionTests
        fixture = SimcBenchmarkExecutionTests()
        fixture.setUp()
        self.panel = fixture.panel
        self.item = WowItemSnapshot.objects.create(
            item_id=123, name='Trinket', catalog_type='equipment',
            slot_key='trinket', inventory_type=12, item_class_id=4,
            metadata={'primary_stat_options': ['strength']},
        )
        self.execution = fixture._create()
        case = self.execution.cases.get()
        fixture._run(case.task, 1, 'completed', 'baseline', dps=1234)
        fixture._run(case.task, 2, 'completed', 'trinket', dps=1300)
        # Seed existing durable results; this test does not execute SimC.
        SimcBenchmarkResult.objects.bulk_create([
            SimcBenchmarkResult(case=case, candidate_key='baseline', dps=1234),
            SimcBenchmarkResult(case=case, candidate_key='trinket', dps=1300),
        ])
        self.candidate = self.panel.candidates.get(key='trinket')
        SimulationRun.objects.filter(candidate_key='trinket').update(
            candidate_params=self.candidate.params,
        )
        # Two changed candidates in one Panel must still enqueue only once.
        SimcBenchmarkCandidate.objects.create(
            panel=self.panel, key='other-trinket', label='Other old label',
            candidate_type='gear_swap', params=self.candidate.params, is_enabled=False,
        )
        self.unchanged_panel = SimcBenchmarkPanel.objects.create(
            name='Unchanged', slug='unchanged-display', created_by_id=fixture.user_id,
            is_active=True,
        )
        SimcBenchmarkCandidate.objects.create(
            panel=self.unchanged_panel, key='trinket', label='回填饰品',
            candidate_type='gear_swap', params=self.candidate.params,
            effect='装备：回填中文特效。', icon_url='/static/wow_icons/small/inv_backfill.jpg',
        )
        spec = self.panel.specs.get()
        profile = spec.profiles.get()
        scenario = self.panel.scenarios.get()
        spec.pk = None
        spec.panel = self.unchanged_panel
        spec.save()
        profile.pk = None
        profile.panel_spec = spec
        profile.save()
        scenario.pk = None
        scenario.panel = self.unchanged_panel
        scenario.save()
        for panel in (self.panel, self.unchanged_panel):
            self.assertTrue(snapshots.rebuild_panel_result_snapshot(panel.pk))
        self.before = snapshots.read_panel_result_snapshot(self.panel, coordinate_filter={})
        self.assertEqual(self._candidate_row(self.before)['label'], 'Trinket')
        # Introduce the metadata only after the old generation has been built.
        WowItemSnapshot.objects.filter(pk=self.item.pk).update(
            name='Backfill Trinket', name_zh='回填饰品',
            description_zh='装备：回填中文特效。', icon='inv_backfill',
        )

    @staticmethod
    def _candidate_row(payload):
        return next(row for row in payload['coordinates'][0]['candidates']
                    if row['key'] == 'trinket')

    def _files(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*.json')}

    def _facts(self):
        # Display-only fields are the command's pre-existing, intentional writes.
        return {
            model.__name__: list(model.objects.order_by('pk').values(*[
                field.attname for field in model._meta.concrete_fields
                if field.name not in excluded
            ]))
            for model, excluded in (
                (SimcTask, set()),
                (SimulationRun, {'candidate_label', 'display_metadata'}),
                (SimcBenchmarkExecution, {'display_metadata'}),
                (SimcBenchmarkResult, set()),
            )
        }

    def test_bulk_backfill_refreshes_only_changed_panel_after_commit_and_noop_does_not_rebuild(self):
        facts = self._facts()
        before_files = self._files()
        output = StringIO()
        with patch.object(snapshots, 'request_snapshot_refresh',
                          wraps=snapshots.request_snapshot_refresh) as enqueue:
            with transaction.atomic():
                call_command('backfill_simc_benchmark_display_metadata', batch_size=1, stdout=output)
                self.assertEqual(self._files(), before_files)
                enqueue.assert_not_called()
            enqueue.assert_called_once_with(self.panel.pk, None)
        self.assertIn('updated 2 candidates, 1 runs, and 1 executions', output.getvalue())
        self.assertTrue(snapshots.snapshot_refresh_pending(self.panel.pk))
        self.assertFalse(snapshots.snapshot_refresh_pending(self.unchanged_panel.pk))
        pending = json.loads((self.root / str(self.panel.pk) / 'pending.json').read_text())
        self.assertEqual(set(pending), {'*'})
        stale = snapshots.read_panel_result_snapshot(self.panel, coordinate_filter={})
        self.assertEqual(stale['snapshot']['state'], 'updating')
        self.assertEqual(stale['snapshot']['generation'], self.before['snapshot']['generation'])
        self.assertTrue(snapshots.rebuild_panel_result_snapshot(self.panel.pk))
        with self.assertNumQueries(0):
            refreshed = snapshots.read_panel_result_snapshot(self.panel, coordinate_filter={})
        row = self._candidate_row(refreshed)
        self.assertEqual(row['label'], '回填饰品')
        self.assertTrue(row['icon_url'].endswith('/small/inv_backfill.jpg'))
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.icon_url, '/static/wow_icons/small/inv_backfill.jpg')
        self.assertIn('装备：回填中文特效。', row['effect'])
        self.assertEqual(row['dps'], self._candidate_row(self.before)['dps'])
        self.assertEqual(refreshed['snapshot']['state'], 'ready')
        self.assertNotEqual(refreshed['snapshot']['generation'], self.before['snapshot']['generation'])
        self.assertEqual(self._facts(), facts)
        files_after_refresh = self._files()
        with patch.object(snapshots, 'request_snapshot_refresh',
                          wraps=snapshots.request_snapshot_refresh) as enqueue:
            repeat = StringIO()
            call_command('backfill_simc_benchmark_display_metadata', stdout=repeat)
            enqueue.assert_not_called()
        self.assertIn('updated 0 candidates, 0 runs, and 0 executions', repeat.getvalue())
        self.assertFalse(snapshots.rebuild_panel_result_snapshot(self.panel.pk))
        self.assertEqual(self._files(), files_after_refresh)

    def test_dry_run_does_not_write_or_enqueue(self):
        before_files = self._files()
        before = list(SimcBenchmarkCandidate.objects.order_by('pk').values())
        call_command('backfill_simc_benchmark_display_metadata', dry_run=True, stdout=StringIO())
        self.assertEqual(list(SimcBenchmarkCandidate.objects.order_by('pk').values()), before)
        self.assertEqual(self._files(), before_files)
        self.assertFalse(snapshots.snapshot_refresh_pending(self.panel.pk))

    def test_late_bulk_failure_rolls_back_real_writes_without_event(self):
        before_files = self._files()
        before = {model: list(model.objects.order_by('pk').values()) for model in (
            SimcBenchmarkCandidate, SimulationRun, SimcBenchmarkExecution,
        )}

        def fail_after_candidate_and_run_writes(*args, **kwargs):
            self.assertEqual(SimcBenchmarkCandidate.objects.get(pk=self.candidate.pk).label, '回填饰品')
            self.assertEqual(SimulationRun.objects.get(candidate_key='trinket').candidate_label, '回填饰品')
            raise DatabaseError('injected final bulk write failure')

        with patch.object(SimcBenchmarkExecution.objects, 'bulk_update',
                          side_effect=fail_after_candidate_and_run_writes):
            with self.assertRaisesMessage(DatabaseError, 'injected final bulk write failure'):
                call_command('backfill_simc_benchmark_display_metadata', stdout=StringIO())
        for model, rows in before.items():
            self.assertEqual(list(model.objects.order_by('pk').values()), rows)
        self.assertEqual(self._files(), before_files)
        self.assertFalse(snapshots.snapshot_refresh_pending(self.panel.pk))

    def test_outer_transaction_rollback_discards_successful_backfill_event(self):
        before_files = self._files()
        with self.assertRaisesMessage(RuntimeError, 'rollback outer transaction'):
            with transaction.atomic():
                call_command('backfill_simc_benchmark_display_metadata', stdout=StringIO())
                self.assertEqual(SimcBenchmarkCandidate.objects.get(pk=self.candidate.pk).label, '回填饰品')
                raise RuntimeError('rollback outer transaction')
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.label, 'Trinket')
        self.assertEqual(self._files(), before_files)
        self.assertFalse(snapshots.snapshot_refresh_pending(self.panel.pk))
