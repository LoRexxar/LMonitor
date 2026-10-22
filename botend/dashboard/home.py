"""Role-specific Dashboard home snapshots; never expose operations to players."""
from datetime import datetime, time, timedelta

from django.db import DatabaseError
from django.db.models import Count, Q
from django.utils import timezone

from botend.constants.wow import CLASS_CN, SPEC_CN, SPEC_ICON, canonical_class_spec
from botend.analytics.models import SiteAnalyticsConfig, SitePageView
from botend.models import MonitorTask, SeasonMeta, SimcTask, UserCharacterBinding
from botend.services.character_bindings import detail_url, quota
from botend.plugin_sync import monitor_task_due_at, PORTAL_DATA_SCHEDULED_TASKS


def home_context(user):
    now = timezone.now()
    if user.is_superuser:
        return {'kind': 'admin', 'as_of': now, **_operations(now)}
    return {'kind': 'player', 'as_of': now, **_characters(user)}


def _characters(user):
    bindings = UserCharacterBinding.objects.filter(user=user)
    counts = bindings.aggregate(total=Count('pk'), active=Count('pk', filter=Q(is_active=True)))
    rows = list(bindings.select_related('player').only(
        'id', 'character_name', 'region', 'realm_slug', 'is_active', 'created_at', 'player_id',
        'player__id', 'player__class_name', 'player__spec_name', 'player__realm',
        'player__avatar_url', 'player__item_level', 'player__score', 'player__last_updated',
    ).order_by('-is_active', '-created_at', '-pk')[:20])
    for row in rows:
        player = row.player
        scope = canonical_class_spec(player.class_name, player.spec_name) if player else None
        row.spec_label = f'{SPEC_CN.get(scope[1], scope[1])} · {CLASS_CN.get(scope[0], scope[0])}' if scope else '专精待更新'
        row.icon_url = (player.avatar_url if player else '') or SPEC_ICON.get(scope, '')
        row.icon_fallback = SPEC_ICON.get(scope, '') if player and player.avatar_url else ''
        row.detail_url = detail_url(row)
    season = SeasonMeta.objects.filter(is_active=True).order_by('-pk').values_list('season_name', flat=True).first()
    return {'characters': rows, 'counts': counts, 'limit': quota(user),
            'closed': counts['total'] - counts['active'], 'season': season,
            'has_more': counts['total'] > len(rows)}


def _operations(now):
    # Read only scheduling metadata. Targets and flags may contain private credentials.
    records = list(MonitorTask.objects.select_related('execution_lease').only(
        'id', 'name', 'is_active', 'wait_time', 'last_scan_time', 'execution_lease__expires_at'))
    tasks = []
    totals = {'total': len(records), 'enabled': 0, 'running': 0, 'due': 0, 'disabled': 0}
    for record in records:
        task = {key: getattr(record, key) for key in ('id', 'name', 'is_active', 'wait_time', 'last_scan_time')}
        interval = max(task['wait_time'], 0)
        due_at = monitor_task_due_at(record, now=now) if task['is_active'] else None
        task['sort_at'] = due_at or task['last_scan_time']
        task['interval'] = f'{interval // 3600} 小时' if interval and interval % 3600 == 0 else f'{interval // 60} 分钟' if interval and interval % 60 == 0 else f'{interval} 秒'
        if task['name'] in PORTAL_DATA_SCHEDULED_TASKS:
            task['interval'] = '每日调度'
        if task['is_active']:
            totals['enabled'] += 1
        lease = getattr(record, 'execution_lease', None)
        running = lease and lease.expires_at > now
        if running:
            key, label, priority = 'running', '正在执行', 1
        elif not task['is_active']:
            key, label, priority = 'disabled', '已停用', 3
        elif task['name'] == 'RaidBurstAggregationMonitor':
            key, label, priority = 'manual', '仅手动触发', 2
            task['interval'] = '手动'
        elif due_at is not None:
            key, label, priority = 'due', '待调度', 0
        else:
            key, label, priority = 'scheduled', '等待下次调度', 2
        if key in totals:
            totals[key] += 1
        task.update(state=key, label=label, priority=priority)
        tasks.append(task)
    tasks.sort(key=lambda t: (t['priority'], t['sort_at'], t['id']))
    queue = SimcTask.objects.aggregate(
        pending=Count('pk', filter=Q(is_active=True, current_status=0)),
        running=Count('pk', filter=Q(is_active=True, current_status=1)),
        failed=Count('pk', filter=Q(current_status=3, modified_time__gte=now-timedelta(hours=24))),
    )
    return {
        'monitor': totals, 'tasks': tasks[:12], 'more_tasks': len(tasks) > 12,
        'simc': queue,
        'traffic': _traffic(now),
    }


def _traffic(now):
    """A bounded snapshot using the same local dates and visitor keys as analytics."""
    today = timezone.localdate(now)
    dates = [today - timedelta(days=6-i) for i in range(8)]
    boundaries = [timezone.make_aware(datetime.combine(day, time.min)) for day in dates]
    events = SitePageView.objects.filter(occurred_at__gte=boundaries[0], occurred_at__lte=now)
    today_filter = Q(occurred_at__gte=boundaries[6])
    aggregates = {
        'sessions': Count('session_key', distinct=True, filter=today_filter),
        'active': Count('visitor_key', distinct=True, filter=Q(occurred_at__gte=now-timedelta(minutes=15))),
    }
    # Conditional aggregates avoid requiring database timezone tables, and do not
    # load individual visits or run the full analytics page's dimension queries.
    for i in range(7):
        window = Q(occurred_at__gte=boundaries[i], occurred_at__lt=boundaries[i+1])
        aggregates[f'pv_{i}'] = Count('pk', filter=window)
        aggregates[f'uv_{i}'] = Count('visitor_key', distinct=True, filter=window)
    try:
        values = events.aggregate(**aggregates)
        pages = list(events.values('path').annotate(pv=Count('pk'), uv=Count('visitor_key', distinct=True)).order_by('-pv', 'path')[:5])
        enabled = SiteAnalyticsConfig.objects.filter(pk=1).values_list('enabled', flat=True).first()
    except DatabaseError:
        return {'unavailable': True}
    peak = max(values[f'pv_{i}'] for i in range(7)) or 1
    daily = [{'date': dates[i], 'pv': values[f'pv_{i}'], 'uv': values[f'uv_{i}'],
              'height': round(values[f'pv_{i}'] * 100 / peak), 'today': i == 6} for i in range(7)]
    return {'pv': values['pv_6'], 'uv': values['uv_6'], 'sessions': values['sessions'],
            'active': values['active'], 'daily': daily, 'pages': pages,
            'week_pv': sum(day['pv'] for day in daily), 'enabled': enabled is not False,
            'timezone': timezone.get_current_timezone_name()}
