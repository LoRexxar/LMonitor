import requests
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from botend.models import WowRealmDirectory
from botend.services.player_external_links import REGION_LOCALES, parse_realm_directory


class Command(BaseCommand):
    help = 'Validate the shared Raider.IO realm directory; --apply publishes it.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        records = []
        try:
            for region in REGION_LOCALES:
                response = requests.get(f'https://raider.io/realms/{region}', timeout=30)
                response.raise_for_status()
                rows = parse_realm_directory(response.text, region)
                records.extend(rows)
                self.stdout.write(f'{region}: {len(rows)} verified realms')
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            raise CommandError(f'Realm directory not published: {exc}') from exc
        if not options['apply']:
            self.stdout.write(f'DRY RUN: {len(records)} realms; no database changes')
            return
        with transaction.atomic():
            for row in records:
                region, slug = row['region'], row['slug']
                WowRealmDirectory.objects.update_or_create(
                    region=region, slug=slug,
                    defaults={key: value for key, value in row.items() if key not in ('region', 'slug')},
                )
        self.stdout.write(self.style.SUCCESS(f'Published {len(records)} realm identities'))
