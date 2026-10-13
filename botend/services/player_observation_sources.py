"""Explicit bounded source refresh; no scheduler, profile gear, or AI inference.

Best + recent is a sample, NOT season history. Teammate facts are centralized
by verified identity, even when the teammate is not a Top20 primary target.
"""
import math
import re
import threading
from datetime import timedelta
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.constants.wow import canonical_class_spec
from botend.models import PlayerSpecTopPlayer, RaiderIOCharacterActivity, RaiderIORunRoster, SeasonMeta
from botend.raiderio_identity import activity_identity_digest, normalized_name
from botend.services.player_raiderio_activity import ActivityCollector, player_identity
from botend.services.raiderio_run_roster import (
    RosterCollector, _pace, _positive, _profile_url, run_reference, validate_details,
)

MAX_RUNS = 18
MAX_CHARACTERS = 40
SCORED_TTL = timedelta(hours=24)
SCORED_URL = 'https://raider.io/api/characters/mythic-plus-scored-runs'


def _scope(rio_season, character_id):
    if not isinstance(rio_season, str) or not re.fullmatch(r'season-[a-z0-9-]+', rio_season):
        raise ValueError('invalid season')
    return {'season': rio_season, 'role': 'all', 'mode': 'scored', 'affixes': 'all',
            'date': 'all', 'characterId': _positive(character_id)}


def _label(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 200
            or any(ord(c) < 32 for c in value)):
        raise ValueError('invalid label')
    return value


def _scored_record(record, rio_season, *, alternate=False):
    if not isinstance(record, dict):
        raise ValueError('invalid scored record')
    if (record.get('season', rio_season) != rio_season
            or record.get('status', 'finished') != 'finished'
            or record.get('deleted_at') not in (None, '')
            or record.get('runPrivacyMode', 'public') != 'public'):
        raise ValueError('wrong scored run scope')
    dungeon = record.get('dungeon')
    if not isinstance(dungeon, dict):
        raise ValueError('missing dungeon')
    clean = {'dungeon': {'id': _positive(dungeon.get('id')),
                         'name': _label(dungeon.get('name')), 'slug': _label(dungeon.get('slug'))},
             'keystone_run_id': _positive(record.get('keystone_run_id')),
             'mythic_level': _positive(record.get('mythic_level'))}
    score = record.get('score')
    if type(score) not in (int, float) or not math.isfinite(score) or score < 0:
        raise ValueError('invalid score')
    chests = record.get('num_chests')
    if type(chests) is not int or not 0 <= chests <= 3:
        raise ValueError('invalid chests')
    completed = record.get('completed_at')
    if not isinstance(completed, str):
        raise ValueError('missing completion time')
    stamp = parse_datetime(completed)
    if stamp is None or timezone.is_naive(stamp):
        raise ValueError('invalid completion time')
    role = record.get('role')
    if role not in ('tank', 'healer', 'dps'):
        raise ValueError('invalid role')
    clean.update(score=score, num_chests=chests, completed_at=completed, role=role)
    if alternate:
        if record.get('alternate') is not None:
            raise ValueError('nested alternate')
    else:
        if 'alternate' not in record:
            raise ValueError('missing alternate')
        alt = record['alternate']
        clean['alternate'] = None if alt is None else _scored_record(alt, rio_season, alternate=True)
        if clean['alternate'] and (clean['alternate']['dungeon'] != clean['dungeon']
                or clean['alternate']['keystone_run_id'] == clean['keystone_run_id']):
            raise ValueError('conflicting alternate')
    return clean


def validate_scored_runs(data, rio_season, character_id):
    """Strict webpage scope and records; return a bounded JSON-safe whitelist.

Empty dungeons is valid but is not evidence a missing dungeon/run contributes
zero. This endpoint describes current scored runs, never historical score delta.
"""
    scope = _scope(rio_season, character_id)
    if (not isinstance(data, dict) or not isinstance(data.get('ui'), dict)
            or data['ui'] != scope or type(data['ui'].get('characterId')) is not int):
        raise ValueError('wrong scored scope')
    rows = data.get('dungeons')
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError('invalid dungeon list')
    clean, dungeon_ids, run_ids = [], set(), set()
    for row in rows:
        item = _scored_record(row, rio_season)
        if item['dungeon']['id'] in dungeon_ids:
            raise ValueError('duplicate dungeon')
        dungeon_ids.add(item['dungeon']['id'])
        for run in (item, item['alternate']):
            if run is not None:
                if run['keystone_run_id'] in run_ids:
                    raise ValueError('duplicate scored run')
                run_ids.add(run['keystone_run_id'])
        clean.append(item)
    return {'ui': scope, 'dungeons': clean, 'source_url': SCORED_URL + '?' + urlencode(scope)}


class _ActivityOnlyCollector(ActivityCollector):
    def _refresh_rosters(self, result, row, season):
        # Observation discovery owns its own best+recent union and hard budget.
        return result


class _SourceBatch:
    """Shared transport counts EVERY API call, including activity and repairs.

Transport injection is private and for tests only. The public entry uses a real
Session, existing proxy settings, 25s timeout, no redirects or automatic retry.
"""
    def __init__(self, budget, transport=None):
        self.session = requests.Session()
        self.transport = transport or self.session.get
        cfg = getattr(settings, 'REQUEST_CONFIG', {})
        self.proxies = cfg.get('proxies') if cfg.get('enable_proxy') else None
        self.stop_event = threading.Event()
        self.budget, self.requests = budget, 0
        self.rate_limited = False
        self.rosters = RosterCollector(transport=self.get, proxies=self.proxies, stop_event=self.stop_event)

    def close(self):
        self.rosters.close()
        self.session.close()

    def get(self, url, **kwargs):
        if self.stop_event.is_set() or self.requests >= self.budget:
            self.stop_event.set()
            raise requests.RequestException('observation budget stopped')
        # RosterCollector already participates in this same process-wide pace.
        if not url.endswith('/run-details') and not _pace(self.stop_event):
            raise requests.RequestException('observation batch stopped')
        self.requests += 1
        kwargs.update(timeout=25, proxies=self.proxies, allow_redirects=False)
        response = self.transport(url, **kwargs)
        if response.status_code == 429:
            self.rate_limited = True
            self.stop_event.set()
        return response

    def roster(self, run, rio_season):
        run_id = run_reference(run, rio_season)
        row = RaiderIORunRoster.objects.filter(rio_season=rio_season, run_id=run_id).first()
        if row and not row.roster_visible:
            return {'run_id': run_id, 'status': 'unavailable'}
        facts = _visible_facts(row, run, rio_season)
        missing_id = bool(facts and any('id' not in m['character'] for m in facts['roster']))
        if missing_id:
            # RosterCollector.force normally bypasses backoff. Lock/check this
            # exact row through the bounded repair so another writer cannot turn
            # a freshness bypass into a privacy/backoff/lease bypass. Normal
            # refreshes still use the collector's short lease transaction.
            with transaction.atomic():
                row = RaiderIORunRoster.objects.select_for_update().get(pk=row.pk)
                if not row.roster_visible:
                    return {'run_id': run_id, 'status': 'unavailable'}
                now = timezone.now()
                if row.next_attempt_at and row.next_attempt_at > now and row.status != 'ready':
                    return {'run_id': run_id, 'status': 'busy' if row.status == 'fetching' else 'backoff'}
                facts = _visible_facts(row, run, rio_season)
                repair = bool(facts and any('id' not in m['character'] for m in facts['roster']))
                return self.rosters.refresh(run, rio_season, force=repair)
        return self.rosters.refresh(run, rio_season)

    def scored(self, character, season, force):
        identity, character_id = character['identity'], character['character_id']
        digest = activity_identity_digest(**identity)
        row, _ = RaiderIOCharacterActivity.objects.get_or_create(
            season=season, identity_digest=digest, defaults=identity)
        result = {'activity_id': row.pk, 'identity_digest': digest, 'character_id': character_id}
        # The schema deliberately has no scored lease fields. A NOWAIT row lock
        # serializes this independent snapshot without changing activity status,
        # timestamps, failure counters, or payload. Held for ONE bounded request.
        try:
            with transaction.atomic():
                row = RaiderIOCharacterActivity.objects.select_for_update(nowait=True).get(pk=row.pk)
                now = timezone.now()
                if row.next_attempt_at and row.next_attempt_at > now and row.status in ('fetching', 'error'):
                    return {**result, 'status': 'busy' if row.status == 'fetching' else 'backoff'}
                if not force and row.scored_runs_fetched_at and row.scored_runs_fetched_at > now - SCORED_TTL:
                    try:
                        validate_scored_runs({key: row.scored_runs_json[key] for key in ('ui', 'dungeons')},
                                             season.rio_season, character_id)
                    except (ValueError, TypeError, KeyError, AttributeError):
                        pass
                    else:
                        return {**result, 'status': 'fresh', 'fetched_at': row.scored_runs_fetched_at.isoformat()}
                try:
                    response = self.get(SCORED_URL, params=_scope(season.rio_season, character_id),
                                        headers={'User-Agent': 'Mozilla/5.0'})
                    if response.status_code != 200:
                        return {**result, 'status': 'rate_limited' if response.status_code == 429 else f'http_{response.status_code}'}
                    facts = validate_scored_runs(response.json(), season.rio_season, character_id)
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    return {**result, 'status': 'invalid_payload'}
                except requests.RequestException:
                    return {**result, 'status': 'transport_error'}
                stamp = timezone.now()
                RaiderIOCharacterActivity.objects.filter(pk=row.pk).update(
                    scored_runs_json=facts, scored_runs_fetched_at=stamp)
                return {**result, 'status': 'ready', 'fetched_at': stamp.isoformat()}
        except DatabaseError:
            return {**result, 'status': 'database_busy_or_error'}


def _visible_facts(row, run, rio_season):
    if row is None or not row.roster_visible:
        return None
    try:
        return validate_details(row.payload_json, run, rio_season)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _references(activity, rio_season, limit):
    refs, conflicts, invalid = {}, set(), 0
    for key, cap in (('mythic_plus_best_runs', 100), ('mythic_plus_recent_runs', 10)):
        rows = activity.payload_json.get(key, [])
        if not isinstance(rows, list) or len(rows) > cap:
            invalid += 1
            continue
        for run in rows:
            try:
                run_id = run_reference(run, rio_season)
            except (ValueError, TypeError):
                invalid += 1
                continue
            if run_id in refs and any(refs[run_id].get(k) != run.get(k) for k in
                    ('dungeon', 'mythic_level', 'clear_time_ms', 'num_keystone_upgrades', 'completed_at')):
                conflicts.add(run_id)
            else:
                refs.setdefault(run_id, run)
    valid = [run for key, run in refs.items() if key not in conflicts]
    return valid[:limit], invalid + len(conflicts), len(valid) > limit


def _characters(facts, subject_digest):
    keyed, id_keys, conflicts = {}, {}, set()
    for fact in facts:
        for member in fact['roster']:
            character = member['character']
            if 'id' not in character or not _profile_url(character):
                continue
            identity = {'region': character['region']['slug'], 'realm_slug': character['realm']['slug'],
                        'character_name': normalized_name(character['name'])}
            digest, character_id = activity_identity_digest(**identity), character['id']
            if digest in keyed and keyed[digest]['character_id'] != character_id:
                conflicts.add(digest)
            if character_id in id_keys and id_keys[character_id] != digest:
                conflicts.update((digest, id_keys[character_id]))
            id_keys[character_id] = digest
            keyed.setdefault(digest, {'identity': identity, 'character_id': character_id,
                                      'path': character['path']})
    return sorted((item for key, item in keyed.items() if key not in conflicts),
                  key=lambda item: (activity_identity_digest(**item['identity']) != subject_digest,
                                    activity_identity_digest(**item['identity']))), len(conflicts)


def refresh_observation_sources(player, *, max_runs=MAX_RUNS, max_characters=MAX_CHARACTERS, force=False):
    """Return JSON-safe statuses. Explicit call only; no automatic monitor.

Hard upper limits: one due activity request + max_runs unique roster requests +
max_characters unique scored requests. force refreshes scored snapshots, NOT
fresh ID-complete rosters, profile gear, privacy tombstones, or active backoffs.
"""
    if type(max_runs) is not int or not 1 <= max_runs <= MAX_RUNS:
        raise ValueError('max_runs must be 1..18')
    if type(max_characters) is not int or not 1 <= max_characters <= MAX_CHARACTERS:
        raise ValueError('max_characters must be 1..40')
    result = {'status': 'partial', 'sample': 'bounded_best_plus_recent', 'complete_season': False,
              'max_runs': max_runs, 'max_characters': max_characters,
              'api_budget': 1 + max_runs + max_characters, 'api_requests': 0,
              'runs': [], 'characters': [], 'invalid_references': 0, 'identity_conflicts': 0,
              'runs_truncated': False, 'characters_truncated': False}
    # Re-read only scalar identity/rank fields: callers cannot spoof a Top20 row
    # via an unsaved object or accidentally trigger profile JSON loading.
    primary = PlayerSpecTopPlayer.objects.filter(pk=player.pk).only(
        'id', 'season_id', 'region', 'realm', 'character_name', 'profile_url',
        'class_name', 'spec_name', 'rank').first()
    if primary is None:
        return {**result, 'status': 'not_top20'}
    season = SeasonMeta.objects.filter(pk=primary.season_id, is_active=True).first()
    if season is None or not season.rio_season:
        return {**result, 'status': 'wrong_season'}
    if (primary.rank is None or not 1 <= primary.rank <= 20
            or not canonical_class_spec(primary.class_name, primary.spec_name)):
        return {**result, 'status': 'not_top20'}
    identity = player_identity(primary)
    if identity is None:
        return {**result, 'status': 'unresolved'}
    digest = activity_identity_digest(**identity)
    result.update(subject_identity_digest=digest, subject_character_id=None,
                  characters_discovered=0, rosters_visible=0, members_without_identity=0)
    batch = _SourceBatch(result['api_budget'])
    try:
        activity_collector = _ActivityOnlyCollector(transport=batch.get, proxies=batch.proxies)
        result['activity'] = activity_collector.refresh(primary, season, include_profile=False)
        if batch.stop_event.is_set() or result['activity']['status'] == 'busy':
            result['status'] = 'rate_limited' if batch.rate_limited else result['activity']['status']
            return result
        activity = RaiderIOCharacterActivity.objects.get(season=season, identity_digest=digest)
        refs, result['invalid_references'], result['runs_truncated'] = _references(activity, season.rio_season, max_runs)
        facts = []
        for run in refs:
            collected = batch.roster(run, season.rio_season)
            result['runs'].append(collected)
            if batch.stop_event.is_set():
                break
            row = RaiderIORunRoster.objects.filter(rio_season=season.rio_season,
                                                   run_id=run_reference(run, season.rio_season)).first()
            fact = _visible_facts(row, run, season.rio_season)
            if fact and collected['status'] != 'busy':
                facts.append(fact)
        characters, result['identity_conflicts'] = _characters(facts, digest)
        result['rosters_visible'] = len(facts)
        result['members_without_identity'] = sum(
            'id' not in member['character'] or not _profile_url(member['character'])
            for fact in facts for member in fact['roster'])
        result['characters_discovered'] = len(characters)
        result['subject_character_id'] = next((item['character_id'] for item in characters
            if activity_identity_digest(**item['identity']) == digest), None)
        result['characters_truncated'] = len(characters) > max_characters
        for character in characters[:max_characters]:
            if batch.stop_event.is_set():
                break
            result['characters'].append(batch.scored(character, season, force))
        if batch.rate_limited:
            result['status'] = 'rate_limited'
        elif (result['characters'] and not result['invalid_references'] and not result['identity_conflicts']
              and all(item['status'] in ('ready', 'fresh') for item in result['characters'])
              and all(item['status'] in ('ready', 'fresh') for item in result['runs'])):
            result['status'] = 'ready'
        return result
    finally:
        result['api_requests'] = batch.requests
        batch.close()
