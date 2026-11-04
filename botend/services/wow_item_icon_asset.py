"""ItemID-bound icon assets: validate bytes on import, compact evidence on reads.

These records establish a source mapping, not an OSS upload receipt. Publication
must deliver the validated bytes before exposing the mapping to consumers.
"""
import hashlib
from io import BytesIO
import json
import re
from urllib.parse import parse_qs, urlsplit

from PIL import Image


MAX_IMAGE_BYTES = 128 * 1024
ICON_SIZES = {'small': 18, 'medium': 36, 'large': 56, 'tiny': 15}


def _valid_stem(icon):
    return (isinstance(icon, str) and 0 < len(icon) <= 255
            and re.fullmatch(r'[a-z0-9_-]+', icon) is not None
            and icon != 'inv_misc_questionmark')


def _sha256(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _source_image_size(item_id, icon, provenance):
    """Require exact item request and exact asset URL, not truthy provenance."""
    if (type(item_id) is not int or item_id <= 0 or not _valid_stem(icon)
            or not isinstance(provenance, dict)
            or provenance.get('source') != 'wowhead_tooltip'):
        raise ValueError('图标来源必须绑定有效 ItemID 与规范 basename')
    tooltip_url, image_url = provenance.get('tooltip_url'), provenance.get('image_url')
    if not isinstance(tooltip_url, str) or not isinstance(image_url, str):
        raise ValueError('图标来源缺少真实 tooltip/image URL')
    try:
        tooltip = urlsplit(tooltip_url)
        image = urlsplit(image_url)
        query = parse_qs(tooltip.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ValueError('图标来源 URL 不合法') from exc
    if (tooltip.scheme != 'https' or tooltip.netloc != 'nether.wowhead.com'
            or tooltip.path != f'/tooltip/item/{item_id}' or tooltip.fragment
            or set(query) != {'dataEnv', 'locale'}
            or any(len(values) != 1 or not re.fullmatch(r'[0-9]+', values[0])
                   for values in query.values())
            or int(query['dataEnv'][0]) <= 0):
        raise ValueError('tooltip URL 与物品或显式环境不匹配')
    sizes = [size for size in ICON_SIZES
             if image.path == f'/images/wow/icons/{size}/{icon}.jpg']
    if (image.scheme != 'https' or image.netloc != 'wow.zamimg.com'
            or image.query or image.fragment or len(sizes) != 1):
        raise ValueError('图片 URL 与图标 basename/尺寸不匹配')
    return ICON_SIZES[sizes[0]]


def verified_item_icon_asset(item):
    """Return only a recognized, fully bound central record; legacy facts stay raw."""
    metadata = getattr(item, 'metadata', None)
    facts = metadata.get('display_facts') if isinstance(metadata, dict) else None
    asset = facts.get('icon') if isinstance(facts, dict) else None
    if not isinstance(asset, dict):
        return None
    item_id = getattr(item, 'item_id', None)
    if (type(item_id) is not int or item_id <= 0
            or type(asset.get('schema_version')) is not int or asset['schema_version'] != 1
            or type(asset.get('item_id')) is not int
            or asset['item_id'] != item_id
            or asset.get('format') != 'JPEG' or not _sha256(asset.get('sha256'))
            or type(asset.get('byte_length')) is not int
            or not 0 < asset['byte_length'] <= MAX_IMAGE_BYTES):
        return None
    try:
        size = _source_image_size(asset['item_id'], asset.get('value'), asset.get('provenance'))
    except ValueError:
        return None
    if (not _sha256(asset['provenance'].get('tooltip_sha256'))
            or any(type(asset.get(key)) is not int or asset[key] != size
                   for key in ('width', 'height'))):
        return None
    return asset


def validated_item_icon_asset(item_id, *, icon, image_bytes, provenance):
    """Validate a frozen Wowhead response and actual JPEG, without network or ORM."""
    size = _source_image_size(item_id, icon, provenance)
    response = provenance.get('tooltip_response')
    if (type(provenance.get('item_id')) is not int or provenance['item_id'] != item_id
            or not isinstance(response, dict) or response.get('icon') != icon
            or not isinstance(response.get('name'), str) or not response['name'].strip()):
        raise ValueError('tooltip 响应与声明 ItemID/icon 不匹配')
    try:
        response_bytes = json.dumps(response, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise ValueError('tooltip 响应必须是有效 JSON') from exc
    if len(response_bytes) > 256 * 1024:
        raise ValueError('tooltip 响应超过大小限制')
    if (not isinstance(image_bytes, bytes) or not 0 < len(image_bytes) <= MAX_IMAGE_BYTES
            or not image_bytes.startswith(b'\xff\xd8\xff')):
        raise ValueError('图标必须是大小受限的真实 JPEG bytes')
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            if image.format != 'JPEG' or image.size != (size, size):
                raise ValueError('图标 JPEG 格式或尺寸与来源不匹配')
            image.verify()
        with Image.open(BytesIO(image_bytes)) as image:
            image.load()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError('图标 JPEG 解码验证失败') from exc
    return {
        'schema_version': 1, 'item_id': item_id, 'value': icon,
        'format': 'JPEG', 'sha256': hashlib.sha256(image_bytes).hexdigest(),
        'width': size, 'height': size, 'byte_length': len(image_bytes),
        'provenance': {
            'source': provenance['source'], 'tooltip_url': provenance['tooltip_url'],
            'tooltip_sha256': hashlib.sha256(response_bytes).hexdigest(),
            'image_url': provenance['image_url'],
        },
    }
