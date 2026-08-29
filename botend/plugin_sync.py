from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone

from utils.log import logger

from botend.models import MonitorTask
from botend.monitor_env import filter_runnable_tasks


PORTAL_DATA_SCHEDULE_HOURS_BY_TASK = {
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
    "SpecDetailPlayerMonitor": 10,
    "SpecDetailRankingMonitor": 20,
    "SpecDetailAggregationMonitor": 30,
}


def monitor_default_wait_time(name):
    if name == "PortalPeakSpecRankMonitor":
        return 600  # 10m，仅轻量刷新榜单；新入榜人物按需初始化
    if name == "PortalMplusCutoffMonitor":
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


def monitor_task_sort_key(task):
    """Keep the global queue fair; priority only breaks equal-time ties."""
    return task.last_scan_time, portal_monitor_task_priority(task)


def portal_data_task_is_due(task, now=None):
    """Return whether a portal raw/aggregate task still owes the latest fixed slot."""
    if getattr(task, "name", "") not in PORTAL_DATA_SCHEDULED_TASKS:
        return None

    now = now or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, PORTAL_DATA_TIMEZONE)
    local_now = now.astimezone(PORTAL_DATA_TIMEZONE)
    today_slots = [
        local_now.replace(hour=hour, minute=0, second=0, microsecond=0)
        for hour in PORTAL_DATA_SCHEDULE_HOURS_BY_TASK[task.name]
    ]
    completed_slots = [slot for slot in today_slots if slot <= local_now]
    latest_slot = completed_slots[-1] if completed_slots else today_slots[-1] - timedelta(days=1)

    last_scan_time = getattr(task, "last_scan_time", None)
    if last_scan_time is None:
        return True
    if timezone.is_naive(last_scan_time):
        last_scan_time = timezone.make_aware(last_scan_time, PORTAL_DATA_TIMEZONE)
    return last_scan_time < latest_slot


def claim_next_monitor_task(now=None):
    """Atomically reserve the globally oldest runnable task for one worker."""
    claim_time = now or timezone.now()
    with transaction.atomic():
        tasks = sorted(
            filter_runnable_tasks(
                MonitorTask.objects.select_for_update().filter(is_active=1)
            ),
            key=monitor_task_sort_key,
        )
        for task in tasks:
            scheduled_due = portal_data_task_is_due(task, now=claim_time)
            if scheduled_due is False:
                continue
            if (
                scheduled_due is None
                and (claim_time - task.last_scan_time).total_seconds() < task.wait_time
            ):
                continue
            task.last_scan_time = claim_time
            task.save(update_fields=('last_scan_time',))
            return task
    return None


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
            MonitorTask.objects.create(
                name=name,
                target=default_target,
                type=idx,
                is_active=default_is_active,
                wait_time=wait_time,
            )
            created += 1

    logger.info(f"[MonitorTask Sync] Done. created={created}, total_plugins={total}, skipped={len(skip)}")

    return created
