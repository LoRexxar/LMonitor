"""资料优先发布，队伍与已登记团本资料按持久化候选分批补齐。"""
from datetime import datetime, timedelta, timezone as utc_timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import math
import time
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from django.db import transaction
from django.db.models import Q, Min
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.models import (CharacterEnrichmentTask, RaiderIOSourceBackoff,
                           RaiderIORunRoster, RaiderIOCharacterActivity, PlayerSpecTopPlayer)

ACTIVE_STATES = ('pending', 'running', 'waiting', 'error')

def source_backoff():
    return RaiderIOSourceBackoff.objects.filter(key='character').values_list('until', flat=True).first()


def _save_backoff(response):
    now = timezone.now()
    value = response.headers.get('Retry-After', '')
    try:
        delay = float(value)
        if not math.isfinite(delay):
            raise ValueError('无效冷却时间')
        until = now + timedelta(seconds=max(60, min(31536000, delay)))
    except (ValueError, TypeError, OverflowError):
        try:
            until = parsedate_to_datetime(value)
            if timezone.is_naive(until):
                raise ValueError('缺少时区')
            until = max(now + timedelta(seconds=60), until)
        except (ValueError, TypeError, OverflowError):
            until = now + timedelta(hours=1)
    with transaction.atomic():
        row, _ = RaiderIOSourceBackoff.objects.get_or_create(key='character', defaults={'until': until})
        row = RaiderIOSourceBackoff.objects.select_for_update().get(pk=row.pk)
        if row.until < until:
            row.until = until
            row.save(update_fields=['until'])
        return row.until


def guarded_get(transport, url, **kwargs):
    """跨工作者遵守429；冷却中的拒绝仍走原采集器的限流处理路径。"""
    if urlsplit(url).hostname not in ('raider.io', 'www.raider.io', 'cdn.raiderio.net'):
        return transport(url, **kwargs)
    until = source_backoff()
    if until and until > timezone.now():
        response = requests.Response()
        response.status_code = 429
        response.headers['Retry-After'] = str(math.ceil((until - timezone.now()).total_seconds()))
        return response
    response = transport(url, **kwargs)
    if response.status_code == 429:
        _save_backoff(response)
    return response


class EnrichmentBudget:
    """限制新工作准入；已发出的单项工作允许按原请求超时结束。"""
    def __init__(self, transport=None, *, post_transport=None, max_requests=40, max_seconds=90):
        if max_requests < 1 or max_seconds <= 0:
            raise ValueError('补采预算必须为正数')
        self.transport = transport or requests.get
        self.post_transport = post_transport or requests.post
        self.max_requests, self.max_seconds = max_requests, max_seconds
        self.started = time.monotonic()
        self.requests = 0

    def can_start(self, cost=1, *, seconds=None):
        return (self.requests + cost <= self.max_requests
                and time.monotonic() - self.started < min(self.max_seconds, seconds if seconds is not None else self.max_seconds))

    def get(self, url, **kwargs):
        def request(endpoint, **options):
            if self.requests >= self.max_requests:
                raise RuntimeError('补采工作超出预留请求预算')
            self.requests += 1
            return self.transport(endpoint, **options)
        return guarded_get(request, url, **kwargs)

    def post(self, url, **kwargs):
        if self.requests >= self.max_requests:
            raise RuntimeError('补采工作超出预留请求预算')
        self.requests += 1
        return self.post_transport(url, **kwargs)


def _enqueue_many(season, entries):
    """批量排除未变且仍有效的候选；需要变更的行仍加锁并复核。"""
    now = timezone.now()
    entries = list(entries)
    for start in range(0, len(entries), 100):
        batch = entries[start:start + 100]
        rows = {(row.kind, row.key): row for row in CharacterEnrichmentTask.objects.filter(
            season=season, kind__in={item[0] for item in batch}, key__in={item[1] for item in batch})}
        for kind, key, payload in batch:
            revision = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            row = rows.get((kind, key))
            if (row and row.revision == revision and row.expires_at > now + timedelta(days=1)
                    and row.state != 'expired'):
                continue
            _enqueue(season, kind, key, payload)


def _enqueue(season, kind, key, payload):
    now = timezone.now()
    revision = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with transaction.atomic():
        task, _ = CharacterEnrichmentTask.objects.get_or_create(season=season, kind=kind, key=key,
            defaults={'payload': payload, 'revision': revision, 'due_at': now,
                      'expires_at': now + timedelta(days=2)})
        task = CharacterEnrichmentTask.objects.select_for_update().get(pk=task.pk)
        if task.revision == revision and task.expires_at > now + timedelta(days=1) and task.state != 'expired':
            return
        task.expires_at = now + timedelta(days=2)
        if task.state == 'expired':
            task.state = 'pending'
        if task.revision != revision:
            task.payload, task.revision = payload, revision
            task.due_at, task.state = now, 'pending'
        task.save(update_fields=['payload', 'revision', 'due_at', 'state', 'expires_at', 'updated_at'])


def enqueue_enrichment(activity, season, player):
    """只登记已发布来源引用；跨人物的同一通关记录共用一项工作。"""
    from botend.services.raiderio_run_roster import run_reference, MAX_BEST_RUNS
    entries = []
    for run in (activity.payload_json.get('mythic_plus_best_runs') or [])[:MAX_BEST_RUNS]:
        try:
            run_id = run_reference(run, season.rio_season)
        except (ValueError, TypeError):
            continue
        reference = {key: run[key] for key in ('url', 'season', 'keystone_run_id', 'dungeon',
            'mythic_level', 'clear_time_ms', 'num_keystone_upgrades', 'completed_at') if key in run}
        entries.append(('roster', str(run_id), {'run': reference, 'rio_season': season.rio_season}))
    if 'raid_first_kills' in activity.payload_json:
        entries.append(('raid', activity.identity_digest,
                        {'activity_id': activity.pk, 'rio_season': season.rio_season}))
    _enqueue_many(season, entries)
    return len(entries)


def enqueue_battlenet_profiles(season, players):
    """候选按资料行应用，实际请求仍按跨专精角色缓存去重。"""
    from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
    entries = [('battlenet', str(p.pk), {'profile_id': p.pk, 'rio_season': season.rio_season,
                'identity': SpecDetailPlayerMonitor._battlenet_scope(p)})
               for p in players if p.pk and p.season_id == season.pk and (p.region or '').lower() != 'cn']
    _enqueue_many(season, entries)
    return len(entries)


def _claim(season, *, allow_raid=True, allow_rio=True, allow_battlenet=True):
    now = timezone.now()
    kinds = (['roster'] + (['raid'] if allow_raid else [])) if allow_rio else []
    if allow_battlenet:
        kinds.append('battlenet')
    with transaction.atomic():
        task = CharacterEnrichmentTask.objects.select_for_update().filter(
            season=season, state__in=ACTIVE_STATES, kind__in=kinds,
            expires_at__gt=now, due_at__lte=now).filter(
                Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)).order_by('due_at', 'pk').first()
        if task is None:
            return None
        task.lease_owner, task.lease_expires_at = uuid4().hex, now + timedelta(minutes=5)
        task.state = 'running'
        task.save(update_fields=['lease_owner', 'lease_expires_at', 'state', 'updated_at'])
        return task


def _finish(task, state, due, error=''):
    with transaction.atomic():
        current = CharacterEnrichmentTask.objects.select_for_update().get(pk=task.pk)
        if current.lease_owner != task.lease_owner:
            return
        current.lease_owner, current.lease_expires_at = '', None
        if current.revision == task.revision:
            current.state, current.due_at, current.last_error = state, due, error[:80]
            current.failure_count = current.failure_count + 1 if error else 0
        else:
            current.state, current.due_at = 'pending', timezone.now()
        current.save()


def _run(task, budget):
    season = task.season
    if not season.is_active or task.payload.get('rio_season') != season.rio_season:
        return 'obsolete', timezone.now() + timedelta(days=2)
    if task.kind == 'battlenet':
        from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
        from botend.services.spec_aggregation_cache import load_state
        from botend.services.spec_stats_snapshot import content_digest
        player = PlayerSpecTopPlayer.objects.filter(pk=task.payload['profile_id'], season_id=season.pk).first()
        if (player is None or (player.region or '').lower() == 'cn'
                or SpecDetailPlayerMonitor._battlenet_scope(player) != task.payload['identity']):
            return 'obsolete', timezone.now() + timedelta(days=2)
        if not hasattr(budget, 'battlenet_monitor'):
            budget.battlenet_monitor = SpecDetailPlayerMonitor(None, None)
            budget.battlenet_monitor._http_get = budget.get
            budget.battlenet_monitor._http_post = budget.post
        monitor = budget.battlenet_monitor
        ok = monitor._crawl_battlenet_stats_for_profile(player)
        cached = load_state(player.season_id, task.payload['identity'], content_digest('battlenet-stats-v1'))
        due = datetime.fromtimestamp(cached['next_attempt'], utc_timezone.utc) if cached else None
        return 'ready' if ok else 'battlenet_failed', due
    if task.kind == 'roster':
        from botend.services.raiderio_run_roster import RosterCollector
        collector = RosterCollector(transport=budget.get)
        result = collector.refresh(task.payload['run'], season.rio_season)
        row = RaiderIORunRoster.objects.filter(rio_season=season.rio_season, run_id=int(task.key)).first()
        return result['status'], row.next_attempt_at if row else None
    if task.kind == 'raid':
        from botend.services.player_raiderio_activity import player_identity
        from botend.raiderio_identity import activity_identity_digest
        from botend.services.player_raid_observation_sources import refresh_raid_sources
        row = RaiderIOCharacterActivity.objects.filter(pk=task.payload['activity_id'], season=season).first()
        if row is None or 'raid_first_kills' not in row.payload_json:
            return 'obsolete', timezone.now() + timedelta(days=2)
        players = PlayerSpecTopPlayer.objects.filter(season_id=season.pk, region=row.region,
            character_name__iexact=row.character_name, rank__gte=1, rank__lte=20).only(
                'id', 'season_id', 'region', 'realm', 'character_name', 'profile_url')
        player = next((p for p in players if (identity := player_identity(p))
                       and activity_identity_digest(**identity) == row.identity_digest), None)
        if player is None:
            return 'obsolete', timezone.now() + timedelta(days=1)
        result = refresh_raid_sources(player, transport=budget.get)
        row.refresh_from_db(fields=['payload_json'])
        due = parse_datetime(row.payload_json.get('raid_first_kills', {}).get('next_attempt_at') or '')
        return result['status'], due
    return 'obsolete', timezone.now() + timedelta(days=2)


def consume_enrichment(season, budget, *, limit=20, seconds=45, allow_rio=True):
    summary = {'attempted': 0, 'ready': 0, 'deferred': 0, 'error': 0, 'stop': ''}
    now = timezone.now()
    CharacterEnrichmentTask.objects.filter(season=season, state__in=ACTIVE_STATES,
        expires_at__lte=now).filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)).update(state='expired')
    for _ in range(limit):
        # 单个团本工作最多三次请求，预留完整额度，不在中间截断事务。
        if not budget.can_start(1, seconds=seconds):
            summary['stop'] = 'budget'
            break
        until = source_backoff()
        rio_ready = allow_rio and not (until and until > timezone.now())
        if not rio_ready:
            summary['stop'] = 'rate_limited'
        task = _claim(season, allow_raid=budget.can_start(3, seconds=seconds), allow_rio=rio_ready,
                      allow_battlenet=budget.can_start(2, seconds=seconds))
        if task is None:
            break
        summary['attempted'] += 1
        try:
            state, due = _run(task, budget)
        except Exception as exc:
            logging.getLogger(__name__).exception('人物补采失败 task_id=%s kind=%s', task.pk, task.kind)
            state, due = type(exc).__name__, None
        now = timezone.now()
        due = due if due and timezone.is_aware(due) and due > now else now + timedelta(
            seconds=min(21600, 300 * 2 ** min(task.failure_count, 6)))
        success = state in ('ready', 'fresh', 'unavailable', 'private', 'obsolete')
        deferred = state in ('busy', 'backoff', 'batch_stopped')
        _finish(task, 'waiting' if success or deferred else 'error', due,
                '' if success or deferred else state)
        summary['ready' if success else 'deferred' if deferred else 'error'] += 1
    summary.update(enrichment_progress(season))
    return summary


def enrichment_progress(season):
    summary = {}
    pending = CharacterEnrichmentTask.objects.filter(season=season, state__in=ACTIVE_STATES, expires_at__gt=timezone.now())
    summary['remaining_due'] = pending.filter(due_at__lte=timezone.now()).count()
    summary['battlenet_due'] = pending.filter(kind='battlenet', due_at__lte=timezone.now()).count()
    oldest = pending.filter(due_at__lte=timezone.now()).aggregate(value=Min('due_at'))['value']
    summary['oldest_due_seconds'] = max(0, int((timezone.now() - oldest).total_seconds())) if oldest else 0
    return summary
