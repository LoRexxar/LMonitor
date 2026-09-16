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
    is_ptr = bool(item['variant_metadata'].get('ptr_preview'))
    # variant 完整时直接返回结构化数据
    if item['variant_id'] and item['tooltip_complete']:
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
    # variant 缺失时从 display_description（含静态属性说明前缀）解析属性
    tooltip_text = item.get('display_description') or item.get('description_zh') or ''
    if not tooltip_text or not item.get('display_name') or item.get('display_name', '').startswith('#'):
        return None
    lines = [l.strip() for l in tooltip_text.splitlines() if l.strip()]
    stats = []
    effects = []
    item_level = None
    for line in lines:
        # 物品等级行
        m = re.search(r'物品等级[：:]\s*(\d[\d,.]*)', line)
        if m:
            try:
                item_level = int(m.group(1).replace(',', ''))
            except ValueError:
                pass
            continue
        # 装备/使用特效行
        if re.match(r'^(装备[：:]|使用[:：])', line):
            effects.append(line)
            continue
        # 静态属性说明：+NNN 属性（DB2 原始格式）
        m = re.match(r'静态属性说明[：:]\s*\+\s*(\d[\d,.]*\s*\S+)', line)
        if m:
            stats.append('+' + m.group(1).strip())
            continue
        # +NNN 属性（Wowhead 格式，无前缀）
        if re.match(r'\+\s*\d[\d,.]*\s', line) and '静态属性说明' not in line:
            stats.append(re.sub(r'\+\s+', '+', line))
            continue
        # 独立副属性行（如 "54暴击" 前面有 "+"）
        if re.fullmatch(r'\d+.{1,6}', line) and not re.search(r'(伤害|速度|耐久|等级|掉落|售价|护甲)', line):
            if stats and stats[-1] == '+':
                stats[-1] = '+' + line
            continue
        if line == '+':
            stats.append(line)
            continue
    source_label = 'LMonitor PTR DB2 + SimulationCraft' if is_ptr else 'LMonitor 装备目录'
    note_prefix = f'PTR {build}' if is_ptr else f'正式服 {build}'
    note = f'{source_label} 基础事实，参考装等 {item_level or "未知"}。' if not stats else f'数值来自 {note_prefix} 装备目录描述文本。'
    return {
        'name': item['display_name'],
        'lines': lines,
        'source': source_label,
        'url': '',
        'icon': item['icon_url'],
        'note': note,
        'item_level': item_level,
        'stats': stats,
        'effects': effects,
        'complete': bool(stats or effects),
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


def _parse_wowhead_item_tooltip(entry_id, difficulty, raw_lines, payload):
    """将 Wowhead 物品 tooltip HTML 行解析为结构化格式。"""
    item_level = None
    stats = []
    effects = []
    skip_headers = {'物品等级', '升级', '耐久', '需要等级', '售价', '卸载', '唯一', '装备唯一'}
    # 合并 "物品等级：\n289" 为单行
    merged = []
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        if line == '物品等级：' and i + 1 < len(raw_lines) and raw_lines[i + 1].isdigit():
            merged.append(f'物品等级：{raw_lines[i + 1]}')
            i += 2
            continue
        merged.append(line)
        i += 1
    raw_lines = merged
    # 合并多行特效：装备：/使用: 后面的连续行拼接为完整特效文本
    merged_effects = []
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        # 装备/使用特效行（可能只含前缀，后续行是效果描述）
        if re.match(r'^(装备[：:]|使用[：:])', line):
            effect_parts = [line.rstrip('：:').rstrip() + '：' if re.match(r'^(装备[：:]|使用[:：])\s*$', line) else line]
            i += 1
            # 收集后续行直到遇到下一个"标题行"或"属性行"或"跳过行"
            while i < len(raw_lines):
                next_line = raw_lines[i]
                if (re.match(r'^(装备[：:]|使用[:：]|掉落于|出售于|需要等级|耐久|升级[:：]|售价)', next_line)
                    or re.match(r'\+\s*\d[\d,.]*\s', next_line)
                    or any(next_line.startswith(h) for h in skip_headers)
                    or next_line in ('史诗', '精良', '优秀', '普通', '粗糙', '传说', '神器', '传家宝')
                    or re.search(r'(点伤害|每秒伤害|速度\s*\d)', next_line)
                    or re.fullmatch(r'\d+护甲', next_line)):
                    break
                effect_parts.append(next_line)
                i += 1
            merged_effects.append(' '.join(part.strip() for part in effect_parts if part.strip()))
            continue
        i += 1
    # 从合并后的特效行中提取属性和特效
    for line in raw_lines:
        # 提取装等
        m = re.search(r'物品等级[：:]\s*(\d[\d,.]*)', line)
        if m:
            try:
                item_level = int(m.group(1).replace(',', ''))
            except ValueError:
                pass
            continue
        if any(line.startswith(h) for h in skip_headers):
            continue
        if line in ('史诗', '精良', '优秀', '普通', '粗糙', '传说', '神器', '传家宝'):
            continue
        if re.fullmatch(r'\d[\d,.]*', line):
            continue
        if re.search(r'(点伤害|每秒伤害|速度\s*\d)', line):
            continue
        if re.fullmatch(r'\d+护甲', line):
            continue
        if re.fullmatch(r'\d+', line):
            continue
        if re.match(r'^(掉落于|出售于)', line):
            continue
        # 统计属性行（+NNN 属性）
        if re.match(r'\+\s*\d[\d,.]*\s', line):
            stats.append(line)
            continue
        # 带属性名的副属性（如 "54暴击"）
        if re.fullmatch(r'\d+.{1,6}', line) and not re.search(r'(点伤害|每秒伤害|速度|耐久|等级|售价|掉落)', line):
            # 检查前一行是否是 "+"
            idx = raw_lines.index(line) if line in raw_lines else -1
            if idx > 0 and raw_lines[idx - 1] == '+':
                stats.append(f'+{line}')
                continue
        # 跳过其他行（绑定、类型、材料、单独的"装备"/"使用"等）
    # 使用合并后的特效
    effects = merged_effects
    icon = str(payload.get('icon') or '')
    wowhead_icon = f'https://wow.zamimg.com/images/wow/icons/large/{icon}.jpg' if re.fullmatch(r'[a-zA-Z0-9_-]+', icon) else ''
    return {
        'name': payload.get('name', ''),
        'lines': raw_lines,
        'source': 'Wowhead',
        'url': f'https://www.wowhead.com/cn/item={int(entry_id)}',
        'icon': wowhead_icon,
        'note': '属性来自 Wowhead 中央数据，仅供参考。',
        'item_level': item_level,
        'stats': stats,
        'effects': effects,
        'complete': True,
    }


def tooltip(kind, entry_id, difficulty, build=''):
    key = cache_key(kind, entry_id, difficulty, build)
    cached = cached_tooltip(kind, entry_id, difficulty, build)
    if cached:
        return cached
    # 物品：中央变体缺失时回退到 Wowhead tooltip API
    if kind == 'item':
        pass  # 不再直接抛异常，下面统一回退 Wowhead
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
        if kind == 'item':
            raise ValueError(f'Wowhead 物品 {int(entry_id)} 暂时无法获取')
        raise
    soup = BeautifulSoup(payload.get('tooltip', ''), 'html.parser')
    for element in soup(['script', 'style', 'iframe']):
        element.decompose()
    lines = [line.strip().replace('\x08', '').replace('\u200b', '') for line in soup.get_text('\n').splitlines() if line.strip()]
    if not payload.get('name') or not lines:
        raise ValueError('来源暂未提供物品或技能详情')
    icon = str(payload.get('icon') or '')
    if kind == 'item':
        result = _parse_wowhead_item_tooltip(entry_id, difficulty, lines, payload)
    else:
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
