"""Real ORM admission plus real RIO collector/storage; HTTP uses existing fixtures.

These deterministic SQLite cases do not claim to prove InnoDB lock contention.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests
from django.db import connection
from django.test import TestCase

from botend import plugin_sync as scheduler
from botend.models import MonitorTask, MonitorTaskLease, SeasonMeta, WclCollectionCycle
from botend.services import rio_statistics as rio
from botend.services.rio_statistics_scope import active_scope
from botend.controller.plugins.portal.RioStatisticsMonitor import RioStatisticsMonitor
from botend.tests.test_rio_statistics import OVERVIEW

BJT = ZoneInfo('Asia/Shanghai')


class MonitorFairnessResumeTests(TestCase):
    def setUp(self):
        MonitorTask.objects.all().delete()
        SeasonMeta.objects.all().delete()
        self.now = datetime(2026, 11, 12, 12, tzinfo=BJT)
        self.clock = self.enterContext(patch('django.utils.timezone.now', side_effect=lambda: self.now))
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('No external network')))
        self.season = SeasonMeta.objects.create(season_key='mn-s2', season_name='测试赛季',
            rio_season='season-mn-2', game_build='12.1.5.70000', is_active=True,
            mplus_zone_id=1, raid_zone_id=2)
        self.scope = active_scope()
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='optimization-rio-')))
        self.enterContext(self.settings(RIO_STATISTICS_ROOT=self.directory))

    def task(self, name, **overrides):
        values = dict(type=900 + MonitorTask.objects.count(), target='', env_limit=0,
            is_active=True, wait_time=scheduler.monitor_default_wait_time(name),
            last_scan_time=self.now - timedelta(days=2))
        values.update(overrides)
        return MonitorTask.objects.create(name=name, **values)

    def claim(self, owner='worker'):
        return scheduler.claim_next_monitor_task(now=self.now, lease_owner=owner, lease_seconds=3600)

    def release(self, task, owner='worker'):
        self.assertTrue(scheduler.release_monitor_task_lease(task.pk, owner))

    def cycle(self):
        old = self.now - timedelta(days=3)
        return WclCollectionCycle.objects.create(season=self.season, slot=old.replace(hour=0),
            owner='old-cycle', phase='aggregating', collected_at=old, last_attempt_at=old)

    def partial(self, *, day=None, token=None, last=None, **fields):
        identity = f"{day or (self.now - timedelta(days=1)).date().isoformat()}@{token or self.scope['token']}"
        return self.task('RioStatisticsMonitor', last_scan_time=last or self.now - timedelta(days=1),
            flag=json.dumps(dict(identity=identity, state='partial', **fields)))

    def test_old_wcl_slot_does_not_beat_longer_waiting_player(self):
        self.cycle()
        self.task('SpecDetailAggregationMonitor', last_scan_time=self.now - timedelta(minutes=40))
        player = self.task('SpecDetailPlayerMonitor')
        self.assertEqual(self.claim().pk, player.pk)

    def test_waiting_time_is_sorted_before_long_candidate_limit(self):
        self.task('MaxrollClassGuideMonitor')
        wago = self.task('WagoSkillDiffMonitor', last_scan_time=self.now - timedelta(days=2))
        self.assertEqual(self.claim().pk, wago.pk)

    def test_repeated_wcl_recovery_rotates_behind_waiting_wago_and_player(self):
        cycle = self.cycle()
        aggregate = self.task('SpecDetailAggregationMonitor')
        wago = self.task('WagoSkillDiffMonitor', last_scan_time=self.now - timedelta(hours=15))
        player = self.task('SpecDetailPlayerMonitor')
        first = self.claim()
        self.assertEqual(first.pk, aggregate.pk)
        self.release(first)
        self.now += timedelta(minutes=31)
        second = self.claim()
        self.assertEqual(second.pk, wago.pk)
        self.release(second)
        third = self.claim()
        self.assertEqual(third.pk, player.pk)
        cycle.refresh_from_db()
        self.assertEqual(cycle.slot, (self.now - timedelta(days=3)).replace(hour=0, minute=0))
        self.assertEqual(WclCollectionCycle.objects.count(), 1)

    def test_wcl_is_only_a_tie_breaker_for_equal_wait(self):
        self.cycle()
        last = self.now - timedelta(hours=1)
        self.task('WagoSkillDiffMonitor', last_scan_time=last - timedelta(hours=1))
        aggregate = self.task('SpecDetailAggregationMonitor', last_scan_time=last)
        self.assertEqual(self.claim().pk, aggregate.pk)

    def test_pending_preparation_never_selects_wcl_instead(self):
        self.cycle()
        self.task('SpecDetailAggregationMonitor')
        version = self.task('WowDataVersionMonitor')
        season = self.task('SpecDetailSeasonMonitor')
        first = self.claim()
        self.assertEqual(first.pk, season.pk)
        self.release(first)
        self.assertEqual(self.claim().pk, version.pk)

    def test_due_calculation_is_reused_for_selection(self):
        self.cycle()
        self.task('SpecDetailAggregationMonitor')
        self.task('WagoSkillDiffMonitor')
        with patch.object(scheduler, 'monitor_task_due_at', wraps=scheduler.monitor_task_due_at) as due:
            self.assertIsNotNone(self.claim())
        self.assertEqual(due.call_count, 2)

    def test_yesterday_partial_can_resume_daytime_but_still_uses_long_capacity(self):
        task = self.partial()
        got = self.claim()
        self.assertIsNotNone(got)
        self.assertEqual(got.pk, task.pk)
        self.assertTrue(got._monitor_task_is_long)
        self.task('WagoSkillDiffMonitor')
        self.assertIsNone(self.claim('other-long'))
        shorts = [self.task(name) for name in ('PortalPostMonitor', 'wowheadMonitor')]
        self.assertCountEqual([self.claim('short-one').pk, self.claim('short-two').pk], [t.pk for t in shorts])
        self.assertIsNone(self.claim('fourth'))
        self.assertEqual(MonitorTaskLease.objects.count(), 3)

    def test_partial_retry_and_live_lease_are_not_bypassed_across_midnight(self):
        task = self.partial(retry_at=(self.now + timedelta(minutes=5)).isoformat())
        self.assertIsNone(self.claim())
        self.now += timedelta(minutes=5)
        first = self.claim()
        self.assertIsNotNone(first)
        self.now += timedelta(minutes=31)
        self.assertIsNone(self.claim('competitor'))
        self.assertEqual(MonitorTaskLease.objects.get(task=task).owner, 'worker')
        self.release(first)
        self.assertEqual(self.claim('next').pk, task.pk)

    def test_scope_mismatch_or_invalid_identity_cannot_resume_in_daytime(self):
        for identity in ('2026-11-11@old-scope', 'not-a-day@' + self.scope['token'],
                         '2026-11-13@' + self.scope['token'], '20261111@' + self.scope['token']):
            with self.subTest(identity=identity):
                task = self.partial(last=self.now - timedelta(hours=2))
                MonitorTask.objects.filter(pk=task.pk).update(flag=json.dumps({'identity': identity, 'state': 'partial'}))
                self.assertIsNone(self.claim())
                task.delete()

    def test_old_complete_and_unstarted_day_do_not_open_new_batch_in_daytime(self):
        task = self.partial()
        flag = json.loads(task.flag)
        flag['state'] = 'complete'
        MonitorTask.objects.filter(pk=task.pk).update(flag=json.dumps(flag))
        self.assertIsNone(self.claim())
        MonitorTask.objects.filter(pk=task.pk).update(flag='')
        self.assertIsNone(self.claim())
        self.now = self.now.replace(hour=1)
        self.assertEqual(self.claim().pk, task.pk)

    def test_legacy_identityless_same_day_claim_still_recovers(self):
        task = self.task('RioStatisticsMonitor', last_scan_time=self.now - timedelta(hours=1), flag='')
        self.assertEqual(self.claim().pk, task.pk)

    def test_same_day_complete_is_not_repeated_even_if_last_scan_is_older(self):
        task = self.partial(day=self.now.date().isoformat())
        flag = json.loads(task.flag)
        flag['state'] = 'complete'
        MonitorTask.objects.filter(pk=task.pk).update(flag=json.dumps(flag))
        self.now = self.now.replace(hour=2)
        self.assertIsNone(self.claim())

    def catalog(self):
        # Minimal valid local metadata scope; only two overview coordinates.
        rio._write(self.directory / 'catalog.json', {
            'schema': 1, 'updated_at': self.now.isoformat(), 'current_season': 'season-mn-2',
            'scope': self.scope, 'seasons': [{'slug': 'season-mn-2', 'name': '测试赛季',
                'starts': {'cn': '2026-08-19T23:00:00Z'}, 'ends': {'cn': '2030-01-01T00:00:00Z'}, 'dungeons': []}],
            'raids': [], 'floors': {}, 'affixes': [], 'periods': [], 'maps': {}})

    def transport(self, calls):
        def get(session, url, **kwargs):
            self.assertEqual(url, 'https://raider.io/api/v1/mythic-plus/cutoff-analysis/summary')
            calls.append(deepcopy(kwargs['params']))
            data = deepcopy(OVERVIEW)
            data['region'] = kwargs['params']['region']
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(data).encode()
            return response
        return get

    def test_real_monitor_collector_preserves_origin_not_fake_historical_day(self):
        task = self.partial()
        origin = json.loads(task.flag)['identity']
        self.catalog()
        calls = []
        with patch('requests.Session.get', new=self.transport(calls)):
            # A real bounded first attempt, then a fresh Monitor instance resumes.
            with patch.object(rio, 'DAILY_REQUEST_BUDGET', 1):
                first = self.claim()
                self.assertIsNotNone(first)
                self.assertFalse(RioStatisticsMonitor(None, first).scan())
            first.refresh_from_db()
            partial = json.loads(first.flag)
            self.assertEqual(partial.get('recovery_of', partial['identity']), origin)
            self.release(first)
            self.now += timedelta(minutes=31)
            second = self.claim('resumed')
            self.assertIsNotNone(second)
            self.assertTrue(RioStatisticsMonitor(None, second).scan())
            self.release(second, 'resumed')
        task.refresh_from_db()
        flag = json.loads(task.flag)
        self.assertEqual(flag['state'], 'complete')
        self.assertEqual(flag.get('recovery_of', flag['identity']), origin)
        self.assertEqual(flag['collection_day'], self.now.date().isoformat())
        self.assertIs(flag['history_backfilled'], False)
        self.assertEqual([c['region'] for c in calls], ['cn', 'world'])
        self.assertIsNone(self.claim())
        with rio.store().connection() as db:
            days = {row['day'] for row in db.execute('SELECT day FROM history')}
            cycles = [json.loads(row['details']) for row in db.execute('SELECT details FROM cycles')]
        self.assertEqual(days, {self.now.date().isoformat()})
        self.assertEqual({row['day'] for row in cycles}, {self.now.date().isoformat()})
        self.assertEqual([row['state'] for row in cycles], ['partial', 'complete'])
        c = rio.coordinate('overview', {'season': 'season-mn-2', 'region': 'cn'})
        body, entry = rio.store().read(c)
        self.assertEqual(datetime.fromisoformat(body['fetched_at']).astimezone(BJT).date(), self.now.date())
        self.assertEqual(body['upstream_at'], OVERVIEW['updatedAt'])

    def test_night_still_admits_two_longs_and_one_short(self):
        self.now = self.now.replace(hour=1)
        longs = [self.task('WagoSkillDiffMonitor'), self.task('MaxrollClassGuideMonitor')]
        claimed = [self.claim('long-one'), self.claim('long-two')]
        self.assertCountEqual([task.pk for task in claimed], [task.pk for task in longs])
        self.partial()
        self.assertIsNone(self.claim('third-long'))
        short = self.task('PortalPostMonitor')
        self.assertEqual(self.claim('short').pk, short.pk)
        self.assertEqual(MonitorTaskLease.objects.count(), 3)
        for task, owner in zip(claimed, ('long-one', 'long-two')):
            self.assertTrue(scheduler.renew_monitor_task_lease(task.pk, owner, now=self.now,
                                                             lease_seconds=12 * 3600))
        self.now = self.now.replace(hour=6)
        self.release(claimed[0], 'long-one')
        self.assertIsNone(scheduler.claim_next_monitor_task(now=self.now,
            task_names={'WagoSkillDiffMonitor', 'MaxrollClassGuideMonitor', 'RioStatisticsMonitor'}))
        self.assertEqual(MonitorTaskLease.objects.filter(expires_at__gt=self.now).count(), 1)

    def test_manual_new_scope_daytime_remains_explicitly_authorized(self):
        task = self.partial(token='previous-scope')
        MonitorTask.objects.filter(pk=task.pk).update(is_active=False)
        self.assertIsNone(self.claim())
        claimed = scheduler.claim_monitor_task(task.pk, now=self.now, lease_owner='manual')
        self.assertIsNotNone(claimed)
        self.assertTrue(MonitorTaskLease.objects.get(task=task).is_manual)
        self.catalog()
        calls = []
        with patch('requests.Session.get', new=self.transport(calls)):
            self.assertTrue(RioStatisticsMonitor(None, claimed).scan())
        task.refresh_from_db()
        flag = json.loads(task.flag)
        self.assertEqual(flag['identity'], f"{self.now.date().isoformat()}@{self.scope['token']}")
        self.assertIsNone(flag['recovery_of'])
        self.assertEqual(len(calls), 2)

    def test_expired_owner_cannot_overwrite_recovery_flag(self):
        from botend.models import MonitorTaskLeaseLost
        task = self.partial()
        first = self.claim()
        self.now += timedelta(hours=1, seconds=1)
        self.assertEqual(self.claim('replacement').pk, task.pk)
        first.flag = '{}'
        with self.assertRaises(MonitorTaskLeaseLost):
            first.save(update_fields=['flag'])
        task.refresh_from_db()
        self.assertEqual(json.loads(task.flag)['state'], 'partial')
        self.assertEqual(MonitorTaskLease.objects.get(task=task).owner, 'replacement')

    def test_collector_checks_scope_again_before_any_source_request(self):
        from botend.services.rio_statistics_scope import ScopeUnavailable
        with patch('requests.Session.get', side_effect=AssertionError('scope fence before HTTP')):
            with self.assertRaises(ScopeUnavailable):
                rio.run_nightly_collection(scope_token='outdated-scope')
        with rio.store().connection() as db:
            row = db.execute('SELECT state,details FROM cycles').fetchone()
            self.assertEqual(row['state'], 'failed')
            self.assertEqual(json.loads(row['details'])['error'], 'ScopeUnavailable')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM history').fetchone()[0], 0)

    def test_real_source_cooldown_survives_monitor_and_claim(self):
        task = self.partial()
        self.catalog()
        response = requests.Response()
        response.status_code = 429
        response.headers['Retry-After'] = '7200'
        response._content = b'{}'
        claimed = self.claim()
        with patch('requests.Session.get', return_value=response) as http:
            self.assertFalse(RioStatisticsMonitor(None, claimed).scan())
            self.assertEqual(http.call_count, 1)
        self.release(claimed)
        task.refresh_from_db()
        retry = datetime.fromisoformat(json.loads(task.flag)['retry_at'])
        self.assertEqual(retry, self.now + timedelta(hours=2))
        self.now += timedelta(minutes=31)
        self.assertIsNone(self.claim())
        with patch('requests.Session.get', side_effect=AssertionError('cooldown forbids HTTP')):
            self.assertEqual(rio.run_nightly_collection(scope_token=self.scope['token'])['requests'], 0)

    def interrupted_start(self, interruption, *, change_scope=False, recovering=False):
        self.now = self.now.replace(hour=1)
        task = self.partial()
        previous = json.loads(task.flag)
        if not recovering:
            previous['state'] = 'complete'
            task.flag = json.dumps(previous)
            task.save(update_fields=['flag'])
        claimed = self.claim()
        self.assertIsNotNone(claimed)
        task.refresh_from_db()
        self.assertEqual(task.last_scan_time, self.now)
        self.assertEqual(json.loads(task.flag), previous)
        if change_scope:
            self.season.game_build = '12.1.5.70001'
            self.season.save(update_fields=['game_build'])
            self.scope = active_scope()
        self.catalog()
        observed = []

        def interrupt(session, url, **kwargs):
            # At the actual collector HTTP boundary, read the durable ORM row,
            # not the claimed instance (which a hard process exit would lose).
            observed.append(json.loads(MonitorTask.objects.get(pk=task.pk).flag))
            raise interruption()

        with patch('requests.Session.get', new=interrupt):
            with self.assertRaises(interruption):
                RioStatisticsMonitor(None, claimed).scan()
        task.refresh_from_db()
        started = json.loads(task.flag)
        identity = f"{self.now.date().isoformat()}@{self.scope['token']}"
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0], started)
        self.assertEqual(started['identity'], identity)
        self.assertEqual(started['state'], 'partial')
        self.assertEqual(started['result']['state'], 'started')
        self.assertEqual(started['collection_day'], self.now.date().isoformat())
        self.assertIs(started['history_backfilled'], False)
        self.assertEqual(started['recovery_of'], previous['identity'] if recovering else None)
        # No release: model the orphaned owner's TTL, then use normal admission.
        self.now = self.now.replace(hour=12)
        resumed = self.claim('replacement')
        self.assertIsNotNone(resumed)
        calls = []
        with patch('requests.Session.get', new=self.transport(calls)):
            self.assertTrue(RioStatisticsMonitor(None, resumed).scan())
        task.refresh_from_db()
        complete = json.loads(task.flag)
        self.assertEqual(complete['identity'], identity)
        self.assertEqual(complete['state'], 'complete')
        self.assertEqual(complete['recovery_of'], previous['identity'] if recovering else None)
        self.assertEqual([c['region'] for c in calls], ['cn', 'world'])
        with rio.store().connection() as db:
            self.assertEqual({row['day'] for row in db.execute('SELECT day FROM history')},
                             {self.now.date().isoformat()})
        self.release(resumed, 'replacement')
        self.assertIsNone(self.claim())

    def test_yesterday_complete_start_survives_keyboard_interrupt_and_resumes_daytime(self):
        self.interrupted_start(KeyboardInterrupt)

    def test_night_scope_change_start_survives_system_exit_and_resumes_daytime(self):
        self.interrupted_start(SystemExit, change_scope=True)

    def test_recovery_origin_is_durable_before_interrupted_first_http(self):
        self.interrupted_start(KeyboardInterrupt, recovering=True)

    def test_stale_owner_start_cannot_write_or_reach_http(self):
        from botend.models import MonitorTaskLeaseLost
        self.catalog()
        for state in ('expired', 'replaced', 'missing'):
            with self.subTest(state=state):
                task = self.partial()
                claimed = self.claim('original')
                self.assertIsNotNone(claimed)
                if state == 'expired':
                    MonitorTaskLease.objects.filter(task=task).update(expires_at=self.now)
                elif state == 'replaced':
                    MonitorTaskLease.objects.filter(task=task).update(owner='replacement')
                else:
                    MonitorTaskLease.objects.filter(task=task).delete()
                before = MonitorTask.objects.get(pk=task.pk).flag
                calls = []
                with patch('requests.Session.get', new=self.transport(calls)):
                    with self.assertRaises(MonitorTaskLeaseLost):
                        RioStatisticsMonitor(None, claimed).scan()
                self.assertEqual(calls, [])
                self.assertEqual(MonitorTask.objects.get(pk=task.pk).flag, before)
                self.assertFalse(rio.store().path.exists())
                task.delete()

    def test_start_and_completion_sample_expiry_after_lease_read(self):
        from botend.models import MonitorTaskLeaseLost
        self.catalog()
        for phase in ('start', 'completion'):
            with self.subTest(phase=phase):
                task = self.partial()
                claimed = self.claim('original')
                self.assertIsNotNone(claimed)
                expires = MonitorTaskLease.objects.get(task=task).expires_at
                selects = []

                def expire_after_lease_read(execute, sql, params, many, context):
                    result = execute(sql, params, many, context)
                    if sql.lstrip().upper().startswith('SELECT'):
                        if connection.ops.quote_name(MonitorTaskLease._meta.db_table) in sql:
                            selects.append('lease')
                            self.now = expires
                        elif connection.ops.quote_name(MonitorTask._meta.db_table) in sql:
                            selects.append('task')
                    return result

                before = task.flag
                calls = []
                monitor = RioStatisticsMonitor(None, claimed)
                with connection.execute_wrapper(expire_after_lease_read), \
                        patch('requests.Session.get', new=self.transport(calls)):
                    with self.assertRaises(MonitorTaskLeaseLost):
                        if phase == 'start':
                            monitor.scan()
                        else:
                            monitor._record(json.loads(task.flag)['identity'], 'complete', {'state': 'complete'})
                self.assertEqual(selects, ['task', 'lease'])
                self.assertEqual(calls, [])
                self.assertEqual(MonitorTask.objects.get(pk=task.pk).flag, before)
                task.delete()

    def test_stale_completion_preserves_replacement_flag_and_admin_fields(self):
        from botend.models import MonitorTaskLeaseLost
        for state in ('expired', 'replaced'):
            with self.subTest(state=state):
                task = self.partial()
                claimed = self.claim('original')
                self.assertIsNotNone(claimed)
                monitor = RioStatisticsMonitor(None, claimed)
                identity = f"{self.now.date().isoformat()}@{self.scope['token']}"
                monitor._record(identity, 'partial', {'state': 'started'})
                if state == 'expired':
                    MonitorTaskLease.objects.filter(task=task).update(expires_at=self.now)
                else:
                    MonitorTaskLease.objects.filter(task=task).update(owner='replacement')
                MonitorTask.objects.filter(pk=task.pk).update(flag='replacement-state', target='admin-target',
                                                             wait_time=777, is_active=False)
                with self.assertRaises(MonitorTaskLeaseLost):
                    monitor._record(identity, 'complete', {'state': 'complete'})
                task.refresh_from_db()
                self.assertEqual((task.flag, task.target, task.wait_time, task.is_active),
                                 ('replacement-state', 'admin-target', 777, False))
                task.delete()

    def test_unclaimed_direct_monitor_still_saves_only_flag(self):
        task = self.partial()
        MonitorTask.objects.filter(pk=task.pk).update(target='admin-target', wait_time=777, is_active=False)
        self.catalog()
        calls = []
        with patch('requests.Session.get', new=self.transport(calls)):
            self.assertTrue(RioStatisticsMonitor(None, task).scan())
        task.refresh_from_db()
        self.assertEqual(json.loads(task.flag)['state'], 'complete')
        self.assertEqual((task.target, task.wait_time, task.is_active), ('admin-target', 777, False))
        self.assertEqual(len(calls), 2)
        self.assertFalse(MonitorTaskLease.objects.exists())

    def test_scope_changes_between_claim_and_monitor_does_not_collect_as_recovery(self):
        task = self.partial()
        claimed = self.claim()
        self.assertIsNotNone(claimed)
        original = task.flag
        self.season.game_build = '12.1.5.70001'
        self.season.save(update_fields=['game_build'])
        with patch('requests.Session.get', side_effect=AssertionError('scope change must stop before HTTP')):
            self.assertFalse(RioStatisticsMonitor(None, claimed).scan())
        task.refresh_from_db()
        self.assertEqual(task.flag, original)
        self.assertFalse(rio.store().path.exists())
