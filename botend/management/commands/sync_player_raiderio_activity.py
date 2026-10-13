"""Backfill only current canonical Top20 identities; no profile/WCL writes."""
import json
import os
from collections import Counter
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from botend.constants.wow import CLASS_SPEC_MAP
from botend.models import SeasonMeta
from botend.services.player_raiderio_activity import ActivityCollector, top20_candidates


class Command(BaseCommand):
    help = 'Inspect current Top20 Raider.IO activity gaps (default dry-run); --apply collects.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--season', type=int, help='Exact active SeasonMeta ID')
        parser.add_argument('--class', dest='class_name', help='Canonical class name')
        parser.add_argument('--spec', help='Canonical spec name (requires --class)')
        parser.add_argument('--limit', type=int, help='Maximum deduplicated identities')
        parser.add_argument('--force', action='store_true', help='Bypass TTL/backoff, never an active lease or 429 batch stop')
        parser.add_argument('--result-json', help='Atomic progress/final JSON file, contains IDs only')

    def handle(self, *args, **options):
        cls, spec = options['class_name'], options['spec']
        if cls and cls not in CLASS_SPEC_MAP:
            raise CommandError('Unknown canonical class')
        if spec and (not cls or spec not in CLASS_SPEC_MAP[cls]):
            raise CommandError('--spec requires a matching canonical --class')
        if options['limit'] is not None and options['limit'] <= 0:
            raise CommandError('--limit must be positive')
        seasons = SeasonMeta.objects.filter(is_active=True)
        if options['season'] is not None:
            seasons = seasons.filter(pk=options['season'])
        if seasons.count() != 1:
            raise CommandError('Exactly one matching active SeasonMeta is required')
        season = seasons.get()
        if not season.rio_season:
            raise CommandError('Active season has no Raider.IO season')
        candidates = top20_candidates(season, class_name=cls, spec_name=spec, limit=options['limit'])
        report = {'season_id': season.pk, 'rio_season': season.rio_season,
                  'apply': options['apply'], 'complete': False, 'candidate_count': len(candidates),
                  'results': [], 'counts': {}}
        path = Path(options['result_json']) if options['result_json'] else None

        def checkpoint():
            report['counts'] = dict(Counter(item['status'] for item in report['results']))
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
                temporary.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
                os.replace(temporary, path)

        checkpoint()
        collector = ActivityCollector()
        for candidate in candidates:
            result = {'player_ids': candidate['player_ids']}
            if not candidate['identity']:
                result['status'] = 'unresolved'
            elif options['apply']:
                result.update(collector.refresh(candidate['player'], season, force=options['force']))
                result.pop('profile_payload', None)
            else:
                result['status'] = 'dry_run'
            report['results'].append(result)
            checkpoint()
        report['complete'] = True
        checkpoint()
        self.stdout.write(json.dumps(report, indent=2))
