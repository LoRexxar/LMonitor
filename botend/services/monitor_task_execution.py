"""Shared admission for bounded manual operations, without advancing a scan cycle."""
from contextlib import contextmanager

from botend.models import MonitorTask, MonitorTaskLeaseLost
from botend.monitor_env import env_limit_hint, is_task_runnable
from botend.plugin_sync import (
    claim_monitor_task,
    complete_monitor_task_lease,
    release_monitor_task_lease,
)


class MonitorTaskAdmissionBusy(RuntimeError):
    """The manual operation was not admitted; no executor has been called."""

    def __init__(self, message, *, code='monitor_task_busy', env_limit=None):
        super().__init__(message)
        self.code = code
        self.env_limit = env_limit


def _check_environment(task):
    if not is_task_runnable(task):
        env_limit = int(task.env_limit or 0)
        raise MonitorTaskAdmissionBusy(
            env_limit_hint(env_limit), code='env_limit_blocked', env_limit=env_limit,
        )


@contextmanager
def monitor_task_execution(task_name):
    """Yield the fenced task admitted through the scheduler's shared three slots.

    Resolve plugin identity from the registry, not a mutable task label. Manual
    slices neither advance last_scan_time/daily cycles nor save a returned flag;
    only the executor's own fenced MonitorTask.save() may persist its flag.
    """
    from LMonitor.config import Monitor_Type_BaseObject_List
    # views imports scheduler/plugin configuration too; avoid a module-level cycle.
    from botend.views import _MonitorTaskLeaseHeartbeat

    task_type = next((index for index, plugin in enumerate(Monitor_Type_BaseObject_List)
                      if plugin.__name__ == task_name), None)
    configured = MonitorTask.objects.filter(type=task_type).order_by('id') if task_type is not None else None
    task = ((configured.filter(name=task_name).first() or configured.first())
            if configured is not None else None)
    if task is None:
        raise MonitorTaskAdmissionBusy(
            '未配置 {} 任务，请先执行 SyncMonitorTasksFromPlugins；未执行'.format(task_name),
            code='monitor_task_not_configured',
        )
    _check_environment(task)
    task = claim_monitor_task(task.pk, advance_schedule=False)
    if task is None:
        raise MonitorTaskAdmissionBusy(
            '{} 未获执行租约（共享槽位已满、同范围或长任务繁忙）；未执行'.format(task_name),
        )

    owner = task._monitor_task_lease_owner
    try:
        # Configuration may change between lookup and atomic admission.
        if task.type != task_type:
            raise MonitorTaskAdmissionBusy(
                '{} 配置已变化；未执行'.format(task_name), code='monitor_task_not_configured',
            )
        _check_environment(task)
        heartbeat = _MonitorTaskLeaseHeartbeat(task.pk, owner)
        try:
            heartbeat.start()
            yield task
        finally:
            heartbeat.stop()
        # Atomic valid-owner completion also catches loss/expiry before the first
        # heartbeat. It releases ownership without writing task control state.
        if heartbeat.lease_lost.is_set() or not complete_monitor_task_lease(task.pk, owner):
            raise MonitorTaskLeaseLost('{} 执行租约已丢失，不能确认执行成功'.format(task_name))
    finally:
        # Includes constructor/start failures and executor exceptions; exact-owner
        # release must never delete a replacement worker's lease.
        release_monitor_task_lease(task.pk, owner)
