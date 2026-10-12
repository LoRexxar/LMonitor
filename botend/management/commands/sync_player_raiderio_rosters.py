"""Bounded Top20 run roster backfill. No profile requests or ranking writes."""
import json
import os
import threading
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections

from botend.models import SeasonMeta
from botend.services.raiderio_run_roster import RosterCollector, top20_run_candidates


def collect_candidates(candidates, rio_season, *, workers, force):
    """At most workers futures; sessions/DB connections belong to each worker."""
    stop = threading.Event()
    local = threading.local()
    collectors = []
    lock = threading.Lock()

    def execute(candidate):
        close_old_connections()
        try:
            if not hasattr(local, 'collector'):
                local.collector = RosterCollector(stop_event=stop)
                with lock:
                    collectors.append(local.collector)
            if candidate['conflict']:
                return {'run_id': candidate['run_id'], 'status': 'conflicting_reference'}
            return {'run_id': candidate['run_id'], **local.collector.refresh(candidate['run'], rio_season, force=force)}
        finally:
            close_old_connections()

    try:
        if workers == 1:
            # Keep the default path on the caller's DB connection.
            collector = RosterCollector(stop_event=stop)
            collectors.append(collector)
            for item in candidates:
                if item['conflict']:
                    yield {'run_id': item['run_id'], 'status': 'conflicting_reference'}
                else:
                    yield {'run_id': item['run_id'], **collector.refresh(item['run'], rio_season, force=force)}
            return
        iterator = iter(candidates)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {}
            for _ in range(workers):
                item = next(iterator, None)
                if item is not None:
                    pending[pool.submit(execute, item)] = item['run_id']
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    run_id = pending.pop(future)
                    try:
                        yield future.result()
                    except Exception:
                        stop.set()
                        yield {'run_id': run_id, 'status': 'worker_error'}
                if not stop.is_set():
                    for _ in range(workers - len(pending)):
                        item = next(iterator, None)
                        if item is None:
                            break
                        pending[pool.submit(execute, item)] = item['run_id']
            for item in iterator:
                yield {'run_id': item['run_id'], 'status': 'batch_stopped'}
    finally:
        for collector in collectors:
            collector.close()


class Command(BaseCommand):
    help = 'Inspect Top20 best-run roster gaps (dry-run); --apply collects without refreshing profiles.'

    def add_arguments(self, parser):
        parser.add_argument('--season', type=int, required=True, help='Exact active SeasonMeta ID')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--limit', type=int, help='Maximum globally unique runs')
        parser.add_argument('--force', action='store_true', help='Bypass TTL/backoff, never a lease or 429 stop')
        parser.add_argument('--workers', type=int, default=1, help='1..4 workers, process-wide maximum 4 requests/s')
        parser.add_argument('--result-json', help='Atomic per-run checkpoint, IDs and statuses only')

    def handle(self, *args, **options):
        if options['limit'] is not None and options['limit'] <= 0:
            raise CommandError('--limit must be positive')
        if not 1 <= options['workers'] <= 4:
            raise CommandError('--workers must be between 1 and 4')
        season = SeasonMeta.objects.filter(pk=options['season'], is_active=True).first()
        if not season or not season.rio_season:
            raise CommandError('An active SeasonMeta with Raider.IO season is required')
        candidates, invalid = top20_run_candidates(season)
        total = len(candidates)
        if options['limit'] is not None:
            candidates = candidates[:options['limit']]
        report = {'season_id': season.pk, 'rio_season': season.rio_season,
            'apply': options['apply'], 'workers': options['workers'], 'complete': False,
            'discovered_count': total, 'candidate_count': len(candidates), 'invalid_reference_count': invalid,
            'results': [], 'counts': {}}
        path = Path(options['result_json']) if options['result_json'] else None

        def checkpoint():
            report['counts'] = dict(Counter(item['status'] for item in report['results']))
            report['processed_count'] = len(report['results'])
            if path:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
                temporary.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
                os.replace(temporary, path)

        checkpoint()
        if options['apply']:
            outcomes = collect_candidates(candidates, season.rio_season,
                workers=options['workers'], force=options['force'])
        else:
            outcomes = ({'run_id': item['run_id'], 'status': 'conflicting_reference' if item['conflict'] else 'dry_run'}
                        for item in candidates)
        for result in outcomes:
            report['results'].append(result)
            checkpoint()
        report['complete'] = True
        report['batch_stopped'] = any(item['status'] in ('rate_limited', 'batch_stopped', 'worker_error') for item in report['results'])
        checkpoint()
        self.stdout.write(json.dumps(report, indent=2))
