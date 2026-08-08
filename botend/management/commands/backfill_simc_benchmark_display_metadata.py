from copy import deepcopy

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from botend.models import (
    SimcBenchmarkCandidate, SimcBenchmarkExecution, SimcBenchmarkPanel, SimulationRun,
    WowItemSnapshot,
)


SPECIAL_BONUS_LABELS = {
    250462: {606: '暴击', 604: '急速', 605: '精通', 607: '全能'},
    248583: {13183: '暴击', 13184: '急速', 13185: '精通', 13186: '全能'},
}


def _item_id(candidate_params):
    if not isinstance(candidate_params, dict):
        return None
    swap = candidate_params.get('gear_swap')
    if not isinstance(swap, dict):
        return None
    value = swap.get('item_id')
    return value if isinstance(value, int) and value > 0 else None


def _bonus_id(swap):
    value = swap.get('bonus_id')
    if isinstance(value, int) and value > 0:
        return value
    raw_value = swap.get('raw_value')
    if not isinstance(raw_value, str):
        return None
    for assignment in raw_value.split(','):
        key, separator, raw_bonus_id = assignment.partition('=')
        if key.strip() != 'bonus_id' or not separator:
            continue
        raw_bonus_id = raw_bonus_id.strip()
        return int(raw_bonus_id) if raw_bonus_id.isdigit() and int(raw_bonus_id) > 0 else None
    return None


def _bonus_label(candidate_params):
    if not isinstance(candidate_params, dict):
        return ''
    swap = candidate_params.get('gear_swap')
    if not isinstance(swap, dict):
        return ''
    item_id = swap.get('item_id')
    if not isinstance(item_id, int) or item_id <= 0:
        return ''
    bonus_id = _bonus_id(swap)
    if bonus_id is None:
        return ''
    return SPECIAL_BONUS_LABELS.get(item_id, {}).get(bonus_id, '')


def _display_metadata(items, candidate_params):
    item_id = _item_id(candidate_params)
    item = items.get(item_id)
    if item is None:
        return '', '', ''
    label = str(item.name_zh or item.name or '').strip()
    variant = _bonus_label(candidate_params)
    if label and variant:
        label = f'{label} · {variant}'
    icon_name = str(item.icon or '').strip().split('?', 1)[0].rsplit('/', 1)[-1]
    while icon_name.rsplit('.', 1)[-1].lower() in {'jpg', 'jpeg', 'png', 'gif', 'webp'}:
        icon_name = icon_name.rsplit('.', 1)[0]
    icon_url = f'/static/wow_icons/small/{icon_name}.jpg' if icon_name else ''
    effect = str(item.description_zh or item.description or '').strip()
    return label, effect, icon_url


def _execution_candidate_metadata(items, snapshot):
    if not isinstance(snapshot, dict):
        return {}
    definitions = snapshot.get('candidates')
    if not isinstance(definitions, list):
        return {}
    metadata = {}
    for candidate in definitions:
        if not isinstance(candidate, dict):
            continue
        key = candidate.get('key')
        params = candidate.get('params')
        if not isinstance(key, str) or not key or not isinstance(params, dict):
            continue
        label, effect, icon_url = _display_metadata(items, params)
        if label or effect or icon_url:
            metadata[key] = {
                **({'label': label} if label else {}),
                **({'effect': effect} if effect else {}),
                **({'icon_url': icon_url} if icon_url else {}),
            }
    return metadata


class Command(BaseCommand):
    help = (
        'Backfill local Chinese item labels and icon URLs into existing Benchmark '
        'candidate and Run display snapshots without creating or rerunning work.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--panel-slug', default='', help='仅回填指定 Benchmark Panel')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        panel_slug = str(options.get('panel_slug') or '').strip()
        candidate_query = SimcBenchmarkCandidate.objects
        execution_query = SimcBenchmarkExecution.objects
        run_query = SimulationRun.objects
        if panel_slug:
            panel = SimcBenchmarkPanel.objects.get(slug=panel_slug)
            candidate_query = candidate_query.filter(panel=panel)
            execution_query = execution_query.filter(panel=panel)
            run_query = run_query.filter(task__benchmark_case__execution__panel=panel)
        candidates = list(candidate_query.only(
            'id', 'key', 'label', 'icon_url', 'effect', 'params',
        ).order_by('id'))
        executions = list(execution_query.only(
            'id', 'config_snapshot', 'display_metadata',
        ).order_by('id'))
        candidate_keys = {candidate.key for candidate in candidates}
        runs = list(run_query.filter(
            Q(task__benchmark_case__isnull=False) | Q(candidate_key__in=candidate_keys),
        ).only('id', 'candidate_label', 'candidate_params', 'display_metadata').order_by('id'))
        item_ids = {
            item_id for item_id in (
                *(_item_id(candidate.params) for candidate in candidates),
                *(_item_id(candidate.get('params')) for execution in executions
                  if isinstance(execution.config_snapshot, dict)
                  for candidate in (execution.config_snapshot.get('candidates') or [])
                  if isinstance(candidate, dict)),
                *(_item_id(run.candidate_params) for run in runs),
            ) if item_id is not None
        }
        items = {
            item.item_id: item for item in WowItemSnapshot.objects.filter(
                item_id__in=item_ids,
            ).only('item_id', 'name_zh', 'name', 'description_zh', 'description', 'icon')
        }

        candidate_updates = []
        for candidate in candidates:
            label, effect, icon_url = _display_metadata(items, candidate.params)
            if not label and not effect and not icon_url:
                continue
            changed = False
            if label and candidate.label != label:
                candidate.label = label
                changed = True
            if icon_url and candidate.icon_url != icon_url:
                candidate.icon_url = icon_url
                changed = True
            if effect and candidate.effect != effect:
                candidate.effect = effect
                changed = True
            if changed:
                candidate_updates.append(candidate)

        run_updates = []
        for run in runs:
            label, effect, icon_url = _display_metadata(items, run.candidate_params)
            if not label and not effect and not icon_url:
                continue
            changed = False
            if label and run.candidate_label != label:
                run.candidate_label = label
                changed = True
            if icon_url and run.display_metadata.get('icon_url') != icon_url:
                metadata = deepcopy(run.display_metadata)
                metadata['icon_url'] = icon_url
                run.display_metadata = metadata
                changed = True
            if effect and run.display_metadata.get('effect') != effect:
                metadata = deepcopy(run.display_metadata)
                metadata['effect'] = effect
                run.display_metadata = metadata
                changed = True
            if changed:
                run_updates.append(run)

        execution_updates = []
        for execution in executions:
            desired = _execution_candidate_metadata(items, execution.config_snapshot)
            if not desired:
                continue
            metadata = deepcopy(execution.display_metadata)
            changed = False
            for key, values in desired.items():
                current = metadata.get(key)
                current = current if isinstance(current, dict) else {}
                merged = {**current}
                for field, value in values.items():
                    if merged.get(field) != value:
                        merged[field] = value
                        changed = True
                metadata[key] = merged
            if changed:
                execution.display_metadata = metadata
                execution_updates.append(execution)

        if not dry_run:
            with transaction.atomic():
                if candidate_updates:
                    SimcBenchmarkCandidate.objects.bulk_update(
                        candidate_updates, ['label', 'icon_url', 'effect'], batch_size=500,
                    )
                if run_updates:
                    SimulationRun.objects.bulk_update(
                        run_updates, ['candidate_label', 'display_metadata'], batch_size=500,
                    )
                if execution_updates:
                    SimcBenchmarkExecution.objects.bulk_update(
                        execution_updates, ['display_metadata'], batch_size=500,
                    )

        action = 'would update' if dry_run else 'updated'
        self.stdout.write(self.style.SUCCESS(
            f'{action} {len(candidate_updates)} candidates, {len(run_updates)} runs, and '
            f'{len(execution_updates)} executions '
            f'from {len(items)} local item snapshots.'
        ))
