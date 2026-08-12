"""Wowhead 服务端渲染 Tooltip 的抓取与文本解析。"""

from __future__ import annotations

import html
import re
import time

import requests

from botend.services.article_image_service import _get_configured_proxies


def description_from_tooltip_html(tooltip_html):
    """提取 Tooltip 中面向玩家的说明正文，保留换行和已渲染数值。"""

    description_match = re.search(
        r'<div\s+class="q">(.*?)</div>',
        str(tooltip_html or ''),
        re.IGNORECASE | re.DOTALL,
    )
    if not description_match:
        return ''
    text = re.sub(
        r'<br\s*/?>',
        '\n',
        description_match.group(1),
        flags=re.IGNORECASE,
    )
    text = re.sub(r'<!--[\s\S]*?-->', '', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text).strip()
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'(?<=\d)\$', '%', text)


def fetch_wowhead_tooltip(
    spell_id,
    *,
    locale,
    data_env=1,
    difficulty_id=8,
    delay=0,
):
    """抓取单个技能的名称、图标 slug 与说明，失败时返回 None。"""

    if delay:
        time.sleep(delay)
    url = f'https://nether.wowhead.com/tooltip/spell/{int(spell_id)}'
    configured_proxies = _get_configured_proxies()
    environment_proxies = requests.utils.get_environ_proxies(url)
    if environment_proxies:
        proxy_candidates = [None, configured_proxies]
    else:
        proxy_candidates = [configured_proxies, None]
    proxy_candidates = [
        candidate
        for index, candidate in enumerate(proxy_candidates)
        if candidate is None or candidate not in proxy_candidates[:index]
    ]
    for attempt in range(3):
        for proxies in proxy_candidates:
            try:
                response = requests.get(
                    url,
                    timeout=45,
                    params={
                        'dataEnv': int(data_env),
                        'locale': int(locale),
                        'dd': int(difficulty_id),
                    },
                    headers={
                        'User-Agent': 'Mozilla/5.0',
                        'Accept-Language': (
                            'zh-CN,zh;q=0.9,en;q=0.8'
                            if int(locale) == 4
                            else 'en-US,en;q=0.9'
                        ),
                    },
                    proxies=proxies,
                )
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                payload = response.json()
                tooltip_html = str(payload.get('tooltip') or '')
                return {
                    'name': str(payload.get('name') or '').strip(),
                    'icon_name': str(payload.get('icon') or '').strip(),
                    'description': description_from_tooltip_html(tooltip_html),
                }
            except Exception:
                continue
        if attempt < 2:
            time.sleep(2 ** attempt)
    return None
