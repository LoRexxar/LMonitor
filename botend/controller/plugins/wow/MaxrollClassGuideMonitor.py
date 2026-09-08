"""通过 botend 统一调度执行一次职业攻略增量更新。"""
from botend.controller.BaseScan import BaseScan
from botend.services.class_guide_service import sync_guides
from utils.log import logger


class MaxrollClassGuideMonitor(BaseScan):
    default_is_active = False
    default_target = 'https://maxroll.gg/wow/class-guides'

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.last_error_detail = ''

    def scan(self, url):
        try:
            run = sync_guides(translate=True, workers=1, request_client=self.req, monitor_task=self.task)
            self.task.flag = f'批次 {run.id} · {run.status} · {len(run.results)} 篇'
            if run.status != 'completed':
                errors = [row.get('error') or row['status'] for row in run.results
                          if row['status'] in ('failed', 'translation_partial')]
                self.last_error_detail = f'攻略同步批次 {run.id} 未全部完成：' + '；'.join(errors)[:1000]
                return False
            logger.info('[MaxrollClassGuideMonitor] %s', self.task.flag)
            return True
        except Exception as exc:
            self.last_error_detail = str(exc)[:1000]
            logger.error('[MaxrollClassGuideMonitor] %s', self.last_error_detail)
            return False
