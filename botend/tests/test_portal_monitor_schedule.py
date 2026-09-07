import time
from datetime import datetime, timedelta
from threading import Barrier, Event, Lock, Thread
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.db import connection, connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from botend.models import MonitorTask, MonitorTaskLease, MonitorTaskLeaseLost
from botend.plugin_sync import (
    monitor_default_wait_time,
    monitor_task_sort_key,
    portal_data_task_is_due,
    portal_monitor_task_priority,
    claim_next_monitor_task,
    complete_monitor_task_lease,
    release_monitor_task_lease,
    renew_monitor_task_lease,
)


class PortalMonitorScheduleTests(SimpleTestCase):
    def _task(self, name, last_scan_time):
        return SimpleNamespace(name=name, last_scan_time=last_scan_time)

    def test_portal_data_tasks_use_daily_staggered_slots_and_ten_minute_peak_refresh(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        player = self._task(
            'SpecDetailPlayerMonitor',
            datetime(2026, 8, 20, 2, 10, tzinfo=shanghai),
        )
        ranking = self._task(
            'SpecDetailRankingMonitor',
            datetime(2026, 8, 20, 3, 10, tzinfo=shanghai),
        )
        aggregation = self._task(
            'SpecDetailAggregationMonitor',
            datetime(2026, 8, 20, 6, 10, tzinfo=shanghai),
        )
        peak = self._task(
            'PortalPeakSpecRankMonitor',
            datetime(2026, 8, 20, 14, 0, tzinfo=shanghai),
        )

        afternoon = datetime(2026, 8, 20, 18, 0, tzinfo=shanghai)
        self.assertFalse(portal_data_task_is_due(player, afternoon))
        self.assertFalse(portal_data_task_is_due(ranking, afternoon))
        self.assertFalse(portal_data_task_is_due(aggregation, afternoon))
        self.assertTrue(portal_data_task_is_due(player, datetime(2026, 8, 21, 2, 0, tzinfo=shanghai)))
        self.assertEqual(monitor_default_wait_time('PortalPeakSpecRankMonitor'), 600)
        self.assertLess(portal_monitor_task_priority(peak), portal_monitor_task_priority(player))
        self.assertLess(portal_monitor_task_priority(player), portal_monitor_task_priority(ranking))
        self.assertLess(portal_monitor_task_priority(ranking), portal_monitor_task_priority(aggregation))

    def test_global_oldest_last_scan_time_wins_before_portal_priority(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        stale_unrelated = self._task(
            'wowheadMonitor',
            datetime(1999, 12, 31, 16, 0, tzinfo=shanghai),
        )
        recent_peak = self._task(
            'PortalPeakSpecRankMonitor',
            datetime(2026, 8, 28, 10, 0, tzinfo=shanghai),
        )

        ordered = sorted([recent_peak, stale_unrelated], key=monitor_task_sort_key)

        self.assertIs(ordered[0], stale_unrelated)

    def test_portal_data_task_missed_slot_runs_later_without_waiting_for_next_slot(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        task = self._task(
            'SpecDetailRankingMonitor',
            datetime(2026, 8, 19, 15, 5, tzinfo=shanghai),
        )

        self.assertTrue(portal_data_task_is_due(task, datetime(2026, 8, 20, 7, 45, tzinfo=shanghai)))

    def test_earlier_due_news_wins_over_older_daily_task(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        now = datetime(2026, 8, 29, 3, 21, tzinfo=shanghai)
        news = self._task(
            'PortalPostMonitor',
            datetime(2026, 8, 28, 23, 29, tzinfo=shanghai),
        )
        news.wait_time = 600
        ranking = self._task(
            'SpecDetailRankingMonitor',
            datetime(2026, 8, 28, 3, 0, tzinfo=shanghai),
        )
        ranking.wait_time = 86400

        self.assertLess(
            monitor_task_sort_key(news, now=now),
            monitor_task_sort_key(ranking, now=now),
        )

    def test_unrelated_task_is_not_controlled_by_portal_data_schedule(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        task = self._task(
            'PortalPeakSpecRankMonitor',
            datetime(2026, 8, 20, 14, 50, tzinfo=shanghai),
        )

        self.assertIsNone(portal_data_task_is_due(task, datetime(2026, 8, 20, 15, 0, tzinfo=shanghai)))


class MonitorTaskClaimTests(TestCase):
    def test_two_worker_claims_reserve_distinct_oldest_runnable_tasks(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        now = datetime(2026, 8, 29, 9, 0, tzinfo=shanghai)
        oldest = MonitorTask.objects.create(
            name='wowheadMonitor', target='', type=16,
            last_scan_time=datetime(2026, 8, 28, 20, 0, tzinfo=shanghai),
            wait_time=600, is_active=True,
        )
        second = MonitorTask.objects.create(
            name='PortalPostMonitor', target='', type=18,
            last_scan_time=datetime(2026, 8, 28, 21, 0, tzinfo=shanghai),
            wait_time=600, is_active=True,
        )

        first_claim = claim_next_monitor_task(now=now)
        second_claim = claim_next_monitor_task(now=now)

        self.assertEqual(first_claim.id, oldest.id)
        self.assertEqual(second_claim.id, second.id)

    def test_long_running_task_is_not_claimed_again_after_its_interval_elapses(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        first_started_at = datetime(2026, 9, 3, 21, 0, tzinfo=shanghai)
        task = MonitorTask.objects.create(
            name='WagoSkillDiffMonitor', target='', type=27,
            last_scan_time=datetime(2026, 9, 3, 19, 0, tzinfo=shanghai),
            wait_time=3600, is_active=True,
        )

        first_claim = claim_next_monitor_task(
            now=first_started_at,
            lease_owner='worker-a-long-run',
            lease_seconds=3 * 3600,
        )
        duplicate_claim = claim_next_monitor_task(
            now=datetime(2026, 9, 3, 23, 0, tzinfo=shanghai),
            lease_owner='worker-b',
        )

        self.assertEqual(first_claim.id, task.id)
        self.assertIsNone(duplicate_claim)

    def test_expired_task_lease_can_be_reclaimed_after_worker_crash(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        started_at = datetime(2026, 9, 3, 21, 0, tzinfo=shanghai)
        task = MonitorTask.objects.create(
            name='WagoSkillDiffMonitor', target='', type=27,
            last_scan_time=datetime(2026, 9, 3, 19, 0, tzinfo=shanghai),
            wait_time=0, is_active=True,
        )

        first_claim = claim_next_monitor_task(
            now=started_at,
            lease_owner='crashed-worker',
            lease_seconds=60,
        )
        recovered_claim = claim_next_monitor_task(
            now=started_at + timedelta(seconds=60),
            lease_owner='replacement-worker',
            lease_seconds=60,
        )

        self.assertEqual(first_claim.id, task.id)
        self.assertEqual(recovered_claim.id, task.id)
        lease = MonitorTaskLease.objects.get(task=task)
        self.assertEqual(lease.owner, 'replacement-worker')

    def test_lease_renewal_and_release_are_fenced_by_owner(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        started_at = datetime(2026, 9, 3, 21, 0, tzinfo=shanghai)
        task = MonitorTask.objects.create(
            name='WagoSkillDiffMonitor', target='', type=27,
            last_scan_time=datetime(2026, 9, 3, 19, 0, tzinfo=shanghai),
            wait_time=0, is_active=True,
        )
        claim_next_monitor_task(
            now=started_at,
            lease_owner='worker-a',
            lease_seconds=60,
        )

        self.assertFalse(renew_monitor_task_lease(
            task.id,
            'worker-b',
            now=started_at + timedelta(seconds=30),
            lease_seconds=60,
        ))
        self.assertTrue(renew_monitor_task_lease(
            task.id,
            'worker-a',
            now=started_at + timedelta(seconds=30),
            lease_seconds=60,
        ))
        self.assertIsNone(claim_next_monitor_task(
            now=started_at + timedelta(seconds=61),
            lease_owner='worker-b',
        ))
        self.assertFalse(release_monitor_task_lease(task.id, 'worker-b'))
        self.assertTrue(MonitorTaskLease.objects.filter(task=task).exists())
        self.assertTrue(release_monitor_task_lease(task.id, 'worker-a'))
        self.assertFalse(MonitorTaskLease.objects.filter(task=task).exists())

    def test_expired_owner_cannot_renew_or_release_reclaimed_lease(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        started_at = datetime(2026, 9, 3, 21, 0, tzinfo=shanghai)
        task = MonitorTask.objects.create(
            name='WagoSkillDiffMonitor', target='', type=27,
            last_scan_time=datetime(2026, 9, 3, 19, 0, tzinfo=shanghai),
            wait_time=0, is_active=True,
        )
        claim_next_monitor_task(
            now=started_at,
            lease_owner='worker-a',
            lease_seconds=60,
        )
        self.assertFalse(renew_monitor_task_lease(
            task.id,
            'worker-a',
            now=started_at + timedelta(seconds=60),
            lease_seconds=60,
        ))
        claim_next_monitor_task(
            now=started_at + timedelta(seconds=60),
            lease_owner='worker-b',
            lease_seconds=60,
        )

        self.assertFalse(release_monitor_task_lease(task.id, 'worker-a'))
        self.assertEqual(
            MonitorTaskLease.objects.get(task=task).owner,
            'worker-b',
        )

    def test_claimed_plugin_save_only_updates_flag_under_valid_lease(self):
        started_at = timezone.now()
        task = MonitorTask.objects.create(
            name='BiliOnlionMonitor', target='before', type=0,
            last_scan_time=started_at - timedelta(seconds=1),
            wait_time=0, is_active=True, flag='0',
        )
        claimed_task = claim_next_monitor_task(
            now=started_at,
            lease_owner='worker-a',
            lease_seconds=60,
        )
        MonitorTask.objects.filter(pk=task.id).update(target='admin-change')

        claimed_task.flag = '1'
        claimed_task.target = 'stale-target'
        claimed_task.save()

        task.refresh_from_db()
        self.assertEqual(task.flag, '1')
        self.assertEqual(task.target, 'admin-change')
        self.assertTrue(MonitorTaskLease.objects.filter(task=task).exists())

    def test_reclaimed_old_plugin_instance_cannot_save_directly(self):
        started_at = timezone.now()
        task = MonitorTask.objects.create(
            name='WagoSkillDiffMonitor', target='', type=27,
            last_scan_time=started_at - timedelta(hours=2),
            wait_time=0, is_active=True, flag='original',
        )
        stale_task = claim_next_monitor_task(
            now=started_at,
            lease_owner='worker-a',
            lease_seconds=60,
        )
        replacement = claim_next_monitor_task(
            now=started_at + timedelta(seconds=60),
            lease_owner='worker-b',
            lease_seconds=60,
        )
        stale_task.flag = 'stale-plugin-save'

        with self.assertRaises(MonitorTaskLeaseLost):
            stale_task.save()

        task.refresh_from_db()
        self.assertEqual(replacement.id, task.id)
        self.assertEqual(task.flag, 'original')
        self.assertEqual(
            MonitorTaskLease.objects.get(task=task).owner,
            'worker-b',
        )

    def test_completion_atomically_updates_flag_and_releases_owned_lease(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        started_at = datetime(2026, 9, 3, 21, 0, tzinfo=shanghai)
        task = MonitorTask.objects.create(
            name='BiliOnlionMonitor', target='', type=0,
            last_scan_time=started_at - timedelta(hours=2),
            wait_time=0, is_active=True, flag='0',
        )
        claim_next_monitor_task(
            now=started_at,
            lease_owner='worker-a',
            lease_seconds=60,
        )

        self.assertTrue(complete_monitor_task_lease(
            task.id,
            'worker-a',
            now=started_at + timedelta(seconds=30),
            task_updates={'flag': '1'},
        ))
        task.refresh_from_db()
        self.assertEqual(task.flag, '1')
        self.assertFalse(MonitorTaskLease.objects.filter(task=task).exists())

    def test_expired_owner_completion_cannot_overwrite_replacement_worker(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        started_at = datetime(2026, 9, 3, 21, 0, tzinfo=shanghai)
        task = MonitorTask.objects.create(
            name='WagoSkillDiffMonitor', target='', type=27,
            last_scan_time=started_at - timedelta(hours=2),
            wait_time=0, is_active=True, flag='original',
        )
        stale_task = claim_next_monitor_task(
            now=started_at,
            lease_owner='worker-a',
            lease_seconds=60,
        )
        stale_task.flag = 'stale-result'
        replacement = claim_next_monitor_task(
            now=started_at + timedelta(seconds=60),
            lease_owner='worker-b',
            lease_seconds=60,
        )

        self.assertEqual(replacement.id, task.id)
        self.assertFalse(complete_monitor_task_lease(
            stale_task.id,
            'worker-a',
            now=started_at + timedelta(seconds=61),
            task_updates={'flag': stale_task.flag},
        ))
        task.refresh_from_db()
        self.assertEqual(task.flag, 'original')
        self.assertEqual(
            MonitorTaskLease.objects.get(task=task).owner,
            'worker-b',
        )

    def test_completion_rejects_non_business_field_updates(self):
        task = MonitorTask.objects.create(
            name='wowheadMonitor', target='before', type=1,
            wait_time=0, is_active=True,
        )
        with self.assertRaises(ValueError):
            complete_monitor_task_lease(
                task.id,
                'worker-a',
                task_updates={'target': 'after'},
            )

    def test_claim_and_renew_reject_nonpositive_lease_ttl(self):
        task = MonitorTask.objects.create(
            name='wowheadMonitor', target='', type=1,
            wait_time=0, is_active=True,
        )
        with self.assertRaises(ValueError):
            claim_next_monitor_task(lease_owner='worker-a', lease_seconds=0)
        with self.assertRaises(ValueError):
            renew_monitor_task_lease(task.id, 'worker-a', lease_seconds=-1)


@skipUnless(connection.vendor == 'mysql', 'requires MySQL row-lock semantics')
class MonitorTaskMySQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def test_parallel_workers_skip_active_lease_and_claim_other_task_once(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        started_at = datetime(2026, 9, 3, 21, 0, tzinfo=shanghai)
        leased_task = MonitorTask.objects.create(
            name='WagoSkillDiffMonitor', target='', type=27,
            last_scan_time=started_at - timedelta(hours=2),
            wait_time=3600, is_active=True,
        )
        other_task = MonitorTask.objects.create(
            name='wowheadMonitor', target='', type=1,
            last_scan_time=started_at - timedelta(hours=2),
            wait_time=3600, is_active=True,
        )
        claim_next_monitor_task(
            now=started_at,
            lease_owner='already-running-worker',
            lease_seconds=3 * 3600,
        )

        barrier = Barrier(3)
        result_lock = Lock()
        results = []
        errors = []

        def claim_in_worker(owner):
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                claimed = claim_next_monitor_task(
                    now=started_at + timedelta(hours=2),
                    lease_owner=owner,
                    lease_seconds=3 * 3600,
                )
                with result_lock:
                    results.append(claimed.id if claimed else None)
            except Exception as exc:
                with result_lock:
                    errors.append(exc)
            finally:
                connections.close_all()

        workers = [
            Thread(target=claim_in_worker, args=('parallel-a',)),
            Thread(target=claim_in_worker, args=('parallel-b',)),
        ]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=5)
        for worker in workers:
            worker.join(timeout=10)

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [])
        self.assertCountEqual(results, [other_task.id, None])
        self.assertEqual(
            MonitorTaskLease.objects.get(task=leased_task).owner,
            'already-running-worker',
        )
        self.assertIn(
            MonitorTaskLease.objects.get(task=other_task).owner,
            {'parallel-a', 'parallel-b'},
        )

    def test_reclaim_and_old_owner_renewal_cannot_both_succeed(self):
        shanghai = ZoneInfo('Asia/Shanghai')
        started_at = datetime(2026, 9, 3, 21, 0, tzinfo=shanghai)
        task = MonitorTask.objects.create(
            name='WagoSkillDiffMonitor', target='', type=27,
            last_scan_time=started_at - timedelta(hours=2),
            wait_time=0, is_active=True,
        )
        claim_next_monitor_task(
            now=started_at,
            lease_owner='old-worker',
            lease_seconds=60,
        )
        claim_paused = Event()
        allow_claim = Event()
        renew_done = Event()
        results = {}
        errors = []
        result_lock = Lock()
        original_update_or_create = MonitorTaskLease.objects.update_or_create

        def delayed_update_or_create(*args, **kwargs):
            claim_paused.set()
            if not allow_claim.wait(timeout=10):
                raise TimeoutError('claim release gate timed out')
            return original_update_or_create(*args, **kwargs)

        def reclaim():
            connections.close_all()
            try:
                results['claim'] = claim_next_monitor_task(
                    now=started_at + timedelta(seconds=60),
                    lease_owner='new-worker',
                    lease_seconds=60,
                )
            except Exception as exc:
                with result_lock:
                    errors.append(exc)
            finally:
                connections.close_all()

        def renew_old_owner():
            connections.close_all()
            try:
                results['renew'] = renew_monitor_task_lease(
                    task.id,
                    'old-worker',
                    now=started_at + timedelta(seconds=59),
                    lease_seconds=60,
                )
            except Exception as exc:
                with result_lock:
                    errors.append(exc)
            finally:
                renew_done.set()
                connections.close_all()

        with patch.object(
            MonitorTaskLease.objects,
            'update_or_create',
            side_effect=delayed_update_or_create,
        ):
            reclaim_thread = Thread(target=reclaim)
            reclaim_thread.start()
            self.assertTrue(claim_paused.wait(timeout=5))
            renew_thread = Thread(target=renew_old_owner)
            renew_thread.start()
            time.sleep(0.25)
            self.assertFalse(
                renew_done.is_set(),
                'renewal did not wait for the parent MonitorTask row lock',
            )
            allow_claim.set()
            reclaim_thread.join(timeout=10)
            renew_thread.join(timeout=10)

        self.assertFalse(reclaim_thread.is_alive())
        self.assertFalse(renew_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results['claim'].id, task.id)
        self.assertFalse(results['renew'])
        self.assertEqual(
            MonitorTaskLease.objects.get(task=task).owner,
            'new-worker',
        )
