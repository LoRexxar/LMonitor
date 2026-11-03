"""WCL ranking startTime is an absolute Unix millisecond UTC instant.

Neither report.startTime (report origin) nor last_updated (ingestion clock)
can substitute for an unknown fight start.
"""
import math
from datetime import datetime, timedelta, timezone

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_ranking_fight_started_at(ranking):
    value = ranking.get('startTime')
    if value is None:
        return None
    if type(value) not in (int, float):
        raise ValueError('invalid ranking startTime: expected Unix milliseconds')
    try:
        if not math.isfinite(value) or value <= 0:
            raise ValueError('invalid ranking startTime')
        # Integer milliseconds preserve upstream precision without float epoch rounding.
        return _EPOCH + timedelta(milliseconds=value)
    except (OverflowError, TypeError) as exc:
        raise ValueError('ranking startTime out of datetime range') from exc
