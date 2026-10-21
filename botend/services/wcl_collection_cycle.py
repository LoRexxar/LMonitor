"""Durable WCL ingestion barrier. Control rows contain references, never facts.

Lock order: MonitorTask -> MonitorTaskLease -> SeasonMeta -> cycle. Network and
aggregation computation run outside transactions. Only scope replacement and
final local publication are performed under a cycle fence.
"""
from contextlib import contextmanager
from copy import copy
from datetime import timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.models import (MonitorTask, MonitorTaskLease, SeasonMeta,
                           SpecDungeonRanking, SpecRaidRanking, WclCollectionCycle)

LOCAL_TZ = ZoneInfo('Asia/Shanghai')
RETRY_DELAY = timedelta(minutes=5)
COLLECTING = ('rankings', 'combatants')
PUBLISHING = ('ready', 'aggregating')
NO_OVERRIDE = object()


class CycleOwnershipLost(RuntimeError):
    pass


def ranking_slot(now=None):
    now = (now or timezone.now()).astimezone(LOCAL_TZ)
    slot = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return slot if slot <= now else slot - timedelta(days=1)


def active_cycle(season_id, *, lock=False, only_fields=None):
    rows = WclCollectionCycle.objects.filter(season_id=season_id).exclude(phase='published')
    if only_fields is not None:
        rows = rows.only(*only_fields)
    if lock:
        rows = rows.select_for_update()
    return rows.order_by('slot', 'pk').first()


@contextmanager
def task_lease_guard(task):
    """Validate a real scheduler lease before taking any downstream lock."""
    with transaction.atomic():
        owner = getattr(task, '_monitor_task_lease_owner', None)
        if owner:
            MonitorTask.objects.select_for_update().get(pk=task.pk)
            if not MonitorTaskLease.objects.select_for_update().filter(
                    task_id=task.pk, owner=owner, expires_at__gt=timezone.now()).exists():
                raise CycleOwnershipLost('monitor task lease lost')
        yield


@contextmanager
def fenced_cycle(cycle, *, phases):
    with transaction.atomic():
        current = WclCollectionCycle.objects.select_for_update().get(pk=cycle.pk)
        if current.owner != cycle.owner or current.phase not in phases:
            raise CycleOwnershipLost(f'cycle {cycle.pk} owner/phase changed')
        yield current


def _save(current, cycle, **updates):
    for key, value in updates.items():
        setattr(current, key, value)
        setattr(cycle, key, value)
    current.save(update_fields=list(updates))


def begin_collection(season, task=None, *, now=None):
    now = now or timezone.now()
    owner = getattr(task, '_wcl_cycle_owner', None) or uuid4().hex
    with task_lease_guard(task):
        SeasonMeta.objects.select_for_update().get(pk=season.pk)
        cycle = active_cycle(season.pk, lock=True)
        if cycle:
            if cycle.phase not in COLLECTING or (cycle.next_retry_at and cycle.next_retry_at > now):
                return None
            cycle.owner = owner
            cycle.last_attempt_at = now
            cycle.next_retry_at = None
            cycle.error = ''
            cycle.save(update_fields=['owner', 'last_attempt_at', 'next_retry_at', 'error'])
            return cycle
        slot = ranking_slot(now)
        # Legacy 03:00 cycles already represent this local day; never duplicate
        # them just because the configured slot moved to midnight.
        if WclCollectionCycle.objects.filter(
                season=season, slot__gte=slot, slot__lt=slot + timedelta(days=1)).exists():
            return None
        cycle, created = WclCollectionCycle.objects.get_or_create(
            season=season, slot=slot,
            defaults={'owner': owner, 'started_at': now, 'last_attempt_at': now})
        return cycle if created else None


def checkpoint_scope(cycle, scope):
    """Call inside the SAME transaction as the fenced ranking replacement."""
    with fenced_cycle(cycle, phases=('rankings',)) as current:
        scopes = list(current.completed_scopes)
        if scope not in scopes:
            scopes.append(scope)
        _save(current, cycle, completed_scopes=scopes)


def defer_cycle(cycle, error, retry_at=None, *, counts=None):
    if isinstance(retry_at, str):
        retry_at = parse_datetime(retry_at)
    if retry_at is not None and timezone.is_naive(retry_at):
        retry_at = timezone.make_aware(retry_at, LOCAL_TZ)
    now = timezone.now()
    # Invalid/past retry hints must not create a hot scheduler loop.
    retry_at = retry_at if retry_at and retry_at > now else now + RETRY_DELAY
    with fenced_cycle(cycle, phases=COLLECTING + ('aggregating',)) as current:
        updates = dict(next_retry_at=retry_at, error=str(error), last_attempt_at=now)
        if counts is not None:
            updates['counts'] = counts
        _save(current, cycle, **updates)


def freeze_samples(cycle, season, identities):
    """Freeze the existing 300-candidate/top100 selector's central row IDs."""
    from botend.services.spec_stats_service import _select_dungeon_sample_records
    expected = {f'dungeon:{enc["id"]}' for enc in season.mplus_encounters or []}
    expected.update(f'raid:{difficulty}:{enc["id"]}'
                    for difficulty in (5, 4) for enc in season.raid_encounters or [])
    with fenced_cycle(cycle, phases=('rankings',)) as current:
        if not expected.issubset(current.completed_scopes):
            raise ValueError('cannot freeze unfinished ranking scopes')
        selected = {}
        for class_name, spec_name in identities:
            scopes = {}
            for enc in season.mplus_encounters or []:
                rows = SpecDungeonRanking.objects.filter(season_id=season.pk,
                    dungeon_id=enc['id'], class_name=class_name, spec_name=spec_name)
                scopes[str(enc['id'])] = [row['id'] for row in _select_dungeon_sample_records(
                    rows, max_samples=100, hydrate_fields=('id',))]
            selected[f'{class_name}/{spec_name}'] = scopes
        _save(current, cycle, selected_samples=selected, phase='combatants', next_retry_at=None, error='')


def frozen_season(cycle, season):
    """Project this cycle's scope references onto metadata without mutating it.

    Season discovery may add/remove encounters while a frozen cycle waits.
    Removed labels come from central ranking facts, never invented names.
    """
    projected = copy(season)
    dungeon_ids = dict.fromkeys(int(key) for scopes in cycle.selected_samples.values() for key in scopes)
    raid_ids = dict.fromkeys(int(scope.split(':')[2]) for scope in cycle.completed_scopes
                             if scope.startswith('raid:'))
    current_dungeons = {int(enc['id']): enc for enc in season.mplus_encounters or []}
    current_bosses = {int(enc['id']): enc for enc in season.raid_encounters or []}
    dungeon_names = {}
    for enc_id, name in SpecDungeonRanking.objects.filter(
            season_id=season.pk, dungeon_id__in=set(dungeon_ids) - current_dungeons.keys()
    ).exclude(dungeon_name='').order_by('dungeon_id', 'dungeon_name').values_list(
            'dungeon_id', 'dungeon_name').distinct():
        dungeon_names.setdefault(enc_id, name)
    boss_facts = {}
    for row in SpecRaidRanking.objects.filter(
            season_id=season.pk, boss_id__in=raid_ids
    ).order_by('boss_id', 'boss_name').values(
            'boss_id', 'boss_name', 'raid_zone_id', 'raid_zone_name').distinct():
        boss_facts.setdefault(row['boss_id'], row)
    projected.mplus_encounters = [dict(current_dungeons[enc_id]) if enc_id in current_dungeons else
        {'id': enc_id, 'name': dungeon_names.get(enc_id) or str(enc_id)} for enc_id in dungeon_ids]
    projected.raid_encounters = [dict(current_bosses[enc_id]) if enc_id in current_bosses else
        {'id': enc_id, 'name': boss_facts.get(enc_id, {}).get('boss_name') or str(enc_id)}
        for enc_id in raid_ids]
    projected.raid_zones = []
    assigned = set()
    for zone in season.raid_zones or []:
        encounters = [dict(enc) for enc in zone.get('encounters', []) if int(enc['id']) in raid_ids]
        if encounters:
            projected.raid_zones.append({**zone, 'encounters': encounters})
            assigned.update(int(enc['id']) for enc in encounters)
    for enc in projected.raid_encounters:
        if enc['id'] in assigned:
            continue
        fact = boss_facts.get(enc['id'], {})
        zone_id, zone_name = fact.get('raid_zone_id'), fact.get('raid_zone_name') or ''
        zone = next((zone for zone in projected.raid_zones if zone.get('id') == zone_id), None)
        if zone is None:
            zone = {'id': zone_id, 'name': zone_name, 'encounters': []}
            projected.raid_zones.append(zone)
        zone['encounters'].append(enc)
    return projected


def frozen_combatant_records(cycle):
    ids = {pk for scopes in cycle.selected_samples.values() for rows in scopes.values() for pk in rows}
    records = list(SpecDungeonRanking.objects.filter(season_id=cycle.season_id, pk__in=ids).values(
        'id', 'report_code', 'fight_id', 'character_name', 'realm', 'region', 'class_name', 'spec_name'))
    if len(records) != len(ids):
        raise ValueError('frozen ranking references disappeared')
    return records


def finish_collection(cycle, counts):
    if counts.get('failed', 0) or counts.get('pending', 0):
        defer_cycle(cycle, 'combatants incomplete', counts.get('retry_at'), counts=counts)
        return False
    with fenced_cycle(cycle, phases=('combatants',)) as current:
        _save(current, cycle, phase='ready', counts=counts, collected_at=timezone.now(),
              next_retry_at=None, error='')
    return True


def begin_aggregation(season, task=None, *, now=None, replay_published=False):
    now = now or timezone.now()
    with task_lease_guard(task):
        SeasonMeta.objects.select_for_update().get(pk=season.pk)
        cycle = active_cycle(season.pk, lock=True)
        if cycle is not None and replay_published:
            return None
        catch_up_burst = False
        if cycle is None:
            published = WclCollectionCycle.objects.select_for_update().filter(
                season=season, phase='published').order_by('-slot').first()
            if published and not replay_published:
                from botend.services.raid_burst_snapshot import needs_raid_burst_refresh
                catch_up_burst = needs_raid_burst_refresh(published)
            if replay_published or catch_up_burst:
                cycle = published
        allowed = PUBLISHING + (('published',) if replay_published or catch_up_burst else ())
        if cycle is None or cycle.phase not in allowed or not cycle.collected_at:
            return None
        due = max(ranking_slot(cycle.slot), cycle.collected_at)
        if not replay_published and (due > now or (cycle.next_retry_at and cycle.next_retry_at > now)):
            return None
        cycle.owner = getattr(task, '_wcl_cycle_owner', None) or uuid4().hex
        cycle.phase = 'aggregating'
        cycle.last_attempt_at = now
        cycle.next_retry_at = None
        cycle.save(update_fields=['owner', 'phase', 'last_attempt_at', 'next_retry_at'])
        return cycle


def finish_publication(cycle):
    with fenced_cycle(cycle, phases=('aggregating',)) as current:
        _save(current, cycle, phase='published', published_at=timezone.now(), next_retry_at=None, error='')


@contextmanager
def legacy_publication(season):
    """Explicit migration compatibility only; never race the first new cycle."""
    with transaction.atomic():
        SeasonMeta.objects.select_for_update().get(pk=season.pk)
        if WclCollectionCycle.objects.filter(season=season).exists():
            raise CycleOwnershipLost('legacy publication cannot bypass a collection cycle')
        yield


def scheduled_cycle_due_at(task, now):
    """Only persisted tasks opt into DB state; simple due-time callers stay pure."""
    if not isinstance(task, MonitorTask) or task._state.adding or not task.pk:
        return NO_OVERRIDE
    if task.name not in ('SpecDetailRankingMonitor', 'SpecDetailAggregationMonitor'):
        return NO_OVERRIDE
    season = SeasonMeta.objects.filter(is_active=True).first()
    if not season:
        return NO_OVERRIDE
    cycle = active_cycle(season.pk)
    if task.name == 'SpecDetailRankingMonitor':
        if cycle:
            if cycle.phase not in COLLECTING:
                return None
            due = cycle.next_retry_at or cycle.last_attempt_at
            return due if due <= now else None
        # Before the first durable claim, preserve the legacy daily-slot contract.
        if not WclCollectionCycle.objects.filter(season=season).exists():
            return NO_OVERRIDE
        slot = ranking_slot(now)
        # last_scan_time is a CLAIM, never completion evidence.
        return None if WclCollectionCycle.objects.filter(
            season=season, slot__gte=slot, slot__lt=slot + timedelta(days=1)).exists() else slot
    if cycle and cycle.phase in PUBLISHING:
        due = max(ranking_slot(cycle.slot),
                  cycle.collected_at or cycle.last_attempt_at,
                  cycle.next_retry_at or cycle.slot)
        if due <= now:
            return due
        return None
    if cycle is None:
        published = WclCollectionCycle.objects.filter(season=season, phase='published').order_by('-slot').first()
        if published:
            from botend.services.raid_burst_snapshot import needs_raid_burst_refresh
            if needs_raid_burst_refresh(published):
                # 新功能在当天发布完成后部署，也会补建；不受 last_scan_time 的已领取标记抑制。
                due = max(ranking_slot(published.slot), published.collected_at)
                return due if due <= now else None
    # The independent leaderboard can run daily; WCL publication still requires ready.
    return NO_OVERRIDE


def fence_claimed_cycle(task, *, claim_time):
    """Called by scheduler under task->lease locks, before returning a claim.

    Persist the first ranking claim and change the cycle token immediately, not
    when the replacement worker starts, so crashes remain resumable and the
    previous worker cannot commit in that interval.
    """
    if task.name not in ('SpecDetailRankingMonitor', 'SpecDetailAggregationMonitor'):
        return
    season = SeasonMeta.objects.filter(is_active=True).first()
    if not season:
        return
    SeasonMeta.objects.select_for_update().get(pk=season.pk)
    cycle = active_cycle(season.pk, lock=True)
    phases = COLLECTING if task.name == 'SpecDetailRankingMonitor' else PUBLISHING
    task._wcl_cycle_owner = uuid4().hex
    if cycle is None and task.name == 'SpecDetailRankingMonitor':
        slot = ranking_slot(claim_time)
        if WclCollectionCycle.objects.filter(
                season=season, slot__gte=slot, slot__lt=slot + timedelta(days=1)).exists():
            return
        cycle, _ = WclCollectionCycle.objects.get_or_create(
            season=season, slot=slot,
            defaults={'owner': task._wcl_cycle_owner, 'started_at': claim_time,
                      'last_attempt_at': claim_time})
    if cycle and cycle.phase in phases:
        WclCollectionCycle.objects.filter(pk=cycle.pk).update(owner=task._wcl_cycle_owner)
