from django.http import JsonResponse
from django.views import View
from django.utils import timezone
from datetime import timedelta

from botend.models import PortalEvent, PortalMplusRun, PortalMythicstatsDpsRow, PortalToolLink, PortalVideo, WowArticle, WowSkillDiffReport
from botend.portal.mythicstats import (
    fetch_current_season_slug,
    fetch_mythicstats_dps,
    upsert_mythicstats_dps_rows,
    upsert_mythicstats_meta_cache,
)


def _fmt_dt(dt):
    if not dt:
        return ''
    try:
        return timezone.localtime(dt).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return ''


def _article_to_dict(a):
    return {
        'title': a.title or '',
        'url': a.url or '',
        'author': a.author or '',
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
        'url': e.url or '',
        'source': e.source or '',
        'tag': e.tag or '',
        'status': status,
        'start_at': _fmt_dt(e.start_at),
        'end_at': _fmt_dt(e.end_at),
    }


def _video_to_dict(v):
    return {
        'title': v.title or '',
        'url': v.url or '',
        'bvid': v.bvid or '',
        'cover_url': v.cover_url or '',
        'published_at': _fmt_dt(v.published_at),
        'author': v.author_name or '',
        'author_url': v.author_url or '',
        'tag': v.tag or '',
    }


def _tool_to_dict(t):
    return {
        'name': t.name or '',
        'url': t.url or '',
        'desc': t.desc or '',
        'icon_path': getattr(t, 'icon_path', '') or '',
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
        'run_url': getattr(r, 'run_url', '') or '',
        'source': r.source or '',
        'season': r.season or '',
        'region': r.region or '',
    }


def _skilldiff_to_dict(r):
    branch = (getattr(r, 'branch', '') or '').strip()
    to_build = (getattr(r, 'to_build', '') or '').strip()
    from_build = (getattr(r, 'from_build', '') or '').strip()
    label = f"{branch} {from_build} → {to_build}".strip()
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


class PortalWowSkillDiffListAPIView(View):
    def get(self, request):
        limit = request.GET.get('limit', '20')
        try:
            limit = max(1, min(100, int(limit)))
        except ValueError:
            limit = 20
        rows = list(WowSkillDiffReport.objects.all().order_by('-created_at')[:limit])
        return JsonResponse({'status': 'success', 'data': [_skilldiff_to_dict(x) for x in rows]})


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
        qs = WowArticle.objects.filter(source='nga', category='hot', is_active=True)
        if not qs.exists():
            qs = WowArticle.objects.filter(source='nga', is_active=True)
        rows = list(qs.order_by('-publish_time', '-id')[:40])
        return JsonResponse({'status': 'success', 'data': [_article_to_dict(x) for x in rows]})


class PortalExwindLatestAPIView(View):
    def get(self, request):
        since = timezone.now() - timedelta(days=7)
        rows = (
            WowArticle.objects.filter(source='exwind', is_active=True, publish_time__gte=since)
            .order_by('-publish_time')[:60]
        )
        return JsonResponse({'status': 'success', 'data': [_article_to_dict(x) for x in rows]})


class PortalEventsAPIView(View):
    def get(self, request):
        rows = list(PortalEvent.objects.filter(is_active=True).order_by('-start_at', '-id')[:30])
        return JsonResponse({'status': 'success', 'data': [_event_to_dict(x) for x in rows]})


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
        return JsonResponse({'status': 'success', 'data': {}})


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

        dungeon_rows = list(
            PortalMythicstatsDpsRow.objects.filter(season=season)
            .exclude(dungeon_id=0)
            .values("dungeon_id", "dungeon_name")
            .order_by("dungeon_id")
            .distinct()
        )
        dungeons = [{"id": 0, "name": "All dungeons"}] + [
            {"id": int(x.get("dungeon_id") or 0), "name": x.get("dungeon_name") or str(x.get("dungeon_id") or "")}
            for x in dungeon_rows
        ]

        def row_to_dict(r):
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
                "spec_url": r.spec_url,
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
                    "dungeons": dungeons,
                    "roles": roles,
                },
            }
        )
