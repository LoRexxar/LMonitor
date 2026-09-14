"""装备只读中央目录；技能按需补充 Wowhead 详情。"""
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

from botend.services.wow_item_display import load_item_tooltip_metadata

FETCH_SLOTS = BoundedSemaphore(4)


def cache_key(kind, entry_id, difficulty, build=''):
    # dd 仅控制技能难度；物品默认变体必须标明参考装等，不能冒充所选难度。
    return f'journal-tooltip:v3:{build}:{kind}:{entry_id}:{difficulty if kind == "spell" else 0}:zhCN'


def _local_item_tooltip(entry_id, build):
    if not build:
        return None
    item = load_item_tooltip_metadata([{
        'item_id': entry_id,
        'game_build': build,
        'allow_default_variant': True,
        'default_variant_order': 'lowest',
        'require_complete_variant': True,
    }])[0]
    if not item['variant_id'] or not item['tooltip_complete']:
        return None
    is_ptr = bool(item['variant_metadata'].get('ptr_preview'))
    item_level = item['item_level']
    return {
        'name': item['display_name'],
        'lines': [item['display_name'], f'物品等级 {item_level}', *item['stat_lines'], *item['effects']],
        'source': 'LMonitor PTR DB2 + SimulationCraft' if is_ptr else 'LMonitor 装备目录',
        'url': '',
        'icon': item['icon_url'],
        'note': (
            f'数值为 PTR {build} 的参考装等 {item_level} 快照，不代表所选难度的初始掉落装等。'
            if is_ptr else
            f'数值为正式服 {build} 当前装备目录的参考装等 {item_level} 快照，不代表所选难度的初始掉落装等。'
        ),
        'item_level': item_level,
        'stats': item['stat_lines'],
        'effects': item['effects'],
        'complete': True,
    }


def cached_tooltip(kind, entry_id, difficulty, build=''):
    if kind == 'item':
        return _local_item_tooltip(entry_id, build)
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
    if kind == 'item':
        raise ValueError(f'中央装备目录缺少 {build or "当前构建"} 的物品 {int(entry_id)} 完整变体')
    if cache.get(key + ':unavailable'):
        raise ValueError('补充资料暂不可用，请稍后重试')
    try:
        with FETCH_SLOTS, requests.Session() as session:
            # Wowhead 使用环境代理；Wago 的直连策略不适用于此来源。
            session.mount('https://', HTTPAdapter(max_retries=Retry(
                total=2, backoff_factor=.4, status_forcelist=[429, 500, 502, 503, 504])))
            params = {'locale': 'zhcn', 'dataEnv': 1, 'dd': int(difficulty)}
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
              'note': '当前难度的 Wowhead 技能资料。'}
    cache.set(key, result, 86400)
    path = cache_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    except OSError:
        pass
    return result
