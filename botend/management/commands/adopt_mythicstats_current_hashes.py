"""Explicit current-only observed-content-hash adoption, never historical repair."""
import json
from django.core.management.base import BaseCommand, CommandError
from botend.services.mythicstats_retention import adopt_current_hashes


class Command(BaseCommand):
    help = ('Validate current Mythicstats closure and fill only missing current content_hash; '
            'default dry-run. Requires existing worker.lock; never clears sticky GC blocks.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        result = adopt_current_hashes(apply=options['apply'])
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if result['busy'] or result['blocked']:
            raise CommandError(result.get('reason', 'mythicstats worker busy'))
