import re
from django import template
from botend.services.site_image_assets import published_image_url
from django.utils.html import strip_tags

register = template.Library()


_DESCRIPTION_LINE_CLASSES = (
    ('equip', ('装备：', 'Equip:', '套装：', 'Set:')),
    ('use', ('使用：', 'Use:')),
    ('requirement', ('需要等级', '需要职业', '需要', 'Requires', 'Classes:')),
    ('set', ('职业：', '耐久:', '耐久：', '套装：', 'Set:', '(', '（')),
    ('sell', ('售价：', 'Sell Price:')),
)

_WOWHEAD_SEO_MARKERS = (
    '添加于 [World of Warcraft',
    'Always up to date with the latest patch',
    '始终保持更新',
    '[In the ',
    '物品放置于',
    '这件0等级',
    '这是295级',
)


def _normalize_icon_name(icon_name):
    icon_name = str(icon_name or '').strip()
    if not icon_name:
        return ''
    icon_name = icon_name.split('?', 1)[0].strip()
    icon_name = icon_name.rsplit('/', 1)[-1].strip()
    while '.' in icon_name:
        base, ext = icon_name.rsplit('.', 1)
        if ext.lower() in {'jpg', 'jpeg', 'png', 'gif', 'webp'}:
            icon_name = base
            continue
        break
    icon_name = ''.join(icon_name.strip().split())
    return icon_name


def named_icon_source(icon_name, size='small'):
    """For named icons only. In particular a Maxroll FileDataID URL is not a slug."""
    match = re.fullmatch(r'([A-Za-z0-9_-]{1,160})(?:\.(?:jpg|blp|png))?', str(icon_name or '').strip(), re.I)
    if not match or match[1].isdigit() or size not in ('small', 'medium', 'large', 'tiny'):
        return ''
    return f'https://wow.zamimg.com/images/wow/icons/{size}/{match[1].lower()}.jpg'


def wow_icon_oss_url(icon_name, size='small'):
    return published_image_url(named_icon_source(icon_name, size) or icon_name)


@register.filter
def wow_item_description_lines(description):
    """Split a WoW item tooltip description into display-friendly lines."""
    text = strip_tags(str(description or '')).replace('\r\n', '\n').replace('\r', '\n')
    if any(marker in text for marker in _WOWHEAD_SEO_MARKERS):
        return []
    text = text.replace('职业：', '\n职业：')
    text = text.replace('耐久:', '\n耐久:')
    text = text.replace('耐久：', '\n耐久：')
    text = text.replace('套装：', '\n套装：')
    text = text.replace('(2)', '\n(2)')
    text = text.replace('(4)', '\n(4)')
    text = text.replace('（2）', '\n（2）')
    text = text.replace('（4）', '\n（4）')
    lines = []
    for raw_line in text.split('\n'):
        line = ' '.join(raw_line.strip().split())
        if not line:
            continue
        line_class = 'plain'
        for candidate_class, prefixes in _DESCRIPTION_LINE_CLASSES:
            if any(line.startswith(prefix) for prefix in prefixes):
                line_class = candidate_class
                break
        lines.append({'text': line, 'class': line_class})
    return lines


@register.simple_tag
def wow_icon(icon_name, size='small'):
    return wow_icon_oss_url(icon_name, size)


@register.inclusion_tag('portal/spec_detail/_equipment_statistics.html')
def equipment_statistics(detail):
    """部位顺序独立于 JSON 键序；旧快照仍可展示已发布的单件统计。"""
    from botend.services.spec_stats_service import equipment_slot_sort_key

    gear = detail.get('gear_popularity')
    if not isinstance(gear, dict):
        gear = {}
    slots = [(slot, gear[slot]) for slot in sorted(gear, key=equipment_slot_sort_key) if slot != '饰品']
    trinkets = detail.get('trinket_popularity') or {}
    if not trinkets:
        return {'slots': slots, 'legacy': True, 'sample_size': detail.get('sample_size'),
                'singles': gear.get('饰品', [])}
    items = {item['itemID']: item for item in trinkets['singles']}
    pairs = [dict(pair, items=[items[item_id] for item_id in pair['item_ids']])
             for pair in trinkets['pairs']]
    return {**trinkets, 'slots': slots, 'pairs': pairs}
