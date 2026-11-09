"""One calendar contract for yesterday's daily and Wednesday weekly editions."""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')


def _report_day(value):
    if type(value) is date:
        return value
    if isinstance(value, str):
        result = date.fromisoformat(value)
        if result.isoformat() == value:
            return result
    raise ValueError('日报内容日期必须是规范的 YYYY-MM-DD')


def report_date_for(now):
    """The normal monitor publishes yesterday in Shanghai, never host/user TZ."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('日报执行时间必须包含时区')
    return now.astimezone(SHANGHAI).date() - timedelta(days=1)


def edition_for_report_date(report_date):
    day = _report_day(report_date)
    weekly = day.weekday() == 1  # Tuesday content is published on Wednesday.
    return {
        'kind': 'wednesday_weekly' if weekly else 'daily',
        'publication_date': (day + timedelta(days=1)).isoformat(),
        'period_start': (day-timedelta(days=6) if weekly else day).isoformat(),
        'period_end': day.isoformat(),
        'timezone': 'Asia/Shanghai',
    }


def published_edition(report_date, ext):
    """Never relabel old Tuesday rows just because of their weekday.

    Only newly generated, explicitly persisted, canonical metadata can label
    an edition weekly. Malformed or legacy metadata does not make a claim.
    """
    if not isinstance(ext, dict) or not isinstance(ext.get('edition'), dict):
        return None
    try:
        expected = edition_for_report_date(report_date)
    except (TypeError, ValueError, OverflowError):
        return None
    return dict(expected) if ext['edition'] == expected else None
