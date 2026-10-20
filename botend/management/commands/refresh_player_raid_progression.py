"""Explicit central-reference import OR exact-player raid-only refresh."""
import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from botend.models import PlayerSpecTopPlayer, SeasonMeta
from botend.services.player_raid_progression import bind_catalog, refresh_raid_progression


class Command(BaseCommand):
    help = 'Dry-run by default. Import captured raid catalog OR refresh exactly one player raid module.'

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument('--player-id', type=int)
        target.add_argument('--catalog', type=Path)
        parser.add_argument('--season-id', type=int)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        try:
            if options['catalog']:
                if not options['season_id']:
                    raise CommandError('--catalog requires --season-id')
                envelope = json.loads(options['catalog'].read_text())
                if envelope.get('status') != 200:
                    raise CommandError('catalog capture must be HTTP 200')
                result = bind_catalog(options['season_id'], envelope['data'], envelope['url'], apply=options['apply'])
                result = {'status': 'applied' if options['apply'] else 'dry_run', **result}
            else:
                if options['season_id']:
                    raise CommandError('--season-id is only for --catalog')
                player = PlayerSpecTopPlayer.objects.get(pk=options['player_id'])
                result = refresh_raid_progression(player, apply=options['apply'])
        except (ValueError, KeyError, TypeError, OSError, PlayerSpecTopPlayer.DoesNotExist, SeasonMeta.DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False))
