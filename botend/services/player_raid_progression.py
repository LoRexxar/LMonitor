"""Central physical-raid references and isolated, bounded progress maintenance.

No per-character catalog, WCL-zone slug inference, or first-kill rule changes.
The page joins only local current Journal facts. Import captured catalog explicitly.
"""
import copy
import hashlib
import json
import re
import uuid
from datetime import timedelta
from types import SimpleNamespace

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from botend.models import SeasonMeta, RaiderIOCharacterActivity
from botend.raiderio_identity import activity_identity_digest
from botend.services.raid_instance_projection import current_raid_encounter_rows

REFERENCE = 'raiderio_physical_raid_refs'
META = 'raid_progression_meta'
MODULE = 'raid_progression'


def physical_scope(season, rows=None):
    """Derive the sole physical scope from central encounter IDs/current Journal."""
    encounters = season.raid_encounters
    if not encounters:
        if season.raid_zones:
            raise ValueError('configured season has no encounters')
        return None  # Legacy, genuinely unconfigured seasons only.
    ids = [e['id'] for e in encounters]
    if any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('invalid raid scope')
    rows = current_raid_encounter_rows(ids) if rows is None else rows
    keyed = {}
    for bid, iid, name, release in rows:
        if bid in keyed or bid not in ids:
            raise ValueError('ambiguous Journal encounter')
        keyed[bid] = (iid, name, release)
    if set(keyed) != set(ids):
        raise ValueError('incomplete current Journal')
    groups = {}
    for bid in ids:
        iid, name, _ = keyed[bid]
        groups.setdefault(iid, {'journal_id': iid, 'name': name, 'boss_ids': []})['boss_ids'].append(bid)
    zones = sorted((z['id'], sorted(e['id'] for e in z.get('encounters', []))) for z in season.raid_zones)
    signature = [season.rio_season, sorted(ids), zones, sorted((b, *keyed[b]) for b in ids)]
    fingerprint = hashlib.sha256(json.dumps(signature, ensure_ascii=False).encode()).hexdigest()
    return {'fingerprint': fingerprint, 'raids': list(groups.values())}


def derive_references(season, rows, catalog_data, source_url):
    """Pure join, including one-boss raids; no selected >=8 first-kill policy."""
    from botend.services.player_raid_observation_sources import validate_catalog, _safe_url
    parts = _safe_url(source_url, 'cdn.raiderio.net')
    if parts.query or not re.fullmatch(r'/assets/data/siteMeta-[A-Za-z0-9_-]+\.json', parts.path):
        raise ValueError('invalid catalog source')
    catalog, _ = validate_catalog(catalog_data, season)
    scope = physical_scope(season, rows)
    if not scope or not season.raid_zones:
        raise ValueError('unconfigured raid scope')
    current_ids = {bid for raid in scope['raids'] for bid in raid['boss_ids']}
    relevant = {slug: {e['wowEncounterId'] for e in raid['encounters']}
                for slug, raid in catalog.items()
                if current_ids.intersection(e['wowEncounterId'] for e in raid['encounters'])}
    bindings = []
    for raid in scope['raids']:
        matches = [slug for slug, ids in relevant.items() if ids == set(raid['boss_ids'])]
        if len(matches) != 1:
            raise ValueError('incomplete or ambiguous catalog binding')
        bindings.append({'slug': matches[0], 'journal_id': raid['journal_id']})
    if set(relevant) != {r['slug'] for r in bindings}:
        raise ValueError('catalog overlaps physical scope')
    return {'scope_fingerprint': scope['fingerprint'], 'source_url': source_url, 'bindings': bindings}


def bind_catalog(season_id, catalog_data, source_url, *, apply=False):
    with transaction.atomic():
        season = SeasonMeta.objects.select_for_update().get(pk=season_id)
        refs = derive_references(season, current_raid_encounter_rows([e['id'] for e in season.raid_encounters]),
                                 catalog_data, source_url)
        if apply:
            zones = copy.deepcopy(season.raid_zones)
            for zone in zones:
                zone.pop(REFERENCE, None)
            zones[0][REFERENCE] = refs
            season.raid_zones = zones
            season.save(update_fields=['raid_zones'])
        return refs


def bound_scope(season, *, scope=None):
    scope = physical_scope(season) if scope is None else scope
    if scope is None:
        return None
    refs = [z[REFERENCE] for z in season.raid_zones if REFERENCE in z]
    if len(refs) != 1 or refs[0].get('scope_fingerprint') != scope['fingerprint']:
        raise ValueError('missing or stale raid references')
    bindings = refs[0].get('bindings', [])
    by_id = {r['journal_id']: r['slug'] for r in bindings}
    if (len(bindings) != len(by_id) or len(set(by_id.values())) != len(bindings)
            or set(by_id) != {r['journal_id'] for r in scope['raids']}
            or any(not isinstance(s, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', s) for s in by_id.values())):
        raise ValueError('invalid raid references')
    return {**scope, 'raids': [dict(r, slug=by_id[r['journal_id']]) for r in scope['raids']]}


def preserve_references(existing, raid_zones, encounters, rio_season):
    """Monitor retains verified references only for the identical current scope."""
    if not existing or not getattr(existing, 'raid_encounters', None):
        return raid_zones
    try:
        old = bound_scope(existing)
        new = physical_scope(SimpleNamespace(raid_zones=raid_zones, raid_encounters=encounters, rio_season=rio_season))
        if old and new and old['fingerprint'] == new['fingerprint']:
            refs = next(z[REFERENCE] for z in existing.raid_zones if REFERENCE in z)
            raid_zones = copy.deepcopy(raid_zones)
            raid_zones[0][REFERENCE] = copy.deepcopy(refs)
    except (ValueError, TypeError, KeyError):
        pass
    return raid_zones


def progress_complete(scope, raw):
    """Assess this response, never its merge with historical facts."""
    if not scope:
        return True  # Unconfigured legacy seasons retain their existing contract.
    if not isinstance(raw, dict):
        return False
    for raid in scope['raids']:
        item = raw.get(raid['slug'])
        total = len(raid['boss_ids'])
        if not isinstance(item, dict) or type(item.get('total_bosses')) is not int or item['total_bosses'] != total:
            return False
        if any(type(item.get(f'{d}_bosses_killed')) is not int or not 0 <= item[f'{d}_bosses_killed'] <= total
               for d in ('normal', 'heroic', 'mythic')):
            return False
    return True


def source_is_older(row, updated):
    """Shared raid clock; legacy global clock is evidence only for stored raids."""
    from botend.services.player_raiderio_activity import _date
    meta = row.payload_json.get(META, {})
    baseline = (_date(meta.get('source_updated_at')) if 'source_updated_at' in meta else
                row.source_updated_at if row.payload_json.get(MODULE) else None)
    return baseline is not None and (updated is None or updated < baseline)


def seed_source_clock(row):
    """Pin a trusted legacy raid baseline before a full refresh changes M+ time."""
    meta = row.payload_json.get(META, {})
    if 'source_updated_at' not in meta and row.payload_json.get(MODULE) and row.source_updated_at:
        row.payload_json[META] = {**meta, 'source_updated_at': row.source_updated_at.isoformat()}


def progress_meta(meta, scope, updated, stamp, complete):
    return {**meta, 'status': 'ready' if complete else 'partial', 'complete': complete, 'last_error': '',
            'fetched_at': stamp.isoformat(), 'source_updated_at': updated.isoformat() if updated else None,
            'scope_fingerprint': scope['fingerprint'] if scope else None,
            'next_attempt_at': (stamp + timedelta(hours=24 if complete else 1)).isoformat()}


def project_progress(season, raw, meta=None):
    """Return None only for unconfigured legacy seasons; never current-tier fallback."""
    try:
        scope = physical_scope(season)
        if scope is None:
            return None
        try:
            scope = bound_scope(season, scope=scope)
            status = ('ready' if progress_complete(scope, raw) else 'partial') if raw else 'missing'
            if (meta or {}).get('status') == 'partial' or (meta or {}).get('complete') is False:
                status = 'partial'
        except (ValueError, KeyError, TypeError):
            status, raw = 'unresolved', {}
        result = []
        for raid in scope['raids']:
            item = raw.get(raid.get('slug'), {}) if isinstance(raw, dict) else {}
            if not isinstance(item, dict) or item.get('total_bosses') != len(raid['boss_ids']):
                item = {}
            counts = {d: item.get(f'{d}_bosses_killed') for d in ('normal', 'heroic', 'mythic')}
            counts = {d: value if type(value) is int and 0 <= value <= len(raid['boss_ids']) else None
                      for d, value in counts.items()}
            result.append({'name': raid['name'], 'total_bosses': len(raid['boss_ids']), **counts})
        return {'status': status, 'rows': result}
    except (ValueError, KeyError, TypeError):
        return {'status': 'unresolved', 'rows': []}


def merge_progress(old, new):
    """Missing raid/difficulty cannot erase a previously valid fact."""
    merged = copy.deepcopy(old) if isinstance(old, dict) else {}
    for slug, value in new.items():
        merged[slug] = {**merged.get(slug, {}), **{k: v for k, v in value.items() if v is not None}}
    return merged


def refresh_raid_progression(player, *, apply=False, transport=None, proxies=None):
    """One exact player, one request, independent lease/backoff/CAS. Dry-run default."""
    from botend.services.player_raiderio_activity import player_identity, validate_payload, _date
    season = SeasonMeta.objects.get(pk=player.season_id)
    identity = player_identity(player)
    if not identity or not season.is_active or not season.rio_season:
        return {'status': 'unresolved'}
    try:
        scope = bound_scope(season)
        if not scope:
            raise ValueError('unconfigured')
    except (ValueError, KeyError, TypeError):
        return {'status': 'unresolved_scope'}
    fields = 'raid_progression:' + ':'.join(sorted(r['slug'] for r in scope['raids'])) + ',mythic_plus_scores_by_season:current'
    if not apply:
        return {'status': 'dry_run', 'player_id': player.pk, 'fields': fields}
    now, token = timezone.now(), uuid.uuid4().hex
    with transaction.atomic():
        row, _ = RaiderIOCharacterActivity.objects.get_or_create(season=season,
            identity_digest=activity_identity_digest(**identity), defaults=identity)
        row = RaiderIOCharacterActivity.objects.select_for_update().get(pk=row.pk)
        meta = row.payload_json.get(META, {})
        if meta.get('next_attempt_at') and _date(meta['next_attempt_at']) > now:
            return {'status': 'busy' if meta.get('status') == 'fetching' else 'backoff'}
        starting_clock = row.module_fetched_at.get(MODULE)
        meta = {**meta, 'token': token, 'status': 'fetching', 'next_attempt_at': (now + timedelta(minutes=2)).isoformat()}
        row.payload_json = {**row.payload_json, META: meta}
        row.save(update_fields=['payload_json'])
    config = getattr(settings, 'REQUEST_CONFIG', {})
    if proxies is None and config.get('enable_proxy'):
        proxies = config.get('proxies')
    error, retry = '', 1800
    try:
        response = (transport or requests.get)('https://raider.io/api/v1/characters/profile',
            params={'region': identity['region'], 'realm': identity['realm_slug'], 'name': player.character_name, 'fields': fields},
            timeout=25, headers={'User-Agent': 'Mozilla/5.0'}, proxies=proxies)
        if response.status_code == 429:
            error = 'rate_limited'
            try:
                retry = max(60, min(86400, int(response.headers.get('Retry-After', 3600))))
            except (ValueError, TypeError):
                retry = 3600
        elif response.status_code != 200:
            error = f'http_{response.status_code}'
        else:
            data = response.json()
            # Only this module is consumed; unrelated payload modules cannot affect it.
            data = {k: v for k, v in data.items() if k not in ('mythic_plus_best_runs', 'mythic_plus_recent_runs', 'mythic_plus_dungeon_run_counts')}
            facts, updated = validate_payload(data, identity, season)
            if not facts.get(MODULE):
                raise ValueError('missing raid progress')
    except requests.RequestException:
        error = 'transport_error'
    except (ValueError, KeyError, TypeError, AttributeError):
        error = 'invalid_payload'
    with transaction.atomic():
        current = RaiderIOCharacterActivity.objects.select_for_update().get(pk=row.pk)
        seed_source_clock(current)
        latest_meta = current.payload_json.get(META, {})
        if latest_meta.get('token') != token:
            return {'status': 'superseded'}
        fresh_season = SeasonMeta.objects.get(pk=season.pk)
        try:
            if not fresh_season.is_active or bound_scope(fresh_season)['fingerprint'] != scope['fingerprint']:
                error = 'scope_changed'
        except (ValueError, TypeError, KeyError):
            error = 'scope_changed'
        if current.module_fetched_at.get(MODULE) != starting_clock:
            error = 'superseded'
        if not error and source_is_older(current, updated):
            error = 'stale_source'
        stamp = timezone.now()
        meta = {**latest_meta, 'status': error or 'ready', 'last_error': error,
                'next_attempt_at': (stamp + timedelta(seconds=retry if error else 86400)).isoformat()}
        if not error:
            current.payload_json = {**current.payload_json, MODULE: merge_progress(current.payload_json.get(MODULE), facts[MODULE])}
            current.module_fetched_at = {**current.module_fetched_at, MODULE: stamp.isoformat()}
            meta = progress_meta(meta, scope, updated, stamp, progress_complete(scope, facts[MODULE]))
        current.payload_json = {**current.payload_json, META: meta}
        current.save(update_fields=['payload_json', 'module_fetched_at'])
    return {'status': meta['status'], 'activity_id': row.pk}
