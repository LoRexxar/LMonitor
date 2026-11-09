"""Deterministic seven-day RIO cards; facts are supplied by the collector."""
from datetime import date
from decimal import Decimal

from .presentation import _escape, _source_href


def _count(value, signed=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return '—'
    number = Decimal(str(value))
    if not number.is_finite() or number != number.to_integral_value():
        return '—'
    integer = int(number)
    return ('+' if signed and integer > 0 else '') + f'{integer:,}'


def _direction(value):
    if _count(value) == '—' or value == 0:
        return 'neutral'
    return 'positive' if value > 0 else 'negative'


def _link(label, value):
    href = _source_href(value)
    return (f'<a href="{_escape(href)}" target="_blank" rel="noopener noreferrer">'
            f'{_escape(label)} ↗</a>') if href else ''


def _day_label(value):
    try:
        day = date.fromisoformat(value)
        return f'{day.month}月{day.day}日'
    except (TypeError, ValueError):
        return value


def _weekly_summary(weekly):
    """Narrate already-computed facts; do not infer activity or distinct players."""
    net, diff = weekly.get('net_change'), weekly.get('change_vs_previous')
    if _count(net) == '—':
        first = '本周比较端点缺失，暂无法计算净变化'
    elif net > 0:
        first = '本周参与角色净增加 ' + _count(net)
    elif net < 0:
        first = '本周参与角色净减少 ' + _count(-net)
    else:
        first = '本周参与角色净变化为 0'
    if _count(net) == '—' or _count(diff) == '—':
        comparison = '上周比较数据不完整'
    elif diff > 0:
        comparison = '净变化较上周增加 ' + _count(diff)
    elif diff < 0:
        comparison = '净变化较上周减少 ' + _count(-diff)
    else:
        comparison = '净变化与上周持平'
    return first + '；' + comparison + '。'


def render_weekly_rio_items(items):
    """Keep region/season identities separate, never sum cumulative populations.

    The end value is strictly Tuesday, not the latest daily fallback. Changes
    and coverage are the collector's explicit facts, never AI-derived text.
    """
    # The generator delegates here: import its existing time formatter lazily.
    from .generator import _fmt_source_time

    cards = ['<div class="rio-weekly-grid">']
    for item in items:
        weekly = item['weekly']
        cards.append(
            f'<article class="rio-weekly-card" data-region="{_escape(item.get("region"))}">'
            f'<header class="rio-weekly-heading"><h3>{_escape(item.get("region_label"))}</h3>'
            f'<span>{_escape(item.get("season_name"))}</span></header>'
            f'<p class="rio-weekly-period">{_escape(weekly.get("period_start"))}—{_escape(weekly.get("period_end"))}</p>'
            f'<p class="rio-weekly-population"><strong>{_count(weekly.get("end_population"))}</strong>'
            '<span>期末参与角色数</span></p><dl class="rio-weekly-metrics">'
        )
        for label, key in [('本周净变化', 'net_change'), ('上周净变化', 'previous_net_change'),
                           ('较上周净变化', 'change_vs_previous')]:
            value = weekly.get(key)
            cards.append(f'<div><dt>{label}</dt><dd data-direction="{_direction(value)}">'
                         f'{_count(value, signed=True)}</dd></div>')
        cards.append('</dl>'
                     f'<p class="rio-weekly-summary">{_escape(_weekly_summary(weekly))}</p>'
                     f'<p class="rio-weekly-coverage">周内有效日期 {_escape(weekly.get("coverage_days"))}/7</p>'
                     '<table class="rio-weekly-table"><thead><tr><th scope="col">日期</th>'
                     '<th scope="col">参与角色</th><th scope="col">较前日</th></tr></thead><tbody>')
        for day in weekly.get('days', []):
            cards.append(f'<tr><th scope="row"><time datetime="{_escape(day.get("date"))}">'
                         f'{_escape(_day_label(day.get("date")))}</time></th>'
                         f'<td>{_count(day.get("population"))}</td><td>{_count(day.get("change"), signed=True)}</td></tr>')
        cards.append('</tbody></table>')
        if (weekly.get('coverage_days') != 7 or any(weekly.get(key) is None for key in
                ('end_population', 'net_change', 'previous_net_change', 'change_vs_previous'))
                or any(day.get('change') is None for day in weekly.get('days', []))):
            cards.append('<p class="rio-weekly-missing">“—”表示对应日期或比较基线缺失；不补零，不以邻近日期替代。</p>')
        cards.append(f'<p class="rio-weekly-source">周初基线（{_escape(weekly.get("baseline_date"))}，上周二）'
                     f'{_count(weekly.get("start_population"))}；上周比较基线 {_escape(weekly.get("previous_baseline_date"))}。'
                     '统计有赛季分数的角色，非独立玩家；国服与全球不相加。</p>')
        cards.append(f'<p class="rio-weekly-source">数据日期 {_escape(item.get("data_date"))}'
                     + (' · 保留快照' if item.get('stale') else '') + '</p>')
        times = []
        if item.get('fetched_at'):
            times.append('采集：' + _fmt_source_time(item['fetched_at']))
        if item.get('upstream_at'):
            times.append('来源更新：' + _fmt_source_time(item['upstream_at']))
        if times:
            cards.append(f'<p class="rio-weekly-source">{_escape(" · ".join(times))}（北京时间）</p>')
        cards.append('<nav class="rio-weekly-links" aria-label="数据周报详情">'
                     + _link('参与趋势', item.get('url')) + _link('副本热力图', item.get('heatmap_url'))
                     + '</nav></article>')
    return ''.join(cards) + '</div>'
