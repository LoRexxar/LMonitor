"""Read-only projection of the manifest shipped with this release. No HTTP or DB."""
import json
import re
from functools import lru_cache
from pathlib import Path

CDN = 'https://oss.wowdaily.cn/'
MANIFEST = Path(__file__).with_suffix('.json')
PREFIX = 'site-images/v1/'
SLUG = r'[A-Za-z0-9_-]{1,160}'
RASTER = r'\.(?:jpg|jpeg|png|webp|gif)'
# Full URL grammars, not suffix/subdomain/path-prefix trust. Only the historical
# Bilibili public covers and our legacy editorial mirror accept http aliases (never fetched).
_ICON = re.compile(r'https://wow\.zamimg\.com/images/wow/icons/(tiny|small|medium|large)/(' + SLUG + r')\.jpg')
_LARGE = re.compile(r'https://(?:static\.icy-veins\.com/images/wow/large_icons|render\.worldofwarcraft\.com/us/icons/56)/(' + SLUG + r')\.jpg')
_BILI = re.compile(r'https?://i[012]\.hdslb\.com/bfs/archive/[0-9a-f]{40}' + RASTER)
_SOURCES = (_ICON, _LARGE, _BILI, *(re.compile(x) for x in (
    # Official CN news CMS objects and our legacy WoWHead editorial mirror only.
    r'https://wow\.res\.netease\.com/images/20[0-9]{6}/[0-9]{13}_[0-9a-f]{10}' + RASTER,
    r'https://nie\.res\.netease\.com/r/pic/20[0-9]{6}/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}' + RASTER,
    r'https?://oss\.shengnong\.club/portal/articles/wowhead/[a-z0-9-]{1,160}-[0-9]{6}/[0-9a-f]{16}' + RASTER,
    r'https://assets-bwa\.worldofwarcraft\.blizzard\.com/static/wow-icon-32x32\.[0-9a-f]{40}\.png',
    # YouTube IDs explicitly observed in public Blizzard tracker editorial embeds, not arbitrary channel media.
    r'https://img\.youtube\.com/vi/(?:MXj5oa878G4|uQuKE0KDgiM|xXaXYxcbA6A)/maxresdefault\.jpg',
    r'https://wow\.lorexxar\.cn/favicon\.ico',
    r'https://assets-ng\.maxroll\.gg/wordpress/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*' + RASTER,
    r'https://bnetcmsus-a\.akamaihd\.net/cms/content_entry_media/[A-Z0-9]{8,64}' + RASTER,
    r'https://static\.icy-veins\.com/wp/static/icydb/wow/icons/[0-9]{1,10}\.webp',
    r'https://wow\.zamimg\.com/images/wow/icons/tiny/' + SLUG + r'\.gif',
    r'https://wow\.zamimg\.com/uploads/screenshots/(?:normal|large|small)/[0-9]{1,12}(?:@2x)?' + RASTER,
    r'https://static\.icy-veins\.com/images/wow/large_icons/' + SLUG + RASTER,
    r'https://bnetcmsus-a\.akamaihd\.net/cms/blog_header/[a-z0-9]{2}/[A-Za-z0-9]{8,64}' + RASTER,
    r'https://static\.icy-veins\.com/images/(?:common/button-icons|wow/(?:tools(?:/bis|/hero-talents)?|classes/'
    r'(?:dk|dh|druid|evoker|hunter|mage|monk|paladin|priest|rogue|shaman|warlock|warrior)(?:/icons|/restoration)?))/' + SLUG + RASTER,
)))


def source_url(url):
    if not isinstance(url, str) or len(url) > 512 or not any(p.fullmatch(url) for p in _SOURCES):
        return None
    return 'https:' + url[5:] if url.startswith('http:') else url


def canonical_icon(url):
    m = _ICON.fullmatch(url)
    if m:
        size, name = m.groups()
    else:
        m = _LARGE.fullmatch(url)
        if not m:
            return None
        size, name = 'large', m[1]
    return f'wow_icons_oss/{size}/{name}.jpg', {'tiny': 15, 'small': 18, 'medium': 36, 'large': 56}[size]


def read_records(path):
    doc = json.loads(path.read_text(encoding='utf-8'))
    if doc['schema'] != 1 or not isinstance(doc['images'], dict):
        raise ValueError('bad site image manifest')
    for url, pair in doc['images'].items():
        if not source_url(url) or not isinstance(pair, list) or len(pair) != 2:
            raise ValueError('invalid manifest entry')
        key, digest = pair
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest) or not isinstance(key, str):
            raise ValueError('invalid manifest identity')
        if not re.fullmatch(re.escape(PREFIX + digest) + r'\.(?:jpg|png|gif|webp|ico)', key):
            icon = canonical_icon(url)
            if not icon or key != icon[0]:
                raise ValueError('invalid manifest destination')
    return doc['images']


@lru_cache(maxsize=1)
def _published():
    try:
        return {u: CDN + v[0] for u, v in read_records(MANIFEST).items()}
    except (OSError, ValueError, TypeError, KeyError):
        return {}  # Never break original-render fallback on an absent/invalid release file.


def published_image_url(url):
    """Exact hit or unchanged input; this helper is not an HTML URL sanitizer."""
    return _published().get(url, url) if isinstance(url, str) else url


def published_image_map():
    """Caller may serialize a copy without modifying the process cache."""
    return _published().copy()
