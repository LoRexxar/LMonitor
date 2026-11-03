"""Deterministic daily-edition HTML, independent of collection and AI clients.

``body_renderers`` maps section keys to callables accepting the original item
list and returning already-sanitized HTML. That HTML is embedded verbatim: this
layer must never truncate, summarize, re-escape or collapse source bodies.
All other values are plain metadata and are escaped here. Without a renderer,
only a small public, plain-text item projection is shown (never a JSON dump).
The caller owns collection windows, source semantics and summary provenance.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


_SHORT_NAMES = {
    'news': '新闻', 'nga': '社区', 'videos': '视频', 'cutoffs': '冲分',
    'today': '北美轮换', 'events': '活动日历', 'updates': '版本热修',
}
_DEFAULT_TITLES = {
    'news': '魔兽世界当天新闻', 'nga': 'NGA 热议',
    'videos': '当前更新的 WoW 视频列表', 'cutoffs': '大秘境分数线汇总',
    'today': '北美轮换', 'events': '活动日历', 'updates': '版本热修',
}
_BEIJING = ZoneInfo('Asia/Shanghai')


def _text(value: Any) -> str:
    """Do not stringify internal collections into public metadata."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dt.date, dt.datetime, int, float)):
        return str(value)
    return ''


def _escape(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _time_text(value: Any) -> str:
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_BEIJING)
        return value.strftime('%Y-%m-%d %H:%M:%S')
    return _text(value)


def _source_href(value: Any) -> str:
    """Allow HTTP(S) and same-site relative paths, not scheme-relative URLs."""
    if not isinstance(value, str) or not value:
        return ''
    # Reject browser-normalized backslashes/control characters before parsing.
    candidate = html.unescape(value)
    if candidate != candidate.strip() or '\\' in candidate or any(ord(c) < 33 or ord(c) == 127 for c in candidate):
        return ''
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme:
            if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname:
                return ''
            _ = parsed.port  # Invalid ports/IPv6 must not escape as exceptions.
        elif parsed.netloc or candidate.startswith('//'):
            return ''
        return value
    except ValueError:
        return ''


def _source_metadata(record: Mapping[str, Any]) -> str:
    out = []
    notice = _text(record.get('notice'))
    if notice:
        out.append(f'<p class="section-notice">{_escape(notice)}</p>')
    meta = []
    region = _text(record.get('region_label'))
    if region:
        meta.append(f'<span class="source-region">地区：{_escape(region)}</span>')
    source_time = _time_text(record.get('source_time'))
    if source_time and source_time not in notice:
        meta.append(f'<span class="source-time">来源时间：{_escape(source_time)}</span>')
    href = _source_href(record.get('source_url'))
    if href:
        meta.append(f'<a class="source-link" href="{_escape(href)}" rel="noopener noreferrer">查看来源</a>')
    if meta:
        out.append('<div class="source-meta">' + ''.join(meta) + '</div>')
    return '\n'.join(out)


def _generic_items(items: Sequence[Any]) -> str:
    """Public text-only fallback for a new source without its own renderer.

    Structured bodies and arbitrary payload fields are deliberately ignored.
    Register a trusted renderer to preserve rich HTML for a new source.
    """
    if not items:
        return '<p class="empty">本期暂无收录记录。</p>'
    cards = []
    for item in items:
        record = item if isinstance(item, Mapping) else {}
        title = _text(record.get('title')) or '站内条目'
        description = _text(record.get('description')) or _text(record.get('text'))
        # A serialized internal payload is no more suitable than a dict/list.
        if description.lstrip().startswith(('{', '[')):
            description = ''
        cards.append(
            '<article class="daily-card source-card">'
            f'<h3>{_escape(title)}</h3>'
            + _source_metadata(record)
            + (f'<p class="body-text">{_escape(description)}</p>' if description else '')
            + '</article>'
        )
    return '<div class="article-list">' + '\n'.join(cards) + '</div>'


_CSS = """
:root{color-scheme:light;--paper:var(--portal-bg,#f7f8fa);--panel:var(--portal-panel,#fff);--ink:var(--portal-text,#172e47);--muted:var(--portal-muted,#607185);--gold:#92651c;--tint:#fbf7ec;--line:#dde3e9;--link:#205a88;--blue-tint:#f0f5fa}
*,:before,:after{box-sizing:border-box}
html{scroll-padding-top:20px}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.8 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;overflow-wrap:anywhere}
a{color:var(--link);text-underline-offset:3px;text-decoration-thickness:1px}
a:hover{text-decoration:underline}
a:focus-visible{outline:2px solid var(--gold);outline-offset:4px}
.daily-report{width:100%;max-width:1160px;margin:0 auto;padding:32px 28px 40px}
.daily-report *{min-width:0}
.skip-link{position:absolute;left:12px;top:0;transform:translateY(-150%);padding:8px 14px;background:var(--panel);z-index:2}
.skip-link:focus{transform:translateY(0)}
.daily-hero{position:relative;display:flex;align-items:flex-end;justify-content:space-between;gap:16px;padding:17px 0 20px;border-top:2px solid var(--ink);border-bottom:1px solid var(--line)}
.daily-hero:before{content:"";position:absolute;top:-2px;left:0;width:56px;height:2px;background:var(--gold)}
.edition-kicker{margin:0 0 5px;color:var(--gold);font-size:11px;font-weight:700;letter-spacing:.14em}
.daily-hero h1{margin:0;font-size:34px;font-weight:750;letter-spacing:.025em;line-height:1.3}
.edition-date{margin:0 0 2px;text-align:right;font-size:20px;font-weight:650;line-height:1.5;font-variant-numeric:tabular-nums;white-space:nowrap}
.edition-date-label{display:block;text-align:right;color:var(--muted);font-size:11px;font-weight:400;letter-spacing:.12em}
.edition-overview{display:flex;align-items:center;gap:24px;margin:0 0 28px;padding:17px 0;border-bottom:1px solid var(--line)}
.edition-overview h2{flex:none;margin:0;color:var(--muted);font-size:11px;font-weight:500;letter-spacing:.08em}
.daily-nav{flex:1}
.overview-list{display:flex;flex-wrap:wrap;gap:8px 30px;margin:0;padding:0;list-style:none}
.overview-list li{display:flex;align-items:baseline;gap:7px;font-size:14px}
.daily-nav a{white-space:nowrap;text-decoration:none;font-weight:600}
.daily-nav a:hover{text-decoration:underline}
.overview-count{font-size:18px;font-weight:650;font-variant-numeric:tabular-nums;color:var(--ink)}
.overview-unit{color:var(--muted);font-size:11px;margin-left:-3px}
.daily-section{margin:0 0 32px;scroll-margin-top:20px}
.section-heading{display:flex;align-items:baseline;flex-wrap:wrap;gap:6px 12px;border-bottom:1px solid var(--line);padding:0 0 10px;margin-bottom:14px}
.section-heading h2{position:relative;margin:0;padding-left:13px;font-size:22px;font-weight:700;line-height:1.5;scroll-margin-top:20px;color:var(--ink)}
.section-heading h2:before{content:"";position:absolute;left:0;top:.3em;width:3px;height:.9em;border-radius:2px;background:var(--gold)}
.section-count{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
.section-summary{margin:0 0 16px;border-left:2px solid var(--gold);padding:10px 14px;background:var(--tint);white-space:pre-wrap;font-size:14px}
.section-notice{margin:0 0 12px;padding:7px 11px;border-left:2px solid #d6bd86;background:var(--tint);color:var(--muted);font-size:12px;line-height:1.75;white-space:pre-wrap}
.source-meta,.meta{display:flex;flex-wrap:wrap;gap:3px 14px;margin:7px 0 12px;color:var(--muted);font-size:12px;line-height:1.7}
.source-link{font-size:12px}
.article-list{display:grid;grid-template-columns:minmax(0,1fr);gap:20px}
.daily-card{max-width:100%;padding:0 0 20px;border-bottom:1px solid var(--line)}
.daily-card:last-child{padding-bottom:0;border-bottom:0}
.daily-card h3{margin:0 0 8px;font-size:21px;line-height:1.6}
.daily-card h3 a{text-decoration:none;color:var(--ink)}
.daily-card h3 a:hover{color:var(--link);text-decoration:underline}
.daily-news .daily-card,.daily-nga .daily-card{padding:26px 30px;background:var(--panel);border:1px solid var(--line);border-radius:8px}
.daily-news .article-list,.daily-nga .article-list{gap:24px}
.daily-news .section-body,.daily-nga .section-body{max-width:960px;margin:0 auto}
.original-link{display:inline-block;margin:0 14px 10px 0;font-size:12px}
.item-summary{margin:10px 0 16px;padding:10px 14px;background:var(--tint);border-left:2px solid var(--gold);font-size:14px}
.original-label{margin:18px 0 7px;font-size:11px;color:var(--muted);letter-spacing:.08em}
.body-html,.body-text{max-width:100%;margin:8px 0 0;line-height:1.9}
.body-text{white-space:pre-wrap}
.nga-plain-text{white-space:pre-wrap}
.body-html p{margin:12px 0}
.body-html :is(h1,h2,h3,h4,h5,h6){margin:24px 0 10px;line-height:1.6}
.body-html :is(ul,ol){padding-left:1.5em;margin:12px 0}
.body-html li+li{margin-top:4px}
.body-html blockquote{margin:16px 0;padding:9px 16px;border-left:3px solid var(--gold);background:var(--tint)}
.body-html figure{margin:16px 0}
img,video,iframe,svg,picture,canvas{max-width:100%!important}
img,video{height:auto}
.body-html img{display:block;margin:12px 0}
.body-html :is(div,p,figure,blockquote,ul,ol){max-width:100%}
pre{display:block;max-width:100%;overflow-x:auto;padding:12px;background:var(--paper);white-space:pre;overflow-wrap:normal}
code{font-size:.9em;overflow-wrap:anywhere}
pre code{overflow-wrap:normal}
table{display:block;width:100%!important;max-width:100%;min-width:0!important;overflow-x:auto;border-collapse:collapse;margin:16px 0;font-size:14px;overscroll-behavior-x:contain;background:var(--panel)}
caption{text-align:left;color:var(--muted)}
.daily-report :is(th,td){text-align:left;padding:10px 13px;border:1px solid var(--line);vertical-align:top;min-width:5.5em}
th{background:var(--blue-tint);font-weight:650}
.delta{font-weight:700;color:var(--gold)}
.video-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px}
.video-card img{width:100%;aspect-ratio:16/9;object-fit:cover}
.daily-events .site-context-list{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.daily-today .site-context-list{grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.daily-events .site-context-card,.daily-today .site-context-card{padding:16px 18px;border:1px solid var(--line);border-radius:7px;background:var(--panel)}
.daily-today .site-context-card{background:var(--blue-tint);border-color:#dfe7ef}
.site-context-card h3{margin:0;font-size:17px;line-height:1.5}
.site-context-card .meta{margin:5px 0 9px;font-size:11px;line-height:1.6}
.site-context-card ul{list-style:none;margin:8px 0 0;padding:0;font-size:14px;line-height:1.75}
.site-context-card li{padding:5px 0;border-bottom:1px solid #e0e7ed}
.site-context-card li:last-child{border-bottom:0;padding-bottom:0}
.context-heading{display:flex;align-items:center;gap:10px;margin:0 0 9px}
.context-icon{flex:none;display:block;width:34px;height:34px;object-fit:cover;border-radius:5px;box-shadow:0 0 0 1px #172e4714}
.context-heading h3{flex:1}
.context-status{flex:none;display:inline-block;padding:1px 8px;border-radius:4px;background:#e9f2ed;color:#39634e;font-size:11px;font-weight:600}
.context-facts li{display:flex;align-items:flex-start;gap:8px}
.context-facts li>span{flex:1}
.context-fact-icon{flex:none;display:block;width:20px;height:20px;margin-top:2px;object-fit:cover;border-radius:3px}
.daily-events .context-facts{font-size:13px}
.daily-events .context-facts li{padding:3px 0;border-bottom:0}
.daily-events .context-time{color:var(--muted);font-size:12px}
.daily-cutoffs .section-body>table{margin:0}
.empty{color:var(--muted);margin:0}
.edition-empty{padding:40px 20px;margin:28px 0;text-align:center;background:var(--panel);border:1px dashed var(--line);border-radius:8px}
.edition-footer{border-top:1px solid var(--line);padding-top:14px;color:var(--muted);font-size:11px;line-height:1.8}
.edition-footer p{margin:0 0 3px}
.edition-meta{color:var(--muted);font-size:11px}
@media(min-width:900px){.daily-cutoffs .section-body>table{display:table}}
@media(max-width:760px){.daily-report{padding:20px 20px 32px}.daily-today .site-context-list{grid-template-columns:minmax(0,1fr)}.overview-list{gap:6px 22px}.edition-overview{gap:16px}.edition-overview h2{position:absolute;width:1px;height:1px;padding:0;margin:-1px;clip-path:inset(50%);white-space:nowrap}.daily-news .daily-card,.daily-nga .daily-card{padding:20px}}
@media(max-width:600px){.daily-report{padding:16px 16px 30px}.daily-hero{gap:10px;padding:13px 0 15px}.daily-hero h1{font-size:27px;letter-spacing:0}.edition-kicker{font-size:9px;letter-spacing:.1em;margin-bottom:4px}.edition-date{font-size:14px;margin-bottom:1px}.edition-date-label{font-size:10px;letter-spacing:.04em}.edition-overview{padding:12px 0;margin-bottom:22px}.overview-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px 12px}.overview-list li{gap:6px;font-size:13px}.overview-count{font-size:16px}.overview-unit{font-size:10px}.daily-section{margin-bottom:26px}.section-heading{padding-bottom:9px;margin-bottom:11px;gap:5px 10px}.section-heading h2{font-size:20px;padding-left:11px}.section-count{font-size:11px}.daily-events .site-context-list{grid-template-columns:minmax(0,1fr);gap:10px}.daily-events .site-context-card,.daily-today .site-context-card{padding:13px 15px}.daily-card h3{font-size:19px}.site-context-card h3{font-size:17px}.daily-news .daily-card,.daily-nga .daily-card{padding:18px 16px}.video-grid{grid-template-columns:minmax(0,1fr)}.daily-report :is(th,td){padding:9px 11px}.body-html blockquote{padding:9px 12px}.section-notice{padding:7px 10px}}
@media print{body{background:#fff}.daily-report{max-width:none;padding:0}.edition-overview,.skip-link{display:none}.daily-section{padding:16px 0}.daily-news .daily-card,.daily-nga .daily-card{border:0;padding:12px 0}.daily-card{break-inside:auto}}
"""


def render_daily_document(
    report_date: dt.date | str,
    sections: Sequence[Mapping[str, Any]],
    body_renderers: Mapping[str, Callable[[Sequence[Any]], str]],
    *,
    generated_at: dt.datetime | str,
) -> str:
    """Return a complete, self-contained Chinese daily edition.

    Section order and original item lists are preserved. Counts are always
    computed from those lists, never from supplied count/summary fields.
    ``generated_at`` is explicit to make repeated renders deterministic; aware
    datetimes are displayed in Beijing time, strings remain caller-labelled.
    Core h2 anchors are ``section-news`` / ``section-nga`` / etc.; the enclosing
    section retains the original key in ``data-section``. Collisions receive a
    stable occurrence suffix. No database, network or model calls occur here.
    """
    date_text = report_date.isoformat() if isinstance(report_date, dt.date) else _text(report_date)
    output, overview = [], []
    seen: dict[str, int] = {}
    used_anchors: set[str] = set()
    for section in sections:
        key = _text(section.get('key')) or 'other'
        items = section.get('items')
        if not isinstance(items, (list, tuple)):
            items = []
        if not items:
            continue
        slug = key if re.fullmatch(r'[a-z][a-z0-9_-]*', key) else 'source-' + hashlib.sha256(key.encode()).hexdigest()[:12]
        base_anchor = f'section-{slug}'
        seen[base_anchor] = seen.get(base_anchor, 0) + 1
        anchor = base_anchor if seen[base_anchor] == 1 else f'{base_anchor}-{seen[base_anchor]}'
        while anchor in used_anchors:
            seen[base_anchor] += 1
            anchor = f'{base_anchor}-{seen[base_anchor]}'
        used_anchors.add(anchor)
        short_name = _SHORT_NAMES.get(key, '站内内容')
        title = _text(section.get('title')) or _DEFAULT_TITLES.get(key, '站内新内容')
        count = len(items)
        unit = {'news': '条新闻', 'nga': '个话题', 'videos': '个视频', 'cutoffs': '个地区',
                'today': '张卡片', 'events': '项活动', 'updates': '份报告'}.get(key, '项')
        overview.append(
            f'<li data-section="{_escape(key)}"><a href="#{anchor}">{short_name}</a>'
            f'<strong class="overview-count">{count}</strong><span class="overview-unit">{unit}</span></li>'
        )
        summary = _text(section.get('summary')) if key != 'cutoffs' else ''
        summary_html = f'<p class="section-summary">{_escape(summary)}</p>' if summary else ''
        renderer = body_renderers.get(key)
        body = renderer(items) if renderer is not None else _generic_items(items)
        output.append(
            f'<section class="daily-section daily-{slug}" data-section="{_escape(key)}" aria-labelledby="{anchor}">\n'
            f'<div class="section-heading"><h2 id="{anchor}">{_escape(title)}</h2>'
            f'<span class="section-count">{count} {unit}</span></div>\n'
            + _source_metadata(section) + '\n' + summary_html + '\n'
            + '<div class="section-body">\n' + body + '\n</div>\n</section>'
        )
    overview_html = (
        '<aside class="edition-overview" aria-labelledby="overview-title">'
        '<h2 id="overview-title">本期导读</h2><nav class="daily-nav" aria-label="本期目录">'
        '<ul class="overview-list">' + ''.join(overview) + '</ul></nav></aside>'
    ) if overview else ''
    content_html = '\n'.join(output) if output else '<p class="empty edition-empty">本期暂无收录内容，稍后再来看看。</p>'
    return (
        '<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>魔兽世界日报 - {_escape(date_text)}</title>\n<style>{_CSS}</style>\n</head>\n<body>\n'
        '<a class="skip-link" href="#edition-content">跳到本期正文</a>\n'
        '<main class="daily-report">\n<header class="daily-hero">'
        '<div class="edition-title"><p class="edition-kicker">WOW DAILY · 每日读本</p><h1>魔兽世界日报</h1></div>'
        f'<div class="edition-dateline"><span class="edition-date-label">本期日期</span><p class="edition-date">{_escape(date_text)}</p></div>'
        '</header>\n' + overview_html + '\n'
        + '<div id="edition-content">\n' + content_html + '\n</div>\n'
        + '<footer class="edition-footer">'
        f'<p class="edition-meta">北京时间（CST / UTC+8） · 生成于 {_escape(_time_text(generated_at))}</p>'
        '<p>来源时间与适用地区以各模块标注为准；导读数量为本期收录记录数。</p></footer>\n'
        + '</main>\n</body>\n</html>\n'
    )
