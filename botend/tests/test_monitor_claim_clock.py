"""Post-lock lease clocks on real InnoDB connections, with an injected clock.

These are controlled-time regressions, not observations of a natural 06:00
transition or a real 601-second wait. Only timezone.now is replaced: the holder,
claim/renew/completion worker, SQL execution and leases use the real database.
SQLite explicitly skips these cases and is not concurrency evidence. Run on an isolated MySQL
schema; observing INNODB_TRX requires the test account's PROCESS privilege.
"""
from datetime import datetime, timedelta
from threading import Event, Thread
from time import monotonic
from unittest import skipUnless
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from botend import plugin_sync as scheduler
from botend.models import MonitorTask, MonitorTaskLease


BJT = ZoneInfo('Asia/Shanghai')
BEFORE_SIX = datetime(2026, 11, 11, 5, 59, 59, tzinfo=BJT)
TTL_SECONDS = 600


@skipUnless(connection.vendor == 'mysql',
            'Requires real MySQL/InnoDB row-lock waits; SQLite is not concurrency evidence')
class MonitorClaimClockInnoDBTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        with connection.cursor() as cursor:
            cursor.execute('SELECT VERSION(), CONNECTION_ID()')
            version, self.holder_connection_id = cursor.fetchone()
            self.assertNotIn('MariaDB', version, 'This gate requires real MySQL')
            cursor.execute(
                'SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES '
                'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN (%s, %s)',
                [MonitorTask._meta.db_table, MonitorTaskLease._meta.db_table],
            )
            self.assertEqual(dict(cursor.fetchall()), {
                MonitorTask._meta.db_table: 'InnoDB',
                MonitorTaskLease._meta.db_table: 'InnoDB',
            })
        MonitorTask.objects.all().delete()
        self.clock = self.enterContext(patch.object(scheduler.timezone, 'now', return_value=BEFORE_SIX))

    def task(self, name, **overrides):
        # Out-of-registry type deliberately exercises name fallback, never an
        # unrelated real plugin's dispatch classification.
        values = dict(type=900 + MonitorTask.objects.count(), target='', env_limit=0,
                      is_active=True, wait_time=scheduler.monitor_default_wait_time(name),
                      last_scan_time=BEFORE_SIX - timedelta(days=2),
                      flag='unchanged-before-claim')
        values.update(overrides)
        return MonitorTask.objects.create(name=name, **values)

    def claim_after_real_parent_lock_wait(self, after_lock_time, operation=None):
        """Advance the injected clock only after MySQL reports LOCK WAIT."""
        entered = Event()
        returned = Event()
        done = Event()
        outcome = {}
        quoted_table = connection.ops.quote_name(MonitorTask._meta.db_table).upper()

        def parent_lock_handshake(execute, sql, params, many, context):
            normalized = ' '.join(sql.upper().split())
            parent_select = (normalized.startswith('SELECT ')
                             and 'FROM ' + quoted_table + ' ' in normalized
                             and 'FOR UPDATE' in normalized)
            if parent_select:
                entered.set()
            # No synthetic blocking here: execute must actually wait in InnoDB.
            result = execute(sql, params, many, context)
            if parent_select:
                returned.set()
            return result

        def claimer():
            connections.close_all()
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SET SESSION innodb_lock_wait_timeout=10')
                    cursor.execute('SELECT CONNECTION_ID()')
                    outcome['connection_id'] = cursor.fetchone()[0]
                with connection.execute_wrapper(parent_lock_handshake):
                    # Crucial: do NOT pass now; production callers need a fresh
                    # clock after the row locks, not a deterministic probe time.
                    if operation is None:
                        outcome['task'] = scheduler.claim_next_monitor_task(
                            lease_owner='post-lock-clock-worker', lease_seconds=TTL_SECONDS)
                    else:
                        outcome['task'] = operation()
            except BaseException as exc:
                outcome['error'] = exc
            finally:
                connections.close_all()
                done.set()

        thread = Thread(target=claimer, name='monitor-claim-clock', daemon=True)
        try:
            with transaction.atomic():
                locked = list(MonitorTask.objects.select_for_update().order_by('id'))
                self.assertEqual(len(locked), MonitorTask.objects.count())
                self.assertTrue(locked, 'the holder must lock real parent rows')
                thread.start()
                self.assertTrue(entered.wait(5), 'claimer never reached the parent SELECT FOR UPDATE: {}'.format(outcome))
                self.assertNotEqual(outcome['connection_id'], self.holder_connection_id)
                # The wrapper signals before execute. Also observe LOCK WAIT in
                # the server so a merely scheduled Python thread is insufficient.
                deadline = monotonic() + 5
                while True:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            'SELECT trx_state FROM information_schema.INNODB_TRX '
                            'WHERE trx_mysql_thread_id=%s', [outcome['connection_id']])
                        state = cursor.fetchone()
                    if state == ('LOCK WAIT',):
                        break
                    self.assertFalse(done.is_set(), 'claimer finished instead of waiting: {}'.format(outcome))
                    self.assertLess(monotonic(), deadline, 'MySQL never reported the real parent-row lock wait')
                    done.wait(0.02)
                self.assertFalse(returned.is_set(), 'parent lock query must still be blocked')
                self.assertFalse(done.is_set(), 'operation must remain in flight while its parent is locked')
                self.clock.return_value = after_lock_time
                # Exiting atomic releases ALL parent locks only after advancing
                # the injected clock; no wall-clock boundary/601-second sleep.
        finally:
            # Join only after atomic has released locks, including on failure.
            if thread.ident is not None:
                thread.join(15)
        self.assertFalse(thread.is_alive(), 'claimer did not terminate after holder released its locks')
        self.assertTrue(done.is_set())
        if 'error' in outcome:
            raise outcome['error']
        self.assertTrue(returned.is_set())
        return outcome['task']

    def test_six_am_boundary_rechecks_long_capacity_after_parent_lock_wait(self):
        running = self.task('MaxrollClassGuideMonitor')
        held = MonitorTaskLease.objects.create(
            task=running, owner='existing-long-worker', is_manual=False,
            claimed_at=BEFORE_SIX - timedelta(minutes=30),
            # Still valid after the controlled 06:00 transition.
            expires_at=BEFORE_SIX + timedelta(days=1))
        pending = self.task('SpecDetailPlayerMonitor')
        before_tasks = list(MonitorTask.objects.order_by('pk').values())
        before_leases = list(MonitorTaskLease.objects.order_by('pk').values())

        got = self.claim_after_real_parent_lock_wait(BEFORE_SIX + timedelta(seconds=2))

        self.assertIsNone(got, '06:00:01 has only one long slot, already occupied')
        self.assertEqual(list(MonitorTask.objects.order_by('pk').values()), before_tasks,
                         'rejected claim must not advance last_scan_time or mutate the daily flag')
        self.assertEqual(list(MonitorTaskLease.objects.order_by('pk').values()), before_leases)
        self.assertFalse(MonitorTaskLease.objects.filter(task=pending).exists())
        self.assertTrue(MonitorTaskLease.objects.filter(pk=held.pk, owner=held.owner).exists())

    def test_ttl_starts_after_parent_lock_wait_longer_than_the_ttl(self):
        pending = self.task('PortalPostMonitor')
        after_lock_time = BEFORE_SIX + timedelta(seconds=601)

        got = self.claim_after_real_parent_lock_wait(after_lock_time)

        self.assertIsNotNone(got)
        self.assertEqual(got.pk, pending.pk)
        lease = MonitorTaskLease.objects.get(task=pending)
        self.assertEqual(lease.owner, 'post-lock-clock-worker')
        self.assertFalse(lease.is_manual)
        self.assertEqual(lease.claimed_at, after_lock_time)
        self.assertEqual(lease.expires_at, after_lock_time + timedelta(seconds=TTL_SECONDS))
        self.assertGreater(lease.expires_at, timezone.now(),
                           'lease must be live against the injected current clock on return')
        pending.refresh_from_db()
        self.assertEqual(pending.last_scan_time, after_lock_time)
        self.assertEqual(pending.flag, 'unchanged-before-claim')

        # Exercise the real renewal API without a now argument, slightly later
        # on the SAME controlled clock. An already-expired pre-lock TTL fails.
        renewed_at = after_lock_time + timedelta(seconds=1)
        self.clock.return_value = renewed_at
        self.assertTrue(scheduler.renew_monitor_task_lease(
            pending.pk, lease.owner, lease_seconds=TTL_SECONDS))
        lease.refresh_from_db()
        self.assertEqual(lease.claimed_at, after_lock_time)
        self.assertEqual(lease.expires_at, renewed_at + timedelta(seconds=TTL_SECONDS))

    def test_renew_rejects_lease_that_expires_during_parent_lock_wait(self):
        task = self.task('PortalPostMonitor')
        lease = MonitorTaskLease.objects.create(
            task=task, owner='expiring-renew-worker', is_manual=False,
            claimed_at=BEFORE_SIX - timedelta(seconds=1),
            expires_at=BEFORE_SIX + timedelta(seconds=TTL_SECONDS))
        before_task = MonitorTask.objects.filter(pk=task.pk).values().get()
        before_lease = MonitorTaskLease.objects.filter(pk=lease.pk).values().get()
        self.assertGreater(lease.expires_at, timezone.now(), 'lease starts valid')
        after_lock_time = lease.expires_at + timedelta(seconds=1)

        got = self.claim_after_real_parent_lock_wait(
            after_lock_time,
            # No now argument: exercise the production post-lock clock path.
            operation=lambda: scheduler.renew_monitor_task_lease(
                task.pk, lease.owner, lease_seconds=TTL_SECONDS))

        self.assertIs(got, False, 'renewal must not revive a lease expired while waiting')
        self.assertEqual(MonitorTask.objects.filter(pk=task.pk).values().get(), before_task)
        self.assertEqual(MonitorTaskLease.objects.filter(pk=lease.pk).values().get(), before_lease,
                         'rejected renewal must preserve the exact owner lease and its expiry')
        lease.refresh_from_db()
        self.assertLess(lease.expires_at, timezone.now())

    def test_completion_rejects_lease_that_expires_during_parent_lock_wait(self):
        task = self.task('PortalPostMonitor')
        lease = MonitorTaskLease.objects.create(
            task=task, owner='expiring-completion-worker', is_manual=False,
            claimed_at=BEFORE_SIX - timedelta(seconds=1),
            expires_at=BEFORE_SIX + timedelta(seconds=TTL_SECONDS))
        before_task = MonitorTask.objects.filter(pk=task.pk).values().get()
        before_lease = MonitorTaskLease.objects.filter(pk=lease.pk).values().get()
        self.assertGreater(lease.expires_at, timezone.now(), 'lease starts valid')
        after_lock_time = lease.expires_at + timedelta(seconds=1)

        got = self.claim_after_real_parent_lock_wait(
            after_lock_time,
            # No now argument; a stale pre-lock clock would publish and delete.
            operation=lambda: scheduler.complete_monitor_task_lease(
                task.pk, lease.owner, task_updates={'flag': 'completed-after-expiry'}))

        self.assertIs(got, False, 'completion must reject a lease expired while waiting')
        self.assertEqual(MonitorTask.objects.filter(pk=task.pk).values().get(), before_task,
                         'rejected completion must not publish flag or any task update')
        self.assertEqual(MonitorTaskLease.objects.filter(pk=lease.pk).values().get(), before_lease,
                         'rejected completion must not delete or mutate the exact owner lease')
        task.refresh_from_db()
        self.assertEqual(task.flag, 'unchanged-before-claim')
        lease.refresh_from_db()
        self.assertLess(lease.expires_at, timezone.now())
