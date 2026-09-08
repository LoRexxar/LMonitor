"""攻略手动同步与后台监控共用任务配置和执行锁。"""
from contextlib import contextmanager
from datetime import timedelta

from django.utils import timezone

from botend.models import MonitorTask, MonitorTaskLeaseLost
from botend.plugin_sync import claim_monitor_task, renew_monitor_task_lease, release_monitor_task_lease


def get_guide_monitor_task():
    # 延迟导入，避免插件注册表与同步服务的循环导入。
    from LMonitor.config import Monitor_Type_BaseObject_List
    name = 'MaxrollClassGuideMonitor'
    index = next(i for i, plugin in enumerate(Monitor_Type_BaseObject_List) if plugin.__name__ == name)
    task, _ = MonitorTask.objects.get_or_create(name=name, defaults={
        'type': index, 'target': 'https://maxroll.gg/wow/class-guides',
        'is_active': False, 'wait_time': 21600,
        'last_scan_time': timezone.now() - timedelta(days=2),
    })
    return task


@contextmanager
def guide_sync_task(task=None):
    task = task if task is not None else get_guide_monitor_task()
    if task.name != 'MaxrollClassGuideMonitor':
        raise ValueError('同步任务必须是 MaxrollClassGuideMonitor')
    if not task.notes.strip():
        raise ValueError('请先在监控任务 MaxrollClassGuideMonitor 的任务备注中登记来源授权说明')
    manual = not getattr(task, '_monitor_task_lease_owner', None)
    if manual:
        task = claim_monitor_task(task.pk)
        if task is None:
            raise MonitorTaskLeaseLost('已有攻略同步任务执行中')
    owner = task._monitor_task_lease_owner

    def heartbeat(*_):
        if not renew_monitor_task_lease(task.pk, owner):
            raise MonitorTaskLeaseLost('攻略同步的任务执行锁已经失效')

    try:
        heartbeat()
        yield task, heartbeat
    finally:
        if manual:
            release_monitor_task_lease(task.pk, owner)
