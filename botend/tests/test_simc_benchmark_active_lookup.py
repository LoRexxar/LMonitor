"""Regression for MySQL filesort carrying a large frozen execution snapshot."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import SimcBenchmarkExecution, SimcBenchmarkPanel, SimcTask
from botend.services.simc_benchmark_execution import (
    BenchmarkExecutionConflict, create_execution,
)


class SimcBenchmarkActiveLookupTests(TestCase):
    def test_manual_duplicate_lookup_sorts_without_json_and_preserves_winner(self):
        owner_id = 701
        panel = SimcBenchmarkPanel.objects.create(
            name='Active lookup', slug='active-lookup', created_by_id=owner_id,
        )
        other_panel = SimcBenchmarkPanel.objects.create(
            name='Other', slug='other-active-lookup', created_by_id=owner_id,
        )
        snapshot = {'frozen_input': 'x' * (2 * 1024 * 1024)}
        tied_older_id = SimcBenchmarkExecution.objects.create(
            panel=panel, config_hash='a' * 64,
        )
        winner = SimcBenchmarkExecution.objects.create(
            panel=panel, config_hash='b' * 64, config_snapshot=snapshot,
            display_metadata={'label': 'Frozen winner'}, status='running',
        )
        higher_id_older_time = SimcBenchmarkExecution.objects.create(
            panel=panel, config_hash='c' * 64,
        )
        now = timezone.now()
        SimcBenchmarkExecution.objects.filter(
            pk__in=[tied_older_id.pk, winner.pk],
        ).update(created_at=now)
        SimcBenchmarkExecution.objects.filter(pk=higher_id_older_time.pk).update(
            created_at=now - timedelta(days=1),
        )
        SimcBenchmarkExecution.objects.create(
            panel=panel, config_hash='d' * 64, completed_at=now, status='success',
        )
        SimcBenchmarkExecution.objects.create(panel=other_panel, config_hash='e' * 64)
        # The existing lookup selects unfinished rows, not the Panel FK shortcut.
        panel.active_execution = higher_id_older_time
        panel.save(update_fields=['active_execution'])
        execution_count = SimcBenchmarkExecution.objects.count()

        for mode in ('supplement', 'full', 'targeted'):
            with self.subTest(mode=mode), CaptureQueriesContext(connection) as queries:
                if mode == 'targeted':
                    with self.assertRaises(BenchmarkExecutionConflict):
                        create_execution(panel, requested_by=owner_id, execution_mode=mode)
                else:
                    result = create_execution(
                        panel, requested_by=owner_id, execution_mode=mode,
                    )
                    self.assertEqual(result.pk, winner.pk)
                    self.assertEqual(result.status, 'running')
                    self.assertEqual(result.config_snapshot, snapshot)
                    self.assertEqual(result.display_metadata, winner.display_metadata)

            execution_queries = [
                row['sql'] for row in queries.captured_queries
                if 'SELECT ' in row['sql'].upper()
                and 'FROM "simc_benchmark_execution"' in row['sql'].replace('`', '"')
            ]
            sorted_queries = [sql for sql in execution_queries if 'ORDER BY' in sql.upper()]
            self.assertEqual(len(sorted_queries), 1)
            for sql in sorted_queries:
                # SQLite cannot raise MySQL 1038; protect the actual SQL shape
                # proven to avoid it by the production read-only MySQL probe.
                projection = sql.upper().split(' FROM ', 1)[0]
                self.assertNotIn('CONFIG_SNAPSHOT', projection)
                self.assertNotIn('DISPLAY_METADATA', projection)
            self.assertTrue(all(
                row['sql'].lstrip().upper().startswith('SELECT ')
                for row in queries.captured_queries
            ))

        with CaptureQueriesContext(connection) as queries:
            with self.assertRaises(PermissionDenied):
                create_execution(panel, requested_by=999)
        self.assertFalse(any(
            'simc_benchmark_execution' in row['sql'] for row in queries.captured_queries
        ))
        self.assertEqual(SimcBenchmarkExecution.objects.count(), execution_count)
        self.assertEqual(SimcTask.objects.count(), 0)
        panel.refresh_from_db()
        self.assertEqual(panel.active_execution_id, higher_id_older_time.pk)
