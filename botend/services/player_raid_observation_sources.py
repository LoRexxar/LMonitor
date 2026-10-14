"""Explicit Top20 raid first-kill refresh, isolated from all M+ freshness/state.

Three bounded GETs: character page -> discovered siteMeta -> complete character
API. No standalone raid-progress request, AI, retries, or per-character catalog
storage. Only payload_json.raid_first_kills is written, under a module-local CAS
lease. Network I/O is outside transactions. Internal webpage APIs are an observed
contract: any contract change fails closed and retains (but may hide) last-good.
"""
import json
import re
import threading
import uuid
from datetime import timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

import requests
from django.conf import settings
from django.db import DatabaseError, transaction
from django.utils import timezone

from botend.constants.wow import canonical_class_spec
from botend.models import PlayerSpecTopPlayer, RaiderIOCharacterActivity, SeasonMeta
from botend.raiderio_identity import activity_identity_digest, normalized_name
from botend.services.player_raiderio_activity import _date, player_identity
from botend.services.raiderio_run_roster import _pace, _positive

MODULE = 'raid_first_kills'
TTL = timedelta(hours=24)
BACKOFF = timedelta(minutes=30)
LEASE = timedelta(minutes=2)


class SourceError(ValueError):
    def __init__(self, code, *, hide=False, retry_after=None):
        super().__init__(code)
        self.code, self.hide, self.retry_after = code, hide, retry_after


def _label(value):
    if not isinstance(value, str) or not value or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise SourceError('invalid_payload')
    return value


def _slug(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', value):
        raise SourceError('invalid_payload')
    return value


def _profile_url(identity):
    return 'https://raider.io/characters/' + '/'.join(quote(identity[k], safe='')
        for k in ('region', 'realm_slug', 'character_name'))


def _safe_url(url, host):
    if not isinstance(url, str) or not url or any(c.isspace() or ord(c) < 32 for c in url) or '\\' in url:
        raise SourceError('invalid_discovery')
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.netloc != host or parts.fragment:
        raise SourceError('invalid_discovery')
    return parts


def _same_path(path, expected):
    # Split before decoding; an encoded slash must not create a new component.
    actual = [unquote(x, errors='strict') for x in path.split('/')]
    target = [unquote(x, errors='strict') for x in expected.split('/')]
    if len(actual) != len(target) or any('/' in x or '\\' in x or any(ord(c) < 32 for c in x) for x in actual):
        return False
    return actual[:-1] == target[:-1] and normalized_name(actual[-1]) == normalized_name(target[-1])


class _Preloads(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag != 'link':
            return
        values = dict(attrs)
        if values.get('rel') == 'preload' and values.get('as') == 'fetch':
            href = values.get('href', '')
            if '/api/characters/' in href:
                if len(values) != len(attrs):
                    raise SourceError('invalid_discovery')
                self.links.append(href)


def discover_sources(html, identity, rio_season):
    """Decode JSON only, never evaluate scripts; validate origin/path/query."""
    if not isinstance(html, str) or len(html) > 2_000_000:
        raise SourceError('invalid_discovery')
    markers = list(re.finditer(r'window\.__RIO_INITIAL_DATA\s*=\s*', html))
    if len(markers) != 1:
        raise SourceError('invalid_discovery')
    initial = json.JSONDecoder().raw_decode(html[markers[0].end():])[0]
    if isinstance(initial, str):
        initial = json.loads(initial)
    catalog_url = initial['siteConfig']['siteMetaUrl']
    catalog = _safe_url(catalog_url, 'cdn.raiderio.net')
    if catalog.query or not re.fullmatch(r'/assets/data/siteMeta-[A-Za-z0-9_-]+\.json', catalog.path):
        raise SourceError('invalid_discovery')
    parser = _Preloads()
    parser.feed(html)
    if len(parser.links) != 1:
        raise SourceError('invalid_discovery')
    href = parser.links[0]
    api = _safe_url('https://raider.io' + href if href.startswith('/') and not href.startswith('//') else href,
                    'raider.io')
    expected = urlsplit(_profile_url(identity)).path.replace('/characters/', '/api/characters/', 1)
    if not _same_path(api.path, expected):
        raise SourceError('identity_mismatch', hide=True)
    pairs = parse_qsl(api.query, keep_blank_values=True, strict_parsing=True)
    query = dict(pairs)
    if len(pairs) != 2 or set(query) != {'season', 'tier'}:
        raise SourceError('invalid_discovery')
    if query['season'] != rio_season:
        raise SourceError('wrong_season')
    tier = query['tier']
    if not re.fullmatch(r'[1-9][0-9]{0,8}', tier):
        raise SourceError('wrong_tier')
    # Reconstruct from trusted identity; never persist a page-supplied URL.
    api_url = 'https://raider.io' + expected + '?' + urlencode({'season': rio_season, 'tier': tier})
    return {'profile_url': _profile_url(identity), 'api_url': api_url, 'catalog_url': catalog_url, 'tier': tier}


def validate_catalog(data, season):
    """RIO ordinal+1 is physical-raid order, never WCL global index/zone order."""
    central = season.raid_encounters
    if not isinstance(central, list):
        raise SourceError('invalid_catalog')
    ids = [_positive(e['id']) for e in central]
    if len(ids) != len(set(ids)):
        raise SourceError('invalid_catalog')
    current_ids = set(ids)
    raids = data['raids']
    if not isinstance(raids, list) or not raids or len(raids) > 200:
        raise SourceError('invalid_catalog')
    catalog, selected, raid_ids, current_seen = {}, [], set(), set()
    for raid in raids:
        slug, rid = _slug(raid['slug']), _positive(raid['id'])
        if slug in catalog or rid in raid_ids:
            raise SourceError('invalid_catalog')
        raid_ids.add(rid)
        encounters = raid['encounters']
        if not isinstance(encounters, list) or not encounters or len(encounters) > 100:
            raise SourceError('invalid_catalog')
        ordered, seen = [], {k: set() for k in ('ordinal', 'slug', 'wowEncounterId', 'encounterId')}
        for encounter in encounters:
            ordinal = encounter['ordinal']
            if type(ordinal) is not int or ordinal < 0:
                raise SourceError('invalid_catalog')
            e = {'ordinal': ordinal, 'slug': _slug(encounter['slug']),
                 'wowEncounterId': _positive(encounter['wowEncounterId']),
                 'encounterId': _positive(encounter['encounterId'])}
            for key, value in e.items():
                if value in seen[key]:
                    raise SourceError('invalid_catalog')
                seen[key].add(value)
            ordered.append(e)
        item = {'slug': slug, 'name': _label(raid['name']),
                'encounters': sorted(ordered, key=lambda e: e['ordinal'])}
        catalog[slug] = item
        if raid.get('difficulty') != 'mythic' or not (seen['wowEncounterId'] & current_ids):
            continue
        # Historical catalogs include legacy one-based ordinals. Only current
        # physical raids enter this rule; never repair their required 0-base.
        if (seen['ordinal'] != set(range(len(ordered)))
                or not seen['wowEncounterId'] <= current_ids
                or current_seen & seen['wowEncounterId']):
            raise SourceError('invalid_catalog')
        current_seen.update(seen['wowEncounterId'])
        if len(ordered) >= 8:
            selected.append(item)
    return catalog, selected


def _character_id(details, identity, expected_id):
    character = details['character']
    try:
        cid = _positive(character['id'])
        if (expected_id is not None and cid != _positive(expected_id)
                or normalized_name(character['name']) != identity['character_name']
                or character['region']['slug'] != identity['region']
                or character['realm']['slug'] != identity['realm_slug']
                or not _same_path(character['path'], urlsplit(_profile_url(identity)).path)):
            raise ValueError('identity')
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SourceError('identity_mismatch', hide=True) from None
    return cid


def validate_details(data, identity, rio_season, source, catalog, selected, expected_id, now):
    details = data['characterDetails']
    cid = _character_id(details, identity, expected_id)
    if type(details.get('isPrivate')) is not bool:
        raise SourceError('invalid_privacy', hide=True)
    if details.get('tier') != source['tier']:
        raise SourceError('wrong_tier')
    scope = data['characterMythicPlusProgress']
    if scope.get('season') != rio_season or scope.get('seasonSlug') != rio_season:
        raise SourceError('wrong_season')
    if details['isPrivate']:
        return cid, None
    progress = data['characterRaidProgress']
    if progress['tier'] != source['tier']:
        raise SourceError('wrong_tier')
    rows = progress['raidProgress']
    if not isinstance(rows, list) or len(rows) > 200:
        raise SourceError('invalid_payload')
    by_raid = {}
    for row in rows:
        slug = _slug(row['raid'])
        if slug in by_raid or slug not in catalog:
            raise SourceError('invalid_payload')
        kills = row['encountersDefeated']['mythic']
        if not isinstance(kills, list) or len(kills) > 100:
            raise SourceError('invalid_payload')
        valid_slugs = {e['slug'] for e in catalog[slug]['encounters']}
        keyed = {}
        for kill in kills:
            key = _slug(kill['slug'])
            if key in keyed or key not in valid_slugs:
                raise SourceError('invalid_payload')
            first, count = kill.get('firstDefeated'), kill.get('numKills')
            if first is not None:
                stamp = _date(first)
                if stamp > now:
                    raise SourceError('invalid_payload')
                # A known first kill must also be an actual positive kill row.
                _positive(count)
            if count is not None:
                _positive(count)
            keyed[key] = {'firstDefeated': first, 'numKills': count}
        by_raid[slug] = keyed
    result = []
    for raid in selected:
        kills = by_raid.get(raid['slug'], {})
        result.append({'slug': raid['slug'], 'name': raid['name'], 'encounters': [
            {'index': e['ordinal'] + 1, 'wowEncounterId': e['wowEncounterId'], 'slug': e['slug'],
             **kills.get(e['slug'], {'firstDefeated': None, 'numKills': None})}
            for e in raid['encounters'] if e['ordinal'] in (5, 6, 7)]})
    return cid, result


class _Transport:
    def __init__(self, transport, now):
        self.session = requests.Session()
        self.transport = transport or self.session.get
        self.requests, self.now = 0, now
        self.stop = threading.Event()
        config = getattr(settings, 'REQUEST_CONFIG', {})
        self.proxies = config.get('proxies') if config.get('enable_proxy') else None

    def get(self, url):
        parts = urlsplit(url)
        if parts.hostname not in ('raider.io', 'cdn.raiderio.net'):
            raise SourceError('invalid_discovery')
        _safe_url(url, parts.hostname)
        if self.requests >= 3 or self.stop.is_set() or not _pace(self.stop):
            raise SourceError('request_budget')
        self.requests += 1
        response = self.transport(url, timeout=25, allow_redirects=False,
            proxies=self.proxies, headers={'User-Agent': 'Mozilla/5.0'})
        if response.status_code == 429:
            self.stop.set()
            value = response.headers.get('Retry-After', '')
            delay = BACKOFF
            try:
                if re.fullmatch(r'[0-9]+', value):
                    delay = max(delay, timedelta(seconds=min(int(value), 31536000)))
                else:
                    stamp = parsedate_to_datetime(value)
                    if timezone.is_aware(stamp):
                        delay = max(delay, stamp - self.now)
            except (ValueError, TypeError, OverflowError):
                pass
            raise SourceError('rate_limited', retry_after=delay)
        if response.status_code != 200:
            raise SourceError(f'http_{response.status_code}')
        return response


def _target(player):
    primary = PlayerSpecTopPlayer.objects.filter(pk=player.pk).only(
        'id', 'season_id', 'region', 'realm', 'character_name', 'profile_url',
        'class_name', 'spec_name', 'rank').first()
    if primary is None or type(primary.rank) is not int or not 1 <= primary.rank <= 20 or not canonical_class_spec(primary.class_name, primary.spec_name):
        raise SourceError('not_top20')
    season = SeasonMeta.objects.filter(pk=primary.season_id, is_active=True).first()
    if season is None or not isinstance(season.rio_season, str) or not re.fullmatch(r'season-[a-z0-9-]+', season.rio_season):
        raise SourceError('wrong_season')
    identity = player_identity(primary)
    if identity is None:
        raise SourceError('unresolved')
    return season, identity


def _empty_module(identity, season):
    return {'version': 1, 'status': 'pending', 'last_attempt_at': None,
        'next_attempt_at': None, 'fetched_at': None, 'last_error': '', 'lease_token': None,
        'visible': False, 'identity_digest': activity_identity_digest(**identity),
        'rio_season': season.rio_season, 'source': {'profile_url': _profile_url(identity),
            'api_url': None, 'catalog_url': None, 'tier': None, 'character_id': None}, 'raids': []}


def _write(row, module):
    row.payload_json = {**row.payload_json, MODULE: module}
    row.save(update_fields=['payload_json'])


def refresh_raid_sources(player, transport=None, now=None):
    """Return {status, activity_id, requests}; explicit, current Top20 only.

    Ready/fresh are public validated facts (possibly raids=[]); private is a
    validated private response. Errors retain last-good and impose 30m backoff.
    Identity/privacy contradictions set a sticky visible=False until a *new*
    fully valid public response, never until an unrelated retry error.
    """
    now = now or timezone.now()
    if timezone.is_naive(now):
        raise ValueError('now must be aware')
    result = {'status': 'error', 'activity_id': None, 'requests': 0}
    try:
        season, identity = _target(player)
    except SourceError as exc:
        return {**result, 'status': exc.code}
    digest = activity_identity_digest(**identity)
    token = str(uuid.uuid4())
    try:
        with transaction.atomic():
            row, _ = RaiderIOCharacterActivity.objects.get_or_create(
                season=season, identity_digest=digest, defaults=identity)
            row = RaiderIOCharacterActivity.objects.select_for_update().get(pk=row.pk)
            result['activity_id'] = row.pk
            module = row.payload_json.get(MODULE)
            module = dict(module) if isinstance(module, dict) else _empty_module(identity, season)
            due = _date(module.get('next_attempt_at'))
            if due and due > now:
                state = module.get('status')
                result['status'] = ('busy' if state == 'fetching' else 'private' if state == 'private'
                    else 'fresh' if state == 'ready' and module.get('visible') is True else 'backoff')
                return result
            module.update(status='fetching', last_attempt_at=now.isoformat(),
                          next_attempt_at=(now + LEASE).isoformat(), lease_token=token)
            _write(row, module)
    except DatabaseError:
        return {**result, 'status': 'database_busy_or_error'}
    batch = _Transport(transport, now)
    error, source, raids, cid, private_seen = None, None, None, None, False
    try:
        if activity_identity_digest(row.region, row.realm_slug, row.character_name) != digest:
            raise SourceError('identity_mismatch', hide=True)
        ui = row.scored_runs_json.get('ui', {})
        expected_id = ui.get('characterId')
        if 'characterId' in ui:
            try:
                _positive(expected_id)
                if ui.get('season') != season.rio_season:
                    raise ValueError('season')
            except (ValueError, TypeError):
                raise SourceError('identity_mismatch', hide=True) from None
        previous_id = module.get('source', {}).get('character_id')
        if previous_id is not None:
            if expected_id is not None and expected_id != previous_id:
                raise SourceError('identity_mismatch', hide=True)
            expected_id = _positive(previous_id)
        html = batch.get(_profile_url(identity)).text
        source = discover_sources(html, identity, season.rio_season)
        catalog, selected = validate_catalog(batch.get(source['catalog_url']).json(), season)
        data = batch.get(source['api_url']).json()
        # A privacy tombstone applies even when another part of that response
        # is malformed. Such a response is an error, not a *validated* private.
        details = data.get('characterDetails') if isinstance(data, dict) else None
        private_seen = isinstance(details, dict) and details.get('isPrivate') is True
        cid, raids = validate_details(data, identity, season.rio_season, source,
                                     catalog, selected, expected_id, now)
    except SourceError as exc:
        error = exc
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        error = SourceError('invalid_payload')
    except requests.RequestException:
        error = SourceError('transport_error')
    finally:
        result['requests'] = batch.requests
        batch.session.close()
    try:
        with transaction.atomic():
            current = RaiderIOCharacterActivity.objects.select_for_update().get(pk=row.pk)
            latest = current.payload_json.get(MODULE, {})
            if latest.get('lease_token') != token or latest.get('status') != 'fetching':
                return {**result, 'status': 'superseded'}
            published = dict(latest)
            published['lease_token'] = None
            if error:
                published.update(status='error', last_error=error.code,
                    next_attempt_at=(now + (error.retry_after or BACKOFF)).isoformat())
                if error.hide or private_seen:
                    published['visible'] = False
                result['status'] = error.code
            else:
                private = raids is None
                published.update(status='private' if private else 'ready', visible=not private,
                    identity_digest=digest, rio_season=season.rio_season, version=1,
                    fetched_at=now.isoformat(), next_attempt_at=(now + TTL).isoformat(),
                    last_error='', source={**source, 'character_id': cid})
                if not private:
                    published['raids'] = raids
                result['status'] = published['status']
            _write(current, published)
    except DatabaseError:
        result['status'] = 'database_busy_or_error'
    return result
