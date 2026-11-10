"""Canonical immutable WCL facts and content-addressed documents.

Only this service writes compact facts. Legacy snapshots are read-only inputs.
All SQL scopes start with the report/fight unique-index prefix.
"""
from collections import defaultdict
from copy import deepcopy
import hashlib
import json

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from botend.models import WclCombatantDocument, WclCombatantFact, WclCombatantSnapshot

IDENTITY_KEYS = ('sourceID', 'specID')
ATTRIBUTE_KEYS = ('race', 'critMelee', 'critSpell', 'critRanged', 'hasteMelee',
                  'hasteSpell', 'hasteRanged', 'mastery', 'versatilityDamageDone',
                  'versatility', 'versa')
BATCH_SIZE = 100


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def canonical_event(event):
    """Keep exactly the audited consumer contract, not transport metadata.

    Complete document lists are deep-copied, never trimmed or reordered.
    Optional missing/null documents mean unavailable, not an invented empty list.
    """
    if not isinstance(event, dict):
        raise ValueError('CombatantInfo event must be an object')
    result = {key: deepcopy(event[key]) for key in (*IDENTITY_KEYS, *ATTRIBUTE_KEYS) if key in event}
    if 'source' in event and isinstance(event['source'], dict):
        result['source'] = {key: deepcopy(event['source'][key])
                            for key in ('id', 'name', 'server') if key in event['source']}
    for key in ('gear', 'talentTree'):
        if event.get(key) is not None:
            if not isinstance(event[key], list):
                raise ValueError(f'CombatantInfo {key} must be a list')
            result[key] = deepcopy(event[key])
    return result


def _positive_int(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value) if str(value).isdigit() and int(value) > 0 else None
    except (TypeError, ValueError):
        return None


def _scope(keys):
    where = Q()
    for code, fight in keys:
        where |= Q(report_code=code, fight_id=fight)
    return where


def _project_fact(row):
    result = {**deepcopy(row.identity_json), **deepcopy(row.attributes_json)}
    if row.gear_document_id is not None:
        result['gear'] = deepcopy(row.gear_document.payload)
    if row.talent_document_id is not None:
        result['talentTree'] = deepcopy(row.talent_document.payload)
    return result


def load_facts(records, *, fetched_before=None):
    """Bounded DB-only full-fight reads; compact rows take precedence per actor."""
    keys = set()
    for record in records:
        code, fight = record.get('report_code'), _positive_int(record.get('fight_id'))
        if isinstance(code, str) and code and fight:
            keys.add((code, fight))
    keys = sorted(keys)
    result = defaultdict(list)
    for start in range(0, len(keys), BATCH_SIZE):
        where = _scope(keys[start:start + BATCH_SIZE])
        compact = WclCombatantFact.objects.filter(where).prefetch_related('gear_document', 'talent_document')
        legacy = WclCombatantSnapshot.objects.filter(where)
        if fetched_before is not None:
            compact = compact.filter(fetched_at__lte=fetched_before)
            legacy = legacy.filter(fetched_at__lte=fetched_before)
        actors = {}
        compact_ids = []
        for row in compact:
            compact_ids.append(row.pk)
            actors[(row.report_code, row.fight_id, row.actor_id)] = _project_fact(row)
        # Inspect only legacy identities first: never transfer its large raw
        # JSON when a compact actor already satisfies the requested cutoff.
        missing_ids = [row['pk'] for row in legacy.values('pk', 'report_code', 'fight_id', 'actor_id')
                       if (row['report_code'], row['fight_id'], row['actor_id']) not in actors]
        for offset in range(0, len(missing_ids), 200):
            for row in WclCombatantSnapshot.objects.filter(pk__in=missing_ids[offset:offset + 200]).values(
                    'report_code', 'fight_id', 'actor_id', 'payload_json'):
                payload = row['payload_json']
                if isinstance(payload, dict) and _positive_int(payload.get('sourceID')) == row['actor_id']:
                    actors[(row['report_code'], row['fight_id'], row['actor_id'])] = canonical_event(payload)
        # Copy+retire commits atomically, but READ COMMITTED SELECTs can straddle
        # that commit in either legacy query. Re-read after all legacy payloads:
        # an actor is then present in legacy or compact (facts are immutable and
        # not GC'd during retirement). Compact still wins. Exclude exact PKs,
        # not a max-PK watermark: a lower allocated PK can commit later.
        # Already projected documents are never transferred a second time.
        for row in compact.exclude(pk__in=compact_ids):
            actors[(row.report_code, row.fight_id, row.actor_id)] = _project_fact(row)
        for (code, fight, actor), payload in sorted(actors.items()):
            result[(code, fight)].append(payload)
    return result


def _persist_chunk(code, inputs, fetched_at):
    """One bounded insert batch; caller owns transaction and rollback."""
    keys = sorted({(code, fight) for fight, actor in inputs})
    where = Q()
    for fight, actor in sorted(inputs):
        where |= Q(report_code=code, fight_id=fight, actor_id=actor)
    # Lock only exact requested identities, select light PKs first, then fetch
    # raw JSON by those PKs without ordering. No fight/actor Cartesian expansion.
    legacy_rows = list(WclCombatantSnapshot.objects.select_for_update().filter(where)
                       .only('id', 'report_code', 'fight_id', 'actor_id', 'fetched_at').order_by('pk'))
    raw_by_pk = dict(WclCombatantSnapshot.objects.filter(pk__in=[row.pk for row in legacy_rows])
                     .order_by().values_list('pk', 'payload_json'))
    for row in legacy_rows:
        row.payload_json = raw_by_pk[row.pk]
    legacy = {(row.fight_id, row.actor_id): row for row in legacy_rows}
    documents = {}
    for raw, canonical in inputs.values():
        for field, kind in (('gear', 'gear'), ('talentTree', 'talents')):
            if field in canonical:
                payload = canonical[field]
                digest = _hash([kind, payload])
                documents[digest] = (kind, payload)
    docs = {row.content_hash: row for row in WclCombatantDocument.objects.select_for_update().filter(
        content_hash__in=sorted(documents)).order_by('content_hash')}
    missing = {digest: value for digest, value in documents.items() if digest not in docs}
    if missing:
        WclCombatantDocument.objects.bulk_create([
            WclCombatantDocument(kind=kind, content_hash=digest, payload=payload)
            for digest, (kind, payload) in sorted(missing.items())],
            ignore_conflicts=True, batch_size=200)
        docs.update({row.content_hash: row for row in WclCombatantDocument.objects.select_for_update().filter(
            content_hash__in=sorted(missing)).order_by('content_hash')})
    for digest, (kind, payload) in documents.items():
        if digest not in docs or docs[digest].kind != kind or _json(docs[digest].payload) != _json(payload):
            raise ValueError('CombatantInfo document hash collision')
    # A uniqueness race rolls back the entire bulk INSERT savepoint. Locking
    # reads re-match winners; never silently accept a conflicting canonical fact.
    for attempt in range(3):
        existing = {(row.fight_id, row.actor_id): row for row in
                    WclCombatantFact.objects.select_for_update().filter(where)}
        pending = []
        for (fight, actor), (raw, canonical) in sorted(inputs.items()):
            digest = _hash(canonical)
            old = legacy.get((fight, actor))
            if old is not None and _hash(canonical_event(old.payload_json)) != digest:
                raise ValueError('Conflicting legacy CombatantInfo fact')
            current = existing.get((fight, actor))
            if current is not None:
                if current.canonical_hash != digest:
                    raise ValueError('Conflicting immutable CombatantInfo fact')
                continue
            pending.append(WclCombatantFact(
                report_code=code, fight_id=fight, actor_id=actor,
                identity_json={key: canonical[key] for key in (*IDENTITY_KEYS, 'source') if key in canonical},
                attributes_json={key: canonical[key] for key in ATTRIBUTE_KEYS if key in canonical},
                gear_document=docs.get(_hash(['gear', canonical['gear']])) if 'gear' in canonical else None,
                talent_document=docs.get(_hash(['talents', canonical['talentTree']])) if 'talentTree' in canonical else None,
                fetched_at=old.fetched_at if old is not None else fetched_at,
                source_hash=_hash(raw), canonical_hash=digest))
        try:
            with transaction.atomic():
                WclCombatantFact.objects.bulk_create(pending, batch_size=200)
            return len(pending)
        except IntegrityError:
            if attempt == 2:
                raise
    return 0


def persist_events(report_code, by_fight, *, fetched_at=None):
    """Bulk-insert all validated actors, refusing canonical conflicts atomically.

    Report input must have passed _validated_events; identity assertions here
    also prevent accidental cross-fight/actor writes by standalone callers.
    Original legacy and existing compact fetched_at clocks never move forward.
    """
    if not isinstance(report_code, str) or not report_code or len(report_code) > 50:
        raise ValueError('Invalid CombatantInfo report code')
    fetched_at = fetched_at if fetched_at is not None else timezone.now()
    written = 0
    with transaction.atomic():
        inputs = {}
        for fight, events in by_fight.items():
            fight = _positive_int(fight)
            if not fight:
                raise ValueError('Invalid CombatantInfo fight')
            for raw in events:
                canonical = canonical_event(raw)
                actor = _positive_int(canonical.get('sourceID'))
                if (not actor or _positive_int((canonical.get('source') or {}).get('id')) != actor
                        or ('fight' in raw and _positive_int(raw['fight']) != fight)):
                    raise ValueError('Inconsistent CombatantInfo identity')
                key = (fight, actor)
                if key in inputs and _hash(inputs[key][1]) != _hash(canonical):
                    raise ValueError('Conflicting CombatantInfo batch actors')
                inputs[key] = (raw, canonical)
                if len(inputs) >= BATCH_SIZE:
                    written += _persist_chunk(report_code, inputs, fetched_at)
                    inputs = {}
        if inputs:
            written += _persist_chunk(report_code, inputs, fetched_at)
    return written


def fact_revision():
    """Index-backed maxima, including read-only compatibility rows."""
    return tuple(model.objects.order_by('-pk').values_list('pk', flat=True).first()
                 for model in (WclCombatantSnapshot, WclCombatantFact))
