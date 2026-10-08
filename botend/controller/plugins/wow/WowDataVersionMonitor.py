"""由现有后台调度器检测装备与天赋版本。"""
from botend.controller.BaseScan import BaseScan
from botend.services.wow_data_update import sync_game_data


class WowDataVersionMonitor(BaseScan):
    default_is_active = True
    default_target = 'https://wago.tools/builds'
    requires_browser = False

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.last_error_detail = ''

    def scan(self, url):
        try:
            sync_game_data(monitor_task=self.task)
            return True
        except Exception as exc:
            self.last_error_detail = f'装备天赋版本更新未完成：{exc}'
            return False
