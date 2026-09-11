"""通过现有监控后端定期同步冒险手册。"""
from botend.controller.BaseScan import BaseScan
from botend.services.journal_service import sync_journal


class AdventureJournalMonitor(BaseScan):
    default_is_active = True
    default_target = 'https://wago.tools/journal'

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.last_error_detail = ''

    def scan(self, url):
        try:
            release = sync_journal(refresh=True)
            self.task.flag = f'冒险手册 {release.build} · {release.report["encounters"]} 个首领'
            return True
        except Exception as exc:
            self.last_error_detail = str(exc)[:1000]
            return False
