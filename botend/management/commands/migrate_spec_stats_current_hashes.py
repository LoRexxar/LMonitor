"""Observe hashes of existing current closures; no publication history repair."""
import json
from django.core.management.base import BaseCommand, CommandError
from botend.services.spec_stats_retention import migrate_current_hashes


class Command(BaseCommand):
    help = 'Explicit current-only hash migration (default dry-run); never clears sticky GC disable.'

    def add_arguments(self, parser):
        parser.add_argument('scope')
        parser.add_argument('--apply', action='store_true', default=False)

    def handle(self, *args, **options):
        result = migrate_current_hashes(options['scope'], apply=options['apply'])
        self.stdout.write(json.dumps(result, ensure_ascii=False))
        if result['status'] not in ('dry-run', 'migrated'):
            raise CommandError(result.get('reason', result['status']))
