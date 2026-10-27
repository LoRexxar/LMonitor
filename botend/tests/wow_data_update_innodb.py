"""Explicit MySQL gate: run with botend.tests.wow_data_update_innodb.

Never default-discovered on SQLite. Exercises real parent/lease row locks and
late source failure with independent connections; no production DB or network.
"""
from datetime import timedelta
from threading import Event, Thread

from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from botend.models import (MonitorTask, MonitorTaskLease, MonitorTaskLeaseLost,
                           WowDataUpdateRun, WowDataUpdateState)
from botend.plugin_sync import claim_monitor_task
from botend.services import wow_data_update as update


class DataUpdateInnoDBTests(TransactionTestCase):
    def setUp(self):
        self.assertEqual(connection.vendor, 'mysql', 'explicit InnoDB gate requires MySQL')
        self.task = MonitorTask.objects.create(name=update.TASK_NAME, type=37,
            target='https://wago.tools/builds', is_active=True, wait_time=0,
            last_scan_time=timezone.now() - timedelta(days=1))
        self.old = claim_monitor_task(self.task.pk, lease_owner='old-owner', lease_seconds=600)
        self.assertIsNotNone(self.old)
        self.state = WowDataUpdateState.objects.create(branch='retail', status='preparing',
            published_build='12.1.5.70529', revision=7, report={'keep': 'last-good'},
            projections_pending=False)

    def _thread(self, target):
        errors = []
        def run():
            connections.close_all()
            try:
                target()
            except Exception as exc:
                errors.append(exc)
            finally:
                connections.close_all()
        thread = Thread(target=run, daemon=True)
        thread.start()
        return thread, errors

    def test_blocked_recovery_rechecks_owner_after_parent_lock(self):
        run = WowDataUpdateRun.objects.create(branch='retail', build='12.1.5.70575')
        started, finished = Event(), Event()
        def recover():
            started.set()
            try:
                update.reconcile_interrupted_runs(self.old)
            finally:
                finished.set()
        thread = None
        try:
            with transaction.atomic():
                MonitorTask.objects.select_for_update().get(pk=self.task.pk)
                thread, errors = self._thread(recover)
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.wait(.15), 'recovery must wait on the real parent lock')
                MonitorTaskLease.objects.filter(task=self.task).update(owner='replacement-owner')
        finally:
            if thread is not None:
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], MonitorTaskLeaseLost)
        run.refresh_from_db()
        self.assertEqual(run.status, 'running')
        replacement = MonitorTask.objects.get(pk=self.task.pk)
        replacement._monitor_task_lease_owner = 'replacement-owner'
        update.reconcile_interrupted_runs(replacement)
        run.refresh_from_db()
        self.assertEqual(run.status, 'interrupted')
        completed = run.finished_at
        update.reconcile_interrupted_runs(replacement)
        run.refresh_from_db()
        self.assertEqual(run.finished_at, completed)
        self.state.refresh_from_db()
        self.assertEqual((self.state.published_build, self.state.revision, self.state.report),
                         ('12.1.5.70529', 7, {'keep': 'last-good'}))

    def test_reclaimed_owner_is_protected_from_late_source_exception(self):
        entered, release = Event(), Event()
        class Source:
            def discover(self, branch):
                return '12.1.5.70575'
            def prepare(self, branch, build, run, heartbeat):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('bounded source handshake timed out')
                raise RuntimeError('late source failure')
        thread, errors = self._thread(lambda: update.sync_game_data(
            monitor_task=self.old, branches=['retail'], source=Source()))
        try:
            self.assertTrue(entered.wait(3))
            old_run = WowDataUpdateRun.objects.get(branch='retail', status='running')
            MonitorTaskLease.objects.filter(task=self.task).update(
                expires_at=timezone.now() - timedelta(seconds=1))
            replacement = claim_monitor_task(self.task.pk, lease_owner='new-owner', lease_seconds=600)
            self.assertIsNotNone(replacement)
            update.reconcile_interrupted_runs(replacement)
            with transaction.atomic():
                update.assert_lease(replacement)
                new_run = WowDataUpdateRun.objects.create(branch='retail', build='12.1.5.70575')
                WowDataUpdateState.objects.filter(pk=self.state.pk).update(
                    status='preparing', error='', report={'keep': 'new-owner'})
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], MonitorTaskLeaseLost)
        old_run.refresh_from_db()
        new_run.refresh_from_db()
        self.state.refresh_from_db()
        self.assertEqual(old_run.status, 'interrupted')
        self.assertEqual(new_run.status, 'running')
        self.assertEqual(self.state.report, {'keep': 'new-owner'})
        self.assertEqual(self.state.status, 'preparing')
        self.assertEqual(MonitorTaskLease.objects.get(task=self.task).owner, 'new-owner')
