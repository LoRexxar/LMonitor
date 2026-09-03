import time

from botend.controller.BaseScan import BaseScan
from botend.services.wow_today_service import WOWHEAD_SOURCE_URL, WowTodayService
from utils.log import logger


class WowTodayMonitor(BaseScan):
    """每日同步 Wowhead 北美正式服当前版本内容。"""

    default_is_active = True
    default_proxy_enabled = True
    default_target = WOWHEAD_SOURCE_URL

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task
        self.last_error_detail = ''

    def scan(self, url):
        try:
            result = WowTodayService(request_client=self.req).sync()
            self.task.flag = '{}@{}'.format(result['snapshot_date'], int(time.time()))
            self.task.save(update_fields=['flag'])
            logger.info('[WowTodayMonitor] 同步完成: %s', result)
            return True
        except Exception as exc:
            self.last_error_detail = str(exc)[:1000]
            logger.error('[WowTodayMonitor] 同步失败: %s', self.last_error_detail)
            return False
