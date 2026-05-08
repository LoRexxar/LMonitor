from django.http import JsonResponse
from django.views import View
from django.utils import timezone
from datetime import timedelta

from botend.models import PortalCache, PortalEvent, PortalMplusRun, PortalToolLink, PortalVideo, WowArticle


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


class PortalBluepostsAPIView(View):
    def get(self, request):
        since = timezone.now() - timedelta(days=31)
        rows = (
            WowArticle.objects.filter(category='bluepost', is_active=True, publish_time__gte=since)
            .order_by('-publish_time')[:60]
        )
        return JsonResponse({'status': 'success', 'data': [_article_to_dict(x) for x in rows]})


class PortalNgaHotAPIView(View):
    def get(self, request):
        cached = PortalCache.objects.filter(key='nga_hot').first()
        if cached and (cached.data or '').strip():
            try:
                import json
                items = json.loads(cached.data) or []
                if isinstance(items, list) and items:
                    return JsonResponse({'status': 'success', 'data': items})
            except Exception:
                pass

        rows = WowArticle.objects.filter(source='nga', is_active=True).order_by('-publish_time')[:40]
        return JsonResponse({'status': 'success', 'data': [_article_to_dict(x) for x in rows]})


class PortalExwindLatestAPIView(View):
    def get(self, request):
        since = timezone.now() - timedelta(days=31)
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
            qs = qs.order_by('dungeon_slug', 'rank')[:240]
        items = [_mplus_to_dict(x) for x in qs]
        return JsonResponse({'status': 'success', 'data': {'dungeons': dungeons, 'items': items}})


class PortalRaidRankingsAPIView(View):
    def get(self, request):
        return JsonResponse({'status': 'success', 'data': []})


class PortalCharacterAPIView(View):
    def get(self, request):
        return JsonResponse({'status': 'success', 'data': {}})
