"""Public display fallback after exact site-image mappings; no DB or network."""
import json
from functools import lru_cache
from pathlib import Path

from botend.services.site_image_assets import CDN, canonical_icon, published_image_url

INDEX = Path(__file__).resolve().parents[2] / 'static/shared/data/published-wow-icons.json'
SIZES = ('tiny', 'small', 'medium', 'large')


@lru_cache(maxsize=1)
def _index():
    try:
        doc = json.loads(INDEX.read_bytes())
        if doc['schema'] == 1 and tuple(doc['sizes']) == SIZES:
            return {name: int(mask) for mask, names in doc['groups'].items()
                    if mask in {str(n) for n in range(1, 16)} for name in names.split()}
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        pass
    return {}


def indexed_icon_url(source):
    if source.startswith(CDN + 'wow_icons_oss/'):
        source = 'https://wow.zamimg.com/images/wow/icons/' + source[len(CDN + 'wow_icons_oss/'):]
        from botend.services.site_image_assets import _published
        if mapped := _published().get(source):
            return mapped
    icon = canonical_icon(source) if isinstance(source, str) and len(source) <= 512 else None
    if icon:
        key = icon[0]
        _, size, filename = key.split('/')
        # Legacy tiny/ may contain 18px small bytes rather than a true tiny.
        if size != 'tiny' and _index().get(filename[:-4], 0) & (1 << SIZES.index(size)):
            return CDN + key
    return ''


# Compatibility name for existing template filters.
published_wow_icon_url = published_image_url
