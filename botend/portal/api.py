import json
import re
from math import ceil
from types import SimpleNamespace

from django.core.paginator import EmptyPage, Paginator
from django.http import JsonResponse
from django.views import View
from django.core.cache import cache
from django.db.models import Count, Q
from django.db.models.functions import Substr
from django.utils import timezone
from datetime import timedelta

from botend.models import PortalEvent, PortalMplusSeasonCutoff, PortalNavigationGroup, PortalToolLink, PortalVideo, SeasonMeta, WowArticle, WowDailyReport, WowTodaySnapshot, WowSkillDiffReport, WowHotfixReport, WowWagoMonitorState
from botend.services.article_content_service import loads_blocks
from botend.services.wow_hotfix_entries import continuous_entries, public_hotfix_entry
from botend.services.wow_today_service import (
    apply_wow_today_section_settings,
    wow_today_sections_for_snapshot,
)
from botend.controller.plugins.wow.wago_regions import wago_region_name
from botend.services.rio_rankings_snapshot import _mplus_member_to_dict, _mplus_to_dict, _peak_row_to_dict
from botend.services.mplus_dps_rankings_service import get_current_mplus_dps_rankings_payload



def _fmt_dt(dt):
    if not dt:
        return ''
    try:
        return timezone.localtime(dt).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ''


def _active_rio_season():
    row = SeasonMeta.objects.filter(is_active=True).first()
    return (getattr(row, 'rio_season', '') or '').strip()


def _normalize_url(v):
    s = (v or '').strip()
    if not s:
        return ''
    if s in ('-', '#'):
        return ''
    if s.startswith('/static/portal/reports/'):
        return '/portal/reports/' + s[len('/static/portal/reports/'):]
    return s


def _portal_report_url(path):
    s = (path or '').strip().lstrip('/')
    if not s:
        return ''
    if s.startswith('static/'):
        s = s[len('static/'):]
    if s.startswith('portal/reports/'):
        return '/portal/reports/' + s[len('portal/reports/'):]
    if s.startswith('/portal/reports/'):
        return s
    return '/portal/reports/' + s


def _load_json_dict(raw):
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or '{}')
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _daily_report_to_dict(row):
    ext = _load_json_dict(getattr(row, 'ext_json', '') or '')
    sections_obj = ext.get('sections')
    sections = sections_obj if isinstance(sections_obj, dict) else {}
    section_items = []
    for key, label in [('news', '新闻'), ('nga', 'NGA'), ('videos', '视频'), ('cutoffs', '大秘境')]:
        info_obj = sections.get(key)
        info = info_obj if isinstance(info_obj, dict) else {}
        count = info.get('count')
        try:
            count = int(count or 0)
        except Exception:
            count = 0
        if key in sections or count:
            source_counts = info.get('source_counts')
            section_items.append({
                'key': key,
                'label': label,
                'title': info.get('title') or label,
                'count': count,
                'source_counts': source_counts if isinstance(source_counts, dict) else {},
            })
    report_date = getattr(row, 'report_date', None)
    return {
        'id': row.id,
        'report_date': report_date.isoformat() if report_date else '',
        'title': f"{report_date.strftime('%-m月%-d日') if report_date else '最新'}魔兽世界日报",
        'url': _portal_report_url(getattr(row, 'md_path', '') or ''),
        'md_path': getattr(row, 'md_path', '') or '',
        'updated_at': _fmt_dt(getattr(row, 'updated_at', None)),
        'generated_at': ext.get('generated_at') or _fmt_dt(getattr(row, 'updated_at', None)),
        'html_renderer': ext.get('html_renderer') or '',
        'sections': section_items,
    }


def _normalize_display_text(v):
    s = (v or '').strip()
    if not s:
        return ''
    if s == 'LMonitor':
        return ''
    return s


ARTICLE_SOURCE_LABELS = {
    'blizzard_cn': '魔兽世界中国',
    'blizzard_tracker': 'Blizzard Tracker',
    'bilibili': 'B 站视频',
    'exwind': 'Exwind 新闻',
    'lhfszs': '老黄蜂说芝士',
    'nga': 'NGA',
    'unknown': '其他来源',
    'wowhead': 'Wowhead',
}


def _article_source_label(source):
    key = (source or 'unknown').strip() or 'unknown'
    return ARTICLE_SOURCE_LABELS.get(key, key)


def _article_to_dict(a):
    return {
        'id': a.id,
        'title': a.title or '',
        'title_cn': a.title_cn or '',
        'url': _normalize_url(a.url),
        'article_url': f'/portal/article/{a.id}/',
        'author': _normalize_display_text(a.author),
        'source': a.source or '',
        'category': a.category or '',
        'publish_time': _fmt_dt(a.publish_time),
        'reply_count': int(getattr(a, 'reply_count', 0) or 0),
        'has_content': bool(a.content),
        'has_translation': bool(a.content_cn),
    }


def _nga_article_to_dict(a):
    from bs4 import BeautifulSoup
    content_preview = BeautifulSoup(a.nga_preview or '', 'html.parser').get_text(' ', strip=True)[:200]
    return {
        'id': a.id,
        'title': a.title or '',
        'title_cn': a.title_cn or '',
        'url': _normalize_url(a.url),
        'article_url': f'/portal/article/{a.id}/',
        'author': _normalize_display_text(a.author),
        'source': a.source or '',
        'category': a.category or '',
        'publish_time': _fmt_dt(a.publish_time),
        'reply_count': int(getattr(a, 'reply_count', 0) or 0),
        'content_preview': content_preview,
        'has_content': bool(a.nga_preview),
        'has_translation': bool(a.nga_translation_preview),
    }


def _event_to_dict(e):
    status = (e.status or '').strip()
    if not status:
        now = timezone.now()
        if e.end_at and now > e.end_at:
            status = '已结束'
        elif e.start_at and now < e.start_at:
            status = '即将开始'
        elif e.start_at:
            status = '进行中'
    return {
        'title': e.title or '',
        'url': _normalize_url(e.url),
        'source': e.source or '',
        'tag': e.tag or '',
        'status': status,
        'start_at': _fmt_dt(e.start_at),
        'end_at': _fmt_dt(e.end_at),
        'summary': getattr(e, 'summary', '') or '',
        'image_url': _normalize_url(getattr(e, 'image_url', '') or ''),
        'external_id': getattr(e, 'external_id', '') or '',
    }


VIDEO_PLACEHOLDER_TAGS = {'测试'}
VIDEO_EXCLUDED_TITLE_KEYWORDS = ['【直播回放】']


def _video_display_tag(tag):
    value = (tag or '').strip()
    return '' if value in VIDEO_PLACEHOLDER_TAGS else value


def _video_to_dict(v):
    return {
        'title': v.title or '',
        'url': _normalize_url(v.url),
        'bvid': v.bvid or '',
        'cover_url': _normalize_url(v.cover_url),
        'published_at': _fmt_dt(v.published_at),
        'author': _normalize_display_text(v.author_name),
        'author_url': _normalize_url(v.author_url),
        'source': (getattr(getattr(v, 'target', None), 'platform', '') or 'bilibili'),
        'tag': _video_display_tag(v.tag),
    }


def _tool_to_dict(t):
    return {
        'name': t.name or '',
        'url': _normalize_url(t.url),
        'desc': t.desc or '',
        'icon_path': _normalize_url(getattr(t, 'icon_path', '') or ''),
        'icon_key': getattr(t, 'icon_key', '') or '',
        'category': getattr(t, 'category', '') or 'tools',
        'badge': getattr(t, 'badge', '') or '',
        'badge_tone': getattr(t, 'badge_tone', '') or 'default',
        'sort_order': t.sort_order or 0,
        'is_topbar': bool(t.is_topbar),
        'topbar_order': t.topbar_order or 0,
        'show_in_guide': bool(getattr(t, 'show_in_guide', False)),
        'show_in_tools': bool(getattr(t, 'show_in_tools', True)),
        'open_in_new_tab': bool(getattr(t, 'open_in_new_tab', True)),
        'source': t.source or '',
    }


def _navigation_item_to_dict(item, group):
    return {
        'name': item.name or '',
        'url': _normalize_url(item.url),
        'desc': item.desc or '',
        'icon_key': item.icon_key or group.icon_key or 'globe',
        'category': group.key,
        'badge': item.badge or '',
        'badge_tone': item.badge_tone or 'default',
        'sort_order': item.sort_order or 0,
        'open_in_new_tab': False,
    }


PORTAL_LINK_CATEGORIES = (
    {'key': 'today', 'name': '官方与资讯', 'description': '官方站点与版本资讯', 'icon_key': 'newspaper'},
    {'key': 'data', 'name': '数据站点', 'description': '角色、日志与模拟数据', 'icon_key': 'chart'},
    {'key': 'mythic', 'name': '大秘境站点', 'description': '路线、排行与赛季数据', 'icon_key': 'refresh'},
    {'key': 'tools', 'name': '实用工具', 'description': '第三方计算与辅助工具', 'icon_key': 'tools'},
    {'key': 'community', 'name': '社区与内容', 'description': '论坛、视频与玩家内容', 'icon_key': 'chat'},
)


def _portal_link_categories(items):
    present = {str(item.get('category') or 'tools') for item in items}
    categories = [dict(item) for item in PORTAL_LINK_CATEGORIES if item['key'] in present]
    known = {item['key'] for item in categories}
    for key in sorted(present - known):
        categories.append({'key': key, 'name': key, 'description': '自定义入口分组', 'icon_key': 'globe'})
    return categories


def _skilldiff_to_dict(r):
    branch = (getattr(r, 'branch', '') or '').strip()
    to_build = (getattr(r, 'to_build', '') or '').strip()
    from_build = (getattr(r, 'from_build', '') or '').strip()
    md = (getattr(r, 'content_md', '') or '').strip()
    summary = ''
    if md:
        for line in md.splitlines():
            line = (line or '').strip()
            if not line:
                continue
            if line.startswith('#'):
                summary = line.lstrip('#').strip()
                break
    if summary and ('职业技能变更报告' in summary):
        summary = ''
    if not summary:
        cc = int(getattr(r, 'class_count', 0) or 0)
        sc = int(getattr(r, 'spell_count', 0) or 0)
        if cc and sc:
            summary = f"职业技能更新（{cc}职业{sc}项）"
        elif sc:
            summary = f"职业技能更新（{sc}项）"
        else:
            summary = "职业技能更新"
    label = f"{summary}（{branch} {from_build} → {to_build}）".strip()
    return {
        'id': r.id,
        'title': label,
        'url': f"/portal/wow-skill-diff/{r.id}/",
        'source': 'Wago',
        'time': _fmt_dt(getattr(r, 'created_at', None)),
        'branch': branch,
        'from_build': from_build,
        'to_build': to_build,
    }


def _state_to_dict(s):
    status = (getattr(s, 'last_event_status', '') or '').strip()
    status_map = {
        'init_has_class_change': '初始化：有职业更新',
        'init_no_class_change': '初始化：无职业更新',
        'build_changed_has_class_change': '更新：有职业更新',
        'build_changed_no_class_change': '更新：无职业更新',
        'hotfix_source_behind_cursor': '源站推送落后游标，待重试',
        'has_class_change': '有职业更新',
        'failed': '失败',
    }
    run_status = (getattr(s, 'last_run_status', '') or '').strip()
    run_map = {
        'success': '正常',
        'failed': '异常',
    }
    summary_title = ''
    ext_raw = (getattr(s, 'ext', '') or '').strip()
    if ext_raw:
        try:
            ext = json.loads(ext_raw)
        except Exception:
            ext = {}
        if isinstance(ext, dict):
            summary_title = (ext.get('summary_title') or '').strip()
    if not summary_title:
        report_url = (getattr(s, 'report_url', '') or '').strip()
        m = re.search(r'/portal/wow-skill-diff/(\d+)/', report_url)
        if m:
            rid = int(m.group(1))
            r = WowSkillDiffReport.objects.filter(id=rid).first()
            if r:
                md = (getattr(r, 'content_md', '') or '').strip()
                for line in md.splitlines():
                    line = (line or '').strip()
                    if not line:
                        continue
                    if line.startswith('#'):
                        summary_title = line.lstrip('#').strip()
                        break
        if summary_title and ('职业技能变更报告' in summary_title):
            summary_title = ''
    report_url = _normalize_url(getattr(s, 'report_url', ''))
    wago_url = (getattr(s, 'wago_diff_url', '') or '').strip()
    if wago_url in ('-', '#'):
        wago_url = ''

    hotfix_status = (getattr(s, 'hotfix_last_event_status', '') or '').strip()
    hotfix_run_status = (getattr(s, 'hotfix_last_run_status', '') or '').strip()
    hotfix_report_url = _normalize_url(getattr(s, 'hotfix_report_url', ''))
    hotfix_wago_url = (getattr(s, 'hotfix_wago_url', '') or '').strip()
    if hotfix_wago_url in ('-', '#'):
        hotfix_wago_url = ''
    hotfix_summary_title = (getattr(s, 'hotfix_summary_title', '') or '').strip()
    if not hotfix_summary_title and hotfix_report_url:
        m = re.search(r'/portal/wow-skill-diff/(\d+)/', hotfix_report_url)
        if m:
            rid = int(m.group(1))
            r = WowSkillDiffReport.objects.filter(id=rid).first()
            if r:
                md = (getattr(r, 'content_md', '') or '').strip()
                for line in md.splitlines():
                    line = (line or '').strip()
                    if not line:
                        continue
                    if line.startswith('#'):
                        hotfix_summary_title = line.lstrip('#').strip()
                        break
        if hotfix_summary_title and ('职业技能变更报告' in hotfix_summary_title):
            hotfix_summary_title = ''
    return {
        'branch': (getattr(s, 'branch', '') or '').strip(),
        'locale': (getattr(s, 'locale', '') or '').strip(),
        'is_active': bool(getattr(s, 'is_active', False)),
        'build': (getattr(s, 'build', '') or '').strip(),
        'last_run_at': _fmt_dt(getattr(s, 'last_run_at', None)),
        'last_run_status': run_map.get(run_status, run_status),
        'last_event_at': _fmt_dt(getattr(s, 'last_event_at', None)),
        'last_event_status': status_map.get(status, status),
        'report_url': report_url,
        'wago_diff_url': wago_url,
        'summary_title': summary_title,
        'ext': (getattr(s, 'ext', '') or '').strip(),
        'hotfix_push_id': int(getattr(s, 'hotfix_push_id', 0) or 0),
        'hotfix_last_run_at': _fmt_dt(getattr(s, 'hotfix_last_run_at', None)),
        'hotfix_last_run_status': run_map.get(hotfix_run_status, hotfix_run_status),
        'hotfix_last_event_at': _fmt_dt(getattr(s, 'hotfix_last_event_at', None)),
        'hotfix_last_event_status': status_map.get(hotfix_status, hotfix_status),
        'hotfix_report_url': hotfix_report_url,
        'hotfix_wago_url': hotfix_wago_url,
        'hotfix_spell_count': int(getattr(s, 'hotfix_spell_count', 0) or 0),
        'hotfix_class_count': int(getattr(s, 'hotfix_class_count', 0) or 0),
        'hotfix_summary_title': hotfix_summary_title,
    }


def _paginate_recent_reports(request, queryset, serializer, *, all_history=False):
    try:
        page = max(1, int(request.GET.get('page') or 1))
    except ValueError:
        page = 1
    try:
        page_size = int(request.GET.get('page_size') or request.GET.get('limit') or 20)
    except ValueError:
        page_size = 20
    page_size = max(1, min(100, page_size))

    if not all_history:
        since = timezone.now() - timedelta(days=60)
        queryset = queryset.filter(created_at__gte=since)
    paginator = Paginator(queryset.order_by('-created_at', '-id'), page_size)
    try:
        page_obj = paginator.page(page)
    except EmptyPage:
        page_obj = paginator.page(paginator.num_pages or 1)

    return JsonResponse({
        'status': 'success',
        'data': [serializer(row) for row in page_obj.object_list],
        'meta': {
            'page': page_obj.number,
            'page_size': page_size,
            'total': paginator.count,
            'total_pages': paginator.num_pages,
            'has_next': page_obj.has_next(),
            'has_previous': page_obj.has_previous(),
        },
    })


class PortalWowSkillDiffListAPIView(View):
    def get(self, request):
        try:
            page = max(1, int(request.GET.get('page') or 1))
        except ValueError:
            page = 1
        try:
            page_size = int(request.GET.get('page_size') or request.GET.get('limit') or 20)
        except ValueError:
            page_size = 20
        page_size = max(1, min(100, page_size))
        since = timezone.now() - timedelta(days=60)
        builds = WowSkillDiffReport.objects.filter(created_at__gte=since)
        hotfixes = (WowHotfixReport.objects.filter(
            created_at__gte=since, collection_complete=True, class_spell_count__gt=0,
        ).exclude(class_content_html_path=''))
        total = builds.count() + hotfixes.count()
        total_pages = max(1, ceil(total / page_size))
        page = min(page, total_pages)
        end = page * page_size
        build_rows = list(builds.annotate(md_head=Substr('content_md', 1, 300))
                          .order_by('-created_at', '-id').values(
                              'id', 'branch', 'from_build', 'to_build', 'class_count', 'spell_count',
                              'created_at', 'md_head',
                          )[:end])
        hotfix_rows = list(hotfixes.order_by('-created_at', '-id').values(
            'id', 'branch', 'region_id', 'from_push', 'to_push', 'build_str',
            'class_spell_count', 'class_class_count', 'class_unresolved_count', 'created_at',
        )[:end])
        ordered = sorted(
            [('build', row) for row in build_rows] + [('hotfix', row) for row in hotfix_rows],
            key=lambda item: (item[1]['created_at'], item[1]['id'], item[0]), reverse=True,
        )[(page - 1) * page_size:end]
        data = []
        for kind, row in ordered:
            if kind == 'build':
                payload = _skilldiff_to_dict(SimpleNamespace(**{**row, 'content_md': row['md_head']}))
                payload['type'] = 'build'
            else:
                region = wago_region_name(row['region_id']) or '区域未确认'
                unresolved = int(row['class_unresolved_count'] or 0)
                scope = (f"已解析 {row['class_spell_count']}+ 技能，{unresolved} 条失效来源归属未核实"
                         if unresolved else f"{row['class_spell_count']} 改动来源")
                payload = {
                    'id': row['id'], 'type': 'hotfix', 'source': 'Wago Hotfix',
                    'title': f"Hotfix 职业更新（{row['class_class_count']} 职业 / {scope}）"
                             f"（{row['branch']} {region} · {row['build_str']} · push {row['from_push']}→{row['to_push']}）",
                    'url': f"/portal/wow-hotfix-class/{row['id']}/",
                    'branch': row['branch'], 'time': _fmt_dt(row['created_at']),
                    'from_push': row['from_push'], 'to_push': row['to_push'],
                    'unresolved_count': unresolved,
                }
            data.append(payload)
        return JsonResponse({'status': 'success', 'data': data, 'meta': {
            'page': page, 'page_size': page_size, 'total': total, 'total_pages': total_pages,
            'has_next': page < total_pages, 'has_previous': page > 1,
        }})


class PortalWowSkillDiffStatesAPIView(View):
    def get(self, request):
        rows = list(WowWagoMonitorState.objects.filter(is_active=True).order_by('branch', 'locale', 'id'))
        return JsonResponse({'status': 'success', 'data': [_state_to_dict(x) for x in rows]})


def _hotfix_report_to_dict(r):
    branch = (getattr(r, 'branch', '') or '').strip()
    from_push = int(getattr(r, 'from_push', 0) or 0)
    to_push = int(getattr(r, 'to_push', 0) or 0)
    summary = (getattr(r, 'summary_title', '') or '').strip()
    if not summary:
        entry_count = int(getattr(r, 'entry_count', 0) or 0)
        table_count = int(getattr(r, 'table_count', 0) or 0)
        summary = f"Hotfix 全量更新：{table_count} 张表 / {entry_count} 项"
    push_label = f"push {from_push}→{to_push}"
    title = summary if push_label in summary else f"{summary}（{push_label}）"
    return {
        'id': r.id,
        'title': title,
        'url': f"/portal/wow-hotfix-report/{r.id}/",
        'source': 'Wago',
        'verified': bool(getattr(r, 'collection_complete', False)),
        'time': _fmt_dt(getattr(r, 'created_at', None)),
        'branch': branch,
        'build': (getattr(r, 'build_str', '') or getattr(r, 'build_num', '') or '').strip(),
        'from_push': from_push,
        'to_push': to_push,
        'table_count': int(getattr(r, 'table_count', 0) or 0),
        'entry_count': int(getattr(r, 'entry_count', 0) or 0),
    }


class PortalHotfixReportsAPIView(View):
    def get(self, request):
        all_history = request.GET.get('scope') == 'all'
        reports = WowHotfixReport.objects.all()
        if all_history:
            branch = (request.GET.get('branch') or '').strip()
            if branch and branch not in ('wow', 'wowt', 'wowxptr', 'wow_beta'):
                return JsonResponse({'error': '无效分支'}, status=400)
            if branch:
                reports = reports.filter(branch=branch)
            query = (request.GET.get('q') or '').strip()[:80]
            if query:
                condition = (
                    Q(summary_title__icontains=query)
                    | Q(build_str__icontains=query)
                    | Q(build_num__icontains=query)
                    | Q(changed_tables_json__icontains=query)
                )
                if query.isdecimal():
                    value = int(query)
                    condition |= Q(from_push=value) | Q(to_push=value)
                reports = reports.filter(condition)
        return _paginate_recent_reports(
            request,
            reports,
            _hotfix_report_to_dict,
            all_history=all_history,
        )


class PortalHotfixEntriesAPIView(View):
    """Continuous, bounded view over completed frozen Hotfix source records."""

    def get(self, request):
        branch = (request.GET.get('branch') or '').strip()
        if branch and branch not in ('wow', 'wowt', 'wowxptr', 'wow_beta'):
            return JsonResponse({'error': '无效分支'}, status=400)
        mode = (request.GET.get('mode') or 'all').strip()
        if mode not in ('all', 'values', 'changes', 'status'):
            return JsonResponse({'error': '无效筛选模式'}, status=400)
        order = (request.GET.get('sort') or 'latest').strip()
        if order not in ('latest', 'changes_first'):
            return JsonResponse({'error': '无效排序方式'}, status=400)
        try:
            page = max(1, int(request.GET.get('page') or 1))
            page_size = max(1, min(50, int(request.GET.get('page_size') or 25)))
        except (ValueError, TypeError):
            return JsonResponse({'error': '页码无效'}, status=400)
        reports = WowHotfixReport.objects.filter(collection_complete=True).exclude(source_facts_json='')
        if branch:
            reports = reports.filter(branch=branch)
        reports = list(reports.order_by('-to_push', '-id').values(
            'id', 'branch', 'locale', 'region_id', 'build_str', 'build_num',
            'entry_count', 'updated_at', 'content_html_path',
        ))
        try:
            rows = continuous_entries(reports)
        except ValueError as exc:
            return JsonResponse({'error': str(exc)}, status=503)
        available_tables = sorted({row['table'] for row in rows}, key=str.lower)
        available_builds = sorted({row['build'] for row in rows if row['build']}, reverse=True)
        table = (request.GET.get('table') or '').strip().lower()[:80]
        build = (request.GET.get('build') or '').strip()[:64]
        query = (request.GET.get('q') or '').strip().casefold()[:80]
        if table:
            rows = [row for row in rows if row['table'].lower() == table]
        if build:
            rows = [row for row in rows if row['build'] == build or row['build'].rsplit('.', 1)[-1] == build]
        if mode != 'all':
            allowed = {'values': ('change', 'new_value'), 'changes': ('change',),
                       'status': ('status', 'unresolved')}[mode]
            rows = [row for row in rows if row['kind'] in allowed]
        if query:
            def searchable(row):
                return ' '.join([
                    row['table'], str(row['record_id']), str(row['push']), row['build'],
                    row['title'], str(row['spell_id'] or ''), row['status_label'],
                    row['region_name'], row['locale'],
                    *(part for field in row['fields'] for part in (field['key'], field['label'], field['text'])),
                    *(part for key, value in row.get('_raw_search', {}).items() for part in (key, value)),
                ]).casefold()
            rows = [row for row in rows if query in searchable(row)]
        counts = {'change': 0, 'new_value': 0, 'status': 0, 'unresolved': 0}
        for row in rows:
            counts[row['kind']] += 1
        if order == 'changes_first':
            # Stable sort: within each kind, preserve the established push/time order.
            rows.sort(key=lambda row: row['kind'] == 'change', reverse=True)
        total = len(rows)
        total_pages = max(1, ceil(total / page_size))
        page = min(page, total_pages)
        start = (page - 1) * page_size
        return JsonResponse({'status': 'success',
                             'data': [public_hotfix_entry(row, query) for row in rows[start:start + page_size]],
                             'meta': {
            'page': page, 'page_size': page_size, 'total': total,
            'total_pages': total_pages, 'has_next': page < total_pages,
            'has_previous': page > 1, 'tables': available_tables,
            'builds': available_builds, 'sort': order, 'counts': counts,
        }})


class PortalDailyReportLatestAPIView(View):
    def get(self, request):
        row = WowDailyReport.objects.all().order_by('-report_date', '-updated_at', '-id').first()
        if not row:
            return JsonResponse({'status': 'success', 'data': None})
        return JsonResponse({'status': 'success', 'data': _daily_report_to_dict(row)})


class PortalWowTodayAPIView(View):
    """返回最近一次北美正式服当前版本中文快照。"""

    def get(self, request):
        row = (
            WowTodaySnapshot.objects.filter(region='na', game_version='retail')
            .order_by('-snapshot_date', '-fetched_at', '-id')
            .first()
        )
        if not row:
            return JsonResponse({'status': 'success', 'data': None})
        sections = apply_wow_today_section_settings(wow_today_sections_for_snapshot(row))
        return JsonResponse({
            'status': 'success',
            'data': {
                'snapshot_date': row.snapshot_date.isoformat(),
                'region': 'na',
                'region_name': '北美',
                'game_version': 'retail',
                'game_version_name': '正式服',
                'expansion_id': int(row.expansion_id or 0),
                'expansion_name': row.expansion_name or '当前版本',
                'source_url': row.source_url or 'https://www.wowhead.com/today-in-wow',
                'fetched_at': _fmt_dt(row.fetched_at),
                'translation_missing': int(row.translation_missing or 0),
                'sections': sections,
            },
        })


class PortalBluepostsAPIView(View):
    def get(self, request):
        since = timezone.now() - timedelta(days=7)
        rows = (
            WowArticle.objects.filter(category='bluepost', is_active=True, publish_time__gte=since)
            .order_by('-publish_time')[:60]
        )
        return JsonResponse({'status': 'success', 'data': [_article_to_dict(x) for x in rows]})


class PortalNgaHotAPIView(View):
    def get(self, request):
        qs = WowArticle.objects.filter(source='nga', category='hot', is_active=True, reply_count__gt=20)
        if not qs.exists():
            qs = WowArticle.objects.filter(source='nga', is_active=True, reply_count__gt=20)
        from django.db.models.functions import Substr
        rows = list(qs.defer('content', 'content_cn', 'content_blocks', 'content_blocks_cn', 'description')
                    .annotate(nga_preview=Substr('content', 1, 600), nga_translation_preview=Substr('content_cn', 1, 1))
                    .order_by('-publish_time', '-id')[:40])
        return JsonResponse({'status': 'success', 'data': [_nga_article_to_dict(x) for x in rows]})


class PortalExwindLatestAPIView(View):
    def get(self, request):
        source = (request.GET.get('source') or '').strip()
        since = timezone.now() - timedelta(days=7)
        if source == 'nga_preview':
            rows = (
                WowArticle.objects.filter(source='nga', is_active=True).filter(
                    Q(nga_board_id='310') | Q(nga_board_id='', category='nga', author='nga前瞻区'))
                .order_by('-publish_time', '-id')[:60]
            )
        else:
            rows = (
                WowArticle.objects.filter(source__in=['exwind', 'blizzard_cn'], is_active=True, publish_time__gte=since)
                .order_by('-publish_time')[:60]
            )
        return JsonResponse({'status': 'success', 'data': [_article_to_dict(x) for x in rows]})


class PortalWowheadLatestAPIView(View):
    def get(self, request):
        since = timezone.now() - timedelta(days=7)
        rows = (
            WowArticle.objects.filter(source='wowhead', category='news', is_active=True, publish_time__gte=since)
            .order_by('-publish_time')[:60]
        )
        return JsonResponse({'status': 'success', 'data': [_article_to_dict(x) for x in rows]})


class PortalNewsIndexAPIView(View):
    def get(self, request):
        q = (request.GET.get('q') or '').strip()
        source = (request.GET.get('source') or '').strip()
        exclude_source = (request.GET.get('exclude_source') or '').strip()
        try:
            page = max(1, int(request.GET.get('page') or 1))
        except ValueError:
            page = 1
        try:
            page_size = int(request.GET.get('page_size') or 30)
        except ValueError:
            page_size = 30
        page_size = max(10, min(60, page_size))

        base_qs = WowArticle.objects.filter(is_active=True).exclude(title__isnull=True).exclude(title='')
        source_rows = list(
            base_qs.values('source')
            .annotate(count=Count('id'))
            .order_by('source')
        )
        sources = [
            {
                'key': (row.get('source') or 'unknown'),
                'label': _article_source_label(row.get('source')),
                'count': int(row.get('count') or 0),
            }
            for row in source_rows
        ]

        qs = base_qs
        if source:
            qs = qs.filter(source=source)
        elif exclude_source:
            qs = qs.exclude(source=exclude_source)
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(title_cn__icontains=q)
                | Q(author__icontains=q)
                | Q(source__icontains=q)
                | Q(category__icontains=q)
            )

        paginator = Paginator(qs.order_by('-publish_time', '-id'), page_size)
        try:
            page_obj = paginator.page(page)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages or 1)

        return JsonResponse({
            'status': 'success',
            'data': [_article_to_dict(x) for x in page_obj.object_list],
            'sources': sources,
            'meta': {
                'q': q,
                'source': source,
                'page': page_obj.number,
                'page_size': page_size,
                'total': paginator.count,
                'total_pages': paginator.num_pages,
                'has_next': page_obj.has_next(),
                'has_previous': page_obj.has_previous(),
            },
        })


class PortalArticleDetailAPIView(View):
    def get(self, request, article_id):
        try:
            article = WowArticle.objects.get(id=article_id, is_active=True)
        except WowArticle.DoesNotExist:
            return JsonResponse({'status': 'error', 'message': '文章不存在'}, status=404)

        content_cn = None
        if article.content_cn:
            try:
                content_cn = json.loads(article.content_cn)
            except Exception:
                content_cn = None
        content_blocks = loads_blocks(article.content_blocks)
        content_blocks_cn = loads_blocks(article.content_blocks_cn)
        content = article.content or ''
        if article.source == 'nga':
            # Keep the legacy hover/generic-article contract plain text while the
            # dedicated NGA reader uses the preserved HTML fact directly.
            from bs4 import BeautifulSoup
            from botend.services.nga_browse_service import render_main_post
            content = BeautifulSoup(str(render_main_post(content)), 'html.parser').get_text('\n', strip=True)

        return JsonResponse({
            'status': 'success',
            'data': {
                'id': article.id,
                'title': article.title or '',
                'title_cn': article.title_cn or '',
                'url': _normalize_url(article.url),
                'author': _normalize_display_text(article.author),
                'source': article.source or '',
                'category': article.category or '',
                'publish_time': _fmt_dt(article.publish_time),
                'content': content,
                'content_cn': content_cn,
                'content_blocks': content_blocks,
                'content_blocks_cn': content_blocks_cn,
            }
        })


class PortalEventsAPIView(View):
    def get(self, request):
        now = timezone.now()
        soon = now + timedelta(days=45)
        window_start = now - timedelta(days=7)
        rows = list(
            PortalEvent.objects.filter(
                Q(end_at__isnull=True, start_at__gte=window_start) | Q(end_at__gte=window_start),
                is_active=True,
                start_at__lte=soon,
            )
            .order_by('start_at', 'end_at', 'id')[:200]
        )
        return JsonResponse({'status': 'success', 'data': [_event_to_dict(x) for x in rows]})


class PortalVideosAPIView(View):
    def get(self, request):
        tag = (request.GET.get('tag') or '').strip()
        since = timezone.now() - timedelta(days=3)
        qs = PortalVideo.objects.filter(is_active=True, published_at__gte=since)
        for keyword in VIDEO_EXCLUDED_TITLE_KEYWORDS:
            qs = qs.exclude(title__icontains=keyword)
        if tag:
            qs = qs.filter(tag=tag)
        rows = list(qs.order_by('-published_at', '-id')[:60])
        tags = sorted({
            display_tag
            for display_tag in (_video_display_tag(value) for value in qs.exclude(tag='').values_list('tag', flat=True))
            if display_tag
        })
        items = [_video_to_dict(x) for x in rows]
        return JsonResponse({'status': 'success', 'data': {'tags': tags, 'items': items}})


class PortalToolsAPIView(View):
    def get(self, request):
        items = list(
            PortalToolLink.objects.filter(
                Q(url__istartswith='http://') | Q(url__istartswith='https://'),
                is_active=True,
                show_in_tools=True,
            )
            .order_by('sort_order', 'id')
        )
        item_dicts = [_tool_to_dict(x) for x in items]
        return JsonResponse({
            'status': 'success',
            'data': {
                'items': item_dicts,
                'categories': _portal_link_categories(item_dicts),
            }
        })


class PortalNavigationAPIView(View):
    """只提供 Portal 站内页面与页内板块入口。"""

    def get(self, request):
        groups = list(
            PortalNavigationGroup.objects.prefetch_related('items')
            .order_by('sort_order', 'id')
        )
        categories = []
        items = []
        for group in groups:
            group_items = [
                _navigation_item_to_dict(item, group)
                for item in sorted(group.items.all(), key=lambda value: (value.sort_order, value.id))
                if item.is_active
            ]
            if not group_items:
                continue
            categories.append({
                'key': group.key,
                'name': group.name,
                'description': group.description,
                'icon_key': group.icon_key or 'globe',
            })
            items.extend(group_items)
        return JsonResponse({
            'status': 'success',
            'data': {'items': items, 'categories': categories},
        })


class PortalMplusAffixesAPIView(View):
    def get(self, request):
        return JsonResponse({'status': 'success', 'data': {}})


class PortalMplusCutoffAPIView(View):
    def get(self, request):
        season = (request.GET.get('season') or '').strip()
        auto_season = (not season) or season in {"season-mn-1", "auto"}
        if auto_season:
            season = _active_rio_season()

        region_map = {
            "us": "美服",
            "eu": "欧服",
            "cn": "国服",
        }
        regions = ["us", "eu", "cn"]
        rows = list(PortalMplusSeasonCutoff.objects.filter(season=season, region__in=regions).order_by('region', '-updated_at', '-id'))
        by_region = {}
        for r in rows:
            key = (getattr(r, 'region', '') or '').strip().lower()
            if key and key not in by_region:
                by_region[key] = r

        items = []
        updated_at = ""
        for r in regions:
            row = by_region.get(r)
            if not row:
                continue
            ut = _fmt_dt(getattr(row, 'updated_at', None))
            if ut and (not updated_at or ut > updated_at):
                updated_at = ut
            cutoff_0_1 = getattr(row, 'cutoff_0_1', None)
            cutoff_1 = getattr(row, 'cutoff_1', None)
            cutoff_0_1_prev = getattr(row, 'cutoff_0_1_prev', None)
            cutoff_1_prev = getattr(row, 'cutoff_1_prev', None)
            title = f"{region_map.get(r, r)} 0.1%：{(round(float(cutoff_0_1), 2) if cutoff_0_1 is not None else '--')} / 1%：{(round(float(cutoff_1), 2) if cutoff_1 is not None else '--')}"
            items.append({
                "region": r,
                "region_name": region_map.get(r, r),
                "season": (getattr(row, 'season', '') or '').strip(),
                "cutoff_0_1": cutoff_0_1,
                "cutoff_1": cutoff_1,
                "cutoff_0_1_prev": cutoff_0_1_prev,
                "cutoff_1_prev": cutoff_1_prev,
                "updated_at": ut,
                "source_updated_at": (getattr(row, 'source_updated_at', '') or '').strip(),
                "source": (getattr(row, 'source', '') or '').strip() or "raiderio",
                "source_url": f"https://raider.io/cn/mythic-plus/cutoffs/{season}/{r}",
                "title": title,
                "time": ut,
            })

        return JsonResponse({
            "status": "success",
            "data": {
                "season": season,
                "updated_at": updated_at,
                "items": items,
            },
        })


class PortalMplusRankingsAPIView(View):
    def get(self, request):
        from botend.services.rio_rankings_snapshot import read_rankings
        payload = read_rankings('mplus', season=request.GET.get('season', ''),
                                region=request.GET.get('region', 'world'), dungeon=request.GET.get('dungeon', ''),
                                catalog=request.GET.get('catalog') == '1')
        response = JsonResponse({'status': 'success', 'data': payload})
        response['Cache-Control'] = 'no-store' if payload['snapshot']['state'] == 'pending' else 'public, max-age=60'
        return response


class PortalPeakSpecRankingsAPIView(View):
    def get(self, request):
        from botend.services.rio_rankings_snapshot import read_rankings
        payload = read_rankings('peak', season=request.GET.get('season', ''),
                                region=request.GET.get('region', 'world'), role=request.GET.get('role', ''))
        response = JsonResponse({'status': 'success', 'data': payload})
        response['Cache-Control'] = 'no-store' if payload['snapshot']['state'] == 'pending' else 'public, max-age=60'
        return response


class PortalRaidRankingsAPIView(View):
    def get(self, request):
        return JsonResponse({'status': 'success', 'data': []})


class PortalCharacterAPIView(View):
    def get(self, request):
        return JsonResponse({'status': 'success', 'data': {}})


class PortalMplusDpsRankingsAPIView(View):
    def get(self, request):
        try:
            return JsonResponse(get_current_mplus_dps_rankings_payload())
        except RuntimeError as exc:
            return JsonResponse({'error': str(exc)}, status=503)


class PortalMythicstatsDpsAPIView(View):
    """公开接口只读统一发布文件；缺数据也不联网或写数据库。"""
    def get(self, request):
        from botend.services.mythicstats_snapshot import read_snapshot
        data = read_snapshot(season=request.GET.get('season', ''),
                             dungeon_id=request.GET.get('dungeon', 0),
                             period_id=request.GET.get('period'))
        response = JsonResponse({'status': 'success', 'data': data})
        response['Cache-Control'] = 'no-store' if data['snapshot']['state'] == 'pending' else 'public, max-age=60'
        return response
