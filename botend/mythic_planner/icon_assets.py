import re


WOWHEAD_ICON_BASE_URL = 'https://wow.zamimg.com/images/wow/icons/large'


def normalize_wowhead_icon_slug(value):
    """把客户端图标文件名转换为 Wowhead CDN 使用的 slug。"""
    return re.sub(r'\s+', '-', str(value or '').strip().lower())


def build_wowhead_icon_url(value):
    slug = normalize_wowhead_icon_slug(value)
    if not slug:
        return ''
    return f'{WOWHEAD_ICON_BASE_URL}/{slug}.jpg'
