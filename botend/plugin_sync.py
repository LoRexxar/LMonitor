from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone

from utils.log import logger

from botend.models import MonitorTask, MonitorTaskLease
from botend.monitor_env import filter_runnable_tasks


MONITOR_TASK_LEASE_SECONDS = int(
    getattr(django_settings, 'MONITOR_TASK_LEASE_SECONDS', 600)
)


def _monitor_task_lease_ttl(lease_seconds=None):
    ttl_seconds = MONITOR_TASK_LEASE_SECONDS if lease_seconds is None else int(lease_seconds)
    if ttl_seconds <= 0:
        raise ValueError('monitor task lease_seconds must be positive')
    return ttl_seconds


PORTAL_DATA_SCHEDULE_HOURS_BY_TASK = {
    # Wowhead 在北美日常重置后会先返回无身份信息的 Active 占位行，
    # 随后才补齐具体地下堡。分三次渐进刷新，配合载荷完整性门禁，避免
    # 占位内容覆盖当天快照，并在上午自动补齐上游迟到的数据。
    "WowTodayMonitor": (4, 8, 10),
    # 每日大更新：人物内容先重抓，完整排名随后，聚合投影最后生成。
    "SpecDetailPlayerMonitor": (2,),
    "SpecDetailRankingMonitor": (3,),
    "SpecDetailAggregationMonitor": (6,),
}
PORTAL_DATA_SCHEDULED_TASKS = frozenset(PORTAL_DATA_SCHEDULE_HOURS_BY_TASK)
PORTAL_DATA_TIMEZONE = ZoneInfo("Asia/Shanghai")

PORTAL_MONITOR_TASK_PRIORITY = {
    # 巅峰榜 Top20 是快速任务；长任务结束后必须先补它，避免按旧
    # last_scan_time 排序时被人物、排名和聚合任务连续阻塞。
    "PortalPeakSpecRankMonitor": 0,
    "SpecDungeonDpsRankingMonitor": 5,
    "SpecDetailPlayerMonitor": 10,
    "SpecDetailRankingMonitor": 20,
    "SpecDetailAggregationMonitor": 30,
}


def monitor_default_wait_time(name):
    if name == "PortalPeakSpecRankMonitor":
        return 600  # 10m，仅轻量刷新榜单；新入榜人物按需初始化
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
    return 600


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
    today_slots = [
        local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
        for hour in PORTAL_DATA_SCHEDULE_HOURS_BY_TASK[task.name]
    ]
    completed_slots = [slot for slot in today_slots if slot <= local_now]
    latest_slot = completed_slots[-1] if completed_slots else today_slots[-1] - timedelta(days=1)

    last_scan_time = getattr(task, "last_scan_time", None)
    if last_scan_time is None:
        return latest_slot
    if timezone.is_naive(last_scan_time):
        last_scan_time = timezone.make_aware(last_scan_time, PORTAL_DATA_TIMEZONE)
    return latest_slot if last_scan_time < latest_slot else None


def monitor_task_due_at(task, now=None):
    """Return when this task became runnable, or None when it is not due."""
    claim_time = _normalized_now(now)
    if getattr(task, "name", "") in PORTAL_DATA_SCHEDULED_TASKS:
        return portal_data_task_due_at(task, now=claim_time)

    last_scan_time = getattr(task, "last_scan_time", None)
    if last_scan_time is None:
        return claim_time
    if timezone.is_naive(last_scan_time):
        last_scan_time = timezone.make_aware(last_scan_time, PORTAL_DATA_TIMEZONE)
    wait_time = int(getattr(task, "wait_time", monitor_default_wait_time(task.name)) or 0)
    due_at = last_scan_time + timedelta(seconds=wait_time)
    return due_at if due_at <= claim_time else None


def monitor_task_sort_key(task, now=None):
    """Run the task that has been due longest; priority only breaks ties."""
    claim_time = _normalized_now(now)
    due_at = monitor_task_due_at(task, now=claim_time)
    return due_at is None, due_at or claim_time, portal_monitor_task_priority(task)


def portal_data_task_is_due(task, now=None):
    """Return whether a portal raw/aggregate task still owes the latest fixed slot."""
    if getattr(task, "name", "") not in PORTAL_DATA_SCHEDULED_TASKS:
        return None
    return portal_data_task_due_at(task, now=now) is not None


def claim_next_monitor_task(now=None, *, lease_owner=None, lease_seconds=None):
    """Atomically reserve the globally oldest runnable task for one worker."""
    claim_time = now or timezone.now()
    owner = str(lease_owner or uuid4().hex)
    ttl_seconds = _monitor_task_lease_ttl(lease_seconds)
    expires_at = claim_time + timedelta(seconds=ttl_seconds)

    with transaction.atomic():
        # Lock the parent task rows in a stable order.  All claimers use this
        # parent-row mutex, so creating or replacing the separate lease row is
        # serialized without exposing lease fields to long-lived plugin models.
        runnable_tasks = list(filter_runnable_tasks(
            MonitorTask.objects.select_for_update().filter(is_active=1).order_by('id')
        ))
        active_lease_task_ids = set(
            MonitorTaskLease.objects.select_for_update()
            .filter(
                task_id__in=[task.id for task in runnable_tasks],
                expires_at__gt=claim_time,
            )
            .values_list('task_id', flat=True)
        )
        tasks = [
            task
            for task in runnable_tasks
            if task.id not in active_lease_task_ids
            and monitor_task_due_at(task, now=claim_time) is not None
        ]
        tasks.sort(key=lambda task: monitor_task_sort_key(task, now=claim_time))
        if tasks:
            task = tasks[0]
            MonitorTaskLease.objects.update_or_create(
                task_id=task.id,
                defaults={
                    'owner': owner,
                    'claimed_at': claim_time,
                    'expires_at': expires_at,
                },
            )
            task.last_scan_time = claim_time
            task.save(update_fields=('last_scan_time',))
            task._monitor_task_lease_owner = owner
            return task
    return None


def renew_monitor_task_lease(task_id, lease_owner, now=None, *, lease_seconds=None):
    """Extend a lease while holding the same parent-row mutex used by claimers."""
    renewed_at = now or timezone.now()
    ttl_seconds = _monitor_task_lease_ttl(lease_seconds)
    with transaction.atomic():
        task_exists = MonitorTask.objects.select_for_update().filter(pk=task_id).exists()
        if not task_exists:
            return False
        return MonitorTaskLease.objects.select_for_update().filter(
            task_id=task_id,
            owner=str(lease_owner),
            expires_at__gt=renewed_at,
        ).update(expires_at=renewed_at + timedelta(seconds=ttl_seconds)) == 1


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
    completed_at = now or timezone.now()
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
            expires_at__gt=completed_at,
        ).first()
        if lease is None:
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
            if t.wait_time != desired_wait_time:
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
                create_values['last_scan_time'] = timezone.now() - timedelta(days=2)
            MonitorTask.objects.create(
                **create_values,
            )
            created += 1

    logger.info(f"[MonitorTask Sync] Done. created={created}, total_plugins={total}, skipped={len(skip)}")

    return created
