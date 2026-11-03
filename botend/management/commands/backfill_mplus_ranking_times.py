"""Recover dates only from bounded, existing, verified private ranking pages.

No HTTP, legacy cache import, cache writes, talent/gear hydration or guessed clocks.
The default is read-only; --apply locks current source rows before exact matching.
"""
import json
from contextlib import nullcontext

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from botend.models import SeasonMeta, SpecDungeonRanking
from botend.constants.wow import CLASS_SPEC_MAP, SPEC_ROLE
from botend.services import spec_aggregation_cache as cache
from botend.services.stats_storage_codec import unpack_detail
from botend.services.wcl_ranking_time import parse_ranking_fight_started_at
from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor

TARGET = 300
# The normal collector stops on 300 observations, not a presumed 200/page.
# With at least one row per hasMorePages page, at most 300 pages can contribute.
MAX_PAGES = TARGET
FIELDS = ('season_id', 'dungeon_id', 'class_name', 'spec_name', 'report_code',
          'fight_id', 'region', 'realm', 'character_name', 'dps', 'keystone_level',
          'clear_time', 'score', 'medal', 'faction', 'guild_name')


def exact_key(values):
    # A verified report/fight identifies the event date even when WCL omits
    # server identity. Keep those absent fields exact; never invent a character.
    if (not values.get('report_code') or type(values.get('fight_id')) is not int
            or values['fight_id'] <= 0):
        return None
    return tuple(values.get(field) for field in FIELDS)


def raw_key(row, scope):
    report, server, guild = row.get('report') or {}, row.get('server') or {}, row.get('guild') or {}
    return exact_key({**scope, 'report_code': report.get('code', ''), 'fight_id': report.get('fightID'),
        'region': server.get('region', ''), 'realm': server.get('name', ''),
        'character_name': row.get('name', ''), 'dps': row['amount'],
        'keystone_level': row.get('hardModeLevel'), 'clear_time': row.get('duration'),
        'score': row.get('score'), 'medal': row.get('medal', ''), 'faction': row.get('faction'),
        'guild_name': guild.get('name', '')})


def read_dates(scope):
    dates, conflicts = {}, set()
    collected = 0
    fingerprint = None
    complete = False
    for page in range(1, MAX_PAGES + 1):
        name = ('ranking-page:{dungeon_id}:{class_name}:{spec_name}:dps:None:' + str(page)).format(**scope)
        path = cache.work_path(scope['season_id'], name)
        data = cache._read_file(path)
        if data is None:
            data = cache._read_file(path.with_suffix(''))
        if not cache._valid(data, scope['season_id'], name):
            break
        if fingerprint is not None and fingerprint != data['fingerprint']:
            break  # Do not combine pages from different collection cycles.
        fingerprint = data['fingerprint']
        try:
            payload = unpack_detail(data['payload'])
            if not SpecDetailRankingMonitor._valid_ranking_page(payload):
                break
            for row in payload['rankings'][:TARGET - collected]:
                collected += 1
                key = raw_key(row, scope)
                date = parse_ranking_fight_started_at(row)
                if key is None or date is None:
                    continue
                if key in dates and dates[key] != date:
                    conflicts.add(key)
                dates[key] = date
            complete = collected >= TARGET or not payload['hasMorePages']
            if complete:
                break
        except (ValueError, KeyError, TypeError, OverflowError):
            break
        finally:
            # Never retain unpacked gear/talents from previous pages/scopes.
            data = None
            payload = None
    for key in conflicts:
        dates.pop(key, None)
    return dates, conflicts, complete


def backfill_scope(scope, *, apply=False):
    dates, conflicts, complete = read_dates(scope)  # All file IO BEFORE row locks.
    counts = dict(updated=0, already=0, unknown=0, conflict=0)
    query = SpecDungeonRanking.objects.filter(**scope)
    with transaction.atomic() if apply else nullcontext():
        counts['already'] = query.filter(fight_started_at__isnull=False).count()
        current = query.filter(fight_started_at__isnull=True).order_by('pk').only(
            'id', 'fight_started_at', *FIELDS)
        if apply:
            current = current.select_for_update()
        rows = list(current[:TARGET + 1])
        if len(rows) > TARGET:
            # An unexpected oversized scope must not become an unbounded write.
            counts['unknown'] = query.filter(fight_started_at__isnull=True).count()
            return counts, False
        pending = []
        for row in rows:
            key = exact_key({field: getattr(row, field) for field in FIELDS})
            if key in conflicts:
                counts['conflict'] += 1
            elif key is None or key not in dates:
                counts['unknown'] += 1
            else:
                row.fight_started_at = dates[key]
                pending.append(row)
        counts['updated'] = len(pending)
        if apply and pending:
            SpecDungeonRanking.objects.bulk_update(pending, ['fight_started_at'], batch_size=TARGET)
    return counts, complete


class Command(BaseCommand):
    help = 'Strict cache-only fight date recovery. Default dry-run; only --apply writes fight_started_at.'

    def add_arguments(self, parser):
        parser.add_argument('--season', required=True, help='Exact SeasonMeta primary key or season_key')
        parser.add_argument('--dungeon', type=int)
        parser.add_argument('--class', dest='class_name', choices=tuple(CLASS_SPEC_MAP))
        parser.add_argument('--spec')
        parser.add_argument('--apply', action='store_true', default=False)

    def handle(self, *args, **options):
        token = options['season']
        seasons = SeasonMeta.objects.filter(pk=int(token)) if token.isdecimal() else SeasonMeta.objects.filter(season_key=token)
        season_id = seasons.values_list('pk', flat=True).first()
        if season_id is None:
            raise CommandError('Exact season not found')
        if options['spec'] and options['spec'] not in (
                CLASS_SPEC_MAP.get(options['class_name'], []) if options['class_name'] else
                [spec for specs in CLASS_SPEC_MAP.values() for spec in specs]):
            raise CommandError('Unknown class/spec combination')
        filters = {'season_id': season_id}
        for option, field in (('dungeon', 'dungeon_id'), ('class_name', 'class_name'), ('spec', 'spec_name')):
            if options[option] is not None:
                filters[field] = options[option]
        scopes = SpecDungeonRanking.objects.filter(**filters).order_by(
            'dungeon_id', 'class_name', 'spec_name').values_list('dungeon_id', 'class_name', 'spec_name').distinct()
        total = dict(updated=0, already=0, unknown=0, conflict=0)
        partial = False
        scope_count = 0
        for dungeon, cls, spec in scopes.iterator(chunk_size=100):
            if SPEC_ROLE.get((cls, spec)) != 'dps':
                continue
            scope = dict(season_id=season_id, dungeon_id=dungeon, class_name=cls, spec_name=spec)
            counts, complete = backfill_scope(scope, apply=options['apply'])
            incomplete = not complete or bool(counts['unknown'] or counts['conflict'])
            partial |= incomplete
            scope_count += 1
            for key in total:
                total[key] += counts[key]
            self.stdout.write(json.dumps(dict(scope=scope, mode='apply' if options['apply'] else 'dry-run',
                status='partial' if incomplete else 'complete', **counts), sort_keys=True))
        self.stdout.write(json.dumps(dict(total=total, scopes=scope_count,
            mode='apply' if options['apply'] else 'dry-run',
            status='partial' if partial else 'complete'), sort_keys=True))
