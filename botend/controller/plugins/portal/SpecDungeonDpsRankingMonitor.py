# -*- coding: utf-8 -*-

import time

from botend.controller.BaseScan import BaseScan
from botend.services.mplus_dps_rankings_service import publish_current_mplus_dps_rankings
from utils.log import logger


class SpecDungeonDpsRankingMonitor(BaseScan):
    """Publish the current-season local WCL DPS leaderboard once per hour."""

    default_is_active = True
    default_target = ''

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url):
        payload = publish_current_mplus_dps_rankings()
        season = payload.get('season') or {}
        ranking_count = len((payload.get('rankings') or {}).get('overall') or [])
        self.task.flag = '{}@specs={}@{}'.format(
            season.get('key') or season.get('id') or 'season',
            ranking_count,
            int(time.time()),
        )
        self.task.save(update_fields=['flag'])
        logger.info(
            '[SpecDungeonDpsRanking] published season=%s scopes=%s specs=%s',
            season.get('key'), len(payload.get('scopes') or []), ranking_count,
        )
        return True
