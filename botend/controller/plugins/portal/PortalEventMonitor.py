import time

from botend.controller.BaseScan import BaseScan
from botend.services.portal_event_service import PortalEventService
from utils.log import logger


class PortalEventMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        service = PortalEventService(request_client=self.req)
        try:
            if (url or "").strip():
                result = service.sync_news_events(source_url=url)
            else:
                result = service.sync_db2_events()
            if result.get("total", 0) == 0:
                fallback = service.seed_fallback_events()
                logger.warning(f"[PortalEventMonitor] no remote events parsed, seeded fallback: {fallback}")
            try:
                self.task.flag = f"events@{int(time.time())}"
                self.task.save()
            except Exception:
                pass
            logger.info(f"[PortalEventMonitor] sync result: {result}")
            return True
        except Exception as e:
            logger.error(f"[PortalEventMonitor] sync failed: {str(e)}")
            return False
