from datetime import timedelta
import json
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone

from utils.log import logger

from botend.models import MonitorTask, MonitorTaskLease
from botend.monitor_env import is_task_runnable


MONITOR_TASK_LEASE_SECONDS = int(
    getattr(django_settings, 'MONITOR_TASK_LEASE_SECONDS', 600)
)


def _monitor_task_lease_ttl(lease_seconds=None):
    ttl_seconds = MONITOR_TASK_LEASE_SECONDS if lease_seconds is None else int(lease_seconds)
    if ttl_seconds <= 0:
        raise ValueError('monitor task lease_seconds must be positive')
    return ttl_seconds


# Resource class, execution cadence and source dependencies are independent.
# News/full-text translation and fast ranking slices intentionally remain short.
MONITOR_WORKER_COUNT = 3
LONG_MONITOR_TASKS = (
    'WowDataVersionMonitor', 'MythicDungeonToolsMonitor', 'RioStatisticsMonitor',
    'MaxrollClassGuideMonitor', 'SpecDetailPlayerMonitor',
    'SpecDetailRankingMonitor', 'SpecDetailAggregationMonitor', 'WagoSkillDiffMonitor',
    'AliyunAvdScan', 'OscsScan', 'QaxScan', 'RaidBurstAggregationMonitor',
)
NIGHT_LANE_TASKS = LONG_MONITOR_TASKS  # Compatibility for existing callers/tests.
DAILY_MONITOR_TASKS = (
    'SpecDetailSeasonMonitor', 'WowDataVersionMonitor', 'MythicDungeonToolsMonitor',
    'RioStatisticsMonitor', 'MaxrollClassGuideMonitor', 'SpecDetailPlayerMonitor',
    'SpecDetailRankingMonitor', 'SpecDetailAggregationMonitor', 'WowDailyReportMonitor',
)
NIGHT_LANE_PRIORITY = {name: index for index, name in enumerate(DAILY_MONITOR_TASKS)}

PORTAL_DATA_SCHEDULE_HOURS_BY_TASK = {
    # Wowhead 在北美日常重置后会先返回无身份信息的 Active 占位行，
    # 随后才补齐具体地下堡。分三次渐进刷新，配合载荷完整性门禁，避免
    # 占位内容覆盖当天快照，并在上午自动补齐上游迟到的数据。
    "WowTodayMonitor": (4, 8, 10),
    # 每日大更新：人物内容先重抓，完整排名随后，聚合投影最后生成。
    **{name: (0,) for name in DAILY_MONITOR_TASKS},
}
PORTAL_DATA_SCHEDULED_TASKS = frozenset(PORTAL_DATA_SCHEDULE_HOURS_BY_TASK)
PORTAL_DATA_TIMEZONE = ZoneInfo("Asia/Shanghai")

PORTAL_MONITOR_TASK_PRIORITY = {
    # 同时到期时优先短周期榜单，普通池统一限制并发。
    "PortalPeakSpecRankMonitor": 0,
    "SpecDungeonDpsRankingMonitor": 5,
    "SpecDetailPlayerMonitor": 10,
    "SpecDetailRankingMonitor": 20,
    "SpecDetailAggregationMonitor": 30,
}


def monitor_default_wait_time(name):
    if name in DAILY_MONITOR_TASKS:
        return 86400

    if name == 'AdventureJournalMonitor':
        return 604800  # 保留历史 type=35 的间隔；任务默认关闭且只允许手动刷新。

    if name in {"PortalPeakSpecRankMonitor", "PlayerRaiderIOActivityMonitor"}:
        return 600  # 10m；榜单与角色活动独立领取，不在短榜单链路同步补采。
    if name == "PortalMplusCutoffMonitor":
        return 3600
    if name == "SpecDungeonDpsRankingMonitor":
        return 3600
    if name == "WagoSkillDiffMonitor":
        return 3600  # 1h，Wago build/hotfix 变更不需要 10 分钟级轮询
    if name == "SpecDetailSeasonMonitor":
        return 86400  # 24h
    if name in PORTAL_DATA_SCHEDULED_TASKS:
        return 86400  # 每日固定窗口由 portal_data_task_is_due 判定
    return 3600  # 通用新任务模板默认 1 小时，避免意外高频运行。


def portal_monitor_task_priority(task):
    return PORTAL_MONITOR_TASK_PRIORITY.get(getattr(task, "name", ""), 100)


def _normalized_now(now=None):
    value = now or timezone.now()
    if timezone.is_naive(value):
        value = timezone.make_aware(value, PORTAL_DATA_TIMEZONE)
    return value


def portal_data_task_due_at(task, now=None):
    """Return the fixed slot still owed by a scheduled task, or None."""
    if getattr(task, "name", "") not in PORTAL_DATA_SCHEDULED_TASKS:
        return None

    local_now = _normalized_now(now).astimezone(PORTAL_DATA_TIMEZONE)
    from botend.services.wcl_collection_cycle import NO_OVERRIDE, scheduled_cycle_due_at
    override = scheduled_cycle_due_at(task, local_now)
    if override is not NO_OVERRIDE:
        return override
    today_slots = [
        local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
        for hour in PORTAL_DATA_SCHEDULE_HOURS_BY_TASK[task.name]
    ]
    completed_slots = [slot for slot in today_slots if slot <= local_now]
    latest_slot = completed_slots[-1] if completed_slots else today_slots[-1] - timedelta(days=1)

    last_scan_time = getattr(task, "last_scan_time", None)
    if last_scan_time is None and task.name != 'RioStatisticsMonitor':
        return latest_slot
    if last_scan_time is not None and timezone.is_naive(last_scan_time):
        last_scan_time = timezone.make_aware(last_scan_time, PORTAL_DATA_TIMEZONE)
    if task.name == 'RioStatisticsMonitor':
        from botend.services.rio_statistics_schedule import resume_due_at, flag_data
        # 传给恢复判定的时钟与通用队列保持一致。
        from types import SimpleNamespace
        normalized = SimpleNamespace(last_scan_time=last_scan_time, flag=getattr(task, 'flag', ''))
        flag = flag_data(normalized.flag)
        scope_token = None
        if (flag.get('identity') and (flag.get('state') == 'partial' or
                (flag.get('state') == 'complete' and
                 str(flag.get('identity', '')).split('@', 1)[0] == local_now.date().isoformat()))):
            from botend.services.rio_statistics_scope import active_scope, ScopeUnavailable
            from django.db import DatabaseError
            try:
                scope_token = active_scope()['token']
            except (ScopeUnavailable, DatabaseError):
                return None
        return resume_due_at(normalized, local_now, latest_slot, scope_token=scope_token)
    if task.name == 'SpecDetailPlayerMonitor' and last_scan_time >= latest_slot:
        from botend.services.player_daily_checkpoint import resume_due_at
        from types import SimpleNamespace
        return resume_due_at(SimpleNamespace(last_scan_time=last_scan_time, flag=getattr(task, 'flag', '')), local_now)
    return latest_slot if last_scan_time < latest_slot else None


def monitor_task_due_at(task, now=None):
    """Return when this task became runnable, or None when it is not due."""
    # Manual-only until an explicit scheduling contract is introduced.
    if getattr(task, "name", "") == "RaidBurstAggregationMonitor":
        return None
    claim_time = _normalized_now(now)
    if getattr(task, "name", "") in PORTAL_DATA_SCHEDULED_TASKS:
        return portal_data_task_due_at(task, now=claim_time)

    last_scan_time = getattr(task, "last_scan_time", None)
    if last_scan_time is None:
        return claim_time
    if timezone.is_naive(last_scan_time):
        last_scan_time = timezone.make_aware(last_scan_time, PORTAL_DATA_TIMEZONE)
    wait_time = int(getattr(task, "wait_time", monitor_default_wait_time(task.name)) or 0)
    progress = {}
    if task.name == 'PlayerRaiderIOActivityMonitor':
        try:
            progress = json.loads(getattr(task, 'flag', '') or '{}')
            remaining = (progress.get('remaining_due'), (progress.get('enrichment') or {}).get('remaining_due'))
            if any(type(value) is int and value > 0 for value in remaining):
                wait_time = min(wait_time, 60)
        except (ValueError, TypeError, AttributeError):
            pass
    due_at = last_scan_time + timedelta(seconds=wait_time)
    if task.name == 'PlayerRaiderIOActivityMonitor':
        try:
            from django.utils.dateparse import parse_datetime
            retry = parse_datetime(progress.get('rate_limited_until') or '')
            if retry and timezone.is_aware(retry):
                from botend.models import CharacterEnrichmentTask
                from botend.services.character_enrichment import ACTIVE_STATES
                bnet_due = CharacterEnrichmentTask.objects.filter(season__is_active=True, kind='battlenet',
                    state__in=ACTIVE_STATES, due_at__lte=claim_time, expires_at__gt=claim_time).exists()
                due_at = min(due_at, last_scan_time + timedelta(seconds=60)) if bnet_due else max(due_at, retry)
        except (ValueError, TypeError, AttributeError):
            pass
    return due_at if due_at <= claim_time else None


def _monitor_task_order(task, due_at, claim_time):
    """A resumed long task waits from its latest attempt, not an ancient slot."""
    name = monitor_plugin_name(task)
    priority = portal_monitor_task_priority(task)
    if name in LONG_MONITOR_TASKS:
        last = getattr(task, 'last_scan_time', None)
        if last is not None and due_at is not None:
            due_at = max(due_at, _normalized_now(last))
        # Keep the existing shared-pool tie order. Long-lane selection applies
        # its own business tie order only after comparing actual waiting time.
    return due_at is None, due_at or claim_time, priority


def monitor_task_sort_key(task, now=None):
    """Run the task that has waited longest; priority only breaks ties."""
    claim_time = _normalized_now(now)
    return _monitor_task_order(task, monitor_task_due_at(task, now=claim_time), claim_time)


def portal_data_task_is_due(task, now=None):
    """Return whether a portal raw/aggregate task still owes the latest fixed slot."""
    if getattr(task, "name", "") not in PORTAL_DATA_SCHEDULED_TASKS:
        return None
    return portal_data_task_due_at(task, now=now) is not None


def monitor_scope_key(task):
    """Plugin dispatch ID + exact target + execution environment; name is a label."""
    return task.type, task.target, task.env_limit


def _locked_monitor_tasks_and_scopes(now=None):
    # Every claimant locks the small configuration table in the same order.
    # Include disabled rows: their in-flight leases still protect the scope.
    rows = list(MonitorTask.objects.select_for_update().order_by('id'))
    leases = list(MonitorTaskLease.objects.select_for_update().filter(
        task_id__in=[r.pk for r in rows],
    ).values('task_id', 'expires_at'))
    # Both lock acquisitions can wait. Evaluate time/capacity only afterwards;
    # otherwise a 06:00 transition or a long wait can admit stale/expired work.
    claim_time = _normalized_now(now)
    leased_task_ids = {lease['task_id'] for lease in leases if lease['expires_at'] > claim_time}
    active_scopes = {monitor_scope_key(row) for row in rows if row.pk in leased_task_ids}
    return rows, active_scopes, leased_task_ids, claim_time


def monitor_plugin_name(task):
    """Dispatch type is authoritative; labels may differ on legacy aliases."""
    from LMonitor.config import Monitor_Type_BaseObject_List
    task_type = getattr(task, 'type', None)
    if isinstance(task_type, int) and 0 <= task_type < len(Monitor_Type_BaseObject_List):
        return Monitor_Type_BaseObject_List[task_type].__name__
    return getattr(task, 'name', '')


def _night_lane_scopes(rows):
    return {monitor_scope_key(row) for row in rows
            if monitor_plugin_name(row) in LONG_MONITOR_TASKS}


def _night_lane_head(tasks, *, order_keys=None):
    if not tasks:
        return None
    if len(tasks) == 1:
        return tasks[0]
    if order_keys is None:
        now = _normalized_now()
        order_keys = {task.pk: monitor_task_sort_key(task, now=now) for task in tasks}
    return min(tasks, key=lambda task: (
        order_keys[task.pk][:2],
        NIGHT_LANE_PRIORITY.get(monitor_plugin_name(task), len(NIGHT_LANE_TASKS)), task.pk))


def _night_lane_candidates(rows, due_tasks, active_scopes, *, long_limit, running_count,
                           order_keys=None):
    """Keep source preparation ordering, otherwise use any free long-task slot."""
    lane_scopes = _night_lane_scopes(rows)
    running = lane_scopes & active_scopes
    if running_count >= long_limit:
        return []
    barrier = {'SpecDetailSeasonMonitor', 'WowDataVersionMonitor', 'MythicDungeonToolsMonitor'}
    barrier_scopes = {monitor_scope_key(r) for r in rows if monitor_plugin_name(r) in barrier}
    if active_scopes & barrier_scopes:
        return []
    pending = [r for r in due_tasks
               if monitor_scope_key(r) in lane_scopes or monitor_plugin_name(r) in barrier]
    preparation = [r for r in pending if monitor_plugin_name(r) in barrier]
    if preparation:
        # Preparation is a dependency barrier, not a fairness competitor.
        return [] if running else [min(preparation, key=lambda task: (
            NIGHT_LANE_PRIORITY[monitor_plugin_name(task)], task.pk))]
    heads = []
    while pending and len(heads) < long_limit - running_count:
        head = _night_lane_head(pending, order_keys=order_keys)
        heads.append(head)
        pending.remove(head)
    return heads


def _claim_monitor_task(now=None, *, task_id=None, lease_owner=None,
                        lease_seconds=None, task_names=None, allow_long_tasks=True,
                        advance_schedule=True):
    """One atomic admission path for automatic and explicitly requested work.

    An explicit request bypasses the schedule/enable switch, not execution
    capacity or same-scope ownership. Every live lease occupies a shared slot.
    """
    owner = str(lease_owner or uuid4().hex)
    ttl_seconds = _monitor_task_lease_ttl(lease_seconds)
    manual = task_id is not None
    with transaction.atomic():
        # Lock all parent rows in the same order across every claimant/process.
        rows, active_scopes, leased_task_ids, claim_time = _locked_monitor_tasks_and_scopes(now)
        if len(leased_task_ids) >= MONITOR_WORKER_COUNT:
            return None
        canonical = {}
        for row in rows:
            eligible = row.pk == task_id if manual else row.is_active and is_task_runnable(row)
            if eligible:
                canonical.setdefault(monitor_scope_key(row), row)
        lane_scopes = _night_lane_scopes(rows)
        due_tasks, order_keys = [], {}
        for row in canonical.values():
            if (monitor_scope_key(row) in active_scopes
                    or (not allow_long_tasks and monitor_scope_key(row) in lane_scopes)):
                continue
            due_at = claim_time if manual else monitor_task_due_at(row, now=claim_time)
            if due_at is not None:
                due_tasks.append(row)
                order_keys[row.pk] = _monitor_task_order(row, due_at, claim_time)
        is_night = claim_time.astimezone(PORTAL_DATA_TIMEZONE).hour < 6
        running_count = sum(row.pk in leased_task_ids and monitor_scope_key(row) in lane_scopes
                            for row in rows)
        lane_heads = _night_lane_candidates(rows, due_tasks, active_scopes,
            long_limit=2 if is_night else 1, running_count=running_count, order_keys=order_keys)
        preparation = {'SpecDetailSeasonMonitor', 'WowDataVersionMonitor', 'MythicDungeonToolsMonitor'}
        tasks = [row for row in due_tasks
                 if (row in lane_heads or (monitor_scope_key(row) not in lane_scopes
                                          and monitor_plugin_name(row) not in preparation))
                 and (task_names is None or row.name in task_names)]
        if not manual:
            tasks.sort(key=lambda row: order_keys[row.pk])
        if not tasks:
            return None
        task = tasks[0]
        MonitorTaskLease.objects.update_or_create(
            task_id=task.id,
            defaults={'owner': owner, 'is_manual': manual,
                      'claimed_at': claim_time,
                      'expires_at': claim_time + timedelta(seconds=ttl_seconds)},
        )
        fields = []
        if advance_schedule:
            task.last_scan_time = claim_time
            fields.append('last_scan_time')
        if not manual and task.name == 'SpecDetailPlayerMonitor':
            task.flag = json.dumps({'kind': 'player_daily',
                'day': claim_time.astimezone(PORTAL_DATA_TIMEZONE).date().isoformat(), 'state': 'claimed'})
            fields.append('flag')
        if fields:
            task.save(update_fields=fields)
        task._monitor_task_lease_owner = owner
        task._monitor_task_is_manual = manual
        task._monitor_task_is_long = monitor_scope_key(task) in lane_scopes
        if advance_schedule:
            from botend.services.wcl_collection_cycle import fence_claimed_cycle
            fence_claimed_cycle(task, claim_time=claim_time)
        return task


def claim_next_monitor_task(now=None, *, lease_owner=None, lease_seconds=None,
                            task_names=None, allow_long_tasks=True):
    """Poll every plugin through the shared three-slot queue; no source workers."""
    return _claim_monitor_task(now, lease_owner=lease_owner, lease_seconds=lease_seconds,
        task_names=task_names, allow_long_tasks=allow_long_tasks)


def claim_monitor_task(task_id, now=None, *, lease_owner=None, lease_seconds=None,
                       advance_schedule=True):
    """Explicit execution uses the same resource limits and scope mutex."""
    return _claim_monitor_task(now, task_id=task_id, lease_owner=lease_owner,
                               lease_seconds=lease_seconds, advance_schedule=advance_schedule)


def renew_monitor_task_lease(task_id, lease_owner, now=None, *, lease_seconds=None):
    """Extend a lease while holding the same parent-row mutex used by claimers."""
    ttl_seconds = _monitor_task_lease_ttl(lease_seconds)
    with transaction.atomic():
        task_exists = MonitorTask.objects.select_for_update().filter(pk=task_id).exists()
        if not task_exists:
            return False
        lease = MonitorTaskLease.objects.select_for_update().filter(
            task_id=task_id, owner=str(lease_owner),
        ).first()
        renewed_at = _normalized_now(now)
        if lease is None or lease.expires_at <= renewed_at:
            return False
        return MonitorTaskLease.objects.filter(pk=lease.pk, owner=str(lease_owner)).update(
            expires_at=renewed_at + timedelta(seconds=ttl_seconds)) == 1


def release_monitor_task_lease(task_id, lease_owner):
    """Release a lease under the parent-row mutex and exact-owner fence."""
    with transaction.atomic():
        task_exists = MonitorTask.objects.select_for_update().filter(pk=task_id).exists()
        if not task_exists:
            return False
        deleted, _ = MonitorTaskLease.objects.select_for_update().filter(
            task_id=task_id,
            owner=str(lease_owner),
        ).delete()
        return deleted == 1


def complete_monitor_task_lease(task_id, lease_owner, *, now=None, task_updates=None):
    """Atomically commit allowed task state and release a still-valid owned lease."""
    updates = dict(task_updates or {})
    unsupported_fields = set(updates) - {'flag'}
    if unsupported_fields:
        raise ValueError(
            'unsupported MonitorTask completion fields: {}'.format(
                ', '.join(sorted(unsupported_fields))
            )
        )

    with transaction.atomic():
        task_exists = MonitorTask.objects.select_for_update().filter(pk=task_id).exists()
        if not task_exists:
            return False
        lease = MonitorTaskLease.objects.select_for_update().filter(
            task_id=task_id,
            owner=str(lease_owner),
        ).first()
        completed_at = _normalized_now(now)
        if lease is None or lease.expires_at <= completed_at:
            return False
        if updates:
            MonitorTask.objects.filter(pk=task_id).update(**updates)
        lease.delete()
        return True


def sync_monitortasks_from_plugin_list(
    plugin_list,
    *,
    default_target="",
    default_is_active=False,
    skip_indexes=None,
):
    skip = set(skip_indexes or [])
    created = 0
    total = len(plugin_list or [])
    name_to_idx = {}
    for idx, plugin_cls in enumerate(plugin_list or []):
        if idx in skip:
            continue
        name = getattr(plugin_cls, "__name__", None) or f"PluginType{idx}"
        name_to_idx[str(name)] = idx

    with transaction.atomic():
        candidates = list(MonitorTask.objects.filter(name__in=list(name_to_idx.keys())))
        to_fix = []
        for t in candidates:
            desired = name_to_idx.get((t.name or "").strip())
            if desired is None:
                continue
            try:
                cur = int(getattr(t, "type", 0) or 0)
            except Exception:
                cur = 0
            if cur != int(desired):
                to_fix.append((int(t.id), int(desired)))
            desired_wait_time = monitor_default_wait_time(t.name)
            if t.wait_time != desired_wait_time and t.name not in ('MaxrollClassGuideMonitor', 'WowDataVersionMonitor'):
                MonitorTask.objects.filter(id=t.id).update(wait_time=desired_wait_time)

        for tid, _desired in to_fix:
            MonitorTask.objects.filter(id=tid).update(type=-tid)
        for tid, desired in to_fix:
            MonitorTask.objects.filter(id=tid).update(type=desired)

        for idx, plugin_cls in enumerate(plugin_list or []):
            if idx in skip:
                continue
            existing = MonitorTask.objects.filter(type=idx).first()
            if existing:
                name = getattr(plugin_cls, "__name__", None) or f"PluginType{idx}"
                if (existing.name or "").strip() != name:
                    logger.warning(f"[MonitorTask Sync] type={idx} name mismatch: db={existing.name} cfg={name}, updating...")
                    MonitorTask.objects.filter(id=existing.id).update(name=name)
                continue

            name = getattr(plugin_cls, "__name__", None) or f"PluginType{idx}"
            wait_time = monitor_default_wait_time(name)
            plugin_target = getattr(plugin_cls, 'default_target', default_target)
            plugin_is_active = bool(getattr(plugin_cls, 'default_is_active', default_is_active))
            plugin_proxy_enabled = bool(getattr(plugin_cls, 'default_proxy_enabled', False))
            create_values = {
                'name': name,
                'target': plugin_target,
                'type': idx,
                'is_active': plugin_is_active,
                'proxy_enabled': plugin_proxy_enabled,
                'wait_time': wait_time,
            }
            if plugin_is_active:
                # 首次部署后立即补一份数据，之后再按固定时段运行。
                create_values['last_scan_time'] = (timezone.now() if name == 'WowDataVersionMonitor'
                                                 else timezone.now() - timedelta(days=2))
            MonitorTask.objects.create(
                **create_values,
            )
            created += 1

    logger.info(f"[MonitorTask Sync] Done. created={created}, total_plugins={total}, skipped={len(skip)}")

    return created
