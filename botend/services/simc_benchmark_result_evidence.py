"""Bounded read-only Run evidence for immutable Benchmark Result projections.

Unknown evidence is NOT a publication/reuse decision: callers must keep the raw
Result visible. This module never reconciles, validates, or updates execution.
"""
import math
import re
from collections import defaultdict
from types import SimpleNamespace

from django.db.models import Func, JSONField, Q

from botend.models import SimcTask, SimulationRun

MAX_TASKS_PER_BATCH = 20
MAX_CANDIDATE_KEYS = 1000
MAX_SOURCE_DEPTH = 32
SEMANTIC_COMPOSITION_SLOTS = (
    'talents', 'action_list', 'simulation_options', 'additional_simc_input',
    'player_identity', 'stat_overrides',
)


class _CandidateKeys(Func):
    """Project only candidate keys; never hydrate candidate params or mode_params.

    The initial_candidates/nonempty-list fallback is the existing
    _expected_candidate_keys contract. Include the array length because MySQL
    wildcard extraction omits missing keys, which must NOT authorize inheritance.
    """
    output_field = JSONField()

    def as_sql(self, compiler, connection, **extra_context):
        field, params = compiler.compile(self.source_expressions[0])
        # Only a plain model field is used, so repeated expressions have no binds.
        if params:
            raise ValueError('Candidate projection requires a plain model field')
        initial = f"JSON_EXTRACT({field}, '$.initial_candidates')"
        fallback = f"JSON_EXTRACT({field}, '$.request_manifest.candidates')"
        if connection.vendor == 'sqlite':
            kind = f"JSON_TYPE({field}, '$.initial_candidates')"
            array_type = 'array'
        elif connection.vendor == 'mysql':
            kind = f'JSON_TYPE({initial})'
            array_type = 'ARRAY'
        else:
            raise NotImplementedError('Candidate evidence supports SQLite and MySQL')
        length = 'JSON_ARRAY_LENGTH' if connection.vendor == 'sqlite' else 'JSON_LENGTH'
        # Guard JSON_ARRAY_LENGTH from non-JSON SQL strings on SQLite.
        chosen = (f"CASE WHEN {kind} = '{array_type}' THEN "
                  f'CASE WHEN {length}({initial}) > 0 THEN {initial} ELSE {fallback} END '
                  f'ELSE {fallback} END')
        fallback_kind = (f"JSON_TYPE({field}, '$.request_manifest.candidates')"
                         if connection.vendor == 'sqlite' else f'JSON_TYPE({fallback})')
        selected_kind = (f"CASE WHEN {kind} = '{array_type}' THEN "
                         f'CASE WHEN {length}({initial}) > 0 THEN {kind} ELSE {fallback_kind} END '
                         f'ELSE {fallback_kind} END')
        # Scalar/missing fallback is unusable, not an empty ownership set.
        bounded = (f"CASE WHEN {selected_kind} = '{array_type}' THEN "
                   f'CASE WHEN {length}({chosen}) BETWEEN 1 AND {MAX_CANDIDATE_KEYS} '
                   f'THEN {chosen} ELSE NULL END ELSE NULL END')
        if connection.vendor == 'sqlite':
            keys = ("(SELECT JSON_GROUP_ARRAY(CASE WHEN entry.type = 'object' "
                    "THEN JSON(entry.value -> '$.candidate_key') ELSE NULL END) "
                    f'FROM JSON_EACH({bounded}) AS entry)')
        else:
            keys = f"JSON_EXTRACT({bounded}, '$[*].candidate_key')"
        # Also cap pathological strings before they cross the DB/ORM boundary.
        sql = (f'CASE WHEN LENGTH({keys}) <= {MAX_CANDIDATE_KEYS * 1208} '
               f"THEN JSON_OBJECT('count', {length}({bounded}), 'keys', {keys}) "
               'ELSE NULL END')
        return sql, []


class _ScalarPaths(Func):
    """Select bounded JSON scalar leaves, preserving booleans/strings on SQLite."""
    output_field = JSONField()

    def __init__(self, field, paths):
        self.paths = paths
        super().__init__(field)

    def as_sql(self, compiler, connection, **extra_context):
        field, params = compiler.compile(self.source_expressions[0])
        if params:
            raise ValueError('Scalar projection requires a plain model field')
        pairs, params = [], []
        for name, path in self.paths.items():
            if connection.vendor == 'sqlite':
                kind = f'JSON_TYPE({field}, %s)'
                raw = f'JSON({field} -> %s)'
                allowed = "'text', 'integer', 'real', 'true', 'false'"
            elif connection.vendor == 'mysql':
                kind = f'JSON_TYPE(JSON_EXTRACT({field}, %s))'
                raw = f'JSON_EXTRACT({field}, %s)'
                allowed = "'STRING', 'INTEGER', 'DOUBLE', 'BOOLEAN', 'DECIMAL'"
            else:
                raise NotImplementedError('Candidate evidence supports SQLite and MySQL')
            pairs.append(f'%s, CASE WHEN {kind} IN ({allowed}) '
                         f'AND LENGTH(JSON_EXTRACT({field}, %s)) <= 256 '
                         f'THEN {raw} ELSE NULL END')
            params.extend((name, path, path, path))
        return f"JSON_OBJECT({', '.join(pairs)})", params


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _batches(items):
    batch, task_ids = [], set()
    for item in items:
        task_id = item[0]
        if batch and (len(batch) >= MAX_CANDIDATE_KEYS or
                      (task_id not in task_ids and len(task_ids) >= MAX_TASKS_PER_BATCH)):
            yield batch
            batch, task_ids = [], set()
        batch.append(item)
        task_ids.add(task_id)
    if batch:
        yield batch


def _owned_keys(projected):
    # Import at call time so serializers can import this reader without a cycle.
    from botend.services.simc_benchmark_execution import _expected_candidate_keys

    if not isinstance(projected, dict):
        return None
    keys = projected.get('keys')
    if not isinstance(keys, list) or len(keys) != projected.get('count'):
        return None
    if any(not isinstance(key, str) or len(key) > 200 for key in keys):
        return None
    expected = _expected_candidate_keys(SimpleNamespace(mode_params={
        'initial_candidates': [{'candidate_key': key} for key in keys],
    }))
    return set(expected) if expected is not None else None


def _owners(requests):
    pending = {request: request[0] for request in requests}
    seen = {request: set() for request in requests}
    cache, owners = {}, {}
    for _ in range(MAX_SOURCE_DEPTH):
        if not pending:
            break
        missing = set(pending.values()) - cache.keys()
        if missing:
            rows = SimcTask.objects.filter(pk__in=missing).order_by().annotate(
                evidence_keys=_CandidateKeys('mode_params'),
            ).values('id', 'source_task_id', 'evidence_keys')
            cache.update(dict.fromkeys(missing))
            for row in rows:
                cache[row['id']] = (row['source_task_id'], _owned_keys(row['evidence_keys']))
        remaining = {}
        for request, task_id in pending.items():
            if task_id in seen[request] or cache[task_id] is None:
                continue
            seen[request].add(task_id)
            source_id, keys = cache[task_id]
            if keys is None:
                continue
            if request[1] in keys:
                # Ownership shadows ancestors even with no materialized Run.
                owners[request] = task_id
            elif source_id is not None:
                remaining[request] = source_id
        pending = remaining
    return owners


def _run_evidence(pairs):
    by_task = defaultdict(list)
    for task_id, key in pairs:
        by_task[task_id].append(key)
    predicate = Q()
    for task_id, keys in by_task.items():
        predicate |= Q(task_id=task_id, candidate_key__in=keys)
    # Phase 1 is scalar-only and bounded. Do not combine any ordering/filesort
    # with JSON hydration on MySQL, even if the projection looks small.
    rows = list(SimulationRun.objects.filter(predicate).order_by().values(
        'id', 'task_id', 'candidate_key', 'status',
        'task__benchmark_case__id', 'task__benchmark_case__execution_id',
    )[:MAX_CANDIDATE_KEYS + 1])
    if len(rows) > MAX_CANDIDATE_KEYS:
        return {}
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row['task_id'], row['candidate_key'])].append(row)
    unique = {rows[0]['id']: rows[0] for rows in grouped.values()
              if len(rows) == 1 and rows[0]['status'] == 'completed'}
    if not unique:
        return {}
    paths = {'frozen_backend_version': '$.backend_version'}
    paths.update({slot: f'$.composition_manifest.slots.{slot}.content_hash'
                  for slot in SEMANTIC_COMPOSITION_SLOTS})
    # Phase 2: exact ID set, explicitly no default model ordering, only leaves.
    summaries = SimulationRun.objects.filter(pk__in=unique).order_by().annotate(
        evidence_summary=_ScalarPaths('result_summary', {
            name: '$.' + name for name in ('dps', 'dps_error', 'dps_error_pct', 'valid')
        }), evidence_manifest=_ScalarPaths('resource_manifest', paths),
    ).values('id', 'evidence_summary', 'evidence_manifest')
    evidence = {}
    for row in summaries:
        source = unique[row['id']]
        summary, manifest = row['evidence_summary'], row['evidence_manifest']
        dps = _number(summary.get('dps'))
        if dps is None or dps <= 0 or summary.get('valid') is False:
            continue
        version = manifest.get('frozen_backend_version')
        evidence[(source['task_id'], source['candidate_key'])] = (dps, {
            'run_id': row['id'], 'task_id': source['task_id'],
            'case_id': source['task__benchmark_case__id'],
            'execution_id': source['task__benchmark_case__execution_id'],
            'dps_error': _number(summary.get('dps_error')),
            'dps_error_pct': _number(summary.get('dps_error_pct')),
            # Scheduling declaration only; never label this as actual build.
            'frozen_backend_version': version if isinstance(version, str) and version else None,
            'composition_slot_hashes': {
                slot: value if isinstance(value := manifest.get(slot), str)
                and re.fullmatch(r'[0-9a-f]{64}', value) else None
                for slot in SEMANTIC_COMPOSITION_SLOTS
            },
        })
    return evidence


def load_candidate_result_evidence(requests):
    """Return {(projection_task_id, candidate_key): evidence | None}.

    ``requests`` maps those keys to immutable Result.dps. Exact numeric equality
    with the actual completed Run is required, not a noise tolerance. Missing,
    corrupt, ambiguous, too-deep or superseded evidence stays unknown. Unknown
    errors are None (not invented zero); raw Results must remain visible.
    """
    resolved = dict.fromkeys(requests)
    valid = (request for request, dps in requests.items()
             if isinstance(request, tuple) and len(request) == 2
             and type(request[0]) is int and request[0] > 0
             and isinstance(request[1], str) and 0 < len(request[1]) <= 200
             and (number := _number(dps)) is not None and number > 0)
    for batch in _batches(valid):
        owners = _owners(batch)
        pairs = dict.fromkeys((owner, request[1]) for request, owner in owners.items())
        evidence = {}
        for pair_batch in _batches(pairs):
            evidence.update(_run_evidence(pair_batch))
        for request, owner in owners.items():
            match = evidence.get((owner, request[1]))
            if match is not None and match[0] == _number(requests[request]):
                resolved[request] = match[1]
    return resolved
