"""Read-only daily references to existing public central content; no collectors."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.urls import reverse
from django.db.models import F

from botend.models import PortalEvent, WowTodaySnapshot
from botend.services.wow_today_service import apply_wow_today_section_settings, wow_today_sections_for_snapshot

REPORT_ZONE = ZoneInfo('Asia/Shanghai')

# Editorial selection for the daily report only; central cards stay unchanged.
EXCLUDED_DAILY_ROTATION_CARD_KEYS = frozenset({
    'season-caps', 'mn-notable-world-quests', 'empowered-abundance',
    'mn-heroic-world-tier', 'mn-ritual-site', 'mn-void-assault',
    'mn-cursed-surges', 'mn-aqir-invasion', 'epiceliteworldmn', 'darkmoon-faire',
})


def day_window(day):
    start = datetime.combine(day, time.min, tzinfo=REPORT_ZONE)
    return start, start + timedelta(days=1)


def stamp(value):
    return value.astimezone(REPORT_ZONE).strftime('%Y-%m-%d %H:%M') if value else ''


def collect_today_section(day):
    start, end = day_window(day)
    snapshot = (WowTodaySnapshot.objects.filter(region='na', game_version='retail',
        snapshot_date__lte=day, fetched_at__gte=start, fetched_at__lt=end)
        .defer('raw_json').order_by('-fetched_at', '-id').first())
    result = {'key': 'today', 'title': '每日轮换参考 · 北美', 'items': []}
    if snapshot is None:
        return result
    public = apply_wow_today_section_settings(wow_today_sections_for_snapshot(snapshot))
    result.update(notice=f'北美正式服；来源日期 {snapshot.snapshot_date.isoformat()}，北京时间 {stamp(snapshot.fetched_at)} 收录。不是国服同日轮换。',
                  source_time=stamp(snapshot.fetched_at), source_url=snapshot.source_url,
                  snapshot_id=snapshot.pk, source_date=snapshot.snapshot_date.isoformat())
    for section in public:
        for card in section.get('modules', []):
            if card.get('key') in EXCLUDED_DAILY_ROTATION_CARD_KEYS:
                continue
            facts = []
            for item in card.get('items', []):
                name = str(item.get('name') or '').strip()
                value = str(item.get('value') or '').strip()
                parts = [part for part in (name, str(item.get('icon_label') or '').strip(), value) if part]
                if item.get('quantity') is not None and 'quantity' in item:
                    parts.append(f'数量 {item["quantity"]}')
                if item.get('extra'):
                    parts.append(str(item['extra']))
                text = ' · '.join(parts)
                if text:
                    facts.append(text)
            if facts:
                result['items'].append({'title': card.get('name') or section.get('name'),
                    'url': card.get('url') or snapshot.source_url, 'facts': facts,
                    'source': section.get('name') or 'Today in WoW',
                    'reference': {'kind': 'wow_today_snapshot', 'id': snapshot.pk, 'card_key': card.get('key')}})
    return result


def _public_event_href(value):
    """Match the public calendar's domains; DB2 inspection links stay private."""
    from urllib.parse import urlsplit
    value = str(value or '').strip()
    if '\\' in value or any(ord(c) < 33 or ord(c) == 127 for c in value):
        return ''
    try:
        parts = urlsplit(value)
        _ = parts.port
        if value.startswith('/') and not parts.netloc and not value.startswith('//'):
            return value
        if parts.scheme in ('http', 'https') and (parts.hostname or '').removeprefix('www.') in ('wowhead.com', 'wowdaily.cn'):
            return value
    except ValueError:
        pass
    return ''


def collect_event_section(day):
    start, end = day_window(day)
    # Seed/demo rows and undated announcements cannot prove an active event.
    rows = PortalEvent.objects.filter(is_active=True, source='db2_holidays',
        start_at__lt=end, end_at__gt=start).filter(end_at__gt=F('start_at')).only(
            'id', 'title', 'url', 'start_at', 'end_at', 'source', 'summary').order_by('start_at', 'id')[:41]
    rows = list(rows)
    items = []
    for row in rows[:40]:
        state = ('当日开始' if row.start_at >= start else '当日结束' if row.end_at < end else '当日持续')
        items.append({'title': row.title, 'url': _public_event_href(row.url),
            'source': '站内 DB2 活动日历',
            'facts': [state, f'{stamp(row.start_at)} 至 {stamp(row.end_at)}（北京时间）'],
            'reference': {'kind': 'portal_event', 'id': row.pk}})
    notice = '按活动起止时间与本日北京时间的交集选取；不是今日新增公告。'
    if len(rows) > 40:
        notice += ' 展示前40项，完整安排请查看站内日历。'
    return {'key': 'events', 'title': '当日活动日历', 'items': items, 'notice': notice,
            'source_url': '/#section-events'}


def _public_report_exists(report):
    from pathlib import Path, PurePosixPath
    from django.conf import settings
    raw = str(report.content_html_path or '').lstrip('/')
    if raw.startswith('static/'):
        raw = raw[len('static/'):]
    if not raw.startswith('portal/reports/') or '\\' in raw:
        return False
    relative = PurePosixPath(raw[len('portal/reports/'):])
    if relative.suffix.lower() != '.html' or '..' in relative.parts:
        return False
    root = (Path(settings.BASE_DIR) / 'static/portal/reports').resolve()
    target = (root / str(relative)).resolve()
    return target.is_relative_to(root) and target.is_file()


def collect_update_section(day):
    from botend.models import WowWagoBuildEvent, WowWagoHotfixEvent
    from botend.controller.plugins.wow.wago_regions import wago_region_name
    start, end = day_window(day)
    items, seen = [], set()
    definitions = (
        (WowWagoHotfixEvent, 'wow_hotfix_report', 'portal_wow_hotfix_report',
         ('region_id', 'build_str', 'build_num', 'summary_title', 'entry_count', 'table_count', 'collection_complete')),
        (WowWagoBuildEvent, 'wow_skill_diff_report', 'portal_wow_skill_diff_report',
         ('display_from_build', 'from_build', 'display_to_build', 'to_build', 'spell_count', 'class_count')),
    )
    for model, kind, route, extra in definitions:
        query = model.objects.filter(detected_at__gte=start, detected_at__lt=end,
            status='report_generated', report__isnull=False).exclude(report__content_html_path='')
        if kind == 'wow_hotfix_report':
            query = query.filter(report__collection_complete=True)
        fields = ['id', 'branch', 'locale', 'report_id', 'detected_at', 'report__id',
                  'report__branch', 'report__locale', 'report__content_html_path']
        if kind == 'wow_hotfix_report':
            fields.append('region_id')
        fields.extend('report__' + field for field in extra)
        for event in query.select_related('report').only(*fields).order_by('-detected_at', '-id')[:41]:
            row = event.report
            key = (kind, row.pk)
            if (key in seen or event.branch != row.branch or event.locale != row.locale
                    or not _public_report_exists(row)):
                continue
            if kind == 'wow_hotfix_report' and event.region_id != row.region_id:
                continue
            seen.add(key)
            branch = {'wow': '正式服', 'wowt': 'PTR', 'wow_beta': 'Beta'}.get(row.branch, row.branch)
            if kind == 'wow_hotfix_report':
                title = row.summary_title or f'{branch} Hotfix'
                source = f'{branch} · {wago_region_name(row.region_id)} · Wago'
                facts = [f'Build {row.build_str or row.build_num}', f'{row.table_count} 张表 / {row.entry_count} 项来源记录']
            else:
                title = f'{branch} Build {row.display_from_build or row.from_build} → {row.display_to_build or row.to_build}'
                source = f'{branch} · Wago'
                facts = [f'{row.class_count} 个职业 / {row.spell_count} 项技能记录；具体变更见报告']
            items.append({'title': title, 'url': reverse(route, kwargs={'report_id': row.pk}),
                'source': source, 'time': stamp(event.detected_at), 'facts': facts,
                'reference': {'kind': kind, 'id': row.pk, 'event_id': event.pk}})
    items.sort(key=lambda item: (item['time'], item['reference']['kind'], item['reference']['id']), reverse=True)
    return {'key': 'updates', 'title': '当日发现的版本与热修报告', 'items': items[:40],
        'notice': '按本站发现时间选取已完成、可公开阅读的报告；不是游戏热修生效时间。PTR、地区与正式服分开标注。' +
                  (' 展示最新40条，更多请查看站内完整报告。' if len(items) > 40 else '')}


def collect_site_sections(day):
    return [section for section in (collect_today_section(day), collect_event_section(day),
            collect_update_section(day)) if section['items']]
