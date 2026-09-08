"""独立作者卡片的来源提取、自定义校验及显示资料。"""
import re
from urllib.parse import urlparse


def normalize_author_profile(value):
    if not isinstance(value, dict):
        raise ValueError('作者资料必须是对象')
    result = {}
    for key, limit in [('name', 200), ('title', 200), ('bio', 5000), ('avatar', 1000)]:
        field = value.get(key, '')
        if not isinstance(field, str) or len(field.strip()) > limit:
            raise ValueError(f'作者字段 {key} 格式错误或超过 {limit} 字符')
        result[key] = field.strip()
    def valid_url(url):
        parsed = urlparse(url)
        return parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
    if result['avatar'] and not valid_url(result['avatar']):
        raise ValueError('作者头像必须是 HTTPS 地址')
    links = value.get('links', [])
    if not isinstance(links, list) or len(links) > 8:
        raise ValueError('作者链接最多 8 项')
    result['links'] = []
    for row in links:
        if not isinstance(row, dict):
            raise ValueError('作者链接格式错误')
        label, url = row.get('label', ''), row.get('url', '')
        if not isinstance(label, str) or not isinstance(url, str) or not 1 <= len(label.strip()) <= 80 or len(url) > 1000 or not valid_url(url):
            raise ValueError('作者链接需填写名称及有效 HTTPS 地址')
        result['links'].append({'label': label.strip(), 'url': url.strip()})
    return result


def extract_author_profile(soup, author):
    profile = {'name': author.get('name', ''), 'avatar': '', 'title': '', 'bio': '', 'links': []}
    widget = soup.select_one('[class*="_Author_" ]')
    if widget:
        for selector, key in [('_Author__nickname_', 'name'), ('_Author__title_', 'title'), ('_Author__description_', 'bio')]:
            node = widget.select_one(f'[class*="{selector}"]')
            if node and node.get_text(strip=True):
                profile[key] = node.get_text(' ', strip=True)
        avatar = widget.select_one('[class*="_Author__image_"]')
        if avatar:
            match = re.search(r'url\([\s\"\x27]*(https://[^)\"\x27\s]+)', avatar.get('style', ''))
            profile['avatar'] = avatar.get('src', '') or (match[1] if match else '')
        for link in widget.select('a[href]'):
            label = link.get('aria-label') or link.get('title') or link.get_text(' ', strip=True)
            if label and link['href'].startswith('https://'):
                profile['links'].append({'label': label[:80], 'url': link['href']})
    return normalize_author_profile(profile)


def author_profile_for(guide):
    profile = normalize_author_profile(guide.author_profile if guide.author_profile is not None else guide.source_author_profile or {'name': guide.author})
    profile['name'] = profile['name'] or guide.author or '站内编辑'
    return profile
