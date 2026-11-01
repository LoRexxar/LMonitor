"""Independent due-only character-season records maintenance, without Chrome."""
import json

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.controller.BaseScan import BaseScan
from botend.models import SeasonMeta
from botend.services.player_raiderio_activity import refresh_due_top20_activity
from utils.log import logger


class PlayerRaiderIOActivityMonitor(BaseScan):
    default_is_active = True
    default_target = ''
    default_wait_time = 600
    requires_browser = False

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url=''):
        # The scheduler may construct a fresh monitor (or hold a stale task).
        # The task lease serializes this scope; always reload its durable gate.
        self.task.refresh_from_db(fields=['flag'])
        try:
            previous = json.loads(self.task.flag or '{}')
        except (ValueError, TypeError):
            previous = {}
        gate = {}
        if previous.get('rate_limited_until') or previous.get('stop') == 'rate_limited' or previous.get('rate_limit_clock_error'):
            try:
                deadline = parse_datetime(previous.get('rate_limited_until') or '')
                if deadline is None or timezone.is_naive(deadline):
                    raise ValueError('missing source clock')
                if previous.get('rate_limit_clock_error'):
                    raise ValueError('uncertain source clock')
                if deadline > timezone.now():
                    gate['rate_limited_until'] = deadline
            except (ValueError, TypeError):
                gate['rate_limit_clock_error'] = 'causal_429_clock_missing_or_changed'
        seasons = list(SeasonMeta.objects.filter(is_active=True)[:2])
        if len(seasons) != 1 or not seasons[0].rio_season:
            summary = {**previous, 'stop': 'invalid_active_season', 'active_count': len(seasons)}
            if gate.get('rate_limit_clock_error'):
                summary['rate_limit_clock_error'] = gate['rate_limit_clock_error']
            self._record(summary)
            logger.error('[PlayerRaiderIOActivity] requires one active SeasonMeta with rio_season: %s', summary)
            return False
        season = seasons[0]
        result = refresh_due_top20_activity(season, limit=100, **gate)
        # No large per-player result/payload in MonitorTask.flag. A completed scan
        # is not a declaration that partial/error/deferred modules are all ready.
        summary = {key: value for key, value in result.items() if key != 'results'}
        summary.update(season_id=season.pk, checked_at=timezone.now().isoformat())
        self._record(summary)
        logger.info('[PlayerRaiderIOActivity] %s', json.dumps(summary, sort_keys=True))
        # Scan outcome describes execution, not full business-data readiness.
        # Bounded backlog and anonymous source identities remain explicit in
        # flag, but are not permanent task failures on every successful pass.
        if (result['stop'] == 'rate_limited' and result['attempted'] == 0
                and not result.get('rate_limit_clock_error')):
            return True  # Known reset wait: no new source attempt or ready claim.
        return not (result['stopped'] or any(result[key] for key in (
            'error', 'partial', 'roster_error', 'raid_error')))

    def _record(self, summary):
        self.task.flag = json.dumps(summary, sort_keys=True, separators=(',', ':'))
        self.task.save(update_fields=['flag'])
