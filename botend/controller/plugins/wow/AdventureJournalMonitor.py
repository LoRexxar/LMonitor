"""保留历史 MonitorTask.type=35 索引；冒险手册仅允许人工按需刷新。"""
from botend.controller.BaseScan import BaseScan


class AdventureJournalMonitor(BaseScan):
    default_is_active = False
    default_target = 'https://wago.tools/journal'

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.last_error_detail = ''

    def scan(self, url):
        self.task.flag = '冒险手册仅手动刷新，后台不会自动同步'
        return True
