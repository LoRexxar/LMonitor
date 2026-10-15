"""Explicit scoped download maintenance; no production discovery."""
import json
from django.core.management.base import BaseCommand, CommandError
from botend.services.download_retention import maintain_download_parts


class Command(BaseCommand):
    help = 'Report proven download parts older than 48h (dry-run unless --apply).'

    def add_arguments(self, parser):
        parser.add_argument('parts_root', help='Exact table.locale.parts directory')
        parser.add_argument('--apply', action='store_true', default=False)
        parser.add_argument('--max-entries', type=int, default=1024)
        parser.add_argument('--max-bytes', type=int, default=128 * 1024 * 1024)

    def handle(self, *args, **options):
        try:
            result = maintain_download_parts(options['parts_root'], apply=options['apply'],
                                             max_entries=options['max_entries'], max_bytes=options['max_bytes'])
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
