"""魔兽世界数据观察：每天一次的夜间长任务。"""
import json
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone
from botend.controller.BaseScan import BaseScan
from botend.services.rio_statistics import run_nightly_collection, root, _load, _stamp
from botend.services.rio_statistics_scope import active_scope
from botend.services.rio_statistics_schedule import flag_data


class RioStatisticsMonitor(BaseScan):
    default_is_active = True
    default_target = 'https://raider.io/api'
    requires_browser = False

    def __init__(self, req, task):
        super().__init__(req, task)
        self.task = task

    def scan(self, url=None):
        scope = active_scope()
        day = timezone.now().astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
        identity = f"{day}@{scope['token']}"
        previous = flag_data(self.task.flag)
        if previous.get('identity') == identity and previous.get('state') == 'complete':
            return True
        try:
            result = run_nightly_collection()
        except Exception as exc:
            self._record(identity, 'partial', {'state': 'failed', 'error': type(exc).__name__})
            raise
        # 只有整个预算内的预设和地图成功才记录完成；失败不清理旧快照。
        complete = (result['state'] == 'complete' and not result.get('remaining')
                    and not result.get('failed') and not result.get('maps_failed') and not result.get('maps_remaining')
                    and not result.get('runs_failed') and not result.get('runs_pending') and not result.get('maintenance_error')
                    and not result.get('views_error'))
        self._record(identity, 'complete' if complete else 'partial', result)
        return complete

    def _record(self, identity, state, result):
        now = timezone.now()
        retry = now + timedelta(minutes=30)
        cooldown = _stamp(_load(root() / 'cooldown.json', {}).get('until'))
        if cooldown:
            retry = max(retry, cooldown)
        fields = ('state', 'requests', 'ready', 'failed', 'remaining', 'maps_failed', 'maps_remaining',
                  'runs_imported', 'runs_failed', 'runs_pending', 'cycle_id', 'storage_state', 'error', 'maintenance_error', 'views_error')
        summary = {key: result[key] for key in fields if key in result}
        self.task.flag = json.dumps({'identity': identity, 'state': state, 'result': summary,
                                    'checked_at': now.isoformat(), 'retry_at': retry.isoformat() if state != 'complete' else None},
                                   ensure_ascii=False, separators=(',', ':'))
        self.task.save(update_fields=['flag'])
