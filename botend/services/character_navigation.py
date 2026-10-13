"""One network-free character navigation policy, evaluated on click.

Snapshot renderers only build links: no per-member queries or stale persisted
Top20 decisions. The resolver reads current central rank/realm facts.
"""
from urllib.parse import quote, unquote, urlencode, urlsplit

from django.urls import reverse

from botend.constants.wow import canonical_class_spec
from botend.models import PlayerSpecTopPlayer, SeasonMeta, WowRealmDirectory
from botend.raiderio_identity import normalized_name
from botend.services.player_external_links import REGION_LOCALES

PLAYER_FIELDS = ('id', 'season_id', 'region', 'realm', 'character_name',
                 'profile_url', 'class_name', 'spec_name', 'rank')


def _segment(value, limit=128):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and value == value.strip() and value not in ('.', '..')
            and not any(c in value for c in ('/', '\\'))
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _identity(region, realm, name):
    if region not in REGION_LOCALES or not _segment(realm) or not _segment(name):
        return None
    return region, realm, name


def _url_identity(value):
    if not isinstance(value, str) or len(value) > 2000:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    parts = parsed.path.strip('/').split('/')
    if (parsed.scheme != 'https' or parsed.netloc not in ('raider.io', 'www.raider.io')
            or parsed.query or parsed.fragment or len(parts) != 4 or parts[0] != 'characters'):
        return None
    return _identity(*[unquote(part) for part in parts[1:]])


def character_link(*, url='', region='', realm='', name='', player_id=None):
    """No DB calls: safe links for both existing and newly published snapshots."""
    if player_id is not None:
        if not str(player_id).isascii() or not str(player_id).isdigit() or int(player_id) <= 0:
            return ''
        params = {'player_id': player_id}
    elif url:
        identity = _url_identity(url)
        if not identity:
            return ''
        params = dict(zip(('region', 'realm', 'name'), identity))
    else:
        if not _identity(region, realm, name):
            return ''
        params = {'region': region, 'realm': realm, 'name': name}
    return reverse('portal_character_redirect') + '?' + urlencode(params)


def character_destination(params):
    """Resolve current Top20 across specs, otherwise the exact IO profile."""
    selected = None
    if params.get('player_id'):
        key = str(params['player_id'])
        if not key.isascii() or not key.isdigit() or len(key) > 18:
            return None
        selected = PlayerSpecTopPlayer.objects.filter(pk=int(key)).values(*PLAYER_FIELDS).first()
        if not selected:
            return None
        identity = (_url_identity(selected['profile_url']) if selected['profile_url'] else
                    _identity(selected['region'], selected['realm'], selected['character_name']))
        if (not identity or identity[0] != selected['region']
                or normalized_name(identity[2]) != normalized_name(selected['character_name'])):
            return None
    elif params.get('url'):
        identity = _url_identity(params['url'])
    else:
        identity = _identity(params.get('region'), params.get('realm'), params.get('name'))
    if not identity:
        return None
    region, realm, name = identity
    # A single regional directory query, reused for every candidate; no N+1.
    aliases = {}
    for item in WowRealmDirectory.objects.filter(region=region).values('slug', 'name', 'localized_name'):
        for alias in (item['slug'], item['name'], item['localized_name']):
            if alias:
                aliases.setdefault(normalized_name(alias), set()).add(item['slug'])

    def canonical_realm(value):
        matches = aliases.get(normalized_name(value), set())
        if len(matches) > 1:
            return None
        return next(iter(matches)) if matches else value.lower()

    realm = canonical_realm(realm)
    if not realm:
        return None
    external = f'https://raider.io/characters/{region}/{quote(realm, safe="")}/{quote(name, safe="")}'
    season_id = SeasonMeta.objects.filter(is_active=True).order_by('pk').values_list('pk', flat=True).first()
    if not season_id or realm == 'anonymous':
        return external
    # Rank scope is bounded by the existing per-spec Top20 contract. Compare
    # Unicode identities in Python, not MySQL's accent-insensitive collation.
    matches = []
    for player in PlayerSpecTopPlayer.objects.filter(season_id=season_id, region=region,
            rank__gte=1, rank__lte=20).values(*PLAYER_FIELDS):
        if normalized_name(player['character_name']) != normalized_name(name):
            continue
        scope = canonical_class_spec(player['class_name'], player['spec_name'])
        candidate = (_url_identity(player['profile_url']) if player['profile_url'] else
                     _identity(player['region'], player['realm'], player['character_name']))
        if (scope and candidate and candidate[0] == region
                and normalized_name(candidate[2]) == normalized_name(name)
                and canonical_realm(candidate[1]) == realm):
            matches.append((player, scope))
    if not matches:
        return external
    player, scope = min(matches, key=lambda row: (
        row[0]['id'] != (selected or {}).get('id'), row[0]['rank'], row[0]['id']))
    return reverse('spec_detail_player_detail', kwargs={
        'class_name': scope[0], 'spec_name': scope[1], 'player_id': player['id']})
