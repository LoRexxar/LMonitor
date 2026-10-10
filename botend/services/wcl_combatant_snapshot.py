"""Central raw log facts. Readers never fetch, and never trust profile attributes."""
from collections import defaultdict
from copy import deepcopy
import math
import re

from django.db.models import Q

from botend.constants.wow import resolve_spec_identity
from botend.models import WclCombatantSnapshot, WowItemSnapshot


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


def _load_facts(records):
    keys = sorted({key for row in records if (key := _fight_key(row))})
    facts = defaultdict(list)
    for start in range(0, len(keys), 100):
        where = Q()
        for code, fight in keys[start:start + 100]:
            where |= Q(report_code=code, fight_id=fight)
        for row in WclCombatantSnapshot.objects.filter(where).values(
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


def enrich_dungeon_records(records, class_name, spec_name):
    """Return independent dicts; stats/race exclusively from the exact WCL log.

    Missing/ambiguous facts keep ranking WCL gear, but never profile stats/race.
    This is a bounded DB-only projection; collection is an explicit separate job.
    """
    records = list(records)
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
            for selected in _select_dungeon_sample_records(qs, max_samples=100):
                identity = _dungeon_log_identity(selected)
                if identity in seen:
                    continue
                seen.add(identity)
                records.append({key: selected.get(key) for key in (
                    'id', 'report_code', 'fight_id', 'character_name', 'realm', 'region')})
                records[-1].update(class_name=class_name, spec_name=spec_name)
    return records


def _validated_events(events, fight_ids):
    """Reject incomplete containers and conflicting duplicate actor events."""
    if not isinstance(events, list):
        raise ValueError('CombatantInfo events must be a list')
    by_fight = defaultdict(dict)
    for event in events:
        if not isinstance(event, dict):
            raise ValueError('Invalid CombatantInfo event')
        fight = _positive_int(event.get('fight'))
        if fight is None and len(fight_ids) == 1:
            fight = fight_ids[0]
        actor = _positive_int(event.get('sourceID'))
        source = event.get('source') or {}
        if fight not in fight_ids or not actor or not isinstance(source, dict) or _positive_int(source.get('id')) != actor:
            raise ValueError('CombatantInfo fight/actor identity missing or inconsistent')
        if actor in by_fight[fight] and by_fight[fight][actor] != event:
            raise ValueError('Conflicting CombatantInfo events for one actor')
        by_fight[fight][actor] = event
    return {fight: list(actors.values()) for fight, actors in by_fight.items()}


def collect_dungeon_combatants(records, fetcher=None, *, apply=False, batch_size=20, log=None):
    """Explicit offline collection. At most 20 fights/report/request; retry on next run.

    Counts partition observations, not API responses. Raw payloads are persisted
    once centrally, never copied to ranking rows. Failed scopes retain last-good.
    """
    from django.db import transaction
    from django.utils import timezone
    if not 1 <= batch_size <= 20:
        raise ValueError('batch_size must be between 1 and 20')
    records = list(records)
    counts = dict(selected=len(records), cached=0, matched=0, missing=0,
                  permission_denied=0, failed=0, pending=0, snapshots_written=0,
                  reports_requested=0, fights_requested=0)
    pending = defaultdict(lambda: defaultdict(list))
    # All-spec repair can contain tens of thousands of observations. Inspect
    # cache hits in bounded batches, never hydrate all raw events together.
    for start in range(0, len(records), 100):
        batch_records = records[start:start + 100]
        facts = _load_facts(batch_records)
        for row in batch_records:
            key = _fight_key(row)
            if not key or not row.get('realm') or not row.get('character_name') or str(row['character_name']).casefold() == 'anonymous':
                counts['missing'] += 1
            elif match_combatant(facts.get(key, []), row, row['class_name'], row['spec_name']):
                counts['cached'] += 1
            else:
                pending[key[0]][key[1]].append(row)
        del facts
    if not apply:
        counts['pending'] = sum(len(rows) for fights in pending.values() for rows in fights.values())
        return counts
    if fetcher is None:
        from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
        fetcher = SpecDetailBase(None, None)
    for code, fights in pending.items():
        counts['reports_requested'] += 1
        fight_ids = sorted(fights)
        for start in range(0, len(fight_ids), batch_size):
            batch = fight_ids[start:start + batch_size]
            batch_rows = [row for fight in batch for row in fights[fight]]
            counts['fights_requested'] += len(batch)
            try:
                events = fetcher.fetch_wcl_combatant_info(code, batch)
                if events is None:
                    status = 'permission_denied' if getattr(fetcher, '_wcl_last_error', '') == 'permission' else 'failed'
                    counts[status] += len(batch_rows)
                    if log:
                        log(f'WCL report={code} fights={batch} status={status} observations={len(batch_rows)}')
                    error = getattr(fetcher, '_wcl_last_error', '')
                    if error in {'rate_limit', 'authentication', 'network'} or str(error).startswith('http_5'):
                        remaining = counts['selected'] - sum(counts[key] for key in (
                            'cached', 'matched', 'missing', 'permission_denied', 'failed'))
                        counts['failed'] += remaining
                        if log:
                            log(f'WCL aborted after {error}; unattempted_failed={remaining}')
                        return counts
                    continue
                by_fight = _validated_events(events, batch)
                now = timezone.now()
                written = 0
                with transaction.atomic():
                    for fight, payloads in by_fight.items():
                        for payload in payloads:
                            _, created = WclCombatantSnapshot.objects.get_or_create(
                                report_code=code, fight_id=fight, actor_id=payload['sourceID'],
                                defaults={'payload_json': payload, 'fetched_at': now})
                            # Reports are historical facts. Existing actors are immutable;
                            # a partial/changed fetch must never overwrite last-good.
                            written += int(created)
                counts['snapshots_written'] += written
                # Independent readback proves the persisted target, including races.
                persisted = _load_facts(batch_rows)
                for row in batch_rows:
                    event = match_combatant(persisted.get(_fight_key(row), []), row,
                                            row['class_name'], row['spec_name'])
                    counts['matched' if event else 'missing'] += 1
                if log:
                    log(f'WCL report={code} fights={batch} written={written} observations={len(batch_rows)}')
            except Exception as exc:
                counts['failed'] += len(batch_rows)
                if log:
                    log(f'WCL report={code} fights={batch} failed={type(exc).__name__}: {exc}')
    return counts
