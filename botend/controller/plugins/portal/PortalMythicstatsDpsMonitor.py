"""由统一服务采集、保存并发布 Mythicstats 全部展示范围。"""
from botend.controller.BaseScan import BaseScan
from botend.services.mythicstats_snapshot import collect_snapshots
from utils.log import logger


class PortalMythicstatsDpsMonitor(BaseScan):
    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        hint = (url or '').strip() or (getattr(self.task, 'target', '') or '').strip()
        try:
            result = collect_snapshots(req=self.req, season_hint=hint)
        except Exception:
            logger.exception('[PortalMythicstatsDpsMonitor] 采集发布失败，保留上次结果')
            return False
        if result['busy']:
            logger.info('[PortalMythicstatsDpsMonitor] 已有后台任务处理，跳过重复执行')
            return False
        logger.info(f"[PortalMythicstatsDpsMonitor] 发布 {result['built']} 个分片，失败 {result['failed']} 个")
        if result['latest_published']:
            self.task.flag = f"{result['season']}@{result['period_id']}"
            self.task.save(update_fields=['flag'])
        return bool(result['latest_published']) and not result['failed']
