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


def _dungeon(value):
    if not isinstance(value, dict):
        raise ValueError('missing dungeon')
    return {'id': _positive(value.get('id')), 'name': _label(value.get('name')),
            'slug': _label(value.get('slug'))}


def _scored_record(record, rio_season, *, alternate=False):
    if not isinstance(record, dict):
        raise ValueError('invalid scored record')
    if (record.get('season', rio_season) != rio_season
            or record.get('status', 'finished') != 'finished'
            or record.get('deleted_at') not in (None, '')
            or record.get('runPrivacyMode', 'public') != 'public'):
        raise ValueError('wrong scored run scope')
    clean = {'dungeon': _dungeon(record.get('dungeon')),
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
    unscored = data.get('unscored_dungeons', [])
    if not isinstance(rows, list) or not isinstance(unscored, list) or len(rows) + len(unscored) > 100:
        raise ValueError('invalid dungeon list')
    clean, empty, dungeon_ids, run_ids = [], [], set(), set()
    for row in rows:
        # Webpage placeholders have precisely these two keys and no run fact.
        # Preserve their dungeon as unknown without discarding valid siblings.
        if isinstance(row, dict) and set(row) == {'mythic0_completions', 'dungeon'}:
            dungeon = _dungeon(row['dungeon'])
            if dungeon['id'] in dungeon_ids:
                raise ValueError('duplicate dungeon')
            dungeon_ids.add(dungeon['id'])
            empty.append(dungeon)
            continue
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
    for dungeon in unscored:
        dungeon = _dungeon(dungeon)
        if dungeon['id'] in dungeon_ids:
            raise ValueError('duplicate dungeon')
        dungeon_ids.add(dungeon['id'])
        empty.append(dungeon)
    result = {'ui': scope, 'dungeons': clean, 'source_url': SCORED_URL + '?' + urlencode(scope)}
    if empty:
        result['unscored_dungeons'] = empty
    return result


class _ActivityOnlyCollector(ActivityCollector):
    def _refresh_rosters(self, result, row, season, player):
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
                if not force and row.scored_runs_fetched_at and now - SCORED_TTL <= row.scored_runs_fetched_at <= now:
                    try:
                        validate_scored_runs(row.scored_runs_json, season.rio_season, character_id)
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


def _score_cache(character, season):
    """Read-only planning: never create rows or turn unspent budget into errors."""
    digest = activity_identity_digest(**character['identity'])
    row = RaiderIOCharacterActivity.objects.filter(season=season, identity_digest=digest).first()
    state = {'identity_digest': digest, 'character_id': character['character_id'], 'status': 'pending'}
    if row is None:
        return state, None
    state['activity_id'] = row.pk
    now = timezone.now()
    if row.next_attempt_at and row.next_attempt_at > now and row.status in ('fetching', 'error'):
        state['status'] = 'busy' if row.status == 'fetching' else 'backoff'
        return state, None
    if row.scored_runs_fetched_at and now - SCORED_TTL <= row.scored_runs_fetched_at <= now:
        try:
            clean = validate_scored_runs(row.scored_runs_json, season.rio_season, character['character_id'])
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        else:
            state.update(status='fresh', fetched_at=row.scored_runs_fetched_at.isoformat())
            return state, clean
    return state, None


def _followup_plan(evidence, activity, entries, characters, states, scores, rio_season):
    """Consume canonical classification; select only relevant incomplete comparisons."""
    from botend.services.player_observation_progression import current_record, RECENT_WINDOW
    metrics = (evidence or {}).get('metrics', {})
    pattern = metrics.get('team_pattern', {})
    progression = metrics.get('progression') or {}
    if pattern.get('kind') == 'fixed_five':
        return [], 'fixed_five'
    if progression.get('status') in ('supported', 'weakened'):
        return [], 'already_resolved'
    core = set(pattern.get('core_members', []))
    four = (pattern.get('kind') in ('suspected_four', 'rotating_core') and len(core) == 4)
    details = {d['run_id']: d for d in progression.get('details', [])}
    recent = {}
    stamp = activity.module_fetched_at.get('mythic_plus_recent_runs') or activity.fetched_at
    if isinstance(stamp, str):
        stamp = parse_datetime(stamp)
    if stamp and timezone.is_aware(stamp):
        for run in activity.payload_json.get('mythic_plus_recent_runs', [])[:10]:
            try:
                completed = parse_datetime(run.get('completed_at', ''))
                if (completed and timezone.is_aware(completed) and stamp - RECENT_WINDOW <= completed <= stamp
                        and type(run.get('num_keystone_upgrades')) is int and run['num_keystone_upgrades'] > 0):
                    recent[run_reference(run, rio_season)] = completed
            except (ValueError, TypeError):
                continue
    candidates = []
    subject = activity.identity_digest
    for run, fact in entries:
        rid = run_reference(run, rio_season)
        if rid not in recent:
            continue
        members, _ = _characters([fact], subject)
        team = {activity_identity_digest(**member['identity']) for member in members}
        if len(team) != 5 or subject not in team:
            continue
        records = {key: current_record(scores.get(key, {}).get('dungeons', []), fact['dungeon'])[0]
                   for key in team}
        missing = {key for key in team if states.get(key, {}).get('status') == 'pending'}
        if four and core <= team:
            if details.get(rid, {}).get('below_core') is False:
                continue
            needed = missing & core
            # Fifth seats are relevant only after the canonical below-core check,
            # or while completing that same four-person comparison.
            if needed or details.get(rid, {}).get('below_core') is True:
                needed |= missing - core
            reason = 'four_core_missing_scores'
        else:
            own = records.get(subject)
            signal = own and any(record and record['mythic_level'] - own['mythic_level'] >= 2
                                 and record['score'] > own['score']
                                 for key, record in records.items() if key != subject)
            if not signal:
                continue
            needed, reason = missing, 'partial_same_dungeon_gap'
        needed &= characters.keys()
        if needed:
            candidates.append((len(needed), -recent[rid].timestamp(), sorted(needed), reason))
    # Prefer comparisons that the small budget can actually complete, not hash order.
    planned, reason = [], 'cache_sufficient' if four else 'no_signal'
    for _, _, needed, why in sorted(candidates):
        reason = why
        for key in sorted(needed, key=lambda key: (key not in core, key)):
            if key not in planned:
                planned.append(key)
    return planned, reason


def _progressive_sources(primary, season, activity, digest, batch, result,
                         max_runs, max_characters, roster_budget, followup_budget):
    # Restrict cache-first prioritization to the authoritative evidence sample;
    # extra cached best runs must never displace an uncached recent reference.
    from botend.services.player_observation import canonical_observation_runs
    _, recent, runs, conflicts, invalid = canonical_observation_runs(activity, season.rio_season)
    refs = list(runs.values())
    result['invalid_references'] = invalid + len(conflicts)
    result['runs_truncated'] = len(refs) > max_runs
    rows = {row.run_id: row for row in RaiderIORunRoster.objects.filter(
        rio_season=season.rio_season, run_id__in=[run_reference(run, season.rio_season) for run in refs])}
    visible = {run_reference(run, season.rio_season): _visible_facts(
        rows.get(run_reference(run, season.rio_season)), run, season.rio_season) for run in refs}
    recent_ids = set()
    for run in recent:
        try:
            recent_ids.add(run_reference(run, season.rio_season))
        except (ValueError, TypeError):
            pass
    def priority(run):
        rid = run_reference(run, season.rio_season)
        try:
            completed = parse_datetime(run.get('completed_at', ''))
            order = -completed.timestamp() if completed and timezone.is_aware(completed) else 0
        except (ValueError, TypeError):
            order = 0
        return (not bool(visible[rid]), rid not in recent_ids,
                not (type(run.get('num_keystone_upgrades')) is int and run['num_keystone_upgrades'] > 0), order)
    refs = sorted(refs, key=priority)[:max_runs]
    roster_start, entries = batch.requests, []
    for run in refs:
        rid = run_reference(run, season.rio_season)
        fact, row = visible[rid], rows.get(rid)
        # Valid immutable run facts need no TTL refresh, except bounded legacy ID repair.
        repair = bool(fact and any('id' not in member['character'] for member in fact['roster']))
        if fact and not repair:
            now = timezone.now()
            protected = row.next_attempt_at and row.next_attempt_at > now and row.status != 'ready'
            state = {'run_id': rid, 'status': ('busy' if row.status == 'fetching' else 'backoff')
                     if protected else 'fresh'}
        elif batch.stop_event.is_set():
            state = {'run_id': rid, 'status': 'batch_stopped'}
        elif batch.requests - roster_start >= roster_budget:
            state = {'run_id': rid, 'status': 'budget_exhausted'}
        else:
            state = batch.roster(run, season.rio_season)
            row = RaiderIORunRoster.objects.filter(rio_season=season.rio_season, run_id=rid).first()
            fact = _visible_facts(row, run, season.rio_season)
        result['runs'].append(state)
        if fact and state['status'] != 'busy':
            entries.append((run, fact))
        if batch.rate_limited:
            break
    facts = [fact for _, fact in entries]
    discovered, result['identity_conflicts'] = _characters(facts, digest)
    result.update(rosters_visible=len(facts), characters_discovered=len(discovered),
                  characters_truncated=len(discovered) > max_characters,
                  members_without_identity=sum('id' not in m['character'] or not _profile_url(m['character'])
                                               for fact in facts for m in fact['roster']),
                  subject_character_id=next((c['character_id'] for c in discovered
                                             if activity_identity_digest(**c['identity']) == digest), None))
    characters = {activity_identity_digest(**c['identity']): c for c in discovered[:max_characters]}
    states, scores = {}, {}
    for key, character in characters.items():
        states[key], score = _score_cache(character, season)
        if score is not None:
            scores[key] = score
    start = batch.requests
    if not batch.stop_event.is_set():
        from botend.services.player_observation import build_evidence
        planned, reason = _followup_plan(build_evidence(primary), activity, entries, characters, states, scores, season.rio_season)
        result.update(followup_triggered=bool(planned), followup_reason=reason,
                      followup_budget_reason='needed' if planned else 'not_needed')
        batch.budget = start + followup_budget
        for key in planned:
            if batch.stop_event.is_set() or batch.requests - start >= followup_budget:
                result['followup_budget_reason'] = 'rate_limited' if batch.rate_limited else 'budget_exhausted'
                break
            # Recheck under the collector lock; force NEVER defeats a needed-cache plan.
            states[key] = batch.scored(characters[key], season, False)
        result['followup_api_requests'] = batch.requests - start
        if batch.rate_limited:
            result['followup_budget_reason'] = 'rate_limited'
        elif planned and all(states[key]['status'] in ('ready', 'fresh') for key in planned):
            result['followup_budget_reason'] = 'completed'
    result['characters'] = list(states.values())
    if batch.rate_limited:
        result['status'] = 'rate_limited'
    elif (not result['invalid_references'] and not result['identity_conflicts']
          and not result['runs_truncated'] and not result['characters_truncated']
          and all(s['status'] in ('ready', 'fresh') for s in result['runs'])
          and not result['followup_triggered']):
        result['status'] = 'ready'
    return result


def refresh_observation_sources(player, *, max_runs=MAX_RUNS, max_characters=MAX_CHARACTERS, force=False, refresh_recent=False,
                                progressive=False, max_followup_requests=2, max_basic_roster_requests=5):
    """Return JSON-safe statuses. Explicit call only; no automatic monitor.

Hard upper limits: one due activity request + max_runs unique roster requests +
max_characters unique scored requests. force refreshes scored snapshots, NOT
fresh ID-complete rosters, profile gear, privacy tombstones, or active backoffs.
refresh_recent explicitly refreshes a successful activity snapshot even within
TTL, so a progressive manual check sees the latest recent records. It never
bypasses an activity error backoff or an active lease.
With progressive=True, basic work reuses all verified cached rosters before up
 to five missing/legacy roster calls; it never scans scored endpoints. Canonical
build_evidence then gates at most two additional actual scored HTTP requests.
Unused basic budget cannot spill into followup; force cannot bypass needed cache.
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
    if type(progressive) is not bool:
        raise ValueError('progressive must be boolean')
    if type(max_followup_requests) is not int or not 0 <= max_followup_requests <= 2:
        raise ValueError('max_followup_requests must be 0..2')
    if type(max_basic_roster_requests) is not int or not 0 <= max_basic_roster_requests <= 5:
        raise ValueError('max_basic_roster_requests must be 0..5')
    if progressive:
        result.update(collection_mode='progressive', api_budget=1 + max_basic_roster_requests + max_followup_requests,
                      basic_api_budget=1 + max_basic_roster_requests, basic_roster_api_budget=max_basic_roster_requests,
                      followup_api_budget=max_followup_requests, basic_api_requests=0, followup_api_requests=0,
                      followup_triggered=False, followup_reason='not_needed', followup_budget_reason='not_needed')
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
        result['activity'] = activity_collector.refresh(primary, season, include_profile=False, refresh_ready=refresh_recent)
        if batch.stop_event.is_set() or result['activity']['status'] not in ('ready', 'fresh', 'partial'):
            result['status'] = 'rate_limited' if batch.rate_limited else result['activity']['status']
            return result
        activity = RaiderIOCharacterActivity.objects.get(season=season, identity_digest=digest)
        if progressive:
            return _progressive_sources(primary, season, activity, digest, batch, result,
                                        max_runs, max_characters, max_basic_roster_requests, max_followup_requests)
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
        if progressive:
            result['basic_api_requests'] = batch.requests - result['followup_api_requests']
        batch.close()
