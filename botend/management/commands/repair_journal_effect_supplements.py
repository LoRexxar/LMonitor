"""Offline metadata-only supplement repair; dry-run unless explicitly fenced."""
import hashlib
import json
import os
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from django.db import transaction
from botend.models import WowItemVariantSnapshot, SeasonMeta
from botend.services.journal_effect_supplement import (
    ITEMS, DIFFICULTIES, baseline_state, validated_supplement, require, adapt_record,
)
from botend.management.commands.import_journal_difficulty_variants import decode


class Command(BaseCommand):
    help = 'Validate exactly four armor items x four difficulties; offline dry-run by default.'

    def add_arguments(self, parser):
        parser.add_argument('--artifact', required=True)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--confirm-artifact-sha256')
        parser.add_argument('--backup', help='New exclusive-create JSON backup path, required for apply')

    def handle(self, *args, **opts):
        try:
            self._run(opts)
        except (ValidationError, KeyError, ValueError, TypeError, OSError) as exc:
            raise CommandError(str(exc)) from exc

    def _run(self, opts):
        path = Path(opts['artifact']).resolve()
        require(path.stat().st_size <= 16 * 1024 * 1024, 'artifact exceeds limit')
        raw = path.read_bytes(); sha = hashlib.sha256(raw).hexdigest()
        artifact = decode(raw)
        require(artifact['schema'] == 1 and len(artifact['records']) == 16, 'expected exactly 16 records')
        records = {r['variant_id']: dict(r, evidence_ref=str(path), evidence_sha256=sha) for r in artifact['records']}
        require(len(records) == 16, 'duplicate variant IDs')
        rows = list(WowItemVariantSnapshot.objects.filter(pk__in=records).select_related('item').order_by('pk'))
        require(len(rows) == 16, 'missing variants')
        require({(v.item.item_id, v.metadata['difficulty_drop']['difficulty_id']) for v in rows} ==
                {(i,d) for i in ITEMS for d in DIFFICULTIES}, 'incomplete item/difficulty scope')
        require(len({(v.season_id,v.batch_key) for v in rows}) == 1, 'mixed batch')
        sid, batch = rows[0].season_id, rows[0].batch_key
        require(SeasonMeta.objects.filter(pk=sid, is_active=True, gear_batch_key=batch).exists(), 'stale season/batch')
        records = {v.pk: adapt_record(v, records[v.pk], artifact['source_build']) for v in rows}
        supplements = {v.pk: validated_supplement(v, records[v.pk]) for v in rows}
        before = {v.pk: dict(state=baseline_state(v), metadata=v.metadata,
                            updated_at=v.updated_at.isoformat()) for v in rows}
        changed = [v.pk for v in rows if v.metadata.get('effect_supplement') != supplements[v.pk]]
        if opts['apply']:
            require(opts['confirm_artifact_sha256'] == sha, 'artifact fence mismatch')
            if changed:
                require(bool(opts['backup']), '--backup required')
                with open(opts['backup'], 'x', encoding='utf-8') as stream:
                    json.dump(dict(schema=1, artifact_sha256=sha, variants=before), stream, ensure_ascii=False, sort_keys=True)
                    stream.flush(); os.fsync(stream.fileno())
                # One small transaction: lock and revalidate all 16 before any write.
                with transaction.atomic():
                    season = SeasonMeta.objects.select_for_update().get(pk=sid)
                    require(season.is_active and season.gear_batch_key == batch, 'season fence changed')
                    locked = list(WowItemVariantSnapshot.objects.select_for_update().filter(pk__in=records).select_related('item').order_by('pk'))
                    require(len(locked) == 16, 'scope changed')
                    for v in locked:
                        require(v.metadata == before[v.pk]['metadata'] and v.updated_at.isoformat() == before[v.pk]['updated_at'], 'concurrent metadata changed')
                        require(validated_supplement(v, records[v.pk]) == supplements[v.pk], 'concurrent facts changed')
                    for v in locked:
                        if v.pk in changed:
                            v.metadata = dict(v.metadata, effect_supplement=supplements[v.pk])
                            v.save(update_fields=['metadata','updated_at'])
                    for v in WowItemVariantSnapshot.objects.filter(pk__in=records):
                        require(baseline_state(v) == before[v.pk]['state'] and
                                v.metadata.get('effect_supplement') == supplements[v.pk], 'readback mismatch')
        self.stdout.write(json.dumps(dict(status='applied' if opts['apply'] else 'dry_run',
                                         count=16, changed=len(changed), artifact_sha256=sha)))
