"""每日凌晨批次的完成与续跑判定；不把领取时间视为完成时间。"""
from datetime import timedelta
import json


def flag_data(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def resume_due_at(task, local_now, slot, *, scope_token=None):
    if local_now.hour >= 6:
        return None
    last = getattr(task, 'last_scan_time', None)
    if last is None or last < slot:
        return slot
    flag = flag_data(getattr(task, 'flag', ''))
    identity = str(flag.get('identity', '')).split('@', 1)
    day = identity[0]
    same_scope = scope_token is None or (len(identity) == 2 and identity[1] == scope_token)
    if flag.get('state') == 'complete' and day == local_now.date().isoformat() and same_scope:
        return None
    # 相同日批次允许恢复；租约互斥仍由原队列保证。退避让其他夜间任务继续运行。
    due = last + timedelta(minutes=30)
    try:
        from datetime import datetime
        retry = datetime.fromisoformat(str(flag.get('retry_at', '')).replace('Z', '+00:00'))
        if retry.tzinfo is not None:
            due = max(due, retry)
    except (ValueError, TypeError):
        pass
    return due if due <= local_now else None
