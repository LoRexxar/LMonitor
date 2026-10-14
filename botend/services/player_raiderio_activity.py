"""Bounded Raider.IO character activity collection and network-free projection.

Only four raw modules are stored, once per season/region/realm/name. Profile
gear/talents remain the existing monitor's responsibility. Missing modules do
not erase last-good facts; source identity and current-season gates are strict.
"""
import logging
import math
import re
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from botend.constants.wow import CLASS_SPEC_MAP, RAID_ZONE_CN
from botend.models import PlayerSpecTopPlayer, RaiderIOCharacterActivity, SeasonMeta
from botend.raiderio_identity import activity_identity_digest, normalized_name
from botend.services.player_external_links import build_player_external_links
from botend.wow_i18n import cn_dungeon_from_slug, cn_spec_from_slug

MODULES = {
    'best_runs': 'mythic_plus_best_runs',
    'recent_runs': 'mythic_plus_recent_runs',
    'dungeon_counts': 'mythic_plus_dungeon_run_counts',
    'raid_progression': 'raid_progression',
}
TTL = timedelta(hours=24)


def activity_fields(rio_season):
    return ','.join(('mythic_plus_best_runs:all', 'mythic_plus_recent_runs',
                     f'mythic_plus_dungeon_run_counts:{rio_season}',
                     'raid_progression:current-tier', 'mythic_plus_scores_by_season:current'))


def player_identity(player):
    links = build_player_external_links(player)
    link = next((link for link in links if link['key'] == 'raiderio'), None)
    if not link:
        return None
    parts = [unquote(part) for part in urlsplit(link['url']).path.strip('/').split('/')]
    if parts[2].lower() == 'anonymous':
        return None
    return {'region': parts[1], 'realm_slug': parts[2], 'character_name': normalized_name(parts[3])}


def _date(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('invalid timestamp')
    parsed = parse_datetime(value)
    if parsed is None or timezone.is_naive(parsed):
        raise ValueError('invalid timestamp')
    return parsed


def _number(value, *, integer=False):
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int if integer else (int, float)):
        raise ValueError('invalid numeric fact')
    if value < 0 or not math.isfinite(value):
        raise ValueError('invalid numeric fact')


def _run_url(value, season):
    if not isinstance(value, str):
        raise ValueError('missing run URL')
    parsed = urlsplit(value)
    parts = parsed.path.strip('/').split('/')
    if (parsed.scheme != 'https' or parsed.netloc not in ('raider.io', 'www.raider.io')
            or len(parts) != 3 or parts[:2] != ['mythic-plus-runs', season]
            or not re.fullmatch(r'[0-9]+(?:-[a-zA-Z0-9-]+)?', parts[2])
            or parsed.query or parsed.fragment):
        raise ValueError('wrong run scope')


def validate_profile_identity(data, identity):
    """Gate both profile and activity publication on the same source identity."""
    if not isinstance(data, dict):
        raise ValueError('invalid envelope')
    source = SimpleNamespace(region=data.get('region'), realm=data.get('realm'),
                             character_name=data.get('name'), profile_url=data.get('profile_url'))
    if not data.get('profile_url') or player_identity(source) != identity:
        raise ValueError('wrong character identity')


def validate_payload(data, identity, season):
    """Validate before publication. Return only activity facts, never profile gear."""
    validate_profile_identity(data, identity)
    scores = data.get('mythic_plus_scores_by_season')
    if (not isinstance(scores, list) or len(scores) != 1 or not isinstance(scores[0], dict)
            or scores[0].get('season') != season.rio_season):
        raise ValueError('wrong current season')
    updated = _date(data.get('last_crawled_at'))
    facts = {}
    for module, field in MODULES.items():
        if field not in data or data[field] is None:
            continue
        value = data[field]
        expected = dict if module == 'raid_progression' else list
        if not isinstance(value, expected):
            raise ValueError('invalid module')
        if len(value) > (10 if module == 'recent_runs' else 100):
            raise ValueError('unbounded module')
        rows = value.values() if isinstance(value, dict) else value
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('invalid row')
            if module in ('best_runs', 'recent_runs'):
                if not isinstance(row.get('dungeon'), str) or not row['dungeon']:
                    raise ValueError('missing dungeon')
                if row.get('mythic_level') is None or row.get('clear_time_ms') is None:
                    raise ValueError('missing run facts')
                for key in ('mythic_level', 'clear_time_ms', 'par_time_ms', 'num_keystone_upgrades'):
                    _number(row.get(key), integer=True)
                _number(row.get('score'))
                _date(row.get('completed_at'))
                _run_url(row.get('url'), season.rio_season)
                spec = row.get('spec')
                if spec is not None and not isinstance(spec, dict):
                    raise ValueError('invalid run spec')
                for text in ((spec or {}).get('slug'), (spec or {}).get('name'), row.get('role')):
                    if text is not None and not isinstance(text, str):
                        raise ValueError('invalid run label')
            elif module == 'dungeon_counts':
                if not isinstance(row.get('dungeon'), str) or not row['dungeon']:
                    raise ValueError('missing dungeon')
                for key in ('zone_id', 'season_runs_total', 'season_runs_timed'):
                    _number(row.get(key), integer=True)
                total, timed = row.get('season_runs_total'), row.get('season_runs_timed')
                if total is not None and timed is not None and timed > total:
                    raise ValueError('inconsistent counts')
            else:
                total = row.get('total_bosses')
                _number(total, integer=True)
                for key in ('normal_bosses_killed', 'heroic_bosses_killed', 'mythic_bosses_killed'):
                    _number(row.get(key), integer=True)
                    if total is not None and row.get(key) is not None and row[key] > total:
                        raise ValueError('inconsistent raid progress')
        facts[field] = value
    return facts, updated


class ActivityCollector:
    """One batch; 429 stops every subsequent request, including forced retries."""
    def __init__(self, transport=None, proxies=None):
        self.transport = transport or requests.get
        cfg = getattr(settings, 'REQUEST_CONFIG', {})
        self.proxies = proxies if proxies is not None else cfg.get('proxies') if cfg.get('enable_proxy') else None
        self.stopped = False
        from botend.services.raiderio_run_roster import RosterCollector
        self.roster_collector = RosterCollector(transport=self.transport, proxies=self.proxies)

    def refresh(self, player, season, *, force=False, include_profile=False, refresh_ready=False):
        if self.stopped:
            return {'status': 'batch_stopped'}
        identity = player_identity(player)
        if not identity:
            return {'status': 'unresolved'}
        if not season.is_active or not season.rio_season or player.season_id != season.pk:
            return {'status': 'wrong_season'}
        now = timezone.now()
        with transaction.atomic():
            row, _ = RaiderIOCharacterActivity.objects.get_or_create(
                season=season, identity_digest=activity_identity_digest(**identity), defaults=identity)
            row = RaiderIOCharacterActivity.objects.select_for_update().get(pk=row.pk)
            deferred = None
            if row.next_attempt_at and row.next_attempt_at > now:
                if row.status == 'fetching':
                    deferred = 'busy'
                elif not (force or (refresh_ready and row.status == 'ready')):
                    deferred = 'fresh' if row.status == 'ready' else 'backoff'
            # A profile refresh can reuse the transport even when activity is fresh;
            # only due activity fields are attached, never another activity request.
            collect_activity = deferred is None
            if collect_activity:
                row.status = 'fetching'
                row.last_attempt_at = now
                row.next_attempt_at = now + timedelta(minutes=2)
                row.save(update_fields=['status', 'last_attempt_at', 'next_attempt_at'])
        if deferred and not include_profile:
            return self._refresh_rosters({'status': deferred, 'activity_id': row.pk}, row, season, player)
        fields = activity_fields(season.rio_season) if collect_activity else ''
        if include_profile:
            fields = ','.join(filter(None, ('gear,talents', fields)))
        result = {'activity_id': row.pk}
        error, retry_seconds = '', None
        try:
            response = self.transport('https://raider.io/api/v1/characters/profile',
                params={'region': identity['region'], 'realm': identity['realm_slug'],
                        'name': player.character_name, 'fields': fields},
                timeout=25, headers={'User-Agent': 'Mozilla/5.0'}, proxies=self.proxies)
            if response.status_code == 429:
                self.stopped = True
                error = 'rate_limited'
                try:
                    retry_seconds = max(60, min(86400, int(response.headers.get('Retry-After', 3600))))
                except (ValueError, TypeError):
                    retry_seconds = 3600
            elif response.status_code != 200:
                error = f'http_{response.status_code}'
            else:
                data = response.json()
                validate_profile_identity(data, identity)
                # Profile freshness is independent of activity validity/season.
                # Retain the verified profile even if activity publication fails.
                if include_profile:
                    result['profile_payload'] = data
                if not collect_activity:
                    return self._refresh_rosters({**result, 'status': deferred}, row, season, player)
                facts, updated = validate_payload(data, identity, season)
        except (ValueError, TypeError, KeyError, AttributeError):
            error = 'invalid_payload'
        except requests.RequestException:
            error = 'transport_error'
        if not collect_activity:
            return {**result, 'status': error}
        with transaction.atomic():
            current = RaiderIOCharacterActivity.objects.select_for_update().get(pk=row.pk)
            if current.last_attempt_at != now or current.status != 'fetching':
                return {**result, 'status': 'superseded'}
            if error:
                current.failure_count += 1
                current.status = 'error'
                current.last_error = error
                current.next_attempt_at = timezone.now() + timedelta(seconds=(retry_seconds or min(21600, 300 * 2 ** min(current.failure_count - 1, 6))))
            else:
                current.payload_json = {**current.payload_json, **facts}
                stamp = timezone.now()
                current.module_fetched_at = {**current.module_fetched_at, **{key: stamp.isoformat() for key in facts}}
                if facts:
                    current.fetched_at = stamp
                    current.source_updated_at = updated
                current.status = 'ready' if len(facts) == len(MODULES) else 'partial'
                current.failure_count = 0
                current.last_error = ''
                current.next_attempt_at = stamp + (TTL if current.status == 'ready' else timedelta(hours=1))
            current.save()
        result['status'] = error or current.status
        return self._refresh_rosters(result, current, season, player)

    def _refresh_rosters(self, result, row, season, player):
        # Only published local references: failed incoming activity never leaks
        # new runs. Run TTL/lease is independent of activity/profile freshness.
        # Called outside the activity transaction, and never during its busy lease.
        if self.stopped or result['status'] == 'busy':
            return result
        # Maintain only explicitly enrolled raid pilots; never expand to all players.
        # Its lease/TTL live inside the module, independent of M+ and profile clocks.
        if result['status'] in ('ready', 'fresh', 'partial') and 'raid_first_kills' in row.payload_json:
            try:
                from botend.services.player_raid_observation_sources import refresh_raid_sources
                raid = refresh_raid_sources(player, transport=self.transport)
                result['raid_first_kills'] = raid
                if raid['status'] == 'rate_limited':
                    self.stopped = True
                    return result
            except Exception as exc:
                logging.getLogger(__name__).warning('Raider.IO raid enrichment failed (%s)', type(exc).__name__)
                result['raid_first_kills'] = {'status': 'enrichment_error'}
        runs = row.payload_json.get('mythic_plus_best_runs')
        if not isinstance(runs, list) or not runs:
            return result
        try:
            result['roster_results'] = self.roster_collector.refresh_best_runs(runs, season.rio_season)
        except Exception:
            # Roster persistence/transport must not discard verified gear/talents.
            logging.getLogger(__name__).exception('Raider.IO roster enrichment failed')
            result['roster_results'] = [{'status': 'enrichment_error'}]
        finally:
            if self.roster_collector.stopped:
                self.stopped = True
        return result


def top20_candidates(season, *, class_name=None, spec_name=None, limit=None):
    """Scalar Top20 IDs first; never scan historical profile JSON for candidates."""
    ids = []
    for cls, specs in CLASS_SPEC_MAP.items():
        if class_name and cls != class_name:
            continue
        for spec in specs:
            if spec_name and spec != spec_name:
                continue
            ids.extend(PlayerSpecTopPlayer.objects.filter(season_id=season.pk,
                class_name=cls, spec_name=spec, rank__gte=1, rank__lte=20)
                .order_by('rank', 'pk').values_list('pk', flat=True)[:20])
    candidates, keyed = [], {}
    fields = ('id', 'season_id', 'region', 'realm', 'character_name', 'profile_url')
    for player in PlayerSpecTopPlayer.objects.filter(pk__in=ids).only(*fields).order_by('pk'):
        identity = player_identity(player)
        key = tuple(identity.values()) if identity else ('unresolved', player.pk)
        if key not in keyed:
            candidate = {'player': player, 'player_ids': [], 'identity': identity}
            keyed[key] = candidate
            candidates.append(candidate)
        keyed[key]['player_ids'].append(player.pk)
    return candidates[:limit] if limit is not None else candidates


def _slug(value):
    return re.sub(r'[^a-z0-9]+', '-', value.lower().replace("'", '')).strip('-')


def _percent(timed, total):
    return round(100 * timed / total, 1) if timed is not None and total else None


def _run(row):
    seconds = row['clear_time_ms'] // 1000
    upgrades = row.get('num_keystone_upgrades')
    spec = row.get('spec') or {}
    return {'dungeon_name': cn_dungeon_from_slug(_slug(row['dungeon']), row['dungeon']),
            'level': row['mythic_level'], 'score': row.get('score'),
            'clear_time': f'{seconds // 60:02d}:{seconds % 60:02d}',
            'completed_at': _date(row.get('completed_at')),
            'timed': upgrades > 0 if upgrades is not None else None, 'upgrades': upgrades,
            'spec_label': cn_spec_from_slug(spec.get('slug'), spec.get('name') or '') if spec else '',
            'role_label': {'tank': '坦克', 'healer': '治疗', 'dps': '输出'}.get(row.get('role'), row.get('role') or ''),
            'url': row['url']}


def project_player_activity(player):
    """Read-only, local ORM + source facts; no source discovery on page opens."""
    season = SeasonMeta.objects.filter(pk=player.season_id).first()
    view = {'status': 'pending', 'source_label': 'Raider.IO',
            'season_name': season.season_name if season else '',
            'rio_season': season.rio_season if season else '',
            'fetched_at': None, 'source_updated_at': None,
            **{key: {'status': 'missing', 'rows': []} for key in MODULES}}
    view['dungeon_counts'].update(total=None, timed=None, timed_percent=None)
    identity = player_identity(player)
    if not identity or not season:
        view['status'] = 'unavailable'
        return view
    row = RaiderIOCharacterActivity.objects.filter(
        season=season, identity_digest=activity_identity_digest(**identity)).first()
    if not row:
        return view
    view.update(fetched_at=row.fetched_at, source_updated_at=row.source_updated_at)
    present = set(row.payload_json)
    if not present:
        view['status'] = 'unavailable' if row.last_error else 'partial' if row.status == 'partial' else 'pending'
        return view
    view['status'] = 'partial' if row.status == 'partial' or len(present) < len(MODULES) else 'ready'
    stamps = [_date(value) for value in row.module_fetched_at.values()]
    if row.last_error or any(stamp and stamp < timezone.now() - TTL for stamp in stamps):
        view['status'] = 'stale'
    for module, field in MODULES.items():
        if field not in present:
            continue
        raw = row.payload_json[field]
        block = view[module]
        block['status'] = 'ready'
        if module in ('best_runs', 'recent_runs'):
            block['rows'] = [_run(run) for run in raw]
            if module == 'best_runs':
                from botend.services.raiderio_run_roster import project_run_rosters
                for projected, roster in zip(block['rows'], project_run_rosters(raw, season.rio_season)):
                    projected.update(roster)
        elif module == 'dungeon_counts':
            block['rows'] = [{'dungeon_name': cn_dungeon_from_slug(_slug(item['dungeon']), item['dungeon']),
                'total': item.get('season_runs_total'), 'timed': item.get('season_runs_timed'),
                'timed_percent': _percent(item.get('season_runs_timed'), item.get('season_runs_total'))} for item in raw]
            for key in ('total', 'timed'):
                values = [item[key] for item in block['rows']]
                block[key] = sum(values) if all(value is not None for value in values) else None
            block['timed_percent'] = _percent(block['timed'], block['total'])
        else:
            raid_names = {_slug(name): translated for name, translated in RAID_ZONE_CN.items()}
            block['rows'] = [{'name': raid_names.get(slug, slug), 'total_bosses': item.get('total_bosses'),
                **{diff: item.get(f'{diff}_bosses_killed') for diff in ('normal', 'heroic', 'mythic')}}
                for slug, item in raw.items()]
    return view
