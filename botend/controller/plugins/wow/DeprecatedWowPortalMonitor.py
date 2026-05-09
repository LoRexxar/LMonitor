from botend.controller.BaseScan import BaseScan


class DeprecatedWowPortalMonitor(BaseScan):
    def scan(self, url):
        return True

