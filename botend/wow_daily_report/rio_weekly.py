"""周三数据观察的纯只读投影：周三至周二完整自然日，不补齐缺失事实。"""
from datetime import timedelta


def project_weekly(body, day):
    """从同一中央正文计算冻结窗口；day 是周二内容日而不是执行/发布日。

    端点精确匹配，不使用最新来源日代替周二，不跨赛季或合并地区。
    历史校验复用日报规则；不读取当前 metadata、中央存储或机器时钟。
    """
    if day.weekday() != 1:
        raise ValueError('周三特刊的内容日期必须为周二')
    # Lazy import avoids a module cycle while sharing strict canonical history validation.
    from .rio_section import _history

    values = _history(body)
    baseline = day - timedelta(days=7)
    previous_baseline = day - timedelta(days=14)

    def change(end, start):
        a, b = values.get(end), values.get(start)
        return a - b if a is not None and b is not None else None

    days = []
    for offset in range(6, -1, -1):
        current = day - timedelta(days=offset)
        days.append({'date': current.isoformat(), 'population': values.get(current),
                     'change': change(current, current - timedelta(days=1))})
    net = change(day, baseline)
    previous_net = change(baseline, previous_baseline)
    return {'period_start': (day - timedelta(days=6)).isoformat(), 'period_end': day.isoformat(),
        'baseline_date': baseline.isoformat(), 'previous_baseline_date': previous_baseline.isoformat(),
        'start_population': values.get(baseline), 'end_population': values.get(day),
        'net_change': net, 'previous_net_change': previous_net,
        'change_vs_previous': net - previous_net if net is not None and previous_net is not None else None,
        'coverage_days': sum(row['population'] is not None for row in days), 'days': days}
