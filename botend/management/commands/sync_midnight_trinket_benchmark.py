import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from botend.models import SimcBenchmarkPanel
from botend.services.midnight_trinket_catalog import (
    build_ptr_12_1_panel_payload,
    parse_ptr_12_1_catalog,
    ptr_12_1_matrix_plan,
)
from botend.services.simc_benchmark_config import normalize_panel_payload, replace_panel_config


class Command(BaseCommand):
    help = 'Synchronize an audited 12.1 PTR Wago DB2 trinket benchmark panel.'

    def add_arguments(self, parser):
        parser.add_argument('--fixture', required=True)
        parser.add_argument('--owner-id', type=int, required=True)
        parser.add_argument('--panel-id', type=int)
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--slug', default='ptr-12-1-mythic-trinkets')

    def handle(self, *args, **opts):
        try:
            with open(opts['fixture'], encoding='utf-8') as stream:
                catalog = parse_ptr_12_1_catalog(json.load(stream))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CommandError(f'PTR trinket catalog rejected: {exc}')
        if opts['panel_id'] is not None:
            panel = SimcBenchmarkPanel.objects.filter(pk=opts['panel_id']).first()
            if panel is None:
                raise CommandError('Target Panel does not exist')
        else:
            panel = SimcBenchmarkPanel.objects.filter(slug=opts['slug']).first()
        if panel is not None and panel.created_by_id != opts['owner_id']:
            raise CommandError('Existing Panel belongs to a different owner')
        try:
            payload = build_ptr_12_1_panel_payload(catalog, opts['owner_id'], opts['slug'])
            snapshot = normalize_panel_payload(payload, opts['owner_id'], panel=panel)
        except ValidationError as exc:
            raise CommandError(f'PTR trinket resources rejected: {exc}')
        self.stdout.write(json.dumps(
            ptr_12_1_matrix_plan(snapshot, catalog.unresolved_item_ids),
            ensure_ascii=False,
            sort_keys=True,
        ))
        if opts['dry_run']:
            self.stdout.write(self.style.WARNING('dry-run: no database writes'))
            return
        try:
            panel, plan = replace_panel_config(snapshot, opts['owner_id'], panel=panel)
        except ValidationError as exc:
            raise CommandError(f'PTR trinket Panel rejected: {exc}')
        self.stdout.write(self.style.SUCCESS(
            f'upserted panel id={panel.pk} cases={plan["case_count"]} '
            f'runs={plan["run_count"]} unresolved={len(catalog.unresolved_item_ids)}'
        ))
