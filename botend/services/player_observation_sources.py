"""Explicit bounded source refresh; no scheduler, profile gear, or AI inference.

Best + recent is a sample, NOT season history. Teammate facts are centralized
by verified identity, even when the teammate is not a Top20 primary target.
"""
import json
import hashlib
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
from botend.services.player_raiderio_activity import ActivityCollector, player_identity, mplus_modules_ready
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


def _request_key(question):
    key = question.get('identity_digest')
    if not isinstance(key, str):
        return None
    if question.get('kind') == 'scored_history':
        did = question.get('dungeon_id')
        return (key, did) if type(did) is int and did > 0 else None
    return key


def _merge_behavior_questions(evidence, recent_ids):
    metrics = (evidence or {}).get('metrics', {})
    review = metrics.get('behavior_review') or {}
    questions = review.get('questions', [])
    if not questions:
        return evidence
    investigation = dict(metrics.get('investigation') or {'version': 1})
    merged = list(investigation.get('questions', []))
    for q in questions[:80]:
        if not isinstance(q, dict):
            continue
        ids = q.get('run_ids')
        if (q.get('kind') not in ('scored_runs', 'scored_history') or not isinstance(ids, list)
                or not ids or any(type(rid) is not int or rid not in recent_ids for rid in ids)):
            continue
        if q.get('purpose') == 'resilience_gap':
            # Do not resurrect raw candidates discarded by the central planner.
            # Existing canonical questions still go through strict validation.
            targets = investigation.get('resilience_history_targets', [])
            if not isinstance(targets, list) or not any(
                    isinstance(target, dict)
                    and all(target.get(field) == q.get(field) for field in
                            ('member_key', 'dungeon_id', 'required_level', 'run_ids'))
                    for target in targets):
                continue
        signature = json.dumps([q.get('kind'), q.get('member_key'), q.get('dungeon_id'),
                                q.get('purpose'), q.get('required_level'), sorted(ids)], sort_keys=True)
        qid = 'behavior_' + hashlib.sha256(signature.encode()).hexdigest()[:20]
        if any(old.get('kind') == q.get('kind') and old.get('identity_digest') == q.get('member_key')
               and old.get('dungeon_id') == q.get('dungeon_id') and old.get('run_ids') == ids
               for old in merged if isinstance(old, dict)):
            continue
        merged.append({**q, 'id': qid, 'identity_digest': q.get('member_key')})
    investigation.update(questions=merged, triggered=investigation.get('triggered') is True or len(merged) > len(investigation.get('questions', [])))
    return {**evidence, 'metrics': {**metrics, 'investigation': investigation}}


def _question_outcomes(investigation, entries, characters, states, scores, rio_season):
    """Bind questions to verified members of their exact canonical runs, never URLs."""
    from botend.services.player_observation_progression import current_record
    runs = {run_reference(run, rio_season): fact for run, fact in entries}
    members = {rid: {activity_identity_digest(**c['identity'])
                     for c in _characters([fact], '')[0]} for rid, fact in runs.items()}
    outcomes, seen = [], set()
    for question in investigation.get('questions', []):
        if not isinstance(question, dict):
            continue
        qid, key = question.get('id'), question.get('identity_digest')
        if not isinstance(qid, str) or not qid or qid in seen:
            continue
        seen.add(qid)
        item = {field: question.get(field) for field in
                ('id', 'kind', 'identity_digest', 'dungeon_id', 'run_ids', 'hypothesis_ids', 'priority', 'reason', 'purpose', 'expected_change', 'core_members', 'prior_run_ids', 'label_codes', 'required_level')}
        item.update(status='invalid_question', request_status='not_requested')
        outcomes.append(item)
        ids = question.get('run_ids')
        if (question.get('kind') not in ('scored_runs', 'scored_history') or not isinstance(ids, list) or not ids
                or any(type(rid) is not int or rid not in runs for rid in ids)):
            continue
        if not isinstance(key, str) or key not in characters:
            item['status'] = 'identity_unresolved'
            continue
        if any(key not in members[rid] for rid in ids):
            continue
        if question.get('purpose') == 'verify_guest_gain':
            core = question.get('core_members')
            proofs = question.get('prior_run_ids')
            if (not isinstance(core, list) or len(set(core)) != 4
                    or not isinstance(proofs, dict) or set(proofs) != set(core)
                    or any(type(rid) is not int or rid <= 0 or rid in ids for rid in proofs.values())
                    or any(set(core) != members[rid] - {key} for rid in ids)):
                continue
        if question.get('purpose') == 'verify_core_history':
            core = question.get('core_members')
            if (not isinstance(core, list) or len(set(core)) != 4 or key not in core
                    or any(not set(core) < members[rid] for rid in ids)):
                continue
        from botend.services.player_observation_resolution import PURPOSES
        if question.get('purpose') in PURPOSES:
            if question.get('kind') != PURPOSES[question['purpose']]:
                continue
            codes = question.get('label_codes')
            allowed = {'joint_progress'} if question['purpose'] == 'verify_joint_history' else {
                'being_carried', 'high_score_collaborator'}
            if question['purpose'] != 'resilience_gap' and (
                    not isinstance(codes, list) or not codes or any(code not in allowed for code in codes)):
                continue
            if question['purpose'] != 'resilience_gap' and any(
                    runs[rid]['dungeon']['id'] != question.get('dungeon_id') for rid in ids):
                continue
        request_key = _request_key(question)
        if question.get('kind') == 'scored_history':
            did = question.get('dungeon_id')
            if type(did) is not int or did <= 0:
                continue
            if question.get('purpose') == 'resilience_gap' or any(runs[rid]['dungeon']['id'] != did for rid in ids):
                level = question.get('required_level')
                targets = investigation.get('resilience_history_targets', [])
                if (question.get('purpose') != 'resilience_gap' or type(level) is not int
                        or any(type(runs[rid].get('mythic_level')) is not int
                               or level != runs[rid]['mythic_level'] + 1 for rid in ids)
                        or not isinstance(targets, list)
                        or not any(isinstance(target, dict)
                                   and target.get('member_key') == key
                                   and type(target.get('dungeon_id')) is int
                                   and target['dungeon_id'] == did
                                   and target.get('run_ids') == ids
                                   and isinstance(target.get('run_ids'), list)
                                   and all(type(rid) is int for rid in target['run_ids'])
                                   and type(target.get('required_level')) is int
                                   and target['required_level'] == level
                                   for target in targets)):
                    continue
        state = states.get(request_key, {'status': 'pending'})['status']
        if question.get('kind') == 'scored_history' and state in ('ready', 'fresh'):
            item.update(status='observed_partial', request_status=state)
        elif state in ('ready', 'fresh'):
            item['request_status'] = 'fresh'
            fetched = parse_datetime(states[key].get('fetched_at', ''))
            item['status'] = ('answered' if all(current_record(
                scores.get(key, {}).get('dungeons', []), runs[rid]['dungeon'])[0] is not None
                and fetched and timezone.is_aware(fetched)
                and (completed := parse_datetime(runs[rid]['completed_at']))
                and timezone.is_aware(completed) and fetched >= completed
                for rid in ids) else 'source_missing')
        elif state == 'pending':
            item['status'] = 'budget_pending'
        else:
            item.update(status='source_failed', request_status=state)
    return outcomes


def _preferred_question_ids(activity, evidence):
    """Reuse only validated AI priorities bound to the same currently valid facts."""
    from botend.models import PlayerObservationAnalysis
    from botend.services.player_observation import _same_publication, _validate_frozen_publication, InvalidResult
    plan = (evidence or {}).get('metrics', {}).get('investigation') or {}
    if plan.get('triggered') is not True or not plan.get('questions'):
        return []
    row = PlayerObservationAnalysis.objects.filter(activity_id=activity.pk, status='ready', last_error='').only(
        'prompt_version', 'evidence_json', 'evidence_hash', 'result_json').first()
    if row is None or not _same_publication(row, evidence):
        return []
    try:
        result = _validate_frozen_publication(row)
    except (InvalidResult, TypeError, ValueError):
        return []
    allowed = {q['id'] for q in plan['questions']}
    return [qid for qid in result.get('investigation', {}).get('next_question_ids', []) if qid in allowed][:3]


def _followup_plan(evidence, activity, entries, characters, states, scores, rio_season, *, preferred_question_ids=()):
    """只执行有具体成员、场次、跑前证明和预期判断变化的待验证问题。"""
    if (evidence or {}).get('source_failed'):
        return [], 'source_failed'
    plan = (evidence or {}).get('metrics', {}).get('investigation') or {}
    if not plan.get('triggered'):
        return [], 'no_signal'
    from botend.services.player_observation_resolution import PURPOSES
    permitted = [q for q in plan.get('questions', []) if isinstance(q, dict)
                 and q.get('reason') and q.get('expected_change') and (
                     q.get('purpose') in PURPOSES and q.get('kind') == PURPOSES[q['purpose']] or
                     q.get('purpose') in ('verify_guest_gain', 'verify_core_history')
                     and q.get('kind') == 'scored_history'
                     and isinstance(q.get('core_members'), list) and len(q['core_members']) == 4
                     and (q['purpose'] == 'verify_core_history' or
                          isinstance(q.get('prior_run_ids'), dict) and len(q['prior_run_ids']) == 4))]
    preference = {qid: i for i, qid in enumerate(preferred_question_ids)}
    permitted.sort(key=lambda q: (preference.get(q.get('id'), len(preference)), -q.get('priority', 0)))
    outcomes = _question_outcomes({**plan, 'questions': permitted}, entries, characters, states, scores, rio_season)
    pending = {q['id'] for q in outcomes if q['status'] == 'budget_pending'}
    planned = [q for q in permitted if q['id'] in pending]
    return list(dict.fromkeys(_request_key(q) for q in planned)), (planned[0]['purpose'] if planned else 'verify_guest_gain')


def _finish_investigation(before, after, entries, characters, states, scores, attempts, result, rio_season):
    """A completed bounded check is not proof that every hypothesis is resolved."""
    initial = before['metrics']['investigation']
    current = (after or {}).get('metrics', {}).get('investigation') or initial
    questions = {q['id']: q for q in initial.get('questions', [])}
    # Successful collection can remove a gap and its authorization. Re-evaluate
    # the original questions with their execution-time scope, but current facts
    # and source states. Never lend that authorization to a new/changed question,
    # even when it reuses an old ID or the same character/dungeon request key.
    outcomes_by_id = {q['id']: q for q in _question_outcomes(
        initial, entries, characters, states, scores, rio_season)}
    changed = [q for q in current.get('questions', []) if q != questions.get(q['id'])]
    attempted_questions = [state['question'] for state in attempts.values()]
    outcomes_by_id.update({q['id']: q for q in _question_outcomes(
        {**current, 'questions': attempted_questions}, entries, characters, states, scores, rio_season)})
    outcomes_by_id.update({q['id']: q for q in _question_outcomes(
        {**current, 'questions': changed}, entries, characters, states, scores, rio_season)})
    outcomes = list(outcomes_by_id.values())
    active = {q['id'] for q in current.get('questions', [])}
    review = (after or {}).get('metrics', {}).get('behavior_review', {})
    confirmed_runs = (set(review.get('witnesses', {}).get('carrying', []))
                      if review.get('supported', {}).get('carrying') else set())
    for item in outcomes:
        if _request_key(item) in attempts:
            item['request_status'] = attempts[_request_key(item)]['status']
        elif item['id'] not in active and item['status'] == 'budget_pending':
            item['status'] = 'not_needed'
        if (item['purpose'] == 'verify_guest_gain' and item['status'] == 'observed_partial'
                and item['run_ids'] and set(item['run_ids']) <= confirmed_runs):
            item['status'] = 'answered'
    triggered = initial.get('triggered') is True
    remaining_runs = [s['run_id'] for s in result['runs'] if s['status'] == 'budget_exhausted']
    allowed_runs = ('ready', 'fresh', 'unavailable', 'not_needed', 'budget_exhausted') if triggered else ('ready', 'fresh', 'unavailable', 'not_needed')
    failed = (bool((after or {}).get('source_failed')) or result.get('invalid_references')
              or result.get('identity_conflicts') or result.get('runs_truncated')
              or any(q['status'] in ('source_failed', 'invalid_question') for q in outcomes)
              or any(s['status'] not in allowed_runs for s in result['runs']))
    remaining = [q['id'] for q in outcomes if q['status'] not in ('answered', 'not_needed')]
    pending = any(q['status'] == 'budget_pending' for q in outcomes) or bool(remaining_runs)
    if result.get('followup_budget_reason') == 'rate_limited':
        stop = 'rate_limited'
    elif failed:
        stop = 'source_failed'
    elif not triggered:
        stop = 'not_triggered'
    elif pending:
        stop = 'budget_completed'
    elif remaining:
        stop = 'source_missing'
    else:
        stop = 'completed'
    result['source_failed'] = bool((after or {}).get('source_failed'))
    result['investigation'] = {**current, 'triggered': triggered,
        'input_evidence_hash': before.get('evidence_hash'), 'evidence_hash': (after or {}).get('evidence_hash'),
        'outcomes': outcomes, 'remaining_question_ids': remaining, 'remaining_run_ids': remaining_runs,
        'requests': [state for state in attempts.values()],
        'api_requests': result['followup_api_requests'], 'api_budget': result['followup_api_budget'],
        'bounded_complete': stop not in ('rate_limited', 'source_failed'), 'stop_reason': stop}
    result['followup_triggered'] = triggered
    result['followup_budget_reason'] = stop if triggered else 'not_needed'


def _progressive_sources(primary, season, activity, digest, batch, result,
                         max_runs, max_characters, roster_budget, followup_budget):
    from botend.services.player_observation_questions import observed_runs, roster_questions
    _, recent, runs, conflicts, invalid = observed_runs(activity, season.rio_season)
    refs = list(runs.values())
    result['invalid_references'] = invalid + len(conflicts)
    rows = {row.run_id: row for row in RaiderIORunRoster.objects.filter(
        rio_season=season.rio_season, run_id__in=runs)}
    visible = {rid: _visible_facts(rows.get(rid), run, season.rio_season) for rid, run in runs.items()}
    result['runs_truncated'] = False
    recent_ids = {run_reference(run, season.rio_season) for run in recent}
    questions = roster_questions(subject=digest, runs=runs, visible=visible, season=season.rio_season, recent_ids=recent_ids)
    from botend.services.player_observation import build_evidence

    def roster_question_resolved():
        review = (build_evidence(primary) or {}).get('metrics', {}).get('behavior_review', {})
        return bool(review.get('supported', {}).get('carrying') or any(
            q.get('purpose') in ('verify_guest_gain', 'verify_core_history') for q in review.get('questions', [])))

    if questions and roster_question_resolved():
        questions = {rid: q for rid, q in questions.items() if q['purpose'] != 'verify_repeated_relationship'
                     or (len(recent_ids) >= 3 and rid in recent_ids)}
    def priority(run):
        rid = run_reference(run, season.rio_season)
        try:
            completed = parse_datetime(run.get('completed_at', ''))
            order = -completed.timestamp() if completed and timezone.is_aware(completed) else 0
        except (ValueError, TypeError):
            order = 0
        return (not bool(visible[rid]), rid not in questions,
                -questions.get(rid, {}).get('prior_level_advantage', 0), rid not in recent_ids,
                not (type(run.get('num_keystone_upgrades')) is int and run['num_keystone_upgrades'] > 0), order)
    refs = sorted(refs, key=priority)
    roster_start, entries = batch.requests, []
    for run in refs:
        rid = run_reference(run, season.rio_season)
        fact, row = visible[rid], rows.get(rid)
        # Immutable facts avoid TTL refresh, not retries of failed collection.
        repair = bool(fact and any('id' not in member['character'] for member in fact['roster']))
        protected = (row and row.next_attempt_at and row.next_attempt_at > timezone.now()
                     and (row.status != 'ready' or row.last_error))
        if fact and not repair and protected:
            state = {'run_id': rid, 'status': 'busy' if row.status == 'fetching' else 'backoff'}
        elif fact and not repair and row.status != 'error' and not row.last_error:
            state = {'run_id': rid, 'status': 'fresh'}
        elif rid not in questions:
            state = {'run_id': rid, 'status': 'not_needed', 'reason': '没有需要该阵容回答的具体问题。'}
        elif batch.stop_event.is_set():
            state = {'run_id': rid, 'status': 'batch_stopped'}
        elif batch.requests - roster_start >= min(roster_budget, max_runs):
            state = {'run_id': rid, 'status': 'budget_exhausted'}
        else:
            state = batch.roster(run, season.rio_season)
            row = RaiderIORunRoster.objects.filter(rio_season=season.rio_season, run_id=rid).first()
            fact = _visible_facts(row, run, season.rio_season)
            state['question'] = questions[rid]
            visible[rid] = fact
            questions = roster_questions(subject=digest, runs=runs, visible=visible, season=season.rio_season, recent_ids=recent_ids)
            if questions and roster_question_resolved():
                questions = {rid: q for rid, q in questions.items() if q['purpose'] != 'verify_repeated_relationship'
                             or (len(recent_ids) >= 3 and rid in recent_ids)}
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
        before = _merge_behavior_questions(build_evidence(primary), recent_ids)
        result['source_failed'] = bool((before or {}).get('source_failed'))
        from botend.services.player_observation_history import history_cache, collect_history
        completion_times = {run_reference(run, season.rio_season): parse_datetime(fact['completed_at'])
                            for run, fact in entries}

        def required_after(question):
            ids = question.get('run_ids')
            dates = [completion_times[rid] for rid in ids if type(rid) is int and rid in completion_times] if isinstance(ids, list) else []
            return max(dates) if dates else None

        for q in ((before or {}).get('metrics', {}).get('investigation') or {}).get('questions', []):
            if not isinstance(q, dict):
                continue
            key, did = q.get('identity_digest'), q.get('dungeon_id')
            if q.get('kind') == 'scored_history' and isinstance(key, str) and key in characters and type(did) is int and did > 0:
                states[(key, did)] = history_cache(characters[key], season, did, required_after=required_after(q))
        investigation = (before or {}).get('metrics', {}).get('investigation')
        allowed_questions = {q['id'] for q in (investigation or {}).get('questions', [])}
        preferred = [qid for qid in _preferred_question_ids(activity, before) if qid in allowed_questions]
        planned, reason = _followup_plan(before, activity, entries, characters, states, scores, season.rio_season,
                                        preferred_question_ids=preferred)
        result['followup_preferred_question_ids'] = preferred
        attempts = {}
        current = before
        result.update(followup_triggered=bool(planned), followup_reason=reason,
                      followup_budget_reason='needed' if planned else 'not_needed')
        batch.budget = start + followup_budget
        while planned and not batch.stop_event.is_set():
            if batch.requests - start >= followup_budget:
                result['followup_budget_reason'] = 'budget_exhausted'
                break
            key = next((key for key in planned if key not in attempts), None)
            if key is None:
                break
            current_plan = (current.get('metrics', {}).get('investigation') or {})
            from botend.services.player_observation_resolution import PURPOSES
            # 相同请求可能服务多个标签，只采用当前仍通过成员、场次与副本校验的问题。
            question = next((q for q in current_plan.get('questions', []) if _request_key(q) == key
                and q.get('purpose') in (*PURPOSES, 'verify_guest_gain', 'verify_core_history')
                and q.get('reason') and q.get('expected_change') and any(
                    item['status'] == 'budget_pending' for item in _question_outcomes(
                        {**current_plan, 'questions': [q]}, entries, characters, states, scores, season.rio_season))), None)
            if not question or question.get('purpose') not in (*PURPOSES, 'verify_guest_gain', 'verify_core_history'):
                break
            request_start = batch.requests
            if question['kind'] == 'scored_runs':
                states[key] = batch.scored(characters[key], season, False)
                _, score = _score_cache(characters[key], season)
                if score is not None:
                    scores[key] = score
            else:
                states[key] = collect_history(batch, characters[key[0]], season, key[1], required_after=required_after(question))
            attempts[key] = {**states[key], 'api_requests': batch.requests - request_start,
                             'question': question}
            # 每次补证后重算，不执行已解决或已失效的旧问题。
            current = _merge_behavior_questions(build_evidence(primary), recent_ids)
            for q in ((current or {}).get('metrics', {}).get('investigation') or {}).get('questions', []):
                target = _request_key(q)
                if isinstance(target, tuple) and target[0] in characters:
                    states[target] = history_cache(characters[target[0]], season, target[1], required_after=required_after(q))
            planned, reason = _followup_plan(current, activity, entries, characters, states, scores,
                season.rio_season, preferred_question_ids=())
        result['followup_api_requests'] = batch.requests - start
        if batch.rate_limited:
            result['followup_budget_reason'] = 'rate_limited'
        elif not planned:
            result['followup_budget_reason'] = 'completed'
        if isinstance(investigation, dict) and investigation.get('version') == 1:
            # Rebuild from the just-published central snapshots, never infer the
            # answer from HTTP success or mutate the original evidence in place.
            _finish_investigation(before, current, entries, characters, states, scores,
                                  attempts, result, season.rio_season)
    result['characters'] = list(states.values())
    if batch.rate_limited:
        result['status'] = 'rate_limited'
    elif (not result['invalid_references'] and not result['identity_conflicts']
          and not result.get('source_failed')
          and not result['runs_truncated'] and not result['characters_truncated']
          and all(s['status'] in ('ready', 'fresh', 'not_needed') for s in result['runs'])
          and not result['followup_triggered']):
        result['status'] = 'ready'
    return result


def refresh_observation_sources(player, *, max_runs=MAX_RUNS, max_characters=MAX_CHARACTERS, force=False, refresh_recent=False,
                                progressive=False, max_followup_requests=2, max_basic_roster_requests=5):
    """显式刷新来源并返回可审计结果；不会启动自动监控。

    渐进分析优先复用已有记录。阵容请求只核验重复成绩后的组队关系；
    历史请求只核验明确重复组合中成员的同本跑前能力，以及具体第五人的可见成绩提升。
    每次补证后重算问题；资料不齐本身不是请求理由。
    原有五次阵容、两次后续请求限制不变，额度不互借；force 不绕过缓存、
    隐私、退避或租约。非渐进模式保留显式来源采集的既有行为。
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
        collector_status = result['activity']['status']
        activity = None
        if collector_status == 'backoff' and not batch.stop_event.is_set():
            # A successful raid-partial envelope has a retry timer too. Reuse
            # only this collector's exact identity/season row, with all M+
            # clocks current to the latest attempt; failed last-good stays blocked.
            activity = RaiderIOCharacterActivity.objects.filter(
                pk=result['activity'].get('activity_id'), season=season, identity_digest=digest).first()
            if activity is not None and activity.status == 'partial' and mplus_modules_ready(activity):
                result['activity'] = {**result['activity'], 'status': 'fresh',
                                      'collector_status': collector_status}
        if batch.stop_event.is_set() or result['activity']['status'] not in ('ready', 'fresh', 'partial'):
            result['status'] = 'rate_limited' if batch.rate_limited else result['activity']['status']
            return result
        if activity is None:
            activity = RaiderIOCharacterActivity.objects.get(season=season, identity_digest=digest)
        # This source checkpoint describes M+, not the global raid+M+ envelope.
        # Keep the actual envelope status for audit; never rewrite central state.
        result['activity'] = {**result['activity'], 'scope': 'mythic_plus',
                              'global_status': activity.status}
        if result['activity']['status'] == 'partial' and mplus_modules_ready(activity):
            result['activity']['status'] = 'ready'
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
