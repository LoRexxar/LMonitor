import datetime
import time

from django.utils import timezone

from botend.controller.BaseScan import BaseScan
from botend.wow_daily_report.generator import generate_wow_daily_report


class WowDailyReportMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        # 日报任务由后台定时器触发。使用“昨天”作为日报日期，保证 wowhead/蓝贴/NGA/视频
        # 这些全天滚动采集源已经有完整一天的时间入库，避免凌晨先生成日报导致漏源。
        report_date = timezone.localdate() - datetime.timedelta(days=1)
        generate_wow_daily_report(report_date=report_date, use_llm=True)
        try:
            self.task.flag = f"{report_date.isoformat()}@{int(time.time())}"
            self.task.save()
        except Exception:
            pass
        return True
