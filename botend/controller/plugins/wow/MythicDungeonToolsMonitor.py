"""按 backend 调度检查 MDT 正式发布并安全升级。"""
from botend.controller.BaseScan import BaseScan
from botend.mythic_planner.auto_update import sync_latest_mdt


class MythicDungeonToolsMonitor(BaseScan):
    default_is_active = True
    default_target = 'https://github.com/Nnoggie/MythicDungeonTools'
    requires_browser = False

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.last_error_detail = ''

    def scan(self, url):
        try:
            sync_latest_mdt(monitor_task=self.task, request_client=self.req)
            return True
        except Exception as exc:
            self.last_error_detail = f'MDT 自动更新失败：{exc}'
            return False
