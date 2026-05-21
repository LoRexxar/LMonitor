import time

from django.utils import timezone

from botend.controller.BaseScan import BaseScan
from botend.wow_daily_report.generator import generate_wow_daily_report


class WowDailyReportMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        report_date = timezone.localdate()
        generate_wow_daily_report(report_date=report_date, use_llm=True)
        try:
            self.task.flag = f"{report_date.isoformat()}@{int(time.time())}"
            self.task.save()
        except Exception:
            pass
        return True
