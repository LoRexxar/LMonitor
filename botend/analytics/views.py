import ipaddress
import json
import logging
import re
import uuid
from datetime import datetime, time, timedelta, timezone as datetime_timezone
from urllib.parse import parse_qs, urlsplit

from django.core.cache import cache
from django.db import DatabaseError, connection
from django.db.models import Count, DateTimeField, ExpressionWrapper, F, Q
from django.db.models.functions import ExtractHour, TruncDate
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt

from botend.analytics.models import SiteAnalyticsConfig, SitePageView
from botend.dashboard.permissions import DashboardPermissionRequiredMixin, SECTION_PERMISSION_CODES

logger = logging.getLogger(__name__)
BOT_PATTERN = re.compile(r'bot|spider|crawler|headless|preview|slurp|curl|wget', re.I)


def config_value():
    return SiteAnalyticsConfig.objects.filter(pk=1).first() or SiteAnalyticsConfig(pk=1)


def digest(kind, value):
    return salted_hmac('site-analytics:' + kind, str(value), algorithm='sha256').hexdigest()


def client_ip(request, networks):
    try:
        address = ipaddress.ip_address(request.META.get('REMOTE_ADDR', ''))
        trusted = [ipaddress.ip_network(item) for item in networks]
        if any(address in network for network in trusted):
            forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
            # 从最靠近本站的代理向外剥离，只接受受信任链条传递的地址。
            for item in reversed(forwarded.split(',')[-20:]):
                if not any(address in network for network in trusted):
                    break
                try:
                    address = ipaddress.ip_address(item.strip())
                except ValueError:
                    break
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        return str(address)
    except ValueError:
        return ''


def origin(value):
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username:
        return None
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == 'https' else 80)


def dimensions(agent):
    value = agent.lower()
    device = '平板' if 'ipad' in value or ('android' in value and 'mobile' not in value) else '手机' if 'mobile' in value or 'iphone' in value else '电脑'
    browser = next((label for token, label in [('edg', 'Edge'), ('opr/', 'Opera'), ('firefox', 'Firefox'), ('fxios', 'Firefox'), ('chrome', 'Chrome'), ('crios', 'Chrome'), ('safari', 'Safari')] if token in value), '其他')
    os_name = next((label for token, label in [('android', 'Android'), ('iphone', 'iOS'), ('ipad', 'iOS'), ('windows', 'Windows'), ('mac os', 'macOS'), ('linux', 'Linux')] if token in value), '其他')
    return device, browser, os_name


def read_json(request):
    if len(request.body) > 4096:
        raise ValueError('请求过大')
    data = json.loads(request.body)
    if not isinstance(data, dict):
        raise ValueError('请求必须为 JSON 对象')
    return data


def normalized_path(value):
    if not isinstance(value, str) or len(value) > 2048 or not value.startswith('/') or value.startswith('//') or '\\' in value or any(ord(char) < 32 for char in value):
        raise ValueError('页面路径无效')
    parsed = urlsplit(value)
    path = parsed.path
    if path == '/dashboard/':
        section = parse_qs(parsed.query).get('section', [''])[0]
        if section in SECTION_PERMISSION_CODES:
            path += '?section=' + section
    if len(path) > 512:
        raise ValueError('页面路径过长')
    return path


@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(never_cache, name='dispatch')
class CollectView(View):
    def post(self, request):
        try:
            source = request.headers.get('Origin') or request.headers.get('Referer', '')
            if not origin(source) or origin(source) != origin(request.build_absolute_uri('/')) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
                return JsonResponse({'message': '只接受本站上报'}, status=403)
            if request.content_type != 'application/json':
                return JsonResponse({'message': '需要 JSON 数据'}, status=415)
            data = read_json(request)
            event_id, visitor_id, session_id = (uuid.UUID(str(data.get(key, ''))) for key in ('event_id', 'visitor_id', 'session_id'))
            path = normalized_path(data.get('path'))
            referrer = data.get('referrer', '')
            if not isinstance(referrer, str) or len(referrer) > 2048:
                raise ValueError('来源无效')
            parsed_referrer = urlsplit(referrer)
            referrer_host = (parsed_referrer.hostname or '').lower() if parsed_referrer.scheme in ('http', 'https') else ''
            if len(referrer_host) > 253:
                raise ValueError('来源域名过长')
            agent = request.headers.get('User-Agent', '')[:1024]
            config = config_value()
            if not config.enabled or BOT_PATTERN.search(agent) or any(path.startswith(prefix) for prefix in config.excluded_prefixes):
                return HttpResponse(status=204)
            address = client_ip(request, config.trusted_proxy_cidrs)
            # 对每个连接地址限流；部署共享缓存后可在多个工作进程之间共享计数。
            key = 'site-analytics:rate:' + digest('ip', address or request.META.get('REMOTE_ADDR', 'unknown'))
            if cache.add(key, 1, timeout=60):
                count = 1
            else:
                try:
                    count = cache.incr(key)
                except ValueError:
                    cache.set(key, 1, timeout=60)
                    count = 1
            if count > 120:
                return JsonResponse({'message': '访问上报过于频繁'}, status=429)
            device, browser, os_name = dimensions(agent)
            area = '后台' if path.startswith('/dashboard/') else '账号' if path.startswith('/auth/') else '前台'
            SitePageView.objects.get_or_create(event_id=event_id, defaults={
                'occurred_at': timezone.now(), 'path': path, 'area': area,
                'visitor_key': digest('visitor', visitor_id),
                'session_key': digest('session', f'{visitor_id}:{session_id}'),
                'ip_key': digest('ip', address) if address else '',
                'referrer_host': referrer_host, 'device': device, 'browser': browser, 'os': os_name,
                'authenticated': bool(getattr(request, 'user', None) and request.user.is_authenticated),
            })
            return HttpResponse(status=204)
        except (ValueError, TypeError, OverflowError):
            return JsonResponse({'message': '上报数据无效'}, status=400)
        except DatabaseError:
            logger.warning('站内统计存储暂不可用')
            return JsonResponse({'message': '统计存储暂不可用'}, status=503)


def metrics(queryset):
    return queryset.aggregate(pv=Count('id'), uv=Count('visitor_key', distinct=True),
                              ips=Count('ip_key', filter=~Q(ip_key=''), distinct=True),
                              sessions=Count('session_key', distinct=True))


@method_decorator(never_cache, name='dispatch')
class AnalyticsAPIView(DashboardPermissionRequiredMixin, View):
    dashboard_permission = 'system.analytics'

    def get(self, request):
        try:
            today = timezone.localdate()
            start = datetime.strptime(request.GET.get('start', str(today - timedelta(days=6))), '%Y-%m-%d').date()
            end = datetime.strptime(request.GET.get('end', str(today)), '%Y-%m-%d').date()
            if end < start or (end - start).days > 92 or start.year < 1970 or end.year > 9998:
                raise ValueError('日期范围必须为 1 至 93 天')
            area = request.GET.get('area', '')
            if area not in ('', '前台', '后台', '账号'):
                raise ValueError('站点区域无效')
            path = request.GET.get('path', '').strip()
            if len(path) > 512:
                raise ValueError('路径筛选过长')
        except ValueError as exc:
            return JsonResponse({'message': str(exc)}, status=400)
        boundary = lambda date: timezone.make_aware(datetime.combine(date, time.min))
        events = SitePageView.objects.filter(occurred_at__gte=boundary(start), occurred_at__lt=boundary(end + timedelta(days=1)))
        if area:
            events = events.filter(area=area)
        if path:
            events = events.filter(path__startswith=path)
        summary = metrics(events)
        summary['pv_per_visitor'] = round(summary['pv'] / summary['uv'], 2) if summary['uv'] else 0
        summary['pv_per_session'] = round(summary['pv'] / summary['sessions'], 2) if summary['sessions'] else 0
        summary['active_visitors'] = events.filter(occurred_at__gte=timezone.now() - timedelta(minutes=15)).values('visitor_key').distinct().count()
        summary['repeat_visitors'] = events.values('visitor_key').annotate(total=Count('id')).filter(total__gte=2).count()
        # 固定时差直接在数据库中平移 UTC，避免 MySQL 未安装时区表时产生空分组。
        offsets = {boundary(start + timedelta(days=day)).utcoffset() for day in range((end - start).days + 2)}
        date_expression, group_timezone = F('occurred_at'), timezone.get_current_timezone()
        if connection.timezone_name == 'UTC' and len(offsets) == 1:
            date_expression = ExpressionWrapper(F('occurred_at') + offsets.pop(), output_field=DateTimeField())
            group_timezone = datetime_timezone.utc
        def groups(field, limit=20):
            return list(events.values(label=F(field)).annotate(
                pv=Count('id'), uv=Count('visitor_key', distinct=True),
                ips=Count('ip_key', filter=~Q(ip_key=''), distinct=True),
                sessions=Count('session_key', distinct=True),
            ).order_by('-pv', 'label')[:limit])
        daily_rows = {str(row.pop('day')): row for row in events.annotate(day=TruncDate(date_expression, tzinfo=group_timezone)).values('day').annotate(
            pv=Count('id'), uv=Count('visitor_key', distinct=True), ips=Count('ip_key', filter=~Q(ip_key=''), distinct=True), sessions=Count('session_key', distinct=True))}
        if 'None' in daily_rows:
            return JsonResponse({'message': '数据库缺少当前站点时区规则，暂时无法生成时间趋势'}, status=503)
        daily = [{'label': str(start + timedelta(days=offset)), **daily_rows.get(str(start + timedelta(days=offset)), {'pv': 0, 'uv': 0, 'ips': 0, 'sessions': 0})} for offset in range((end - start).days + 1)]
        hourly_rows = dict(events.annotate(hour=ExtractHour(date_expression, tzinfo=group_timezone)).values('hour').annotate(pv=Count('id')).values_list('hour', 'pv'))
        def frequency(field):
            counts = events.exclude(**{field: ''}).values(field).annotate(total=Count('id'))
            return [{'label': label, 'count': counts.filter(total__gte=low, **({'total__lte': high} if high else {})).count()} for label, low, high in [('1 次', 1, 1), ('2–5 次', 2, 5), ('6–10 次', 6, 10), ('11 次以上', 11, None)]]
        config = config_value()
        return JsonResponse({'summary': summary, 'daily': daily,
            'hourly': [{'label': f'{hour:02d}:00', 'pv': hourly_rows.get(hour, 0)} for hour in range(24)],
            'pages': groups('path', 50), 'sources': groups('referrer_host'), 'devices': groups('device'),
            'browsers': groups('browser'), 'systems': groups('os'), 'areas': groups('area'),
            'authentication': groups('authenticated'), 'visitor_frequency': frequency('visitor_key'),
            'ip_frequency': frequency('ip_key'),
            'filters': {'start': str(start), 'end': str(end), 'area': area, 'path': path}, 'timezone': timezone.get_current_timezone_name(),
            'config': {'enabled': config.enabled, 'excluded_prefixes': config.excluded_prefixes, 'trusted_proxy_cidrs': config.trusted_proxy_cidrs}})

    def post(self, request):
        try:
            data = read_json(request)
            if not isinstance(data.get('enabled'), bool):
                raise ValueError('启用状态必须为布尔值')
            prefixes = data.get('excluded_prefixes', [])
            networks = data.get('trusted_proxy_cidrs', [])
            if not isinstance(prefixes, list) or len(prefixes) > 30 or any(not isinstance(item, str) or not item.startswith('/') or len(item) > 512 for item in prefixes):
                raise ValueError('排除路径必须以 / 开头，最多 30 项')
            if not isinstance(networks, list) or len(networks) > 30 or any(not isinstance(item, str) for item in networks):
                raise ValueError('可信代理网段无效')
            networks = [str(ipaddress.ip_network(item, strict=False)) for item in networks]
            if any(ipaddress.ip_network(item).prefixlen == 0 for item in networks):
                raise ValueError('不能将所有 IP 设为可信代理')
            config, _ = SiteAnalyticsConfig.objects.update_or_create(pk=1, defaults={
                'enabled': data['enabled'], 'excluded_prefixes': list(dict.fromkeys(prefixes)),
                'trusted_proxy_cidrs': list(dict.fromkeys(networks)),
            })
            return JsonResponse({'message': '统计设置已保存', 'enabled': config.enabled})
        except (ValueError, TypeError):
            return JsonResponse({'message': '设置无效，请检查开关、路径前缀和代理网段'}, status=400)
