"""攻略后台与通用监控后台共用同一个任务配置。"""
from datetime import timedelta

from django.utils import timezone

from botend.models import MonitorTask


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
