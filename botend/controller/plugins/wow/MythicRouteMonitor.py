"""每日更新 Raider.IO 公开推荐路线。"""
from botend.controller.BaseScan import BaseScan
from botend.mythic_planner.rio_routes import collect_routes


class MythicRouteMonitor(BaseScan):
    default_is_active = True
    requires_browser = False
    default_target = 'https://raider.io'

    def scan(self, url):
        try:
            collect_routes()
        except Exception as exc:
            self.last_error_detail = str(exc)
            return False
        return True
