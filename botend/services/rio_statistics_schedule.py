"""每日凌晨批次的完成与续跑判定；不把领取时间视为完成时间。"""
from datetime import date, datetime, timedelta
import json


def flag_data(value):
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def identity_day(identity, local_now, scope_token):
    """Validate the persisted day@scope contract without guessing token syntax."""
    if not isinstance(identity, str) or not scope_token:
        return None
    parts = identity.split('@', 1)
    if len(parts) != 2 or parts[1] != scope_token:
        return None
    try:
        day = date.fromisoformat(parts[0])
    except ValueError:
        return None
    return day if day.isoformat() == parts[0] and day <= local_now.date() else None


def partial_identity(flag, local_now, scope_token):
    """Only a validated partial batch authorizes cross-day recovery."""
    identity = flag.get('identity')
    if flag.get('state') != 'partial' or identity_day(identity, local_now, scope_token) is None:
        return None
    return identity


def retry_due_at(flag, last):
    due = last + timedelta(minutes=30) if last is not None else None
    try:
        retry = datetime.fromisoformat(str(flag.get('retry_at', '')).replace('Z', '+00:00'))
        if retry.tzinfo is not None:
            due = max(due, retry) if due is not None else retry
    except (ValueError, TypeError):
        pass
    return due


def resume_due_at(task, local_now, slot, *, scope_token=None):
    last = getattr(task, 'last_scan_time', None)
    flag = flag_data(getattr(task, 'flag', ''))
    day = identity_day(flag.get('identity'), local_now, scope_token)
    if flag.get('state') == 'complete' and day == local_now.date():
        return None
    # Check persisted identity BEFORE today's startup slot. Yesterday's partial
    # may resume, but an old/unknown scope cannot turn into a daytime new batch.
    resuming = last is not None and partial_identity(flag, local_now, scope_token) is not None
    legacy_claim = (not flag.get('identity') and flag.get('state') != 'complete'
                    and last is not None and last >= slot)
    due = retry_due_at(flag, last)
    if not (resuming or legacy_claim):
        if local_now.hour >= 6:
            return None
        due = max(slot, due) if due is not None else slot
    return due if due is not None and due <= local_now else None
