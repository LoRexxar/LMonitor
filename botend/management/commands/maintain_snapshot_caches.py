"""Explicit bounded projection GC; defaults to a read-only dry-run."""
import json
from django.core.management.base import BaseCommand, CommandError
from botend.services.snapshot_cache_gc import ROOTS, maintain_snapshot_cache


class Command(BaseCommand):
    help = 'Validate and collect obsolete news/RIO/journal cache content (dry-run by default).'

    def add_arguments(self, parser):
        parser.add_argument('--kind', choices=['all', *ROOTS], default='all')
        parser.add_argument('--apply', action='store_true', default=False)
        parser.add_argument('--limit', type=int, default=None, help='Per-root delete batch (1..1000; default setting or 32)')

    def handle(self, *args, **options):
        if options['limit'] is not None and not 1 <= options['limit'] <= 1000:
            raise CommandError('--limit must be 1..1000')
        kinds = ROOTS if options['kind'] == 'all' else [options['kind']]
        results = [maintain_snapshot_cache(kind, apply=options['apply'], limit=options['limit']) for kind in kinds]
        self.stdout.write(json.dumps(results, ensure_ascii=False, sort_keys=True))
        if any(result['status'] == 'blocked' for result in results):
            raise CommandError('One or more roots blocked; no unsafe deletion attempted.')
