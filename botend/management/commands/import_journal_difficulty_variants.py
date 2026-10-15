"""Manual offline import. No collection, base-item creation or snapshot publication.

The frozen manifest is a completeness assertion, not the authority for scope.
All selected equipment coordinates are independently enumerated from live Journal
ORM facts before the first writer. SHA256 is byte-exact, not an upstream signature.
"""
import argparse
import hashlib
import json
from pathlib import Path

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from botend.journal_models import JournalState
from botend.models import SeasonMeta, WowItemSnapshot, WowItemVariantSnapshot
from botend.portal.adventure_journal import difficulty_item_context, instance_source
from botend.services.journal_snapshot import _source
from botend.services.journal_classification import instance_kind
from botend.services.wow_item_difficulty_import import import_difficulty_variant

MAX_FILE_BYTES = 300 * 1024 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_PROOF_LINES = 100000
MAX_TARGETS = 100000
TARGET_FIELDS = ('instance_id', 'instance_name', 'expansion', 'difficulty_id',
                 'item_context', 'item_id', 'source', 'class_id', 'slot', 'base_quality')


def same(left, right):
    """JSON identity comparison must not treat True as integer 1."""
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def require(condition, message):
    if not condition:
        raise CommandError(message)


def strict_instance(value):
    # argparse's int() accepts whitespace, signs and leading zeros. Do not repair.
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or value.startswith('0'):
        raise argparse.ArgumentTypeError('instance must be a canonical positive integer')
    return int(value)


def integer(value, name, minimum=1):
    require(type(value) is int and value >= minimum, 'invalid ' + name)
    return value


def pairs(rows):
    result = {}
    for key, value in rows:
        require(key not in result, 'duplicate JSON key: ' + key)
        result[key] = value
    return result


def decode(raw):
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(CommandError('nonfinite JSON')))


def file_bytes(path):
    with Path(path).open('rb') as stream:
        require(stream.seek(0, 2) <= MAX_FILE_BYTES, 'file size limit exceeded')
        stream.seek(0)
        raw = stream.read(MAX_FILE_BYTES + 1)
    require(len(raw) <= MAX_FILE_BYTES, 'file size limit exceeded')
    return raw


def branch_source(source, release, instance_id):
    require(isinstance(source, dict), 'invalid source identity')
    result = dict(source)
    branch = 'ptr' if str(instance_id) in (release.manifest.get('ptr_overlays') or {}) else 'retail'
    require('data_branch' not in result or result['data_branch'] == branch, 'source branch mismatch')
    result['data_branch'] = branch
    return result


def coord(row):
    require(isinstance(row, dict), 'invalid target')
    return tuple(integer(row.get(k), k, 0 if k == 'item_context' else 1)
                 for k in ('instance_id', 'difficulty_id', 'item_context', 'item_id'))


def target_identity(row, release):
    require(all(k in row for k in TARGET_FIELDS), 'incomplete target identity')
    coord(row)
    for key in ('expansion', 'class_id', 'slot', 'base_quality'):
        integer(row[key], key, 0)
    return {**{k: row[k] for k in TARGET_FIELDS},
            'source': branch_source(row['source'], release, row['instance_id'])}


def season_identity(season):
    require(season is not None and bool(season.gear_batch_key), 'no active canonical season/batch')
    return dict(id=season.pk, key=season.season_key, batch=season.gear_batch_key)


def actual_scope(manifest, selected):
    release, season, version = _source()  # Pure ORM reads; never refresh/prewarm.
    header = dict(id=release.pk, build=release.build,
                  updated=release.completed_at.isoformat() if release.completed_at else None,
                  counts={k: release.report.get(k, 0) for k in ('instances', 'encounters', 'loot')})
    for key, count in header['counts'].items():
        integer(count, 'source count ' + key, 0)
    require(same(manifest.get('release'), header), 'release header changed')
    require(manifest.get('source_version') == version, 'release/source version changed')
    require(same(manifest.get('season'), season_identity(season)), 'active season/batch changed')
    declared = manifest.get('instances')
    require(isinstance(declared, list) and len(declared) <= 1000, 'invalid manifest instances')
    declared_by_id = {}
    for row in declared:
        require(isinstance(row, dict), 'invalid manifest instance')
        iid = integer(row.get('id'), 'instance id')
        require(iid not in declared_by_id, 'duplicate instance')
        declared_by_id[iid] = row
    actual = list(release.instances.order_by('journal_id').prefetch_related('encounters'))
    require(set(declared_by_id) == {r.journal_id for r in actual}, 'manifest instance set differs from actual release')
    selected = set(declared_by_id) if selected is None else set(selected)
    require(bool(selected) and selected <= set(declared_by_id), 'unknown instance selection')
    equipment = set(WowItemSnapshot.objects.filter(catalog_type='equipment').values_list('item_id', flat=True))
    expected, nongear = {}, {}
    for instance in actual:
        if instance.journal_id not in selected:
            continue
        iid, payload = instance.journal_id, instance.payload
        frozen = declared_by_id[iid]

        for key in ('id', 'name', 'kind', 'expansion', 'difficulty_ids'):
            actual_value = instance_kind(iid, payload.get(key)) if key == 'kind' else payload.get(key)
            require(same(frozen.get(key), actual_value), 'instance header mismatch: ' + str(iid))
        source = branch_source(instance_source(release, iid, season=season), release, iid)
        require(branch_source(frozen.get('source'), release, iid) == source, 'instance source mismatch')
        allowed = payload['difficulty_ids']
        require(isinstance(allowed, list) and all(type(d) is int and d > 0 for d in allowed), 'invalid actual difficulties')
        for boss in instance.encounters.all():
            for drop in boss.payload['loot']:
                item_id = integer(drop.get('item_id'), 'actual item id')
                for field in ('class_id', 'slot', 'quality'):
                    integer(drop.get(field), 'actual ' + field, 0)
                for difficulty in drop['difficulty_ids']:
                    integer(difficulty, 'actual difficulty')
                    if difficulty not in allowed or difficulty not in boss.payload['difficulty_ids']:
                        continue
                    context = difficulty_item_context(release.manifest['catalog']['difficulties'], difficulty)
                    integer(context, 'actual context', 0)
                    key = (iid, difficulty, context, item_id)
                    row = dict(instance_id=iid, instance_name=instance.name, expansion=instance.expansion,
                               difficulty_id=difficulty, item_context=context, item_id=item_id, source=source,
                               class_id=drop['class_id'], slot=drop['slot'], base_quality=drop['quality'])
                    row = target_identity(row, release)
                    if drop.get('class_id') not in (2, 4) and item_id not in equipment:
                        require(key not in nongear or nongear[key] == row, 'conflicting actual nongear identity')
                        nongear[key] = row
                        continue
                    require(key not in expected or expected[key] == row, 'conflicting actual drop identity')
                    expected[key] = row
    targets = manifest.get('targets')
    require(isinstance(targets, list) and len(targets) <= MAX_TARGETS, 'invalid manifest targets')
    declared_targets = {}
    for row in targets:
        key = coord(row)
        require(key[0] in declared_by_id, 'target outside manifest instances')
        if key[0] not in selected:
            continue
        if key in nongear:
            require(target_identity(row, release) == nongear[key], 'nongear target identity mismatch')
            continue
        require(key not in declared_targets, 'duplicate manifest target')
        declared_targets[key] = target_identity(row, release)
    require(expected == declared_targets,
            f'actual scope mismatch: expected={len(expected)} declared={len(declared_targets)} '
            f'missing={len(expected.keys() - declared_targets.keys())} extra={len(declared_targets.keys() - expected.keys())}')
    return release, season, version, selected, expected, len(nongear)


def load_proofs(path, release, season, selected, expected):
    verified = {}
    with Path(path).open('rb') as stream:
        require(stream.seek(0, 2) <= MAX_FILE_BYTES, 'proof file size limit exceeded')
        stream.seek(0)
        consumed = 0
        for number in range(1, MAX_PROOF_LINES + 2):
            raw = stream.readline(MAX_LINE_BYTES + 1)
            if not raw:
                break
            consumed += len(raw)
            require(consumed <= MAX_FILE_BYTES, 'proof file size limit exceeded')
            require(number <= MAX_PROOF_LINES and len(raw) <= MAX_LINE_BYTES, 'proof line/count limit exceeded')
            row = decode(raw)
            key = coord(row)
            if key[0] not in selected:
                continue
            require(key in expected, 'proof outside actual equipment scope')
            require(target_identity(row, release) == expected[key], 'proof source/target identity mismatch')
            require(type(row.get('release_id')) is int and row['release_id'] == release.pk
                    and same(row.get('season'), season_identity(season)), 'proof release/season mismatch')
            if row.get('status') != 'verified':
                continue
            # A later unresolved retry cannot erase verified evidence; two verified
            # conflicting facts can never be hidden by last-wins deduplication.
            core = {k: row.get(k) for k in ('rule', 'payload', 'tooltip_evidence')}
            require(key not in verified or verified[key] == core, 'conflicting verified proofs')
            verified[key] = core
    missing = expected.keys() - verified.keys()
    require(not missing, f'missing verified proofs: {len(missing)} of {len(expected)} expected equipment coordinates')
    return verified


class Command(BaseCommand):
    help = 'Validate a complete frozen Journal scope; explicitly apply verified central difficulty variants offline.'

    def add_arguments(self, parser):
        parser.add_argument('--manifest', required=True)
        parser.add_argument('--proofs', required=True)
        parser.add_argument('--instance', action='append', type=strict_instance)
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--confirm-manifest-sha256')

    def handle(self, *args, **options):
        try:
            self.run_import(options)
        except CommandError:
            raise
        except (ObjectDoesNotExist, ValidationError, ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError) as exc:
            raise CommandError(f'import refused: {exc}') from exc

    def run_import(self, options):
        raw = file_bytes(options['manifest'])
        digest = hashlib.sha256(raw).hexdigest()
        if options['apply']:
            require(options.get('confirm_manifest_sha256') == digest, 'apply requires exact --confirm-manifest-sha256 of manifest file bytes')
        manifest = decode(raw)
        require(isinstance(manifest, dict), 'invalid manifest')
        selection = options.get('instance')
        if selection is not None:
            require(all(type(i) is int and i > 0 for i in selection), 'invalid instance selection')
        release, season, version, selected, expected, nongear = actual_scope(manifest, selection)
        proofs = load_proofs(options['proofs'], release, season, selected, expected)
        prepared = []
        for key, target in sorted(expected.items()):
            kwargs = dict(proofs[key], item_id=key[3], data_branch=target['source']['data_branch'],
                          instance_id=key[0], difficulty_id=key[1], item_context=key[2], rule_build=target['source']['build'])
            result = import_difficulty_variant(**kwargs, apply=False)
            prepared.append((kwargs, result))
        created = noop = 0
        if options['apply']:
            with transaction.atomic():
                JournalState.objects.select_for_update().get(pk='wow-zhCN')
                SeasonMeta.objects.select_for_update().get(pk=season.pk)
                def fence():
                    current = actual_scope(manifest, selection)
                    require(current[0].pk == release.pk and current[2] == version and current[4] == expected,
                            'scope changed during import')
                    require(hashlib.sha256(file_bytes(options['manifest'])).hexdigest() == digest, 'manifest file changed')
                fence()
                applied = []
                for kwargs, preview in prepared:
                    result = import_difficulty_variant(**kwargs, apply=True, expected_before_hash=preview['before_hash'])
                    applied.append((kwargs, result))
                    created += result['status'] == 'created'
                    noop += result['status'] == 'noop'
                for kwargs, result in applied:
                    row = WowItemVariantSnapshot.objects.get(pk=result['variant_id'])
                    require(row.season_id == season.pk and row.batch_key == season.gear_batch_key,
                            'readback season/batch mismatch')
                    reread = import_difficulty_variant(**kwargs, apply=False)
                    require(reread['status'] == 'noop' and reread['variant_id'] == row.pk, 'central readback mismatch')
                fence()
        else:
            noop = sum(result['status'] == 'noop' for _, result in prepared)
            # Catch a moving release/batch even on the read-only path.
            actual_scope(manifest, selection)
        self.stdout.write(json.dumps(dict(status='applied' if options['apply'] else 'dry_run',
            dry_run=not options['apply'], created=created, noop=noop, expected_count=len(expected),
            validated_count=len(prepared), selected_instance_count=len(selected), selected_instances=sorted(selected),
            skipped_nongear_count=nongear, manifest_sha256=digest), sort_keys=True))
