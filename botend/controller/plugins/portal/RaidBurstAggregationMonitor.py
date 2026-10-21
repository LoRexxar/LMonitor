"""Manual-only raid burst analysis, sharing the standard monitor executor."""
from botend.controller.BaseScan import BaseScan
from botend.services.raid_burst_runner import run_raid_burst


class RaidBurstAggregationMonitor(BaseScan):
    default_is_active = False
    requires_browser = False

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        self.result = run_raid_burst(self.task)
        return self.result['status'] == 'completed'
