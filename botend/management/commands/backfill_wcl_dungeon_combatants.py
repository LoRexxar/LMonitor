"""Backfill only the active-season, per-dungeon top-100 statistics cohort."""
import json

from django.core.management.base import BaseCommand, CommandError

from botend.constants.wow import specialization_catalog, canonical_class_spec
from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
from botend.models import SeasonMeta
from botend.services.wcl_combatant_snapshot import (
    collect_dungeon_combatants, select_dungeon_combatant_records,
)


class Command(BaseCommand):
    help = 'Backfill central WCL CombatantInfo facts; DB-only dry-run unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument('--season', type=int, help='SeasonMeta ID (must be the active season)')
        parser.add_argument('--class', dest='class_name')
        parser.add_argument('--spec', dest='spec_name')
        parser.add_argument('--all-specs', action='store_true')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--batch-size', type=int, default=20, help='Fights per report request, 1–20')

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        if not 1 <= batch_size <= 20:
            raise CommandError('--batch-size must be between 1 and 20')
        season = SeasonMeta.objects.filter(is_active=True).first()
        if not season or (options['season'] is not None and options['season'] != season.id):
            raise CommandError('Only the active SeasonMeta may be backfilled')
        class_name, spec_name = options['class_name'], options['spec_name']
        if options['all_specs'] and (class_name or spec_name):
            raise CommandError('--all-specs cannot be combined with --class/--spec')
        if spec_name and not class_name:
            raise CommandError('--spec requires --class; omit both for all specializations')
        identities = [(row['class_name'], row['spec_name']) for row in specialization_catalog()]
        if class_name and spec_name:
            identity = canonical_class_spec(class_name, spec_name)
            if identity not in identities:
                raise CommandError('Unknown class/spec identity')
            identities = [identity]
        elif class_name:
            identities = [pair for pair in identities if pair[0].casefold() == class_name.casefold()]
            if not identities:
                raise CommandError('Unknown class identity')
        records = select_dungeon_combatant_records(season, identities)
        counts = collect_dungeon_combatants(records, batch_size=batch_size)
        self.stdout.write(json.dumps({'mode': 'plan', 'season': season.id, 'specs': len(identities), **counts}, sort_keys=True))
        if not options['apply']:
            self.stdout.write('DRY-RUN: no network requests or database writes; use --apply to collect.')
            return
        fetcher = SpecDetailBase(None, None)
        if counts['pending']:
            # One truthful budget observation, not a made-up estimate per character.
            budget = fetcher._wcl_graphql(
                '{rateLimitData {limitPerHour pointsSpentThisHour pointsResetIn}}', {}, retries=2)
            rate = (budget or {}).get('rateLimitData')
            if not isinstance(rate, dict):
                self.stdout.write(json.dumps({'mode': 'apply', **counts,
                                              'status': 'budget_request_failed'}, sort_keys=True))
                raise CommandError('WCL rate-limit query failed; nothing was written')
            self.stdout.write(json.dumps({'rateLimitData': rate}, sort_keys=True))
        counts = collect_dungeon_combatants(records, fetcher, apply=True,
                                            batch_size=batch_size, log=self.stdout.write)
        incomplete = counts['failed'] + counts['permission_denied'] + counts['missing']
        self.stdout.write(json.dumps({'mode': 'apply', 'season': season.id, **counts,
                                     'status': 'partial' if incomplete else 'complete'}, sort_keys=True))
        if incomplete:
            raise CommandError(f'Incomplete WCL cohort: {incomplete} missing/denied/failed observations; rerun reuses saved actors')
