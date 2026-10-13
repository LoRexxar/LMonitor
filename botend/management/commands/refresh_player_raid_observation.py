"""Opt-in single-character collection; the default never writes or uses network."""
import json

from django.core.management.base import BaseCommand, CommandError

from botend.models import PlayerSpecTopPlayer, RaiderIOCharacterActivity
from botend.raiderio_identity import activity_identity_digest
from botend.services.player_raid_observation_sources import (
    MODULE, SourceError, _target, refresh_raid_sources,
)


class Command(BaseCommand):
    help = 'Inspect one current Top20 player; --apply explicitly collects raid first kills (no AI).'

    def add_arguments(self, parser):
        parser.add_argument('--player-id', type=int, required=True)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--apply', action='store_true', help='Allow up to three GETs and module-only writes.')
        mode.add_argument('--dry-run', action='store_true', help='Read-only inspection (the default).')

    def handle(self, *args, **options):
        player = PlayerSpecTopPlayer.objects.filter(pk=options['player_id']).only('id').first()
        result = {'status': 'not_found', 'activity_id': None, 'requests': 0}
        if player is not None and options['apply']:
            result = refresh_raid_sources(player)
        elif player is not None:
            try:
                season, identity = _target(player)
                activity = RaiderIOCharacterActivity.objects.filter(season=season,
                    identity_digest=activity_identity_digest(**identity)).only('id', 'payload_json').first()
                module = (activity.payload_json.get(MODULE) or {}) if activity else {}
                result.update(status='dry_run', activity_id=activity.pk if activity else None,
                    module_status=module.get('status', 'missing'), eligible=True,
                    max_requests=3)
            except SourceError as exc:
                result['status'] = exc.code
        result.update(player_id=options['player_id'], apply=bool(options['apply']))
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
        allowed = ('ready', 'fresh', 'private') if options['apply'] else ('dry_run',)
        if result['status'] not in allowed:
            raise CommandError(result['status'])
