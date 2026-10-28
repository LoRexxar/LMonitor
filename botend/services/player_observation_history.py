"""Question-only scored history. Partial observations, never a current score source."""
import json
import math
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from django.db import DatabaseError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from botend.models import RaiderIOCharacterActivity
from botend.raiderio_identity import activity_identity_digest
from botend.services.raiderio_run_roster import _positive

MODULE = 'mythic_plus_scored_history'
URL = 'https://raider.io/api/characters/mythic-plus-runs'
TTL = timedelta(hours=48)
MAX_DUNGEONS = 16
MAX_RUNS = 200


def scope(season, character_id, dungeon_id):
    from botend.services.player_observation_sources import _scope
    return {**_scope(season, character_id), 'dungeonId': _positive(dungeon_id)}


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = parse_datetime(value)
        return stamp if stamp and timezone.is_aware(stamp) else None
    except (ValueError, TypeError):
        return None


class PrivateHistory(ValueError):
    pass


def validate_history(data, rio_season, character_id, dungeon_id):
    expected = scope(rio_season, character_id, dungeon_id)
    if (not isinstance(data, dict) or data.get('ui') != expected
            or type(data['ui'].get('characterId')) is not int
            or type(data['ui'].get('dungeonId')) is not int):
        raise ValueError('wrong history scope')
    if data.get('isPrivate') is True or data.get('runPrivacyMode', 'public') != 'public':
        raise PrivateHistory('private')
    if data.get('error') or data.get('errors'):
        raise ValueError('source error')
    rows = data.get('runs')
    if not isinstance(rows, list) or len(rows) > 2000:
        raise ValueError('invalid history list')
    clean = {}
    for row in rows:
        summary = row['summary']
        if summary['season'] != rio_season or _positive(summary['dungeon']['id']) != dungeon_id:
            raise ValueError('wrong run scope')
        if summary.get('deleted_at') not in (None, ''):
            continue
        if summary.get('status', 'finished') != 'finished':
            raise ValueError('unfinished run')
        if summary.get('runPrivacyMode', 'public') != 'public':
            raise PrivateHistory('private')
        rid = _positive(summary['keystone_run_id'])
        level = _positive(summary['mythic_level'])
        chests, score = summary['num_chests'], row['score']
        if (type(chests) is not int or not 0 <= chests <= 3
                or type(score) not in (float, int) or not math.isfinite(score) or score < 0
                or not _date(summary['completed_at'])):
            raise ValueError('invalid run values')
        item = dict(run_id=rid, dungeon_id=dungeon_id, level=level, score=score,
                    timed=chests > 0, completed_at=summary['completed_at'])
        if rid in clean and clean[rid] != item:
            raise ValueError('conflicting run')
        clean[rid] = item
    return {'ui': expected, 'source_url': URL + '?' + urlencode(expected),
            'runs': sorted(clean.values(), key=lambda r: (_date(r['completed_at']), r['run_id']), reverse=True)[:MAX_RUNS],
            'coverage': 'observed_partial'}


def _state(entry, expected, now):
    if not isinstance(entry, dict):
        return 'pending'
    due = _date(entry.get('next_attempt_at'))
    waiting = due is not None and due > now
    status = entry.get('status')
    if status == 'fetching':
        return 'busy' if waiting else 'pending'
    if entry.get('private') is True or status == 'private':
        # Only a newly validated public response can clear a sticky tombstone.
        return 'private' if waiting else 'pending'
    if status == 'error' or entry.get('last_error'):
        return 'backoff' if waiting else 'pending'
    stamp = _date(entry.get('fetched_at'))
    if (stamp and now - TTL <= stamp <= now and entry.get('ui') == expected
            and entry.get('coverage') == 'observed_partial'
            and isinstance(entry.get('runs'), list) and len(entry['runs']) <= MAX_RUNS):
        try:
            raw = [{'score': r['score'], 'summary': {
                'season': expected['season'], 'dungeon': {'id': r['dungeon_id']},
                'keystone_run_id': r['run_id'], 'mythic_level': r['level'],
                'completed_at': r['completed_at'], 'num_chests': 1 if r['timed'] else 0}}
                for r in entry['runs'] if type(r['timed']) is bool]
            if len(raw) != len(entry['runs']):
                return 'pending'
            verified = validate_history({'ui': entry['ui'], 'runs': raw}, expected['season'],
                                        expected['characterId'], expected['dungeonId'])
            if entry.get('source_url') != verified['source_url']:
                return 'pending'
        except (ValueError, TypeError, KeyError, AttributeError):
            return 'pending'
        return 'fresh'
    return 'pending'


def _event_state(entry, expected, now, required_after=None):
    """仅当具体待核验场次晚于缓存时，允许刷新仍在有效期内的历史。"""
    state = _state(entry, expected, now)
    if (state == 'fresh' and required_after is not None
            and timezone.is_aware(required_after) and required_after <= now
            and _date(entry.get('fetched_at')) < required_after):
        return 'pending'
    return state


def history_cache(character, season, dungeon_id, *, required_after=None):
    digest = activity_identity_digest(**character['identity'])
    row = RaiderIOCharacterActivity.objects.filter(season=season, identity_digest=digest).first()
    result = {'identity_digest': digest, 'character_id': character['character_id'],
              'dungeon_id': dungeon_id, 'kind': 'scored_history', 'status': 'pending'}
    if row:
        now = timezone.now()
        if row.next_attempt_at and row.next_attempt_at > now and row.status in ('fetching', 'error'):
            return {**result, 'status': 'busy' if row.status == 'fetching' else 'backoff'}
        entry = row.payload_json.get(MODULE, {}).get(str(dungeon_id), {})
        result.update(activity_id=row.pk, status=_event_state(entry,
            scope(season.rio_season, character['character_id'], dungeon_id), now, required_after))
        if entry.get('fetched_at'):
            result['fetched_at'] = entry['fetched_at']
    return result


class _NonTargetHistoryChange(Exception):
    pass


def _snapshot_json(values):
    # DjangoJSONEncoder truncates datetime microseconds; exact JSON also keeps
    # bool/int differences and array ordering significant.
    return json.dumps({k: v.isoformat() if isinstance(v, datetime) else v
                       for k, v in values.items()}, sort_keys=True,
                      ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _row_snapshot(row):
    return _snapshot_json({f.attname: getattr(row, f.attname)
                           for f in row._meta.concrete_fields})


def _write(row, did, entry, season, *, preserve_non_target=False):
    # Called only with a freshly locked row; never overwrite concurrent raid/profile modules.
    entries = row.payload_json.get(MODULE, {})
    entries = {k: v for k, v in entries.items() if isinstance(v, dict)
               and v.get('ui', {}).get('season') == season}
    entries[str(did)] = entry
    if len(entries) > MAX_DUNGEONS:
        keep = sorted((k for k in entries if k != str(did)),
                      key=lambda k: entries[k].get('fetched_at') or '', reverse=True)[:MAX_DUNGEONS-1]
        entries = {k: entries[k] for k in [str(did), *keep]}
    if preserve_non_target:
        original = row.payload_json.get(MODULE, {})
        if _snapshot_json({k: v for k, v in original.items() if k != str(did)}) != _snapshot_json(
                {k: v for k, v in entries.items() if k != str(did)}):
            raise _NonTargetHistoryChange
    row.payload_json = {**row.payload_json, MODULE: entries}
    row.save(update_fields=['payload_json'])


def collect_history(batch, character, season, dungeon_id, *, required_after=None,
                    expected_snapshot=None):
    """Optionally fence maintenance to a complete ORM values() row.

    Datetimes accept native values or exact isoformat strings with microseconds.
    None retains the ordinary contract. Guarded calls require autocommit so row
    locks cannot span HTTP. Finalize drift leaves the entire row/lease untouched;
    a refusal is not permission to clear the lease or retry the request.
    """
    expected = scope(season.rio_season, character['character_id'], dungeon_id)
    guarded = expected_snapshot is not None
    result = ({'identity_digest': activity_identity_digest(**character['identity']),
               'character_id': character['character_id'], 'dungeon_id': dungeon_id,
               'kind': 'scored_history', 'status': 'pending'} if guarded else
              history_cache(character, season, dungeon_id, required_after=required_after))
    if guarded:
        if not transaction.get_autocommit() or transaction.get_connection().in_atomic_block:
            return {**result, 'status': 'cas_requires_autocommit'}
        fields = {f.attname for f in RaiderIOCharacterActivity._meta.concrete_fields}
        if (not isinstance(expected_snapshot, dict) or set(expected_snapshot) != fields
                or type(expected_snapshot['id']) is not int):
            return {**result, 'status': 'cas_claim_mismatch'}
        approved_id = expected_snapshot['id']
        try:
            approved_snapshot = _snapshot_json(expected_snapshot)
        except (TypeError, ValueError):
            return {**result, 'status': 'cas_claim_mismatch'}
    token = uuid.uuid4().hex
    try:
        with transaction.atomic():
            if guarded:
                # Missing approved rows must never create a replacement.
                row = RaiderIOCharacterActivity.objects.select_for_update(nowait=True).filter(
                    pk=approved_id, season=season,
                    identity_digest=result['identity_digest']).first()
                if row is None or _row_snapshot(row) != approved_snapshot:
                    return {**result, 'status': 'cas_claim_mismatch'}
                result['activity_id'] = row.pk
            else:
                row, _ = RaiderIOCharacterActivity.objects.get_or_create(season=season,
                    identity_digest=result['identity_digest'], defaults=character['identity'])
                row = RaiderIOCharacterActivity.objects.select_for_update(nowait=True).get(pk=row.pk)
            now = timezone.now()
            if row.next_attempt_at and row.next_attempt_at > now and row.status in ('fetching', 'error'):
                return {**result, 'status': 'busy' if row.status == 'fetching' else 'backoff'}
            entry = dict(row.payload_json.get(MODULE, {}).get(str(dungeon_id), {}))
            if entry.get('ui') is not None and entry['ui'] != expected:
                return {**result, 'status': 'identity_mismatch'}
            state = _event_state(entry, expected, now, required_after)
            if state != 'pending':
                return {**result, 'status': state}
            if batch.stop_event.is_set() or batch.requests >= batch.budget:
                return {**result, 'status': 'budget_exhausted'}
            entry.update(ui=expected, lease_token=token, status='fetching',
                         next_attempt_at=(now + timedelta(minutes=2)).isoformat())
            _write(row, dungeon_id, entry, season.rio_season, preserve_non_target=guarded)
            if guarded:
                # _write refused collateral filtering/trimming before save.
                # Only the target's legal claim changes enter this baseline.
                claimed_snapshot = _row_snapshot(row)
        error, facts, retry = '', None, timedelta(minutes=30)
        try:
            response = batch.get(URL, params=expected, headers={'User-Agent': 'Mozilla/5.0'})
            if response.status_code != 200:
                error = 'rate_limited' if response.status_code == 429 else f'http_{response.status_code}'
                if response.status_code == 429:
                    try:
                        retry = timedelta(seconds=max(60, min(86400, int(response.headers.get('Retry-After', 3600)))))
                    except (TypeError, ValueError):
                        retry = timedelta(hours=1)
                # Bare 403 is not proof of a private character.
                if response.status_code in (401, 403):
                    try:
                        if response.json().get('message') == 'Character Profile is Private':
                            error = 'private'
                    except (ValueError, AttributeError):
                        pass
            else:
                facts = validate_history(response.json(), season.rio_season, character['character_id'], dungeon_id)
        except PrivateHistory:
            error = 'private'
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            error = 'invalid_payload'
        except requests.RequestException:
            error = 'transport_error'
        with transaction.atomic():
            if guarded:
                row = RaiderIOCharacterActivity.objects.select_for_update().filter(pk=row.pk).first()
                if row is None or _row_snapshot(row) != claimed_snapshot:
                    return {**result, 'status': 'cas_finalize_mismatch'}
            else:
                row = RaiderIOCharacterActivity.objects.select_for_update().get(pk=row.pk)
            latest = row.payload_json.get(MODULE, {}).get(str(dungeon_id), {})
            if latest.get('lease_token') != token:
                return {**result, 'status': 'superseded'}
            stamp = timezone.now()
            if error:
                entry = {**latest, 'status': 'private' if error == 'private' else 'error',
                         'last_error': error, 'next_attempt_at': (stamp + retry).isoformat()}
                # Keep a sticky tombstone across subsequent transport failures.
                if latest.get('private') or error == 'private':
                    entry['private'] = True
            else:
                entry = {**facts, 'fetched_at': stamp.isoformat(), 'status': 'ready'}
            entry.pop('lease_token', None)
            _write(row, dungeon_id, entry, season.rio_season, preserve_non_target=guarded)
        return {**result, 'status': error or 'ready', 'fetched_at': entry.get('fetched_at')}
    except _NonTargetHistoryChange:
        return {**result, 'status': 'cas_non_target_change'}
    except DatabaseError:
        return {**result, 'status': 'database_busy_or_error'}
