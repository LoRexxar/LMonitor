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

from botend.models import WowItemVariantSnapshot
from botend.services.gear_builder import active_season
from botend.services.gear_builder_catalog_source import _tooltip_details
from botend.services.ptr_journal_gear_overlay import exact_build_variant_is_complete
from botend.services.wow_item_display import STAT_LABELS

FETCH_SLOTS = BoundedSemaphore(4)


def cache_key(kind, entry_id, difficulty, build=''):
    # dd 仅控制技能难度；物品默认变体必须标明参考装等，不能冒充所选难度。
    return f'journal-tooltip:v3:{build}:{kind}:{entry_id}:{difficulty if kind == "spell" else 0}:zhCN'


def _format_number(value):
    parsed = float(value)
    if parsed.is_integer():
        return f'{int(parsed):,}'
    return f'{parsed:,.2f}'.rstrip('0').rstrip('.')


def _local_item_tooltip(entry_id, build):
    if not build:
        return None
    season = active_season()
    if season is None or not season.gear_batch_key:
        return None
    combined_labels = {
        'stragiint': '力量／敏捷／智力',
        'agiint': '敏捷／智力',
        'stragi': '力量／敏捷',
        'strint': '力量／智力',
    }

    def render_details(row):
        if not isinstance(row.stats_json, dict) or not isinstance(row.effects_json, list):
            return [], []
        rendered_stats = []
        for stat, value in row.stats_json.items():
            if stat in {'weapon_dps', 'min_damage', 'max_damage'}:
                continue
            try:
                amount = _format_number(value)
            except (TypeError, ValueError):
                continue
            label = combined_labels.get(stat) or STAT_LABELS.get(stat, stat)
            rendered_stats.append(f'+{amount} {label}')
        rendered_effects = [
            str(effect.get('description_zh') or effect.get('description') or '').strip()
            for effect in row.effects_json if isinstance(effect, dict)
        ]
        return rendered_stats, [value for value in rendered_effects if value]

    candidates = WowItemVariantSnapshot.objects.select_related('item').filter(
        item__item_id=int(entry_id),
        season_id=season.pk,
        batch_key=season.gear_batch_key,
        game_build=str(build),
    ).order_by('item_level', 'id')

    variant = None
    stats = []
    effects = []
    for row in candidates:
        variant_metadata = row.metadata if isinstance(row.metadata, dict) else {}
        is_ptr_row = bool(variant_metadata.get('ptr_preview'))
        if is_ptr_row and not exact_build_variant_is_complete(
                build=build,
                metadata=variant_metadata,
                stats=row.stats_json,
                effects=row.effects_json):
            continue
        row_stats, row_effects = render_details(row)
        if row_stats or row_effects:
            variant = row
            stats = row_stats
            effects = row_effects
            break
    if variant is None:
        return None
    variant_metadata = variant.metadata if isinstance(variant.metadata, dict) else {}
    is_ptr = bool(variant_metadata.get('ptr_preview'))
    item = variant.item
    icon = str(item.icon or '')
    result = {
        'name': item.name_zh or item.name,
        'lines': [item.name_zh or item.name, f'物品等级 {variant.item_level}', *stats, *effects],
        'source': (
            'LMonitor PTR DB2 + SimulationCraft' if is_ptr else 'LMonitor 装备目录'
        ),
        'url': '',
        'icon': (f'https://wow.zamimg.com/images/wow/icons/large/{icon}.jpg'
                 if re.fullmatch(r'[a-zA-Z0-9_-]+', icon) else ''),
        'note': (
            f'数值为 PTR {build} 的参考装等 {variant.item_level} 快照，不代表所选难度的初始掉落装等。'
            if is_ptr else
            f'数值为正式服 {build} 当前装备目录的参考装等 {variant.item_level} 快照，不代表所选难度的初始掉落装等。'
        ),
        'item_level': variant.item_level,
        'stats': stats,
        'effects': effects,
        'complete': True,
    }
    return result


def cached_tooltip(kind, entry_id, difficulty, build=''):
    if kind == 'item':
        local = _local_item_tooltip(entry_id, build)
        if local:
            return local
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
