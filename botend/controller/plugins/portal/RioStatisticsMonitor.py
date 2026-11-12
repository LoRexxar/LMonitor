"""魔兽世界数据观察：每天一次的夜间长任务。"""
import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from botend.models import MonitorTask, MonitorTaskLease, MonitorTaskLeaseLost
from botend.controller.BaseScan import BaseScan
from botend.services.rio_statistics import run_nightly_collection, root, _load, _stamp
from botend.services.rio_statistics_scope import active_scope
from botend.services.rio_statistics_schedule import flag_data, identity_day, partial_identity
from utils.log import logger


class RioStatisticsMonitor(BaseScan):
    default_is_active = True
    default_target = 'https://raider.io/api'
    requires_browser = False

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url=None):
        scope = active_scope()
        local_now = timezone.now().astimezone(ZoneInfo('Asia/Shanghai'))
        day = local_now.date().isoformat()
        identity = f"{day}@{scope['token']}"
        previous = flag_data(self.task.flag)
        if previous.get('identity') == identity and previous.get('state') == 'complete':
            return True
        partial = partial_identity(previous, local_now, scope['token'])
        if (previous.get('identity') and not partial and local_now.hour >= 6
                and not getattr(self.task, '_monitor_task_is_manual', False)):
            # Scope may have changed after claim. Do not convert recovery into
            # a new daytime batch, nor rewrite the old batch as the new scope.
            return False
        recovery_of = None
        if partial:
            origin = previous.get('recovery_of')
            origin_day = identity_day(origin, local_now, scope['token'])
            partial_day = identity_day(partial, local_now, scope['token'])
            recovery_of = origin if origin_day and origin_day <= partial_day else partial
            if recovery_of == identity:
                recovery_of = None
        # Commit the admitted day's identity before Collector/HTTP: BaseException
        # or a hard exit cannot run the result recorder. An old complete flag
        # must not hide today's interrupted batch from daytime recovery.
        self._record(identity, 'partial', {'state': 'started'},
                     recovery_of=recovery_of, collection_day=day)
        try:
            result = run_nightly_collection(scope_token=scope['token'])
        except Exception as exc:
            self._record(identity, 'partial', {'state': 'failed', 'error': type(exc).__name__},
                         recovery_of=recovery_of, collection_day=day)
            raise
        # 只有整个预算内的预设和地图成功才记录完成；失败不清理旧快照。
        complete = (result['state'] == 'complete' and not result.get('remaining')
                    and not result.get('failed') and not result.get('maps_failed') and not result.get('maps_remaining')
                    and not result.get('runs_failed') and not result.get('runs_pending') and not result.get('maintenance_error')
                    and not result.get('views_error'))
        self._record(identity, 'complete' if complete else 'partial', result,
                     recovery_of=recovery_of, collection_day=day)
        return complete

    def _record(self, identity, state, result, *, recovery_of=None, collection_day=None):
        logger.info('[RioStatistics] ' + json.dumps({'identity': identity, 'state': state,
                    'result': result}, ensure_ascii=False, default=str))
        now = timezone.now()
        retry = now + timedelta(minutes=30)
        cooldown = _stamp(_load(root() / 'cooldown.json', {}).get('until'))
        if cooldown:
            retry = max(retry, cooldown)
        fields = ('state', 'requests', 'ready', 'failed', 'remaining', 'maps_failed', 'maps_remaining',
                  'runs_imported', 'runs_failed', 'runs_pending', 'cycle_id', 'storage_state', 'error', 'maintenance_error', 'views_error')
        summary = {key: result[key] for key in fields if key in result}
        # Recovery fetches current API facts: origin is audit metadata only.
        # Neither snapshot fetched_at/history.day nor the collector day is backdated.
        flag = json.dumps({'identity': identity, 'state': state, 'result': summary,
                           'recovery_of': recovery_of, 'collection_day': collection_day,
                           'history_backfilled': False,
                           'checked_at': now.isoformat(), 'retry_at': retry.isoformat() if state != 'complete' else None},
                          ensure_ascii=False, separators=(',', ':'))
        self._save_flag(flag)

    def _save_flag(self, flag):
        owner = getattr(self.task, '_monitor_task_lease_owner', None)
        if not owner:
            # Direct callers retain the ordinary model/management save contract.
            self.task.flag = flag
            self.task.save(update_fields=['flag'])
            return
        using = self.task._state.db or 'default'
        with transaction.atomic(using=using):
            # Same parent -> lease order as claim/renew/complete. Sample expiry
            # only AFTER both reads acquire their locks, never in a SQL filter
            # built before a potentially blocking lease read.
            exists = MonitorTask.objects.using(using).select_for_update().filter(pk=self.task.pk).exists()
            if not exists:
                raise MonitorTaskLeaseLost('claimed MonitorTask row no longer exists')
            lease = MonitorTaskLease.objects.using(using).select_for_update().filter(
                task_id=self.task.pk, owner=str(owner)).first()
            if lease is None or lease.expires_at <= timezone.now():
                raise MonitorTaskLeaseLost('MonitorTask lease is missing, expired, or owned by another worker')
            MonitorTask.objects.using(using).filter(pk=self.task.pk).update(flag=flag)
        self.task.flag = flag
