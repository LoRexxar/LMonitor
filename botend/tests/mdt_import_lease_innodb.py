"""Real import/fence contention; explicit isolated MySQL/InnoDB gate.

Run with isolated MySQL settings:
    python manage.py test botend.tests.mdt_import_lease_innodb --settings=<isolated_mysql>
Not named test*.py so ordinary SQLite discovery does not run this backend gate.
An explicit invocation on the wrong backend still fails rather than skipping.

SQL wrappers only pause after real writes; no importer, ORM, clock or renewal
is replaced. Both publication passes run the production management command.
"""
import copy
import json
import tempfile
import threading
import time
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.core.management import call_command
from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from botend import plugin_sync
from botend.models import (
    MonitorTask, MonitorTaskLease, MonitorTaskLeaseLost,
    MythicDungeonDataVersion, MythicDungeonRoute, MythicDungeonRouteShare,
    MythicDungeonSpawn,
)
from botend.mythic_planner import auto_update as updater
from botend.mythic_planner.importer import import_mythic_dungeon_payload
from botend.tests.test_mdt_auto_update import package, RELEASE


class MdtImportLeaseInnoDBTests(TransactionTestCase):
    # Deliberately fail, not skip, if this explicit backend gate is misconfigured.
    def setUp(self):
        self.assertEqual(connection.vendor, 'mysql', 'requires isolated MySQL, not SQLite')
        with connection.cursor() as cursor:
            cursor.execute('SELECT VERSION(), @@version_comment, CONNECTION_ID()')
            self.server = cursor.fetchone()
            self.assertNotIn('MariaDB', self.server[0])
            cursor.execute('SELECT DISTINCT ENGINE FROM information_schema.tables WHERE table_schema=DATABASE()')
            self.assertEqual(cursor.fetchall(), (('InnoDB',),))
        self.original = package()
        import_mythic_dungeon_payload(self.original, activate=True, replace=True)
        self.version = MythicDungeonDataVersion.objects.get(key='mdt-6-2-20')
        dungeon = self.version.dungeons.get()
        spawn = MythicDungeonSpawn.objects.filter(enemy__dungeon=dungeon).first()
        spawn.x = 12.5
        spawn.metadata = {**spawn.metadata, 'manual_position_override': True}
        spawn.save()
        self.manual_spawn_pk = spawn.pk
        data = {'version': 1, 'data_version_key': self.version.key, 'dungeon_key': dungeon.key}
        MythicDungeonRoute.objects.create(dungeon=dungeon, name='旧路线', route_data=data)
        MythicDungeonRouteShare.objects.create(dungeon=dungeon, name='旧短链', route_data=data, content_hash='c' * 64)
        self.before = self.snapshot()
        self.candidate = copy.deepcopy(self.original)
        self.candidate['data_version'].update(key='mdt-6-2-21', label='lease regression')
        self.candidate['data_version']['metadata'].update(source_tag=RELEASE['tag'], source_commit=RELEASE['commit'])
        self.candidate['dungeons'][0]['name_zh'] = '真实导入的候选名称'
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = updater.write_payload(self.candidate, Path(self.temp.name) / 'candidate.json')
        self.task = updater.get_mdt_monitor_task()

    def snapshot(self):
        return {model.__name__: list(model.objects.order_by('pk').values())
                for model in apps.get_app_config('botend').get_models()
                if model.__name__.startswith('Mythic') and model.__name__ != 'MythicDungeonSyncRun'}

    def exercise(self, *, isolation, manual=False, loss=None, loss_pass=2):
        # The shorter test TTL exercises the real wall clock and real renewer.
        from botend import views
        entered = [threading.Event(), threading.Event()]
        resume = [threading.Event(), threading.Event()]
        done = threading.Event()
        outcome = {}
        trace = []
        task = None if manual else plugin_sync.claim_monitor_task(self.task.pk, lease_seconds=5)
        if not manual:
            self.assertIsNotNone(task)

        def sql_gate(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            # Final activation UPDATE is after the real entity writes, before
            # importer return / database_counts / final lease fence.
            if (sql.startswith(f'UPDATE `{MythicDungeonDataVersion._meta.db_table}` SET `is_active` =')
                    and params[0] is True):
                index = len(trace)
                trace.append({'pass': index + 1, 'sql': sql, 'entered': time.monotonic()})
                entered[index].set()
                if not resume[index].wait(12):
                    raise TimeoutError('coordinator did not release import SQL gate')
            return result

        def worker():
            try:
                with connection.cursor() as cursor:
                    cursor.execute('SET SESSION TRANSACTION ISOLATION LEVEL ' + isolation.upper())
                    cursor.execute('SELECT CONNECTION_ID(), @@transaction_isolation')
                    outcome['connection'] = cursor.fetchone()
                with connection.execute_wrapper(sql_gate):
                    if manual:
                        # Exercise the actual manual command and sync/update_lease
                        # lifecycle. Only upstream preparation/upload is offline.
                        client = mock.Mock()
                        client.latest_release.return_value = RELEASE
                        with (
                            mock.patch.object(updater, 'UpstreamClient', return_value=client),
                            mock.patch.object(updater, 'prepare_candidate', return_value=self.candidate),
                            mock.patch.object(updater, 'publish_maps'),
                            self.settings(BASE_DIR=Path(self.temp.name)),
                        ):
                            call_command('update_mythic_dungeon_tools', stdout=StringIO())
                    else:
                        def heartbeat():
                            if not plugin_sync.renew_monitor_task_lease(task.pk, task._monitor_task_lease_owner, lease_seconds=5):
                                raise MonitorTaskLeaseLost('test synchronous heartbeat lost')
                        updater.import_candidate(self.candidate, self.path, self.version, task, heartbeat)
                outcome['success'] = True
            except BaseException as exc:
                outcome['error'] = exc
            finally:
                connections.close_all()
                done.set()

        with mock.patch.object(plugin_sync, 'MONITOR_TASK_LEASE_SECONDS', 2), mock.patch.object(views, 'MONITOR_TASK_LEASE_HEARTBEAT_SECONDS', .2):
            thread = threading.Thread(target=worker, name='mdt-real-import', daemon=True)
            thread.start()
            try:
                for index in range(2):
                    self.assertTrue(entered[index].wait(10), str(outcome))
                    self.assertNotEqual(outcome['connection'][0], self.server[2])
                    lease = MonitorTaskLease.objects.get(task_id=self.task.pk)
                    initial_expiry = lease.expires_at
                    # Readers still see the entire old dataset during both passes.
                    self.assertEqual(self.snapshot(), self.before)
                    if loss and index + 1 == loss_pass:
                        with transaction.atomic():
                            MonitorTask.objects.select_for_update().get(pk=self.task.pk)
                            change = {'owner': 'replacement-owner'} if loss == 'owner' else {'expires_at': timezone.now() - timedelta(seconds=1)}
                            MonitorTaskLease.objects.filter(task_id=self.task.pk).update(**change)
                        trace[index]['loss'] = loss
                        resume[index].set()
                        break
                    renewals = 0
                    durations = []
                    while timezone.now() <= initial_expiry + timedelta(seconds=.15):
                        if not manual:
                            start = time.monotonic()
                            self.assertTrue(plugin_sync.renew_monitor_task_lease(task.pk, task._monitor_task_lease_owner, lease_seconds=5))
                            durations.append(time.monotonic() - start)
                            renewals += 1
                        time.sleep(.15)
                    lease.refresh_from_db()
                    self.assertGreater(lease.expires_at, timezone.now(), 'lease must stay live beyond its initial TTL')
                    self.assertGreater(lease.expires_at, initial_expiry)
                    trace[index].update(initial_expiry=initial_expiry.isoformat(), renewed_expiry=lease.expires_at.isoformat(),
                                        renewals=renewals, max_renew_seconds=max(durations, default=None),
                                        held_seconds=time.monotonic() - trace[index]['entered'])
                    resume[index].set()
                self.assertTrue(done.wait(10), 'import worker did not exit')
            finally:
                for event in resume:
                    event.set()
                thread.join(15)
                self.assertFalse(thread.is_alive(), 'worker leaked')
        print('MDT_INNODB_EVIDENCE', json.dumps({'isolation': isolation, 'manual': manual, 'loss': loss,
              'loss_pass': loss_pass, 'server': self.server, 'worker': outcome.get('connection'),
              'trace': trace, 'error': repr(outcome.get('error'))}, ensure_ascii=False), flush=True)
        if loss:
            # Management command deliberately translates RuntimeError to CommandError.
            self.assertIn('MDT 更新执行锁已失效', str(outcome.get('error')))
            self.assertNotIn('success', outcome)
            self.assertEqual(self.snapshot(), self.before)
            if loss == 'owner':
                self.assertEqual(MonitorTaskLease.objects.get(task_id=self.task.pk).owner, 'replacement-owner')
        else:
            self.assertNotIn('error', outcome)
            self.assertTrue(outcome.get('success'))
            self.version.refresh_from_db()
            self.assertEqual(self.version.key, 'mdt-6-2-21')
            self.assertEqual(updater.database_counts(self.version), updater.payload_counts(self.candidate))
            self.assertEqual(self.version.dungeons.get().name_zh, '真实导入的候选名称')
            after = self.snapshot()
            for name in ('MythicDungeonRoute', 'MythicDungeonRouteShare', 'MythicPlannerConfig'):
                self.assertEqual(after[name], self.before[name])
            spawn = MythicDungeonSpawn.objects.get(pk=self.manual_spawn_pk)
            self.assertEqual(spawn.x, 12.5)
        if manual:
            self.assertFalse(any(t.name == f'monitor-lease-{self.task.pk}' for t in threading.enumerate()))
            if loss != 'owner':
                self.assertFalse(MonitorTaskLease.objects.filter(task_id=self.task.pk).exists())

    def test_monitor_renewal_during_both_import_passes(self):
        self.exercise(isolation='read committed')

    def test_repeatable_read_renewal_uses_latest_fence(self):
        self.exercise(isolation='repeatable read')

    def test_manual_command_renews_without_an_external_heartbeat(self):
        self.exercise(isolation='read committed', manual=True)

    def test_owner_replaced_in_final_pass_rolls_back_all_data(self):
        self.exercise(isolation='repeatable read', loss='owner')

    def test_expiry_in_final_pass_rolls_back_all_data(self):
        self.exercise(isolation='repeatable read', loss='expiry')

    def test_owner_replaced_in_dry_run_rolls_back_all_data(self):
        self.exercise(isolation='read committed', loss='owner', loss_pass=1)

    def test_expiry_in_dry_run_rolls_back_all_data(self):
        self.exercise(isolation='read committed', loss='expiry', loss_pass=1)

    def test_manual_owner_loss_preserves_replacement_and_rolls_back(self):
        self.exercise(isolation='repeatable read', manual=True, loss='owner')

    def test_manual_expiry_rejects_publication_and_rolls_back(self):
        self.exercise(isolation='repeatable read', manual=True, loss='expiry')
