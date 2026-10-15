"""Offline, bounded Mythicstats body retention; dry-run unless --apply."""
import json
from django.core.management.base import BaseCommand, CommandError
from botend.services.mythicstats_retention import collect_garbage, MAX_DELETE


class Command(BaseCommand):
    help = ('Audit/reclaim Mythicstats bodies (default zero-write dry-run; existing root requires worker.lock). '
            'Apply may initialize root/lock; legacy current hashes require explicit adopt_mythicstats_current_hashes.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--limit', type=int, default=MAX_DELETE)

    def handle(self, *args, **options):
        try:
            result = collect_garbage(apply=options['apply'], limit=options['limit'])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if result['busy'] or result['blocked']:
            raise CommandError(result.get('reason', 'mythicstats worker busy'))
