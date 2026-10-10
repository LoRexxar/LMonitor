"""Character links projected from the shared Raider.IO realm directory.

No network calls on page reads. Regional routes mirror Raider.IO's character
buttons; WCL uses localized realm slugs for ru_RU/zh_CN/zh_TW/ko_KR.
"""
import json
import re
from urllib.parse import quote, unquote, urlencode, urlsplit

from django.db.models import Q
from botend.models import WowRealmDirectory

REGION_LOCALES = {'us': 'en-us', 'eu': 'en-gb', 'kr': 'ko-kr', 'tw': 'zh-tw', 'cn': 'zh-cn'}
LOCALIZED_WCL = {'ru_RU', 'zh_CN', 'zh_TW', 'ko_KR'}


def parse_realm_directory(html, region):
    """Validate one complete regional /realms page before any database writes."""
    match = re.search(r'window\.__RIO_INITIAL_DATA\s*=\s*("(?:\\.|[^"\\])*")', html)
    if not match or region not in REGION_LOCALES:
        raise ValueError('Missing Raider.IO realm directory')
    listing = json.loads(json.loads(match.group(1))).get('realmListing') or {}
    if (listing.get('region') or {}).get('slug') != region or listing.get('isLoading'):
        raise ValueError('Incomplete or wrong-region realm directory')
    groups = listing.get('realms')
    if not isinstance(groups, list) or not groups:
        raise ValueError('Empty realm directory')
    rows, seen = [], set()
    for group in groups:
        if (group.get('region') or {}).get('slug') != region or not group.get('connectedRealms'):
            raise ValueError('Invalid realm group')
        for realm in group['connectedRealms']:
            slug, name, locale = (realm.get(k) for k in ('slug', 'name', 'locale'))
            localized = realm.get('alt_name') or ''
            if not all(isinstance(v, str) and v for v in (slug, name, locale)) or slug in seen:
                raise ValueError('Missing or duplicate realm identity')
            if locale in LOCALIZED_WCL and not localized:
                raise ValueError('Missing localized realm identity')
            seen.add(slug)
            rows.append({'region': region, 'slug': slug, 'name': name, 'localized_name': localized,
                         'locale': locale, 'source_url': f'https://raider.io/realms/{region}'})
    return rows


def _localized_slug(name):
    # Raider.IO realm-name slug rules; preserve non-Latin characters.
    value = re.sub(r"[!@#$%^&*()~`_+=\[\]{};:\".,<>/?'\-•]", '', name)
    return re.sub(r'\s+', '-', value).lower()


def build_player_external_links(player):
    region = str(player.region or '').lower()
    name = str(player.character_name or '').strip()
    if region not in REGION_LOCALES or not name:
        return []
    profile_url = str(player.profile_url or '').strip()
    realm = None
    slug = ''
    if profile_url:
        try:
            url = urlsplit(profile_url)
            parts = url.path.strip('/').split('/')
            if (url.scheme != 'https' or url.netloc not in ('raider.io', 'www.raider.io')
                    or len(parts) != 4 or parts[0] != 'characters' or parts[1] != region
                    or unquote(parts[3]).casefold() != name.casefold()):
                return []
            slug = unquote(parts[2])
            if not slug or '/' in slug or '\\' in slug or slug in ('.', '..'):
                return []
        except ValueError:
            return []
        realm = WowRealmDirectory.objects.filter(region=region, slug=slug).first()
    else:
        candidates = list(WowRealmDirectory.objects.filter(region=region).filter(
            Q(name__iexact=player.realm) | Q(localized_name__iexact=player.realm) | Q(slug__iexact=player.realm)
        )[:2]) if player.realm else []
        if len(candidates) != 1:
            return []
        realm = candidates[0]
        slug = realm.slug
    encoded_name, encoded_slug = quote(name, safe=''), quote(slug, safe='')
    links = [{'key': 'raiderio', 'label': 'Raider.IO',
              'url': f'https://raider.io/characters/{region}/{encoded_slug}/{encoded_name}'}]
    if not realm or slug.lower() == 'anonymous':
        return links
    armory = (f'https://wow.blizzard.cn/character/#/{encoded_slug}/{encoded_name}' if region == 'cn'
              else f'https://worldofwarcraft.com/{REGION_LOCALES[region]}/character/{region}/{encoded_slug}/{encoded_name}')
    links.append({'key': 'armory', 'label': '暴雪英雄榜', 'url': armory})
    localized_slug = _localized_slug(realm.localized_name) if realm.localized_name else ''
    if realm.locale not in LOCALIZED_WCL or localized_slug:
        wcl_slug = localized_slug.replace('й', 'и') if realm.locale in LOCALIZED_WCL else slug
        links.append({'key': 'warcraftlogs', 'label': 'WCL',
                      'url': f'https://www.warcraftlogs.com/character/{region}/{quote(wcl_slug, safe="")}/{encoded_name}'})
    raidbots_slug = localized_slug if realm.locale in ('zh_CN', 'zh_TW', 'ko_KR') else slug
    if raidbots_slug:
        links.append({'key': 'raidbots', 'label': 'Raidbots', 'url': 'https://www.raidbots.com/simbot/quick?' + urlencode(
            {'region': region, 'realm': raidbots_slug, 'name': name})})
    return links
