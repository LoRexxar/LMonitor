"""Central minimal run facts, bounded collection, and network-free roster projection."""
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import unquote, urlsplit

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.models import RaiderIORunRoster, RaiderIOCharacterActivity
from botend.constants.wow import CLASS_COLOR, SPEC_ICON, canonical_class_spec
from botend.raiderio_identity import activity_identity_digest, normalized_name
from botend.wow_i18n import cn_class_from_slug, cn_spec_from_slug
from botend.services.character_navigation import character_link

TTL = timedelta(days=7)
MAX_BEST_RUNS = 8
MAX_CANDIDATES = 40 * 20 * MAX_BEST_RUNS
_RATE_LOCK = threading.Lock()
_last_request = None


def _pace(stop_event):
    """One process-wide budget (at most four requests/second)."""
    global _last_request
    with _RATE_LOCK:
        now = time.monotonic()
        if _last_request is not None:
            time.sleep(max(0, .25 - (now - _last_request)))
        if stop_event.is_set():
            return False
        _last_request = time.monotonic()
        return True


def _positive(value):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise ValueError('invalid id')
    return value


def run_reference(run, rio_season):
    """Validate tokens before parsing; conflicting ID/URL is never repaired."""
    if not isinstance(rio_season, str) or not re.fullmatch(r'season-[a-z0-9-]+', rio_season):
        raise ValueError('invalid season')
    if not isinstance(run, dict) or run.get('season', rio_season) != rio_season:
        raise ValueError('wrong season')
    if not isinstance(run.get('url'), str):
        raise ValueError('missing run URL')
    url = urlsplit(run['url'])
    match = re.fullmatch(r'/mythic-plus-runs/' + re.escape(rio_season) + r'/([1-9][0-9]*)(?:-[a-zA-Z0-9-]+)?', url.path)
    if (url.scheme != 'https' or url.netloc not in ('raider.io', 'www.raider.io')
            or url.query or url.fragment or not match):
        raise ValueError('wrong run URL')
    run_id = _positive(int(match[1]))
    if 'keystone_run_id' in run and _positive(run['keystone_run_id']) != run_id:
        raise ValueError('conflicting run id')
    return run_id


def _text(value, maximum=200):
    if value is None:
        return ''
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError('invalid text')
    return value


def _identity_part(value, keys):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('invalid identity')
    result = {}
    for key in keys:
        if key not in value or value[key] is None:
            continue
        result[key] = _positive(value[key]) if key == 'id' or key.endswith('_id') else _text(value[key])
    return result


def _profile_url(character):
    path = character.get('path', '')
    if not path:
        return ''
    parsed = urlsplit(path)
    parts = [unquote(x) for x in parsed.path.split('/')]
    if (parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or len(parts) != 5
            or parts[:2] != ['', 'characters'] or parts[2] not in ('us', 'eu', 'kr', 'tw', 'cn')
            or parts[2] != character.get('region', {}).get('slug')
            or parts[3] != character.get('realm', {}).get('slug')
            or not parts[3] or parts[3] in ('anonymous', '.', '..')
            or any(c in parts[3] for c in '/\\?#')
            or normalized_name(parts[4]) != normalized_name(character.get('name', ''))
            or not parts[4] or any(c in parts[4] for c in '/\\?#')):
        raise ValueError('invalid member path')
    return 'https://raider.io' + path


def validate_details(data, run, rio_season):
    run_id = run_reference(run, rio_season)
    if (not isinstance(data, dict) or data.get('season') != rio_season
            or _positive(data.get('keystone_run_id')) != run_id):
        raise ValueError('wrong detail identity')
    # A verified deletion is authoritative even if the source omits run facts.
    if data.get('deleted_at') not in (None, ''):
        return None
    dungeon = _identity_part(data.get('dungeon'), ('id', 'name', 'slug', 'map_challenge_mode_id'))
    if not dungeon.get('name') or dungeon['name'] != run.get('dungeon'):
        raise ValueError('wrong dungeon')
    for key in ('mythic_level', 'clear_time_ms', 'num_chests'):
        if type(data.get(key)) is not int or data[key] < 0:
            raise ValueError('invalid run fact')
    if data['mythic_level'] != run.get('mythic_level'):
        raise ValueError('wrong level')
    for source, detail in (('clear_time_ms', 'clear_time_ms'), ('num_keystone_upgrades', 'num_chests')):
        if run.get(source) is not None and run[source] != data[detail]:
            raise ValueError('conflicting run facts')
    completed = _text(data.get('completed_at'))
    stamp = parse_datetime(completed)
    if stamp is None or timezone.is_naive(stamp):
        raise ValueError('invalid completion time')
    if run.get('completed_at') and parse_datetime(run['completed_at']) != stamp:
        raise ValueError('conflicting completion time')
    privacy = data.get('runPrivacyMode')
    roster = data.get('roster')
    if privacy != 'public' or roster is None or roster == []:
        return None
    if not isinstance(roster, list) or len(roster) != 5:
        raise ValueError('invalid roster size')
    members, identities = [], set()
    for member in roster:
        if not isinstance(member, dict) or not isinstance(member.get('character'), dict):
            raise ValueError('invalid member')
        source = member['character']
        character = {key: _text(source.get(key)) for key in ('name', 'path')}
        # Raider.IO can anonymize one member as id=0/anonymous realm/no path
        # while retaining a display name. This is NOT a valid zero character ID:
        # withdraw the entire roster, preserving all normal ID/scope validation.
        if (type(source.get('id')) is int and source['id'] == 0
                and not character['path'] and isinstance(source.get('realm'), dict)
                and source['realm'].get('slug') == 'anonymous'):
            return None
        # Legacy central facts omit this field; present IDs must be real integers.
        if 'id' in source:
            character['id'] = _positive(source['id'])
        for key, fields in (('class', ('id', 'name', 'slug')), ('spec', ('id', 'name', 'slug', 'role')),
                            ('realm', ('slug',)), ('region', ('slug',))):
            character[key] = _identity_part(source.get(key), fields)
        if not character['name'] or character['name'].lower() == 'anonymous':
            return None
        path = _profile_url(character)
        if path:
            key = normalized_name(path)
            if key in identities:
                raise ValueError('duplicate member')
            identities.add(key)
        members.append({'role': _text(member.get('role')), 'character': character})
    return {'season': rio_season, 'keystone_run_id': run_id, 'dungeon': dungeon,
            **{key: data[key] for key in ('mythic_level', 'clear_time_ms', 'num_chests', 'completed_at')},
            'runPrivacyMode': 'public', 'deleted_at': None, 'roster': members}


def _snapshot_json(values):
    # Exact full-row encoding: retain microseconds and bool/int distinctions.
    return json.dumps({k: v.isoformat() if isinstance(v, datetime) else v
                       for k, v in values.items()}, sort_keys=True,
                      ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _row_snapshot(row):
    return _snapshot_json({f.attname: getattr(row, f.attname)
                           for f in row._meta.concrete_fields})


class RosterCollector:
    """Each instance is one batch; force never bypasses a lease or batch stop."""
    def __init__(self, transport=None, proxies=None, stop_event=None):
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.session = requests.Session() if transport is None else None
        self.transport = transport or self.session.get
        cfg = getattr(settings, 'REQUEST_CONFIG', {})
        self.proxies = proxies if proxies is not None else cfg.get('proxies') if cfg.get('enable_proxy') else None

    @property
    def stopped(self):
        return self.stop_event.is_set()

    @stopped.setter
    def stopped(self, value):
        if value:
            self.stop_event.set()

    def close(self):
        if self.session:
            self.session.close()

    def refresh(self, run, rio_season, *, force=False, expected_snapshot=None,
                expected_absent=False):
        """Opt-in full-row CAS; defaults retain the ordinary collector contract.

        Supply a complete values() row (datetimes native or exact isoformat), or
        expected_absent=True, never both. Guarded calls require autocommit and
        never bypass future backoff/TTL, even with force. Finalize/stop drift
        leaves the whole row and lease untouched; refusal is not retry permission.
        """
        if self.stopped:
            return {'status': 'batch_stopped'}
        try:
            run_id = run_reference(run, rio_season)
        except (ValueError, TypeError):
            return {'status': 'invalid_reference'}
        result = {'run_id': run_id}
        guarded = expected_snapshot is not None or expected_absent is not False
        if guarded:
            if not transaction.get_autocommit() or transaction.get_connection().in_atomic_block:
                return {**result, 'status': 'cas_requires_autocommit'}
            fields = {f.attname for f in RaiderIORunRoster._meta.concrete_fields}
            if (type(expected_absent) is not bool
                    or (expected_absent and expected_snapshot is not None)):
                return {**result, 'status': 'cas_claim_mismatch'}
            if not expected_absent:
                if not isinstance(expected_snapshot, dict) or set(expected_snapshot) != fields:
                    return {**result, 'status': 'cas_claim_mismatch'}
                try:
                    approved_snapshot = _snapshot_json(expected_snapshot)
                except (TypeError, ValueError):
                    return {**result, 'status': 'cas_claim_mismatch'}
        now, token = timezone.now(), uuid.uuid4().hex
        with transaction.atomic():
            if expected_absent:
                # INSERT under the existing unique constraint, never adopt a
                # competing creator's row as our own newly claimed row.
                try:
                    with transaction.atomic():
                        row = RaiderIORunRoster.objects.create(rio_season=rio_season, run_id=run_id)
                except IntegrityError:
                    return {**result, 'status': 'cas_claim_mismatch'}
            elif guarded:
                row = RaiderIORunRoster.objects.select_for_update().filter(
                    rio_season=rio_season, run_id=run_id).first()
                if row is None or _row_snapshot(row) != approved_snapshot:
                    return {**result, 'status': 'cas_claim_mismatch'}
            else:
                row, _ = RaiderIORunRoster.objects.get_or_create(rio_season=rio_season, run_id=run_id)
                row = RaiderIORunRoster.objects.select_for_update().get(pk=row.pk)
            if row.next_attempt_at and row.next_attempt_at > now:
                if row.status == 'fetching':
                    return {**result, 'status': 'busy'}
                if guarded or not force:
                    return {**result, 'status': 'fresh' if row.status == 'ready' else 'backoff'}
            previous = {field: getattr(row, field) for field in ('status', 'last_attempt_at', 'next_attempt_at', 'lease_token')}
            row.status, row.lease_token = 'fetching', token
            row.last_attempt_at, row.next_attempt_at = now, now + timedelta(minutes=2)
            row.save(update_fields=['status', 'lease_token', 'last_attempt_at', 'next_attempt_at'])
            if guarded:
                claimed_snapshot = _row_snapshot(row)
        error, facts, retry = '', None, None
        try:
            if not _pace(self.stop_event):
                if guarded:
                    with transaction.atomic():
                        current = RaiderIORunRoster.objects.select_for_update().filter(pk=row.pk).first()
                        if current is None or _row_snapshot(current) != claimed_snapshot:
                            return {**result, 'status': 'cas_finalize_mismatch'}
                        RaiderIORunRoster.objects.filter(pk=row.pk).update(**previous)
                else:
                    RaiderIORunRoster.objects.filter(pk=row.pk, lease_token=token).update(**previous)
                return {**result, 'status': 'batch_stopped'}
            response = self.transport('https://raider.io/api/v1/mythic-plus/run-details',
                params={'season': rio_season, 'id': run_id}, timeout=25,
                headers={'User-Agent': 'Mozilla/5.0'}, proxies=self.proxies)
            if response.status_code == 429:
                self.stopped, error = True, 'rate_limited'
                try:
                    retry = max(60, min(86400, int(response.headers.get('Retry-After', 3600))))
                except (ValueError, TypeError):
                    retry = 3600
            elif response.status_code != 200:
                error = f'http_{response.status_code}'
            else:
                facts = validate_details(response.json(), run, rio_season)
        except (ValueError, TypeError, KeyError, AttributeError):
            error = 'invalid_payload'
        except requests.RequestException:
            error = 'transport_error'
        with transaction.atomic():
            if guarded:
                row = RaiderIORunRoster.objects.select_for_update().filter(pk=row.pk).first()
                if row is None or _row_snapshot(row) != claimed_snapshot:
                    return {**result, 'status': 'cas_finalize_mismatch'}
            else:
                row = RaiderIORunRoster.objects.select_for_update().get(pk=row.pk)
            if row.lease_token != token or row.status != 'fetching':
                return {**result, 'status': 'superseded'}
            stamp = timezone.now()
            if error:
                row.failure_count += 1
                row.status, row.last_error = 'error', error
                row.next_attempt_at = stamp + timedelta(seconds=retry or min(21600, 300 * 2 ** min(row.failure_count - 1, 6)))
            else:
                row.status = 'ready' if facts else 'unavailable'
                row.roster_visible = bool(facts)
                if facts:
                    row.payload_json, row.fetched_at = facts, stamp
                row.failure_count, row.last_error = 0, ''
                row.next_attempt_at = stamp + (TTL if facts else timedelta(hours=24))
            row.lease_token = ''
            row.save()
        return {**result, 'status': error or row.status}

    def refresh_best_runs(self, runs, rio_season):
        results, seen = [], set()
        for run in runs[:MAX_BEST_RUNS]:
            try:
                key = run_reference(run, rio_season)
            except (ValueError, TypeError):
                results.append({'status': 'invalid_reference'})
                continue
            if key in seen:
                continue
            seen.add(key)
            results.append(self.refresh(run, rio_season))
            if self.stopped:
                break
        return results


def _project(row, run, rio_season):
    if row is None:
        return {'roster_status': 'pending', 'roster': []}
    if not row.roster_visible:
        return {'roster_status': 'unavailable', 'roster': []}
    if not row.payload_json:
        return {'roster_status': 'unavailable' if row.last_error else 'pending', 'roster': []}
    try:
        facts = validate_details(row.payload_json, run, rio_season)
    except (ValueError, TypeError, KeyError, AttributeError):
        return {'roster_status': 'unavailable', 'roster': []}
    if not facts:
        return {'roster_status': 'unavailable', 'roster': []}
    members = []
    # Stable within each actual run role; never infer role from character.spec.
    for member in sorted(facts['roster'], key=lambda item: {'tank': 0, 'healer': 1, 'dps': 2}.get(item['role'], 3)):
        character = member['character']
        cls, spec = character['class'], character['spec']
        identity = canonical_class_spec(cls.get('slug') or cls.get('name'), spec.get('slug') or spec.get('name'))
        profile_url = _profile_url(character)
        members.append({'name': character['name'], 'class_label': cn_class_from_slug(cls.get('slug'), cls.get('name', '')),
            'spec_label': cn_spec_from_slug(spec.get('slug'), spec.get('name', '')),
            'class_slug': cls.get('slug', ''), 'spec_slug': spec.get('slug', ''),
            'class_color': CLASS_COLOR.get(identity[0], '#94a3b8') if identity else '#94a3b8',
            'spec_icon_url': SPEC_ICON.get(identity, '').replace('/small/', '/large/'),
            'role_label': {'tank': '坦克', 'healer': '治疗', 'dps': '输出'}.get(member['role'], ''),
            'profile_url': profile_url, 'character_url': character_link(url=profile_url)})
    stale = bool(row.last_error or not row.fetched_at or row.fetched_at < timezone.now() - TTL)
    return {'roster_status': 'stale' if stale else 'ready', 'roster': members}


def project_run_rosters(runs, rio_season):
    """Exactly one bulk lookup, including cached misses; output keeps run order."""
    keys = []
    for run in runs:
        try:
            keys.append(run_reference(run, rio_season))
        except (ValueError, TypeError):
            keys.append(None)
    rows = {row.run_id: row for row in RaiderIORunRoster.objects.filter(
        rio_season=rio_season, run_id__in={key for key in keys if key is not None})}
    return [_project(rows.get(key), run, rio_season) if key is not None
            else {'roster_status': 'unavailable', 'roster': []} for key, run in zip(keys, runs)]


def top20_run_candidates(season):
    """Bounded identities first; no historical activity JSON or profile refresh."""
    from botend.services.player_raiderio_activity import top20_candidates
    candidates = top20_candidates(season)
    digests = [activity_identity_digest(**item['identity']) for item in candidates if item['identity']]
    activities = RaiderIOCharacterActivity.objects.filter(season=season, identity_digest__in=digests).only('id', 'payload_json').order_by('pk')
    runs, invalid, conflicts = {}, 0, set()
    for activity in activities:
        for run in activity.payload_json.get('mythic_plus_best_runs', [])[:MAX_BEST_RUNS]:
            try:
                key = run_reference(run, season.rio_season)
            except (ValueError, TypeError):
                invalid += 1
                continue
            if key in runs:
                if any(runs[key].get(field) != run.get(field) for field in ('dungeon', 'mythic_level', 'clear_time_ms')):
                    conflicts.add(key)
            else:
                runs[key] = run
    return [{'run_id': key, 'run': value, 'conflict': key in conflicts}
            for key, value in list(runs.items())[:MAX_CANDIDATES]], invalid
