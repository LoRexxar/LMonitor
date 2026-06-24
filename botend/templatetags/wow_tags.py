import os
from urllib.parse import quote
from django import template
from django.conf import settings
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
    return icon_name.strip()


def _wow_icon_oss_base_url():
    base_url = str(getattr(settings, 'WOW_ICON_OSS_BASE_URL', '') or '').strip()
    if base_url:
        return base_url.rstrip('/')

    oss_config = getattr(settings, 'OSS_CONFIG', {}) or {}
    base_url = str(oss_config.get('wow_icon_base_url') or '').strip()
    if base_url:
        return base_url.rstrip('/')

    oss_base_url = str(oss_config.get('base_url') or '').strip()
    if not oss_base_url:
        return ''
    prefix = str(
        getattr(settings, 'WOW_ICON_OSS_PREFIX', '')
        or oss_config.get('wow_icon_prefix')
        or 'wow_icons_oss'
    ).strip().strip('/')
    if not prefix:
        return oss_base_url.rstrip('/')
    return f"{oss_base_url.rstrip('/')}/{prefix}"


def _build_wow_icon_oss_url(base_url, size, icon_name):
    encoded_icon = quote(icon_name, safe='')
    encoded_size = quote(str(size or 'small'), safe='')
    return f'{base_url}/{encoded_size}/{encoded_icon}.jpg'


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
    """返回 WoW 图标 URL。

    生产环境优先使用 OSS 图标前缀；没有 OSS 配置时保留本地 static
    回退，方便开发环境或临时离线验证。
    """
    icon_name = _normalize_icon_name(icon_name)
    if not icon_name:
        icon_name = 'inv_misc_questionmark'

    candidate_sizes = []
    for candidate in (size, 'small'):
        candidate = str(candidate or '').strip() or 'small'
        if candidate not in candidate_sizes:
            candidate_sizes.append(candidate)

    oss_base_url = _wow_icon_oss_base_url()
    if oss_base_url:
        return _build_wow_icon_oss_url(oss_base_url, candidate_sizes[0], icon_name)

    for candidate_size in candidate_sizes:
        local_rel = os.path.join('wow_icons', candidate_size, f'{icon_name}.jpg')
        full_path = os.path.join(settings.BASE_DIR, 'static', local_rel)
        if os.path.exists(full_path):
            return f"/static/{local_rel.replace(os.sep, '/')}"

    placeholder_rel = os.path.join('wow_icons', 'small', 'inv_misc_questionmark.jpg')
    placeholder_path = os.path.join(settings.BASE_DIR, 'static', placeholder_rel)
    if os.path.exists(placeholder_path):
        return f"/static/{placeholder_rel.replace(os.sep, '/')}"

    return ''
