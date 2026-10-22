"""Exercise only 0239 -> 0240 DDL, including on isolated InnoDB.

Legacy MySQL migrations are deliberately not replayed. The test runner supplies
current schema; reverse 0240 establishes the exact predecessor budget schema,
then execute the real target forward, preserving a pre-existing account row.
"""
from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase
from django.utils import timezone

from botend.models import WclApiBudget, WclApiReservation
from botend.tests.migration_test_helpers import restore_migration_schema


class ParallelBudgetMigrationTests(TransactionTestCase):
    def test_real_0240_forward_reverse_preserves_0239_account(self):
        executor = MigrationExecutor(connection)
        target = ('botend', '0240_wcl_parallel_budget')
        migration = executor.loader.get_migration(*target)
        before = executor.loader.project_state([('botend', '0239_wcl_budget_collection_cycle')])
        def restore_budget_migration():
            # Direct unapply leaves descendants recorded as applied, so a
            # migrate(all_leaves) plan alone cannot see this intermediate gap.
            if target not in MigrationRecorder(connection).applied_migrations():
                executor.apply_migration(before.clone(), migration, fake=False)

        # LIFO: repair the bounded DDL gap first, then flush and restore all
        # repository leaves, even when legacy fixture creation fails.
        self.addCleanup(restore_migration_schema)
        self.addCleanup(restore_budget_migration)
        executor.unapply_migration(before.clone(), migration, fake=False)
        OldBudget = before.apps.get_model('botend', 'WclApiBudget')
        now = timezone.now()
        original = OldBudget.objects.create(account_key='pre-existing-account',
            limit_per_hour=1234, spent=456, reset_at=now + timedelta(seconds=321),
            blocked_until=now + timedelta(seconds=123), costs={'known': 12.5},
            lease_owner='legacy-live', lease_expires_at=now + timedelta(seconds=90))
        original_fields = OldBudget.objects.values().get(pk=original.pk)
        try:
            executor.apply_migration(before.clone(), migration, fake=False)
            self.assertIn(target, MigrationRecorder(connection).applied_migrations())
            migrated = WclApiBudget.objects.get(pk=original.pk)
            for field, value in original_fields.items():
                self.assertEqual(getattr(migrated, field), value)
            self.assertEqual(migrated.collection_workers, 10)
            self.assertEqual((migrated.charged, migrated.completed, migrated.window_generation), (0, 0, 0))
            reservation = WclApiReservation.objects.create(budget=migrated, request_id='probe',
                generation=0, cost=40, expires_at=now + timedelta(seconds=90))
            self.assertEqual(WclApiReservation.objects.get(pk=reservation.pk).budget_id, original.pk)
            with connection.cursor() as cursor:
                columns = {c.name for c in connection.introspection.get_table_description(cursor, migrated._meta.db_table)}
            self.assertIn('collection_workers', columns)
            self.assertIn('wow_wcl_api_reservation', connection.introspection.table_names())
            executor.unapply_migration(before.clone(), migration, fake=False)
            self.assertNotIn(target, MigrationRecorder(connection).applied_migrations())
            self.assertEqual(OldBudget.objects.values().get(pk=original.pk), original_fields)
            self.assertNotIn('wow_wcl_api_reservation', connection.introspection.table_names())
        finally:
            restore_budget_migration()
        print('MIGRATION_PROOF engine=%s 0239->0240->0239->0240 real_DDL default_workers=10 facts_preserved=True' % connection.vendor)
