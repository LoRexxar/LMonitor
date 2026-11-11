"""Central raw log facts. Readers never fetch, and never trust profile attributes."""
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timedelta
from queue import Empty, SimpleQueue
import math
import re
import time

from django.db.models import Q

from botend.constants.wow import resolve_spec_identity
from botend.models import WclCombatantFetchState, WowItemSnapshot
from botend.services import wcl_combatant_store
from botend.services.wcl_raw_staging import load_staged_events, stage_events, discard_staged_events, record_duration


COMBATANT_SAVE_TIMINGS = (
    'validation_seconds', 'staging_seconds', 'staging_prepare_seconds',
    'staging_lock_wait_seconds', 'staging_lock_hold_seconds', 'staging_write_seconds',
    'fact_transaction_seconds', 'fact_lookup_seconds', 'fact_write_seconds', 'fact_readback_seconds',
    'fetch_state_seconds', 'staging_cleanup_seconds',
)


# WCL CombatantInfo uses zero-based equipment slots, not InventoryType IDs.
WCL_SLOTS = ('head', 'neck', 'shoulder', 'shirt', 'chest', 'waist', 'legs', 'feet',
             'wrist', 'hands', 'finger1', 'finger2', 'trinket1', 'trinket2',
             'back', 'main_hand', 'off_hand', 'tabard')


def _positive_int(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value) if str(value).isdigit() and int(value) > 0 else None
    except (ValueError, TypeError):
        return None


def _realm(value):
    return re.sub(r'[\s-]+', '', str(value or '')).casefold()


def _fight_key(record):
    code, fight = record.get('report_code'), _positive_int(record.get('fight_id'))
    return (code, fight) if isinstance(code, str) and code and fight else None


def match_combatant(events, record, class_name, spec_name):
    """Require name + nonempty normalized server + canonical spec, uniquely."""
    try:
        spec_id = resolve_spec_identity(class_name=class_name, spec_name=spec_name)[0]
    except ValueError:
        return None
    name = str(record.get('character_name') or '').strip().casefold()
    realm = _realm(record.get('realm'))
    if not name or name == 'anonymous' or not realm:
        return None
    matches = []
    for event in events:
        if not isinstance(event, dict):
            continue
        source = event.get('source') or {}
        if not isinstance(source, dict):
            continue
        if (_positive_int(event.get('specID')) == spec_id
                and str(source.get('name') or '').strip().casefold() == name
                and _realm(source.get('server')) == realm
                and _positive_int(source.get('id')) == _positive_int(event.get('sourceID'))
                and _positive_int(event.get('sourceID'))):
            matches.append(event)
    return matches[0] if len(matches) == 1 else None


def _load_facts(records, *, fetched_before=None, identities_only=False, actor_ids_by_fight=None):
    return wcl_combatant_store.load_facts(
        records, fetched_before=fetched_before, identities_only=identities_only,
        actor_ids_by_fight=actor_ids_by_fight)


def _fact_read_batches(records, target_limit=100):
    """同场目标不拆批；按目标数控制常规批量，每批最多100场完整事实。

    单场目标超过上限时独占一批，只扩大轻量目标列表，不重复加载该场JSON。
    查询只传每场一个代表引用，避免在SQL范围构造中重复遍历全部目标。
    """
    groups = defaultdict(list)
    for row in records:
        groups[_fight_key(row)].append(row)
    observations, references = [], []
    for rows in groups.values():
        if observations and len(observations) + len(rows) > target_limit:
            yield observations, references
            observations, references = [], []
        observations.extend(rows)
        references.append(rows[0])
    if observations:
        yield observations, references


def combatant_stats(payload):
    """Raw event ratings (possibly buffed/negative); do not derive percentages."""
    fields = {'crit': ('critMelee', 'critSpell', 'critRanged'),
              'haste': ('hasteMelee', 'hasteSpell', 'hasteRanged'),
              'mastery': ('mastery',), 'versatility': ('versatilityDamageDone', 'versatility', 'versa')}
    result = {}
    for stat, keys in fields.items():
        for key in keys:
            value = payload.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                result[stat] = {'rating': value}
                break
    return result


def _enchant_catalog(events):
    ids = set()
    for event in events:
        for item in event.get('gear') or []:
            if isinstance(item, dict):
                for field in ('permanentEnchant', 'temporaryEnchant'):
                    if eid := _positive_int(item.get(field)):
                        ids.add(eid)
    from botend.services.wow_item_display import load_enchantment_catalog
    return load_enchantment_catalog(ids)


def combatant_gear(gear, enchant_catalog=None):
    if not isinstance(gear, list):
        return []
    result = []
    for index, raw in enumerate(gear):
        if not isinstance(raw, dict) or not _positive_int(raw.get('id')):
            continue
        item = deepcopy(raw)
        slot = raw.get('slot', index)
        if isinstance(slot, int) and not isinstance(slot, bool):
            slot = WCL_SLOTS[slot] if 0 <= slot < len(WCL_SLOTS) else 'unknown'
        item['slot'] = slot or 'unknown'
        item['gems_detail'] = [deepcopy(g) for g in raw.get('gems') or []
                               if isinstance(g, dict) and _positive_int(g.get('id'))]
        item['enchants_detail'] = []
        for field, kind in (('permanentEnchant', 'permanent'), ('temporaryEnchant', 'temporary')):
            eid = _positive_int(raw.get(field))
            if not eid:
                continue
            detail = {'enchantment_id': eid, 'kind': kind}
            meta = (enchant_catalog or {}).get(eid)
            if meta:
                detail.update(id=meta['item_id'], name=meta['name_zh'] or meta['name'], icon=meta['icon'])
            item['enchants_detail'].append(detail)
        result.append(item)
    return result


def matched_facts(records, facts, class_name, spec_name):
    """按样本划分日志内容；保留唯一身份匹配的角色，不携带其他队友详情。"""
    selected = defaultdict(dict)
    for row in records:
        fight = _fight_key(row)
        event = match_combatant(facts.get(fight, []), row, class_name, spec_name)
        if event is not None:
            selected[fight][int(event['sourceID'])] = event
    return {key: [events[actor] for actor in sorted(events)] for key, events in sorted(selected.items())}


def load_matched_facts(records, class_name, spec_name, *, fetched_before=None):
    """先核对整场轻量身份，再读取样本角色的属性、装备和天赋。"""
    records = list(records)
    identities = _load_facts(records, fetched_before=fetched_before, identities_only=True)
    selected = matched_facts(records, identities, class_name, spec_name)
    actor_scopes = {key: [int(event['sourceID']) for event in events] for key, events in selected.items()}
    del identities, selected
    if not actor_scopes:
        return {}
    return _load_facts(records, fetched_before=fetched_before, actor_ids_by_fight=actor_scopes)


def enrich_dungeon_records(records, class_name, spec_name, *, facts=None):
    """Return independent dicts; stats/race exclusively from the exact WCL log.

    Missing/ambiguous facts keep ranking WCL gear, but never profile stats/race.
    This is a bounded DB-only projection; collection is an explicit separate job.
    """
    records = list(records)
    if facts is None:
        facts = _load_facts(records)
    matched = [match_combatant(facts.get(_fight_key(row), []), row, class_name, spec_name)
               for row in records]
    catalog = _enchant_catalog([{'gear': (event or {}).get('gear') or row.get('gear_json') or []}
                                for row, event in zip(records, matched)])
    result = []
    for original, event in zip(records, matched):
        row = deepcopy(original)
        row['stats_json'] = {}
        row['race'] = ''
        row.pop('wcl_actor_id', None)
        # Old rankings remain a WCL source; resolve only their explicit enchant IDs.
        for item in row.get('gear_json') or []:
            if isinstance(item, dict) and (item.get('permanentEnchant') or item.get('temporaryEnchant')):
                item['enchants_detail'] = combatant_gear([item], catalog)[0]['enchants_detail'] if _positive_int(item.get('id')) else []
        if event:
            row['wcl_actor_id'] = event['sourceID']
            row['stats_json'] = combatant_stats(event)
            if event.get('race'):
                row['race'] = deepcopy(event['race'])
            if event.get('talentTree'):
                from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
                talents = SpecDetailBase.parse_wcl_talent_tree(event['talentTree'])
                if talents:
                    row['talents_json'] = talents
            gear = combatant_gear(event.get('gear'), catalog)
            if gear:
                row['gear_json'] = gear
        result.append(row)
    return result


def select_dungeon_combatant_records(season, identities, dungeon_ids=None):
    """Reuse the public statistics selector, retaining only collection identities."""
    from botend.models import SpecDungeonRanking
    from botend.services.spec_stats_service import _select_dungeon_sample_records, _dungeon_log_identity
    active_ids = {int(enc['id']) for enc in season.mplus_encounters or []}
    ids = active_ids if dungeon_ids is None else active_ids.intersection(dungeon_ids)
    records, seen = [], set()
    for dungeon_id in sorted(ids):
        for class_name, spec_name in identities:
            qs = SpecDungeonRanking.objects.filter(season_id=season.id, dungeon_id=dungeon_id,
                                                   class_name=class_name, spec_name=spec_name)
            for selected in _select_dungeon_sample_records(
                    qs, max_samples=100,
                    hydrate_fields=('id', 'report_code', 'fight_id', 'character_name', 'realm', 'region')):
                identity = _dungeon_log_identity(selected)
                if identity in seen:
                    continue
                seen.add(identity)
                records.append({key: selected.get(key) for key in (
                    'id', 'report_code', 'fight_id', 'character_name', 'realm', 'region')})
                records[-1].update(class_name=class_name, spec_name=spec_name)
    return records


def _validated_events(events, fight_ids):
    """Keep usable players from a complete response, without inventing facts.

    Known players with absent name/server/spec are unavailable for exact
    character/spec statistics, not a failure of the other players. Broken
    actor joins, conflicting events and malformed fields still fail the batch
    before immutable inserts or negative caching. Both raid and M+ use this
    contract; optional missing fields do not discard an otherwise usable actor.
    """
    if not isinstance(events, list):
        raise ValueError('CombatantInfo events must be a list')
    by_fight = defaultdict(dict)
    seen = {}
    for event in events:
        if not isinstance(event, dict):
            raise ValueError('Invalid CombatantInfo event')
        source = event.get('source') or {}
        # Missing masterData/actor joins are not evidence of unavailable data.
        if isinstance(source, dict) and source.get('type') in ('NPC', 'Pet'):
            continue
        fight = _positive_int(event.get('fight'))
        actor = _positive_int(event.get('sourceID'))
        if fight not in fight_ids or not actor or not isinstance(source, dict) or _positive_int(source.get('id')) != actor:
            raise ValueError('CombatantInfo fight/actor identity missing or inconsistent')
        key = (fight, actor)
        if key in seen and seen[key] != event:
            raise ValueError('Conflicting CombatantInfo events for one actor')
        seen[key] = event
        name, server = source.get('name'), source.get('server')
        if any(value is not None and not isinstance(value, str) for value in (name, server)):
            raise ValueError('Invalid CombatantInfo player name/server type')
        spec = event.get('specID')
        missing_spec = (spec is None or (isinstance(spec, str) and not spec.strip())
                        or (type(spec) is int and spec == 0))
        if not missing_spec and not _positive_int(spec):
            raise ValueError('Invalid CombatantInfo player spec')
        # Validate before skipping unavailable players: corrupt containers must
        # not be hidden by one missing identity field.
        for field in ('talents', 'talentTree', 'gear'):
            value = event.get(field)
            if value is not None and (not isinstance(value, list)
                                      or any(not isinstance(item, dict) for item in value)):
                raise ValueError(f'Invalid CombatantInfo {field}')
        if (not name or not name.strip() or name.strip().casefold() == 'anonymous'
                or not _realm(server) or missing_spec):
            if source.get('type') == 'Player':
                continue
            raise ValueError('CombatantInfo player name/server/spec identity incomplete')
        by_fight[fight][actor] = event
    return {fight: list(actors.values()) for fight, actors in by_fight.items()}


# Retry policy, not an estimate of WCL request cost or account quota.
WCL_PERMISSION_DENIED_TTL = timedelta(hours=1)
WCL_COMBATANT_MISSING_TTL = timedelta(hours=6)
WCL_TRANSIENT_RETRY_DELAY = timedelta(minutes=5)
WCL_TRANSIENT_FAILURE_LIMIT = 3


def _load_negative_fetch_states(records, now):
    """Read only active states for a bounded preflight batch and its reports."""
    keys = {key for row in records if (key := _fight_key(row))}
    if not keys:
        return {}
    keys.update((code, 0) for code, _ in tuple(keys))
    where = Q()
    for code, fight in sorted(keys):
        where |= Q(report_code=code, fight_id=fight)
    return {(row['report_code'], row['fight_id']): row
            for row in WclCombatantFetchState.objects.filter(where, retry_after__gt=now).values(
                'report_code', 'fight_id', 'status', 'retry_after')}


def _save_negative_fetch_state(code, fight, status, checked_at):
    ttl = WCL_PERMISSION_DENIED_TTL if status == 'permission_denied' else WCL_COMBATANT_MISSING_TTL
    WclCombatantFetchState.objects.update_or_create(
        report_code=code, fight_id=fight,
        defaults={'status': status, 'checked_at': checked_at, 'retry_after': checked_at + ttl})


def _defer_transient_report(code, retry_at=None):
    """持久化报告级短期退避，不把网络故障记作日志缺失或覆盖有效权限状态。"""
    from django.db import transaction
    from django.utils import timezone
    now = timezone.now()
    deadline = now + WCL_TRANSIENT_RETRY_DELAY
    if isinstance(retry_at, datetime) and timezone.is_aware(retry_at):
        deadline = max(deadline, retry_at)
    with transaction.atomic():
        state, created = WclCombatantFetchState.objects.select_for_update().get_or_create(
            report_code=code, fight_id=0, defaults={
                'status': 'transient_error', 'checked_at': now, 'retry_after': deadline})
        if not created:
            if state.status == 'permission_denied' and state.retry_after > now:
                return state.retry_after
            state.status = 'transient_error'
            state.checked_at = now
            state.retry_after = max(deadline, state.retry_after)
            state.save(update_fields=['status', 'checked_at', 'retry_after'])
        return state.retry_after


def _is_transient_error(error):
    return error == 'network' or bool(re.fullmatch(r'http_5\d\d', str(error)))


def _collection_worker_limit(fetcher):
    from django.conf import settings
    from botend.services.wcl_api_budget import collection_worker_limit
    client_id = (getattr(settings, 'WCL_V2_CONFIG', {}) or {}).get('client_id')
    return max(1, min(16, int(collection_worker_limit(client_id))))


def _fetch_combatant_batch(fetcher, code, batch, *, factory=None):
    """Workers perform transport only; no central fact writes or shared errors."""
    from django.db import close_old_connections, connections
    from botend.services.wcl_api_budget import business_requests_sent
    sent_token = business_requests_sent.set(0)
    started = time.monotonic()
    try:
        staged = load_staged_events(code, batch, with_fetched_at=True)
        if staged is not None:
            events, fetched_at_by_fight = staged
            return events, '', None, time.monotonic() - started, fetched_at_by_fight
        if factory is not None:
            close_old_connections()
            fetcher = factory()
        events = fetcher.fetch_wcl_combatant_info(code, batch)
        error = getattr(fetcher, '_wcl_last_error', '')
        if error == 'admission_busy' and business_requests_sent.get():
            # Pagination may already have sent HTTP. Defer, never replay it.
            error = 'admission_deferred'
        from django.utils import timezone
        fetched_at = timezone.now()
        return (events, error, getattr(fetcher, '_wcl_retry_at', None), time.monotonic() - started,
                {fight: fetched_at for fight in batch})
    except Exception:
        # No exception bodies, report codes, player names, or credentials in logs.
        return None, 'worker_exception', None, time.monotonic() - started, {}
    finally:
        business_requests_sent.reset(sent_token)
        if factory is not None:
            connections.close_all()


def _persist_combatant_batch(code, batch, batch_rows, events, *, fetched_at_by_fight=None, timings=None,
                             matched_rows=None):
    """Validate a complete batch and read back immutable facts on the caller."""
    from django.db import transaction
    from django.utils import timezone
    with record_duration(timings, 'validation_seconds'):
        by_fight = _validated_events(events, batch)
    now = timezone.now()
    fetched_at_by_fight = fetched_at_by_fight or {}
    # Include authoritative empty fights, so recovery is all-or-nothing.
    with record_duration(timings, 'staging_seconds'):
        staged = stage_events(code, {fight: by_fight.get(fight, []) for fight in batch}, fetched_at=now,
                              fetched_at_by_fight=fetched_at_by_fight, timings=timings)
    if staged['status'] != 'stored':
        import logging
        logging.getLogger(__name__).warning('WCL raw staging skipped status=%s', staged['status'])
    with record_duration(timings, 'fact_transaction_seconds'), transaction.atomic():
        # Collection has always been insert-only: an already recorded actor
        # wins even if a later source response differs. Keep that contract at
        # this boundary; the store's explicit migration/conflict checks remain
        # strict for its other callers. No new teammate is dropped.
        with record_duration(timings, 'fact_lookup_seconds'):
            existing = wcl_combatant_store.load_facts(
                [{'report_code': code, 'fight_id': fight} for fight in batch], identities_only=True)
        recorded = {fight: {event['sourceID'] for event in payloads}
                    for (_, fight), payloads in existing.items()}
        new_events = {fight: [event for event in payloads
                             if event['sourceID'] not in recorded.get(fight, set())]
                      for fight, payloads in by_fight.items()}
        with record_duration(timings, 'fact_write_seconds'):
            written = wcl_combatant_store.persist_events(
                code, new_events, fetched_at=now, fetched_at_by_fight=fetched_at_by_fight)
    matched, missing = 0, 0
    missing_fights = set()
    with record_duration(timings, 'fact_readback_seconds'):
        for observations, references in _fact_read_batches(batch_rows):
            persisted = _load_facts(references, identities_only=True)
            for row in observations:
                key = _fight_key(row)
                if match_combatant(persisted.get(key, []), row, row['class_name'], row['spec_name']):
                    matched += 1
                    if matched_rows is not None:
                        matched_rows.append(row)
                else:
                    missing += 1
                    missing_fights.add(key[1])
            del persisted
    with record_duration(timings, 'fetch_state_seconds'), transaction.atomic():
        WclCombatantFetchState.objects.filter(
            report_code=code, fight_id__in=[0, *sorted(set(batch) - missing_fights)]).delete()
        for fight in sorted(missing_fights):
            _save_negative_fetch_state(code, fight, 'missing', fetched_at_by_fight.get(fight, now))
    # Do not extend a negative-cache window with successful stale raw responses.
    def cleanup():
        with record_duration(timings, 'staging_cleanup_seconds'):
            discard_staged_events(code, batch)
    transaction.on_commit(cleanup)
    return written, matched, missing


def collect_dungeon_combatants(records, fetcher=None, *, apply=False, batch_size=20, log=None, progress=None,
                              admission_check=None, on_positive=None):
    """Explicit offline collection. At most 20 fights/report/request.

    Counts partition observations, not API responses; negative_cached is only a
    diagnostic subset. Exact positive facts always win over expiring negatives.
    Raw facts stay immutable and centralized. Dry runs never write or fetch.
    Only explicitly isolated fetchers run concurrently; validation and immutable
    ORM persistence stay on the caller. Progress is aggregate and identifier-free.
    """
    from django.utils import timezone
    if not 1 <= batch_size <= 20:
        raise ValueError('batch_size must be between 1 and 20')
    records = list(records)
    counts = dict(selected=len(records), cached=0, matched=0, missing=0,
                  permission_denied=0, failed=0, pending=0, snapshots_written=0,
                  reports_requested=0, fights_requested=0, negative_cached=0,
                  transient_errors=0, transient_cached=0, deferred_reports=0, circuit_open=0)
    pending = defaultdict(lambda: defaultdict(list))
    deferred_reports = set()
    local_retry_at = fatal_retry_at = None

    def remember_local_retry(deadline):
        nonlocal local_retry_at
        local_retry_at = min(local_retry_at, deadline) if local_retry_at else deadline

    def sync_retry():
        # 账户级暂停期限优先；只有局部故障时按最早可恢复的报告调度。
        deadline = fatal_retry_at or local_retry_at
        if deadline is not None:
            counts['retry_at'] = deadline.isoformat()

    # 以战斗为单位共享事实，避免按样本切片反复读回并解析整场JSON。
    preflight_started = preflight_logged = time.monotonic()
    counts.update(preflight_batches=0, preflight_fights=0, preflight_fact_rows=0,
                  preflight_load_seconds=0.0, preflight_state_seconds=0.0,
                  preflight_match_seconds=0.0, preflight_seconds=0.0)

    def emit_preflight():
        counts['preflight_seconds'] = time.monotonic() - preflight_started
        if log:
            fields = ('selected', 'cached', 'negative_cached', 'transient_cached', 'preflight_batches',
                      'preflight_fights', 'preflight_fact_rows', 'preflight_load_seconds',
                      'preflight_state_seconds', 'preflight_match_seconds', 'preflight_seconds')
            log('WCL combatants preflight ' + ' '.join(
                f'{key}={round(counts[key], 3) if isinstance(counts[key], float) else counts[key]}'
                for key in fields))

    for batch_records, references in _fact_read_batches(records):
        positive_rows = []
        started = time.monotonic()
        facts = _load_facts(references, identities_only=True)
        counts['preflight_load_seconds'] += time.monotonic() - started
        counts['preflight_fact_rows'] += sum(len(rows) for rows in facts.values())
        counts['preflight_fights'] += sum(_fight_key(row) is not None for row in references)
        counts['preflight_batches'] += 1
        started = time.monotonic()
        states = _load_negative_fetch_states(references, timezone.now())
        counts['preflight_state_seconds'] += time.monotonic() - started
        started = time.monotonic()
        for row in batch_records:
            key = _fight_key(row)
            report_state = states.get((key[0], 0), {}) if key else {}
            fight_state = states.get(key, {})
            if not key or not row.get('realm') or not row.get('character_name') or str(row['character_name']).casefold() == 'anonymous':
                counts['missing'] += 1
            elif match_combatant(facts.get(key, []), row, row['class_name'], row['spec_name']):
                counts['cached'] += 1
                if on_positive:
                    positive_rows.append(row)
            elif report_state.get('status') == 'permission_denied':
                counts['permission_denied'] += 1
                counts['negative_cached'] += 1
            elif fight_state.get('status') == 'missing':
                counts['missing'] += 1
                counts['negative_cached'] += 1
            elif report_state.get('status') == 'transient_error':
                counts['transient_cached'] += 1
                deferred_reports.add(key[0])
                remember_local_retry(report_state['retry_after'])
            else:
                pending[key[0]][key[1]].append(row)
        counts['preflight_match_seconds'] += time.monotonic() - started
        if on_positive and positive_rows:
            on_positive(positive_rows)
        del facts, states
        if time.monotonic() - preflight_logged >= 30:
            emit_preflight()
            preflight_logged = time.monotonic()
    emit_preflight()
    counts['deferred_reports'] = len(deferred_reports)
    counts['pending'] = counts['selected'] - sum(counts[key] for key in (
        'cached', 'matched', 'missing', 'permission_denied', 'failed'))
    sync_retry()
    if not apply:
        return counts
    if not pending:
        return counts
    if admission_check:
        admission_check()
    if fetcher is None:
        from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
        fetcher = SpecDetailBase(None, None)
    # Only an explicit class-level factory opts a fetcher into threading. In
    # particular, dynamic Mock attributes or arbitrary bound methods are NOT a
    # promise of thread safety. Legacy callers execute on the calling thread.
    prepare = getattr(type(fetcher), 'prepare_combatant_worker_factory', None)
    factory = prepare(fetcher) if prepare else None
    workers = 1
    ready = deque(pending)
    remaining = {code: deque(sorted(fights)) for code, fights in pending.items()}
    requested_reports = set()
    inflight = {}
    completed_responses = SimpleQueue()
    stopped = False
    fatal_error = ''
    consecutive_transient = 0
    admission_parked = []
    admission_rounds = 0
    admission_last_completed = -1
    started = last_progress = time.monotonic()
    counts.update(batches_completed=0, batches_dispatched=0, fetch_seconds=0.0, save_seconds=0.0)
    counts.update({key: 0.0 for key in COMBATANT_SAVE_TIMINGS})

    def stop_collection(error, deadline=None):
        nonlocal stopped, fatal_error, fatal_retry_at
        stopped = True
        fatal_error = fatal_error or error
        if isinstance(deadline, datetime):
            fatal_retry_at = max(fatal_retry_at, deadline) if fatal_retry_at else deadline
        sync_retry()

    def emit_progress(force=False):
        nonlocal last_progress
        counts['pending'] = counts['selected'] - sum(counts[key] for key in (
            'cached', 'matched', 'missing', 'permission_denied', 'failed'))
        counts['deferred_reports'] = len(deferred_reports)
        sync_retry()
        now = time.monotonic()
        if not force and now - last_progress < 30:
            return
        last_progress = now
        counts.update(workers=workers, in_flight=len(inflight),
                      undispatched_batches=sum((len(fights) + batch_size - 1) // batch_size
                                               for fights in remaining.values()),
                      elapsed_seconds=round(now - started, 3))
        if log:
            fields = ('batches_completed', 'batches_dispatched', 'undispatched_batches',
                      'in_flight', 'workers', 'pending', 'matched', 'missing',
                      'permission_denied', 'failed', 'transient_errors', 'transient_cached',
                      'deferred_reports', 'circuit_open', 'fetch_seconds', 'save_seconds', 'elapsed_seconds') + COMBATANT_SAVE_TIMINGS
            log('WCL combatants progress ' + ' '.join(f'{key}={counts[key]}' for key in fields))
        if progress:
            progress(dict(counts))

    # One batch per report in flight prevents permission denial from racing
    # another batch of the same report. Other reports remain independently live.
    # Only <= workers futures are submitted, never one future per input batch.
    with ThreadPoolExecutor(max_workers=16, thread_name_prefix='wcl-combatants') as pool:
        while ready or inflight or admission_parked:
            if admission_parked and not inflight and not stopped:
                # Drain the wave before retrying *unsent* batches. Retry only
                # while facts progress, with a hard round cap; external owners
                # get a short resumable defer rather than a hot spin/90s lease.
                completed = counts['matched'] + counts['missing'] + counts['permission_denied']
                if admission_rounds >= 16 or completed == admission_last_completed:
                    stop_collection('admission_busy', timezone.now() + timedelta(seconds=1))
                else:
                    admission_rounds += 1
                    admission_last_completed = completed
                    ready.extend(admission_parked)
                    admission_parked.clear()
            if factory and not stopped:
                try:
                    workers = _collection_worker_limit(fetcher)
                except Exception as exc:
                    # A broken control/budget read cannot justify dispatching
                    # on a stale limit. Still persist already in-flight success.
                    stopped = True
                    if log:
                        log(f'WCL combatants control_failed error_type={type(exc).__name__}')
            if not counts['batches_dispatched']:
                emit_progress(force=True)
            while ready and not stopped and not admission_parked and len(inflight) < workers:
                if admission_check:
                    admission_check()
                # Observe completed failures before filling another slot.
                if any(future.done() for future in inflight):
                    break
                code = ready.popleft()
                batch = [remaining[code].popleft() for _ in range(min(batch_size, len(remaining[code])))]
                batch_rows = [row for fight in batch for row in pending[code][fight]]
                if code not in requested_reports:
                    requested_reports.add(code)
                    counts['reports_requested'] += 1
                counts['fights_requested'] += len(batch)
                counts['batches_dispatched'] += 1
                if factory:
                    future = pool.submit(_fetch_combatant_batch, None, code, batch, factory=factory)
                else:
                    future = Future()
                    future.set_result(_fetch_combatant_batch(fetcher, code, batch))
                inflight[future] = (code, batch, batch_rows)
                future.add_done_callback(completed_responses.put)
            if not inflight:
                break
            wait(inflight, timeout=1, return_when=FIRST_COMPLETED)
            # 按响应完成回调的接收顺序处理，避免无序集合误判连续故障。
            done = []
            while True:
                try:
                    done.append(completed_responses.get_nowait())
                except Empty:
                    break
            for future in done:
                # Admission failures propagate to the cycle owner, not into
                # failed/negative source facts. In-flight requests are drained
                # by the executor without dispatching or publishing more work.
                if admission_check:
                    admission_check()
                code, batch, batch_rows = inflight.pop(future)
                try:
                    events, error, retry_at, fetch_seconds, fetched_at_by_fight = future.result()
                except Exception:
                    events, error, retry_at, fetch_seconds, fetched_at_by_fight = None, 'worker_exception', None, 0.0, {}
                counts['batches_completed'] += 1
                counts['fetch_seconds'] += fetch_seconds  # summed worker wall, not elapsed throughput
                save_started = time.monotonic()
                try:
                    if events is not None:
                        positive_rows = []
                        written, matched, missing = _persist_combatant_batch(
                            code, batch, batch_rows, events, fetched_at_by_fight=fetched_at_by_fight, timings=counts,
                            matched_rows=positive_rows if on_positive else None)
                        if on_positive and positive_rows:
                            from django.db import transaction
                            # 只有事实事务提交后，断点才允许跳过这些样本。
                            transaction.on_commit(lambda rows=positive_rows: on_positive(rows))
                        counts['snapshots_written'] += written
                        counts['matched'] += matched
                        counts['missing'] += missing
                        consecutive_transient = 0
                    elif _is_transient_error(error):
                        deadline = _defer_transient_report(code, retry_at)
                        remember_local_retry(deadline)
                        deferred_reports.add(code)
                        remaining[code].extendleft(reversed(batch))
                        counts['transient_errors'] += 1
                        consecutive_transient += 1
                        if consecutive_transient >= WCL_TRANSIENT_FAILURE_LIMIT:
                            counts['circuit_open'] = 1
                            stop_collection(error, timezone.now() + WCL_TRANSIENT_RETRY_DELAY)
                    elif error == 'admission_busy':
                        remaining[code].extendleft(reversed(batch))
                        admission_parked.append(code)
                    elif error == 'admission_deferred':
                        # Its short deadline may pass while other HTTP drains.
                        # It is still pending, not a failed/empty observation.
                        retry_at = max(retry_at, timezone.now() + timedelta(seconds=1))
                        stop_collection(error, retry_at)
                    elif isinstance(retry_at, datetime) and retry_at > timezone.now():
                        stop_collection(error, retry_at)
                    elif error == 'permission':
                        consecutive_transient = 0
                        _save_negative_fetch_state(code, 0, 'permission_denied', timezone.now())
                        counts['permission_denied'] += len(batch_rows) + sum(
                            len(pending[code][fight]) for fight in remaining[code])
                        remaining[code].clear()
                    else:
                        counts['failed'] += len(batch_rows)
                        if error in {'rate_limit', 'budget_wait', 'authentication', 'http_403', 'worker_exception'}:
                            stop_collection(error)
                except Exception as exc:
                    from django.db import DatabaseError
                    counts['failed'] += len(batch_rows)
                    if isinstance(exc, DatabaseError):
                        stop_collection('storage_error')
                    if log:
                        log(f'WCL combatants batch_failed error_type={type(exc).__name__}')
                finally:
                    counts['save_seconds'] += time.monotonic() - save_started
                if remaining[code] and code not in admission_parked and code not in deferred_reports:
                    ready.append(code)
            emit_progress()
            if stopped and not inflight:
                break
        # Publish aggregate fatal state only on the caller, after draining.
        # Successful workers must not erase an earlier stop/retry decision.
        if factory:
            fetcher._wcl_last_error = fatal_error
            fetcher._wcl_retry_at = fatal_retry_at
        emit_progress(force=True)
    return counts
