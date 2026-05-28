import json
import re

from django.http import JsonResponse
from django.views import View
from django.core.cache import cache
from django.utils import timezone
from datetime import timedelta

from botend.models import PortalEvent, PortalMplusRun, PortalMplusSeasonCutoff, PortalMythicstatsDpsRow, PortalPeakSpecRankRow, PortalToolLink, PortalVideo, WowArticle, WowSkillDiffReport, WowWagoMonitorState
from botend.controller.plugins.wow.wago_regions import wago_region_name
from botend.portal.mythicstats import (
    fetch_current_season_slug,
    fetch_mythicstats_dps,
    get_mythicstats_source_cache,
    upsert_mythicstats_dps_rows,
    upsert_mythicstats_meta_cache,
    upsert_mythicstats_source_cache,
)


def _fmt_dt(dt):
    if not dt:
        return ''
    try:
        return timezone.localtime(dt).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ''


def _normalize_url(v):
    s = (v or '').strip()
    if not s:
        return ''
    if s in ('-', '#'):
        return ''
    return s


def _normalize_display_text(v):
    s = (v or '').strip()
    if not s:
        return ''
    if s == 'LMonitor':
        return ''
    return s


def _article_to_dict(a):
    return {
        'title': a.title or '',
        'url': _normalize_url(a.url),
        'author': _normalize_display_text(a.author),
        'source': a.source or '',
        'category': a.category or '',
        'publish_time': _fmt_dt(a.publish_time),
        'reply_count': int(getattr(a, 'reply_count', 0) or 0),
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
    }


def _video_to_dict(v):
    return {
        'title': v.title or '',
        'url': _normalize_url(v.url),
        'bvid': v.bvid or '',
        'cover_url': _normalize_url(v.cover_url),
        'published_at': _fmt_dt(v.published_at),
        'author': _normalize_display_text(v.author_name),
        'author_url': _normalize_url(v.author_url),
        'tag': v.tag or '',
    }


def _tool_to_dict(t):
    return {
        'name': t.name or '',
        'url': _normalize_url(t.url),
        'desc': t.desc or '',
        'icon_path': _normalize_url(getattr(t, 'icon_path', '') or ''),
        'sort_order': t.sort_order or 0,
        'is_topbar': bool(t.is_topbar),
        'topbar_order': t.topbar_order or 0,
        'source': t.source or '',
    }


def _mplus_to_dict(r):
    dungeon_map = {
        "algethar-academy": "艾杰斯亚学院",
        "magisters-terrace": "魔导师平台",
        "maisara-caverns": "迈萨拉洞窟",
        "nexuspoint-xenas": "节点希纳斯",
        "pit-of-saron": "萨隆矿坑",
        "seat-of-the-triumvirate": "执政团之座",
        "skyreach": "通天峰",
        "windrunner-spire": "风行者之塔",
    }
    party = []
    if getattr(r, 'party_json', None):
        try:
            import json
            party = json.loads(r.party_json) or []
        except Exception:
            party = []
    dps = []
    if r.dps_json:
        try:
            import json
            dps = json.loads(r.dps_json) or []
        except Exception:
            dps = []
    return {
        'rank': r.rank,
        'dungeon': r.dungeon,
        'dungeon_slug': getattr(r, 'dungeon_slug', '') or '',
        'dungeon_cn': dungeon_map.get(getattr(r, 'dungeon_slug', '') or '', r.dungeon),
        'level': r.level,
        'time_seconds': r.time_seconds,
        'score': r.score,
        'tank': r.tank or '',
        'healer': r.healer or '',
        'party': party,
        'dps': dps,
        'run_url': _normalize_url(getattr(r, 'run_url', '') or ''),
        'source': r.source or '',
        'season': r.season or '',
        'region': r.region or '',
    }


def _peak_row_to_dict(r):
    profile = (getattr(r, "character_path", "") or "").strip()
    profile_url = ""
    if profile:
        profile_url = "https://raider.io" + (profile if profile.startswith("/") else f"/{profile}")
    return {
        "rank": int(getattr(r, "rank", 0) or 0),
        "name": (getattr(r, "character_name", "") or "").strip(),
        "score": getattr(r, "score", None),
        "score_color": (getattr(r, "score_color", "") or "").strip(),
        "profile_url": profile_url,
        "realm_name": (getattr(r, "realm_name", "") or "").strip(),
        "rio_region_slug": (getattr(r, "rio_region_slug", "") or "").strip(),
    }


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
    report_url = (getattr(s, 'report_url', '') or '').strip()
    if report_url in ('-', '#'):
        report_url = ''
    wago_url = (getattr(s, 'wago_diff_url', '') or '').strip()
    if wago_url in ('-', '#'):
        wago_url = ''

    hotfix_status = (getattr(s, 'hotfix_last_event_status', '') or '').strip()
    hotfix_run_status = (getattr(s, 'hotfix_last_run_status', '') or '').strip()
    hotfix_region = (getattr(s, 'hotfix_region', '') or '').strip()
    hotfix_report_url = (getattr(s, 'hotfix_report_url', '') or '').strip()
    if hotfix_report_url in ('-', '#'):
        hotfix_report_url = ''
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
        'hotfix_build': (getattr(s, 'hotfix_build', '') or '').strip(),
        'hotfix_push_id': int(getattr(s, 'hotfix_push_id', 0) or 0),
        'hotfix_region_id': int(getattr(s, 'hotfix_region_id', 0) or 0),
        'hotfix_region': hotfix_region,
        'hotfix_region_name': hotfix_region or wago_region_name(getattr(s, 'hotfix_region_id', 0) or 0),
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


class PortalWowSkillDiffListAPIView(View):
    def get(self, request):
        limit = request.GET.get('limit', '20')
        try:
            limit = max(1, min(100, int(limit)))
        except ValueError:
            limit = 20
        rows = list(WowSkillDiffReport.objects.all().order_by('-created_at')[:limit])
        return JsonResponse({'status': 'success', 'data': [_skilldiff_to_dict(x) for x in rows]})


class PortalWowSkillDiffStatesAPIView(View):
    def get(self, request):
        rows = list(WowWagoMonitorState.objects.filter(is_active=True).order_by('branch', 'locale', 'id'))
        return JsonResponse({'status': 'success', 'data': [_state_to_dict(x) for x in rows]})


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
        rows = list(qs.order_by('-publish_time', '-id')[:40])
        return JsonResponse({'status': 'success', 'data': [_article_to_dict(x) for x in rows]})


class PortalExwindLatestAPIView(View):
    def get(self, request):
        since = timezone.now() - timedelta(days=7)
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



class PortalEventsAPIView(View):
    def get(self, request):
        rows = list(PortalEvent.objects.filter(is_active=True).order_by('-start_at', '-id')[:30])


class PortalVideosAPIView(View):
    def get(self, request):
        tag = (request.GET.get('tag') or '').strip()
        since = timezone.now() - timedelta(days=2)
        qs = PortalVideo.objects.filter(is_active=True, published_at__gte=since)
        if tag:
            qs = qs.filter(tag=tag)
        rows = list(qs.order_by('-published_at', '-id')[:60])
        tags = list(qs.exclude(tag='').values_list('tag', flat=True).distinct())
        items = [_video_to_dict(x) for x in rows]
        return JsonResponse({'status': 'success', 'data': {'tags': tags, 'items': items}})


class PortalToolsAPIView(View):
    def get(self, request):
        topbar = list(
            PortalToolLink.objects.filter(is_active=True, is_topbar=True)
            .order_by('topbar_order', 'sort_order', 'id')
        )
        items = list(
            PortalToolLink.objects.filter(is_active=True)
            .order_by('sort_order', 'id')
        )
        return JsonResponse({
            'status': 'success',
            'data': {
                'topbar': [_tool_to_dict(x) for x in topbar],
                'items': [_tool_to_dict(x) for x in items],
            }
        })


class PortalMplusAffixesAPIView(View):
    def get(self, request):
        return JsonResponse({'status': 'success', 'data': {}})


class PortalMplusCutoffAPIView(View):
    def get(self, request):
        season = (request.GET.get('season') or '').strip()
        auto_season = (not season) or season in {"season-mn-1", "auto"}
        if auto_season:
            last = PortalMplusSeasonCutoff.objects.all().order_by('-updated_at', '-id').first()
            season = (getattr(last, 'season', '') or '').strip() or 'season-mn-1'

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
            title = f"{region_map.get(r, r)} 0.1%：{(round(float(cutoff_0_1), 2) if cutoff_0_1 is not None else '--')} / 1%：{(round(float(cutoff_1), 2) if cutoff_1 is not None else '--')}"
            items.append({
                "region": r,
                "region_name": region_map.get(r, r),
                "season": (getattr(row, 'season', '') or '').strip(),
                "cutoff_0_1": cutoff_0_1,
                "cutoff_1": cutoff_1,
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
        season = (request.GET.get('season') or 'season-mn-1').strip()
        region = (request.GET.get('region') or 'world').strip()
        dungeon = (request.GET.get('dungeon') or '').strip()
        dungeons = [
            {'slug': 'algethar-academy', 'name_cn': '艾杰斯亚学院'},
            {'slug': 'magisters-terrace', 'name_cn': '魔导师平台'},
            {'slug': 'maisara-caverns', 'name_cn': '迈萨拉洞窟'},
            {'slug': 'nexuspoint-xenas', 'name_cn': '节点希纳斯'},
            {'slug': 'pit-of-saron', 'name_cn': '萨隆矿坑'},
            {'slug': 'seat-of-the-triumvirate', 'name_cn': '执政团之座'},
            {'slug': 'skyreach', 'name_cn': '通天峰'},
            {'slug': 'windrunner-spire', 'name_cn': '风行者之塔'},
        ]

        qs = PortalMplusRun.objects.filter(is_active=True, season=season, region=region)
        if dungeon:
            qs = qs.filter(dungeon_slug=dungeon).order_by('rank')[:30]
        else:
            qs = qs.order_by('-score', 'rank', 'id')[:60]

        items = []
        if dungeon:
            items = [_mplus_to_dict(x) for x in qs]
        else:
            for i, x in enumerate(qs):
                it = _mplus_to_dict(x)
                it['rank'] = i + 1
                items.append(it)
        return JsonResponse({'status': 'success', 'data': {'dungeons': dungeons, 'items': items}})


class PortalPeakSpecRankingsAPIView(View):
    def get(self, request):
        role = (request.GET.get("role") or "").strip().lower()
        if role not in {"tank", "healer", "dps"}:
            role = "all"

        season = (request.GET.get("season") or "").strip()
        if not season or season in {"auto", "season-mn-1"}:
            last = PortalPeakSpecRankRow.objects.filter(is_active=True).order_by("-updated_at", "-id").first()
            season = (getattr(last, "season", "") or "").strip() or "season-mn-1"

        region = (request.GET.get("region") or "world").strip()
        qs = PortalPeakSpecRankRow.objects.filter(is_active=True, season=season, region=region)
        if role != "all":
            qs = qs.filter(spec_role=role)
        rows = list(qs.order_by("class_slug", "spec_slug", "rank", "id"))

        groups = {}
        for r in rows:
            key = ((getattr(r, "class_slug", "") or "").strip(), (getattr(r, "spec_slug", "") or "").strip())
            if key not in groups:
                groups[key] = {
                    "class_slug": key[0],
                    "class_name": (getattr(r, "class_name", "") or "").strip(),
                    "spec_slug": key[1],
                    "spec_name": (getattr(r, "spec_name", "") or "").strip(),
                    "spec_role": (getattr(r, "spec_role", "") or "").strip(),
                    "items": [],
                    "updated_at": _fmt_dt(getattr(r, "updated_at", None)),
                }
            groups[key]["items"].append(_peak_row_to_dict(r))

        items = list(groups.values())
        items.sort(key=lambda x: (x.get("class_slug") or "", x.get("spec_slug") or ""))

        return JsonResponse({
            "status": "success",
            "data": {
                "season": season,
                "region": region,
                "role": role,
                "items": items,
            },
        })


class PortalRaidRankingsAPIView(View):
    def get(self, request):
        return JsonResponse({'status': 'success', 'data': []})


class PortalCharacterAPIView(View):
    def get(self, request):
        return JsonResponse({'status': 'success', 'data': {}})


class PortalMythicstatsDpsAPIView(View):
    def get(self, request):
        season = (request.GET.get('season') or '').strip()
        auto_season = (not season) or season in {"season-mn-1", "auto"}
        if auto_season:
            season = ""
        dungeon_id_raw = (request.GET.get('dungeon') or '').strip()
        period_id_raw = (request.GET.get('period') or '').strip()
        try:
            dungeon_id = int(dungeon_id_raw) if dungeon_id_raw else 0
        except ValueError:
            dungeon_id = 0
        try:
            period_id = int(period_id_raw) if period_id_raw else None
        except ValueError:
            period_id = None

        def ensure_data():
            nonlocal season
            base = fetch_mythicstats_dps(req=None, season=season, dungeon_id=dungeon_id, period_id=None)
            if auto_season:
                season = base.get("season") or season
            periods = base.get("periods") or []
            dungeons = base.get("dungeons") or [{"id": 0, "name": "All dungeons"}]
            upsert_mythicstats_meta_cache(season=season or base.get("season"), dungeons=dungeons, periods=periods)
            if not periods:
                return
            top3 = periods[:3]
            for idx, p in enumerate(top3):
                pid = p.get("id")
                if not pid:
                    continue
                cur = base
                if int(pid) != int(base.get("period_id") or 0):
                    cur = fetch_mythicstats_dps(req=None, season=season, dungeon_id=dungeon_id, period_id=int(pid))
                period_label = cur.get("period_label") or str(pid)
                cur_season = cur.get("season") or season
                upsert_mythicstats_source_cache(
                    season=cur_season,
                    dungeon_id=dungeon_id,
                    period_id=int(pid),
                    source_note=cur.get("source_note") or "",
                    key_min=cur.get("key_min"),
                    key_max=cur.get("key_max"),
                )
                exists = PortalMythicstatsDpsRow.objects.filter(season=cur_season, period_id=int(pid), dungeon_id=dungeon_id).exists()
                if idx > 0 and exists:
                    continue
                rankings = cur.get("rankings") or {}
                dungeon_name = "All dungeons"
                for d in dungeons:
                    try:
                        if int(d.get("id") or 0) == int(dungeon_id):
                            dungeon_name = d.get("name") or dungeon_name
                            break
                    except Exception:
                        continue
                for role in ("damage", "tank", "healer"):
                    rows = rankings.get(role) or []
                    upsert_mythicstats_dps_rows(
                        season=cur_season,
                        period_id=int(pid),
                        period_label=period_label,
                        dungeon_id=dungeon_id,
                        dungeon_name=dungeon_name,
                        role=role,
                        rows=rows,
                        replace_batch=(idx == 0),
                    )

        if auto_season and not season:
            slug, _label = fetch_current_season_slug(req=None)
            if slug:
                season = slug
        if not season:
            season = "unknown"

        if not PortalMythicstatsDpsRow.objects.filter(season=season, dungeon_id=dungeon_id).exists():
            ensure_data()
        elif period_id and not PortalMythicstatsDpsRow.objects.filter(season=season, dungeon_id=dungeon_id, period_id=period_id).exists():
            base = fetch_mythicstats_dps(req=None, season=season, dungeon_id=dungeon_id, period_id=period_id)
            dungeons = base.get("dungeons") or [{"id": 0, "name": "All dungeons"}]
            periods = base.get("periods") or []
            upsert_mythicstats_meta_cache(season=season, dungeons=dungeons, periods=periods)
            upsert_mythicstats_source_cache(
                season=base.get("season") or season,
                dungeon_id=dungeon_id,
                period_id=period_id,
                source_note=base.get("source_note") or "",
                key_min=base.get("key_min"),
                key_max=base.get("key_max"),
            )
            dungeon_name = "All dungeons"
            for d in dungeons:
                try:
                    if int(d.get("id") or 0) == int(dungeon_id):
                        dungeon_name = d.get("name") or dungeon_name
                        break
                except Exception:
                    continue
            for role in ("damage", "tank", "healer"):
                upsert_mythicstats_dps_rows(
                    season=base.get("season") or season,
                    period_id=period_id,
                    period_label=base.get("period_label") or str(period_id),
                    dungeon_id=dungeon_id,
                    dungeon_name=dungeon_name,
                    role=role,
                    rows=(base.get("rankings") or {}).get(role) or [],
                    replace_batch=False,
                )

        period_rows = list(
            PortalMythicstatsDpsRow.objects.filter(season=season, dungeon_id=dungeon_id)
            .values("period_id", "period_label")
            .order_by("-period_id")
            .distinct()[:3]
        )
        periods = [{"id": int(x["period_id"]), "label": x.get("period_label") or str(x["period_id"])} for x in period_rows]
        active_period = period_id or (periods[0]["id"] if periods else None)
        source_payload = get_mythicstats_source_cache(season=season, dungeon_id=dungeon_id, period_id=active_period or 0)
        source_note = (source_payload.get("source_note") or "").strip()
        key_min = source_payload.get("key_min")
        key_max = source_payload.get("key_max")

        dungeons = []
        meta = cache.get(f"mythicstats_dps_meta:{season}")
        if isinstance(meta, dict):
            dungeons = meta.get("dungeons") or []

        if not dungeons:
            dungeon_rows = list(
                PortalMythicstatsDpsRow.objects.filter(season=season)
                .exclude(dungeon_id=0)
                .values("dungeon_id", "dungeon_name")
                .order_by("dungeon_id")
                .distinct()
            )
            dungeons = [
                {"id": int(x.get("dungeon_id") or 0), "name": x.get("dungeon_name") or str(x.get("dungeon_id") or "")}
                for x in dungeon_rows
            ]

        dungeons = [{"id": 0, "name": "All dungeons"}] + [
            {"id": int(x.get("id") or x.get("dungeon_id") or 0), "name": x.get("name") or x.get("dungeon_name") or ""}
            for x in (dungeons or [])
        ]
        seen = set()
        uniq = []
        for d in dungeons:
            try:
                did = int(d.get("id") or 0)
            except Exception:
                did = 0
            if did in seen:
                continue
            seen.add(did)
            name = d.get("name") or ("All dungeons" if did == 0 else str(did))
            uniq.append({"id": did, "name": name})
        dungeons = uniq

        def row_to_dict(r):
            spec_url = (r.spec_url or "").strip()
            if spec_url.startswith("/"):
                spec_url = "https://mythicstats.com" + spec_url
            elif spec_url and (not re.match(r"^https?://", spec_url, flags=re.I)):
                spec_url = "https://mythicstats.com/" + spec_url.lstrip("/")
            return {
                "rank": r.rank,
                "diff_raw": r.diff_raw,
                "diff_value": r.diff_value,
                "tier": r.tier,
                "avg": r.avg_text,
                "avg_value": r.avg_value,
                "top": r.top_text,
                "top_value": r.top_value,
                "runs": r.runs_text,
                "spec_name": r.spec_name,
                "spec_slug": r.spec_slug,
                "spec_url": spec_url,
                "week": r.week,
            }

        roles = {"damage": [], "tank": [], "healer": []}
        if active_period:
            for role in roles.keys():
                qs = (
                    PortalMythicstatsDpsRow.objects.filter(
                        season=season,
                        dungeon_id=dungeon_id,
                        period_id=int(active_period),
                        role=role,
                    )
                    .order_by("rank")[:80]
                )
                roles[role] = [row_to_dict(x) for x in qs]

        return JsonResponse(
            {
                "status": "success",
                "data": {
                    "season": season,
                    "seasons": list(
                        PortalMythicstatsDpsRow.objects.exclude(season__in=["unknown", "season-mn-1"])
                        .values_list("season", flat=True)
                        .distinct()
                        .order_by("season")
                    ),
                    "dungeon_id": dungeon_id,
                    "periods": periods,
                    "active_period": active_period,
                    "source_note": source_note,
                    "key_min": key_min,
                    "key_max": key_max,
                    "source_url": "https://mythicstats.com/dps",
                    "dungeons": dungeons,
                    "roles": roles,
                },
            }
        )
