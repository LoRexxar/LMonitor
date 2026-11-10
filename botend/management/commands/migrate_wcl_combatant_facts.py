"""Bounded, opt-in copy/retirement of immutable legacy WCL facts.

No network, background loops, GC or task/lease changes. One invocation commits
all selected rows or none. MySQL ROW/FULL deletion before-images need headroom.
"""
import hashlib
import json
from collections import defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, models, transaction
from django.db.models.functions import Cast, Length


class MigrationRejected(Exception):
    pass


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _source(row):
    try:
        raw = json.loads(row['raw_text'])
        actor = raw.get('sourceID') if isinstance(raw, dict) else None
        if (not isinstance(actor, int) or isinstance(actor, bool) or actor <= 0
                or actor != row['actor_id'] or row['fight_id'] <= 0
                or not row['report_code'] or row['fetched_at'] is None):
            raise ValueError
        if 'fight' in raw and raw['fight'] != row['fight_id']:
            raise ValueError
        source = raw.get('source')
        if source is not None and (not isinstance(source, dict)
                or ('id' in source and source['id'] != actor)):
            raise ValueError
        return raw
    except (ValueError, TypeError, OverflowError):
        raise MigrationRejected('invalid_payload') from None


def _fingerprint(row):
    return (row['report_code'], row['fight_id'], row['actor_id'],
            row['fetched_at'], hashlib.sha256(row['raw_text'].encode('utf-8')).digest())


def _read_row(queryset, pk):
    # Clear even model default ordering: never filesort the large JSON payload.
    return queryset.filter(pk=pk).order_by().annotate(
        raw_text=Cast('payload_json', output_field=models.TextField())
    ).values('pk', 'report_code', 'fight_id', 'actor_id', 'fetched_at', 'raw_text').get()


def _verify(store, Fact, row, expected):
    identity = {key: row[key] for key in ('report_code', 'fight_id', 'actor_id')}
    # Read the real compact row, not a compatibility reader which can silently
    # fall back to legacy JSON or load every actor in an unbudgeted fight.
    facts = Fact.objects.filter(**identity).order_by()
    fact = facts.select_related('gear_document', 'talent_document').first()
    if fact is None or fact.fetched_at != row['fetched_at']:
        raise MigrationRejected('timestamp_or_fact_conflict')
    projected = {**fact.identity_json, **fact.attributes_json}
    if fact.gear_document_id is not None:
        projected['gear'] = fact.gear_document.payload
    if fact.talent_document_id is not None:
        projected['talentTree'] = fact.talent_document.payload
    if projected != expected:
        raise MigrationRejected('projection_conflict')
    if not facts.filter(fetched_at__lte=row['fetched_at']).exists():
        raise MigrationRejected('cutoff_conflict')
    if facts.filter(fetched_at__lte=row['fetched_at'] - timedelta(microseconds=1)).exists():
        raise MigrationRejected('cutoff_conflict')


class Command(BaseCommand):
    help = 'Read-only by default; bounded atomic WCL copy with opt-in verified retirement.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--retire-legacy', action='store_true')
        parser.add_argument('--after-id', type=int, default=0)
        parser.add_argument('--limit', type=int, default=500)
        parser.add_argument('--max-payload-bytes', type=int, default=8 * 1024 * 1024)

    def handle(self, *args, **options):
        after, limit, budget = (options['after_id'], options['limit'], options['max_payload_bytes'])
        if after < 0 or after > 9223372036854775807:
            raise CommandError('after_id_out_of_range')
        if not 1 <= limit <= 2000:
            raise CommandError('limit_out_of_range')
        if not 1 <= budget <= 64 * 1024 * 1024:
            raise CommandError('payload_budget_out_of_range')
        if options['retire_legacy'] and not options['apply']:
            raise CommandError('retirement_requires_apply')
        from botend.models import WclCombatantFact, WclCombatantSnapshot
        from botend.services import wcl_combatant_store as store

        receipt = dict(mode='apply' if options['apply'] else 'dry-run',
                       end_pk=after, processed=0, created=0, verified=0,
                       retired=0, bytes=0, remaining=False, atomic=True)
        try:
            # All fields in this first ordered query are lightweight; JSON is not selected.
            ids = list(WclCombatantSnapshot.objects.filter(pk__gt=after)
                       .order_by('pk').values_list('pk', flat=True)[:limit])
            # UTF-8 <=4 bytes/codepoint; reserve 6x source plus row overhead for
            # raw + normalized inserts + indexes + optional ROW/FULL before-image.
            sizes = dict(WclCombatantSnapshot.objects.filter(pk__in=ids).order_by()
                         .annotate(n=Length(Cast('payload_json', models.TextField())))
                         .values_list('pk', 'n'))
            if len(sizes) != len(ids):
                raise MigrationRejected('legacy_drift')
            selected, estimate = [], 0
            for pk in ids:
                cost = (sizes[pk] or 0) * 24 + 4096
                if estimate + cost > budget:
                    if not selected:
                        raise MigrationRejected('payload_budget_exceeded')
                    break
                selected.append(pk)
                estimate += cost
            ids = selected
            sizes = {pk: sizes[pk] for pk in ids}
            rows = []
            for pk in ids:
                bounded = WclCombatantSnapshot.objects.annotate(
                    payload_chars=Length(Cast('payload_json', models.TextField()))
                ).filter(payload_chars__lte=sizes[pk])
                row = _read_row(bounded, pk)
                raw = _source(row)
                expected = store.canonical_event(raw)
                if not isinstance(expected, dict) or expected.get('sourceID') != row['actor_id']:
                    raise MigrationRejected('invalid_projection')
                rows.append((row, raw, expected, _fingerprint(row)))
            actual = sum(len(row['raw_text'].encode('utf-8')) * 3
                         + len(_encoded(expected)) * 3 + 4096
                         for row, raw, expected, digest in rows)
            if actual > budget:
                raise MigrationRejected('payload_budget_exceeded')
            receipt['bytes'] = max(estimate, actual)
            with transaction.atomic():
                # Exact-PK locks and raw re-read protect against drift after preflight.
                # No skip_locked: an unavailable row must not advance the checkpoint.
                bounded_rows = WclCombatantSnapshot.objects.annotate(
                    payload_chars=Length(Cast('payload_json', models.TextField())))
                for row, raw, expected, digest in rows:
                    scoped = bounded_rows.filter(payload_chars__lte=sizes[row['pk']])
                    current = _read_row(scoped.select_for_update()
                                        if options['apply'] else scoped, row['pk'])
                    if _fingerprint(current) != digest:
                        raise MigrationRejected('legacy_drift')
                    facts = WclCombatantFact.objects
                    if options['apply']:
                        facts = facts.select_for_update()
                    existing = facts.filter(
                        report_code=row['report_code'], fight_id=row['fight_id'], actor_id=row['actor_id']).first()
                    if existing is not None:
                        _verify(store, WclCombatantFact, row, expected)
                        if not options['apply']:
                            receipt['verified'] += 1
                if options['apply']:
                    # One fight per call also prevents the store's legacy
                    # compatibility lookup from expanding a fight/actor Cartesian
                    # product beyond this frozen, budgeted PK selection.
                    groups = defaultdict(list)
                    for row, raw, expected, digest in rows:
                        groups[(row['report_code'], row['fetched_at'], row['fight_id'])].append(raw)
                    for (code, fetched_at, fight), payloads in groups.items():
                        receipt['created'] += store.persist_events(code, {fight: payloads}, fetched_at=fetched_at)
                    for row, raw, expected, digest in rows:
                        _verify(store, WclCombatantFact, row, expected)
                        receipt['verified'] += 1
                        if options['retire_legacy']:
                            current = _read_row(WclCombatantSnapshot.objects.select_for_update(), row['pk'])
                            if _fingerprint(current) != digest:
                                raise MigrationRejected('legacy_drift')
                            # No model deletion collector: exact old table only, no cascades.
                            count = WclCombatantSnapshot.objects.filter(pk=row['pk'])._raw_delete('default')
                            if count != 1:
                                raise MigrationRejected('legacy_delete_conflict')
                            receipt['retired'] += 1
                receipt['processed'] = len(rows)
                receipt['end_pk'] = ids[-1] if ids else after
                receipt['remaining'] = WclCombatantSnapshot.objects.filter(pk__gt=receipt['end_pk']).exists()
        except Exception as exc:
            # Never render database/JSON exception bodies: they may contain raw facts.
            reason = str(exc) if isinstance(exc, MigrationRejected) else (
                'database_failure' if isinstance(exc, DatabaseError) else 'validation_failure')
            receipt.update(end_pk=after, processed=0, created=0, verified=0, retired=0,
                           remaining=True, error=reason)
            self.stdout.write(json.dumps(receipt, sort_keys=True))
            raise CommandError(reason) from None
        self.stdout.write(json.dumps(receipt, sort_keys=True))
