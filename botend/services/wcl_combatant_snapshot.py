"""Central raw log facts. Readers never fetch, and never trust profile attributes."""
from collections import defaultdict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timedelta
import math
import re
import time

from django.db.models import Q

from botend.constants.wow import resolve_spec_identity
from botend.models import WclCombatantFetchState, WclCombatantSnapshot, WowItemSnapshot


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


def _load_facts(records, *, fetched_before=None):
    keys = sorted({key for row in records if (key := _fight_key(row))})
    facts = defaultdict(list)
    for start in range(0, len(keys), 100):
        where = Q()
        for code, fight in keys[start:start + 100]:
            where |= Q(report_code=code, fight_id=fight)
        queryset = WclCombatantSnapshot.objects.filter(where)
        if fetched_before is not None:
            queryset = queryset.filter(fetched_at__lte=fetched_before)
        for row in queryset.order_by('report_code', 'fight_id', 'actor_id').values(
                'report_code', 'fight_id', 'actor_id', 'payload_json'):
            payload = row['payload_json']
            if isinstance(payload, dict) and _positive_int(payload.get('sourceID')) == row['actor_id']:
                facts[(row['report_code'], row['fight_id'])].append(payload)
    return facts


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
    matches = defaultdict(list)
    if ids:
        for row in WowItemSnapshot.objects.filter(enchantment_id__in=ids).values(
                'enchantment_id', 'item_id', 'name', 'name_zh', 'icon'):
            matches[row['enchantment_id']].append(row)
    # A many-to-one enchantment/scroll mapping cannot prove a particular item.
    return {eid: rows[0] for eid, rows in matches.items() if len(rows) == 1}


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
            if not row.get('talents_json') and event.get('talentTree'):
                from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
                row['talents_json'] = SpecDetailBase.parse_wcl_talent_tree(event['talentTree'])
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


def _load_negative_fetch_states(records, now):
    """Read only active states for a bounded preflight batch and its reports."""
    keys = {key for row in records if (key := _fight_key(row))}
    if not keys:
        return {}
    keys.update((code, 0) for code, _ in tuple(keys))
    where = Q()
    for code, fight in sorted(keys):
        where |= Q(report_code=code, fight_id=fight)
    return {(row['report_code'], row['fight_id']): row['status']
            for row in WclCombatantFetchState.objects.filter(where, retry_after__gt=now).values(
                'report_code', 'fight_id', 'status')}


def _save_negative_fetch_state(code, fight, status, checked_at):
    ttl = WCL_PERMISSION_DENIED_TTL if status == 'permission_denied' else WCL_COMBATANT_MISSING_TTL
    WclCombatantFetchState.objects.update_or_create(
        report_code=code, fight_id=fight,
        defaults={'status': status, 'checked_at': checked_at, 'retry_after': checked_at + ttl})


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
        if factory is not None:
            close_old_connections()
            fetcher = factory()
        events = fetcher.fetch_wcl_combatant_info(code, batch)
        error = getattr(fetcher, '_wcl_last_error', '')
        if error == 'admission_busy' and business_requests_sent.get():
            # Pagination may already have sent HTTP. Defer, never replay it.
            error = 'admission_deferred'
        return events, error, getattr(fetcher, '_wcl_retry_at', None), time.monotonic() - started
    except Exception:
        # No exception bodies, report codes, player names, or credentials in logs.
        return None, 'worker_exception', None, time.monotonic() - started
    finally:
        business_requests_sent.reset(sent_token)
        if factory is not None:
            connections.close_all()


def _persist_combatant_batch(code, batch, batch_rows, events):
    """Validate a complete batch and read back immutable facts on the caller."""
    from django.db import transaction
    from django.utils import timezone
    by_fight = _validated_events(events, batch)
    now = timezone.now()
    written = 0
    with transaction.atomic():
        for fight, payloads in by_fight.items():
            for payload in payloads:
                _, created = WclCombatantSnapshot.objects.get_or_create(
                    report_code=code, fight_id=fight, actor_id=payload['sourceID'],
                    defaults={'payload_json': payload, 'fetched_at': now})
                written += int(created)
    matched, missing = 0, 0
    missing_fights = set()
    for offset in range(0, len(batch_rows), 100):
        observations = batch_rows[offset:offset + 100]
        persisted = _load_facts(observations)
        for row in observations:
            key = _fight_key(row)
            if match_combatant(persisted.get(key, []), row, row['class_name'], row['spec_name']):
                matched += 1
            else:
                missing += 1
                missing_fights.add(key[1])
        del persisted
    with transaction.atomic():
        WclCombatantFetchState.objects.filter(
            report_code=code, fight_id__in=[0, *sorted(set(batch) - missing_fights)]).delete()
        for fight in sorted(missing_fights):
            _save_negative_fetch_state(code, fight, 'missing', now)
    return written, matched, missing


def collect_dungeon_combatants(records, fetcher=None, *, apply=False, batch_size=20, log=None, progress=None):
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
                  reports_requested=0, fights_requested=0, negative_cached=0)
    pending = defaultdict(lambda: defaultdict(list))
    # All-spec repair can contain tens of thousands of observations. Inspect
    # facts and retry states together, never hydrate all raw events together.
    for start in range(0, len(records), 100):
        batch_records = records[start:start + 100]
        facts = _load_facts(batch_records)
        states = _load_negative_fetch_states(batch_records, timezone.now())
        for row in batch_records:
            key = _fight_key(row)
            if not key or not row.get('realm') or not row.get('character_name') or str(row['character_name']).casefold() == 'anonymous':
                counts['missing'] += 1
            elif match_combatant(facts.get(key, []), row, row['class_name'], row['spec_name']):
                counts['cached'] += 1
            elif states.get((key[0], 0)) == 'permission_denied':
                counts['permission_denied'] += 1
                counts['negative_cached'] += 1
            elif states.get(key) == 'missing':
                counts['missing'] += 1
                counts['negative_cached'] += 1
            else:
                pending[key[0]][key[1]].append(row)
        del facts, states
    if not apply:
        counts['pending'] = sum(len(rows) for fights in pending.values() for rows in fights.values())
        return counts
    if not pending:
        return counts
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
    stopped = False
    fatal_error, fatal_retry_at = '', None
    admission_parked = []
    admission_rounds = 0
    admission_last_completed = -1
    started = last_progress = time.monotonic()
    counts.update(batches_completed=0, batches_dispatched=0, fetch_seconds=0.0, save_seconds=0.0)

    def emit_progress(force=False):
        nonlocal last_progress
        counts['pending'] = counts['selected'] - sum(counts[key] for key in (
            'cached', 'matched', 'missing', 'permission_denied', 'failed'))
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
                      'permission_denied', 'failed', 'fetch_seconds', 'save_seconds', 'elapsed_seconds')
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
                    stopped = True
                    fatal_error = 'admission_busy'
                    fatal_retry_at = timezone.now() + timedelta(seconds=1)
                    counts['retry_at'] = fatal_retry_at.isoformat()
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
            if not inflight:
                break
            done, _ = wait(inflight, timeout=1, return_when=FIRST_COMPLETED)
            # Drain ALL already completed responses before any new dispatch.
            done.update(future for future in inflight if future.done())
            for future in done:
                code, batch, batch_rows = inflight.pop(future)
                try:
                    events, error, retry_at, fetch_seconds = future.result()
                except Exception:
                    events, error, retry_at, fetch_seconds = None, 'worker_exception', None, 0.0
                counts['batches_completed'] += 1
                counts['fetch_seconds'] += fetch_seconds  # summed worker wall, not elapsed throughput
                save_started = time.monotonic()
                try:
                    if events is not None:
                        written, matched, missing = _persist_combatant_batch(code, batch, batch_rows, events)
                        counts['snapshots_written'] += written
                        counts['matched'] += matched
                        counts['missing'] += missing
                    elif error == 'admission_busy':
                        remaining[code].extendleft(reversed(batch))
                        admission_parked.append(code)
                    elif error == 'admission_deferred':
                        # Its short deadline may pass while other HTTP drains.
                        # It is still pending, not a failed/empty observation.
                        stopped = True
                        retry_at = max(retry_at, timezone.now() + timedelta(seconds=1))
                        counts['retry_at'] = max(counts.get('retry_at', ''), retry_at.isoformat())
                        if fatal_retry_at is None or retry_at > fatal_retry_at:
                            fatal_error, fatal_retry_at = error, retry_at
                    elif isinstance(retry_at, datetime) and retry_at > timezone.now():
                        stopped = True
                        counts['retry_at'] = max(counts.get('retry_at', ''), retry_at.isoformat())
                        if fatal_retry_at is None or retry_at > fatal_retry_at:
                            fatal_error, fatal_retry_at = error, retry_at
                    elif error == 'permission':
                        _save_negative_fetch_state(code, 0, 'permission_denied', timezone.now())
                        counts['permission_denied'] += len(batch_rows) + sum(
                            len(pending[code][fight]) for fight in remaining[code])
                        remaining[code].clear()
                    else:
                        counts['failed'] += len(batch_rows)
                        if (error in {'rate_limit', 'budget_wait', 'authentication', 'network',
                                      'http_403', 'worker_exception'} or str(error).startswith('http_5')):
                            stopped = True
                            fatal_error = fatal_error or error
                except Exception as exc:
                    counts['failed'] += len(batch_rows)
                    if log:
                        log(f'WCL combatants batch_failed error_type={type(exc).__name__}')
                finally:
                    counts['save_seconds'] += time.monotonic() - save_started
                if remaining[code] and code not in admission_parked:
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
