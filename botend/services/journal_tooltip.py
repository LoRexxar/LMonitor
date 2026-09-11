"""按需补充 Wowhead 物品与技能详情，输出纯文本并缓存结果。"""
import re
import json
import time
from pathlib import Path
from threading import BoundedSemaphore

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.conf import settings

from botend.services.gear_builder_catalog_source import _tooltip_details
from botend.services.wow_item_display import STAT_LABELS

FETCH_SLOTS = BoundedSemaphore(4)


def cache_key(kind, entry_id, difficulty, build=''):
    # dd 仅控制技能难度；物品默认变体必须标明参考装等，不能冒充所选难度。
    return f'journal-tooltip:v3:{build}:{kind}:{entry_id}:{difficulty if kind == "spell" else 0}:zhCN'


def cached_tooltip(kind, entry_id, difficulty, build=''):
    key = cache_key(kind, entry_id, difficulty, build)
    result = cache.get(key)
    if result:
        return result
    path = cache_path(key)
    try:
        if time.time() - path.stat().st_mtime < 86400:
            result = json.loads(path.read_text(encoding='utf-8'))
            cache.set(key, result, 3600)
            return result
    except (OSError, ValueError):
        pass
    return None


def cache_path(key):
    import hashlib
    return Path(settings.BASE_DIR) / '.cache' / 'adventure-journal-tooltips' / (hashlib.sha256(key.encode()).hexdigest() + '.json')


def tooltip(kind, entry_id, difficulty, build=''):
    key = cache_key(kind, entry_id, difficulty, build)
    cached = cached_tooltip(kind, entry_id, difficulty, build)
    if cached:
        return cached
    if cache.get(key + ':unavailable'):
        raise ValueError('补充资料暂不可用，请稍后重试')
    try:
        with FETCH_SLOTS, requests.Session() as session:
            # Wowhead 使用环境代理；Wago 的直连策略不适用于此来源。
            session.mount('https://', HTTPAdapter(max_retries=Retry(
                total=2, backoff_factor=.4, status_forcelist=[429, 500, 502, 503, 504])))
            params = {'locale': 'zhcn', 'dataEnv': 1}
            if kind == 'spell':
                params['dd'] = int(difficulty)
            response = session.get(f'https://nether.wowhead.com/tooltip/{kind}/{int(entry_id)}',
                                   params=params, timeout=(5, 12))
            response.raise_for_status()
            payload = response.json()
    except (requests.RequestException, ValueError):
        cache.set(key + ':unavailable', True, 15)
        raise
    soup = BeautifulSoup(payload.get('tooltip', ''), 'html.parser')
    for element in soup(['script', 'style', 'iframe']):
        element.decompose()
    lines = [line.strip().replace('\x08', '').replace('\u200b', '') for line in soup.get_text('\n').splitlines() if line.strip()]
    if not payload.get('name') or not lines:
        raise ValueError('来源暂未提供物品或技能详情')
    icon = str(payload.get('icon') or '')
    result = {'name': payload['name'], 'lines': lines, 'source': 'Wowhead',
              'url': f'https://www.wowhead.com/cn/{kind}={int(entry_id)}',
              'icon': f'https://wow.zamimg.com/images/wow/icons/large/{icon}.jpg' if re.fullmatch(r'[a-zA-Z0-9_-]+', icon) else '',
              'note': '数值对应下方参考装等，不代表所选难度的初始掉落装等。' if kind == 'item' else '当前难度的 Wowhead 技能资料。'}
    if kind == 'item':
        details = _tooltip_details(payload)
        level = re.search(r'<!--ilvl-->(\d+)', payload.get('tooltip', ''))
        stats = [f'+{value:,} {STAT_LABELS.get(stat, stat)}' for stat, value in details['stats'].items()]
        primary = details['primary_options']
        if primary:
            # 不指定专精时，保留可切换的全部主属性。
            grouped = {}
            for stat, value in primary.items():
                grouped.setdefault(value, []).append(STAT_LABELS[stat])
            stats = [f'+{value:,} {"／".join(names)}' for value, names in grouped.items()] + stats
        result.update(item_level=int(level[1]) if level else None, stats=stats,
                      effects=[row.get('description_zh') or row.get('description', '') for row in details['effects']],
                      complete=True)
    cache.set(key, result, 86400)
    path = cache_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass
    return result
