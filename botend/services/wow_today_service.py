import hashlib
import html as html_lib
import json
import re
import time
from datetime import datetime, timezone as datetime_timezone
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from django.db import transaction
from django.utils import timezone

from botend.models import (
    WowTodayCardSetting,
    WowTodayCardSnapshot,
    WowTodaySectionSetting,
    WowTodaySnapshot,
)
from botend.services.article_translation_service import build_translation_service
from botend.templatetags.wow_tags import wow_icon_oss_url
from utils.log import logger


WOWHEAD_SOURCE_URL = 'https://www.wowhead.com/today-in-wow'
WOWHEAD_DATA_URL = 'https://www.wowhead.com/'
WOWHEAD_TIW_SCRIPT_ID = 'data.wow.todayInWow'
WOWHEAD_REGION_KEYS = frozenset({'US', 'NA'})
WOWHEAD_REQUEST_HEADERS = {
    'Accept-Encoding': 'gzip, deflate',
    'Cache-Control': 'max-age=0',
    'DNT': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
}

LEGACY_ROOT_NAMES = frozenset({
    'the war within', 'dragonflight', 'shadowlands', 'battle for azeroth',
    'legion', 'warlords of draenor', 'mists of pandaria', 'cataclysm',
    'wrath of the lich king', 'the burning crusade', 'classic',
})
LEGACY_ID_MARKERS = (
    'tww-', '-tww', 'dragonflight', '-df', 'shadowlands', '-sl', 'bfa',
    'legion', 'draenor', 'mop', 'cataclysm', 'wotlk', 'classic',
)

DEFAULT_HIDDEN_PUBLIC_ROOT_NAMES = frozenset({'quests', 'economy', '任务', '经济'})
DEFAULT_HIDDEN_PUBLIC_ROOT_ID_PREFIXES = ('quests', 'economy')
EXCLUDED_PUBLIC_GROUP_TYPES = frozenset({'mythic-progression'})

EXPANSION_NAMES_ZH = {
    11: '午夜之境',
    10: '地心之战',
    9: '巨龙时代',
    8: '暗影国度',
    7: '争霸艾泽拉斯',
    6: '军团再临',
    5: '德拉诺之王',
}

BUILTIN_ZH = {
    'Dungeons & Raids': '地下城与团队副本',
    'Events & Rares': '事件与稀有敌人',
    'Quests': '任务',
    'Economy': '经济',
    'Mythic+ Affixes': '大秘境词缀',
    'Seasonal Caps': '赛季上限',
    'The Venomous Abyss (Mythic)': '烈毒之渊（史诗）',
    'Notable World Quests': '值得关注的世界任务',
    'Empowered Abundance Event': '强化丰裕事件',
    'Heroic World Tier': '英雄世界层级',
    'Active Ritual Site': '当前仪式场地',
    'Cursed Surges': '诅咒涌动',
    'Stormarion Assault': '斯托玛利昂突袭',
    'Bountiful Delves': '丰裕地下堡',
    'World Event': '游戏事件',
    'Midnight World Boss': '午夜之境世界首领',
    'Darkmoon Faire': '暗月马戏团',
    'Daily Quest Reset': '每日任务重置',
    'WoW Token': '时光徽章',
    "Lindormi's Guidance": '林多米的指引',
    "Xal'atath's Bargain: Devour": '萨拉塔斯的交易：吞噬',
    'Fortified': '强韧',
    'Tyrannical': '残暴',
    "Xal'atath's Guile": '萨拉塔斯的狡诈',
    'Adventurer Mistcrest': '冒险者雾纹章',
    'Veteran Mistcrest': '老兵雾纹章',
    'Champion Mistcrest': '勇士雾纹章',
    'Hero Mistcrest': '英雄雾纹章',
    'Myth Mistcrest': '神话雾纹章',
    'Spark of Tides': '潮汐火花',
    'Venomblight Manaflux': '毒蚀魔力流',
    'Special Assignment: Shade and Claw': '特别任务：暗影与利爪',
    'Special Assignment: Demand and Supply': '特别任务：需求与供给',
    "Reward: Fabled Adventurer's Cache": '奖励：传说冒险者宝箱',
    "Zul'Aman": '祖阿曼',
    'Eversong Woods': '永歌森林',
    'Harandar': '哈兰达尔',
    'Val': '瓦尔',
    'The Malformed Leviathan': '畸变利维坦',
    "The Broodmother's Nest": '巢母之巢',
    'The Looming Mutagenitor': '迫近的诱变体',
    'Mlurkkr Massacre': '姆勒克尔屠场',
    'Siege at the Whispering Marsh': '低语沼泽围攻',
    "Isle of Quel'Danas": '奎尔丹纳斯岛',
    'Parhelion Plaza': '幻日广场',
    'Eversong Woods: The Shadow Enclave': '永歌森林：暗影飞地',
    'Silvermoon City': '银月城',
    'The Darkway': '幽暗之路',
    "Isle of Quel'Danas: Parhelion Plaza": '奎尔丹纳斯岛：幻日广场',
    'Silvermoon City: The Darkway': '银月城：幽暗之路',
    'PvP Brawl: Temple of Hotmogu': 'PvP 乱斗：灼热魔古寺',
    'Northrend Cup': '诺森德杯',
    'Delves Bonus Event': '地下堡奖励活动',
    "Lu'ashal": '卢阿沙尔',
    'Highest': '最高',
    'Lowest': '最低',
}


def _clean_text(value):
    return ' '.join(html_lib.unescape(str(value or '')).split()).strip()


def _has_untranslated_english(value):
    text = re.sub(r'\b(?:WoW|PvP|NA|US|M)\b', '', str(value or ''), flags=re.I)
    return bool(re.search(r'[A-Za-z]{2,}', text))


def _safe_int(value, default=0):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _absolute_wowhead_url(value):
    url = _clean_text(value)
    if not url or url in {'#', '-'}:
        return ''
    return urljoin('https://www.wowhead.com/', url)


def _icon_url(icon):
    name = _clean_text(icon).lower()
    if not name or not re.fullmatch(r'[a-z0-9_-]+', name):
        return ''
    return wow_icon_oss_url(name, size='small')


def extract_today_json(html_text):
    text = str(html_text or '')
    if not text.strip():
        return None
    try:
        soup = BeautifulSoup(text, 'html.parser')
        tag = soup.find('script', id=WOWHEAD_TIW_SCRIPT_ID)
        raw = (tag.string or tag.get_text() or '').strip() if tag else ''
        if raw:
            data = json.loads(raw)
            return data if isinstance(data, list) else None
    except Exception:
        pass
    match = re.search(
        r'<script[^>]+id=["\']data\.wow\.todayInWow["\'][^>]*>([\s\S]*?)</script>',
        text,
        flags=re.I,
    )
    if not match:
        return None
    try:
        data = json.loads(match.group(1).strip())
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _source_region(node):
    if not isinstance(node, dict):
        return ''
    return _clean_text(node.get('regionId') or node.get('regionID') or node.get('region')).upper()


def _group_expansion(group):
    if not isinstance(group, dict):
        return 0
    return _safe_int(group.get('wowExpansion') or group.get('expansionId'))


def _current_expansion_id(region_roots):
    values = []
    for root in region_roots:
        for group in (root.get('groups') or []) if isinstance(root, dict) else []:
            value = _group_expansion(group)
            if value > 0:
                values.append(value)
    return max(values) if values else 0


def _is_legacy_root(root):
    name = _clean_text((root or {}).get('name')).lower()
    return name in LEGACY_ROOT_NAMES


def _is_default_hidden_public_root(root):
    name = _clean_text((root or {}).get('name')).lower()
    root_id = _clean_text((root or {}).get('id') or (root or {}).get('key')).lower()
    return name in DEFAULT_HIDDEN_PUBLIC_ROOT_NAMES or root_id.startswith(DEFAULT_HIDDEN_PUBLIC_ROOT_ID_PREFIXES)


def _is_excluded_public_group(group):
    group_type = _clean_text((group or {}).get('type') or (group or {}).get('kind')).lower()
    return group_type in EXCLUDED_PUBLIC_GROUP_TYPES


def _is_current_group(group, current_expansion_id):
    expansion_id = _group_expansion(group)
    if expansion_id and current_expansion_id and expansion_id != current_expansion_id:
        return False
    group_id = _clean_text((group or {}).get('id')).lower()
    if not expansion_id and any(marker in group_id for marker in LEGACY_ID_MARKERS):
        return False
    return True


def select_current_na_roots(today_json):
    roots = [x for x in (today_json or []) if isinstance(x, dict) and _source_region(x) in WOWHEAD_REGION_KEYS]
    current_expansion_id = _current_expansion_id(roots)
    selected = []
    for root in roots:
        if _is_legacy_root(root):
            continue
        groups = [
            dict(group)
            for group in (root.get('groups') or [])
            if (
                isinstance(group, dict)
                and _is_current_group(group, current_expansion_id)
                and not _is_excluded_public_group(group)
            )
        ]
        if not groups:
            continue
        item = dict(root)
        item['groups'] = groups
        selected.append(item)
    return selected, current_expansion_id


def _reject_incomplete_placeholder_lines(roots):
    """不发布 Wowhead 在日常重置后短暂返回的地下堡 Active 占位行。"""
    for root in roots:
        for group in root.get('groups') or []:
            group_id = _clean_text(group.get('id')).lower()
            if not group_id.endswith('bountiful-delves'):
                continue
            if _clean_text(group.get('type')).lower() != 'lines':
                continue
            lines = [
                line
                for line in (group.get('content') or {}).get('lines') or []
                if isinstance(line, dict)
            ]
            placeholder_lines = [
                line
                for line in lines
                if _clean_text(line.get('name')).lower() == 'active'
                and not _absolute_wowhead_url(line.get('url'))
                and not _clean_text(line.get('icon'))
                and not _clean_text(line.get('iconLabel'))
            ]
            if len(placeholder_lines) >= 2:
                raise ValueError('Wowhead 当前版本丰裕地下堡仍是无身份信息的 Active 占位数据')


def filter_public_sections(sections):
    """过滤不公开的模块；顶层板块显隐由服务端配置动态决定。"""
    filtered = []
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        modules = [
            dict(module)
            for module in section.get('modules') or []
            if isinstance(module, dict) and not _is_excluded_public_group(module)
        ]
        if modules:
            item = dict(section)
            item['modules'] = modules
            filtered.append(item)
    return filtered


def public_section_key(section):
    key = _clean_text((section or {}).get('key') or (section or {}).get('id'))
    if key:
        return key[:128]
    name = _clean_text((section or {}).get('name')) or 'section'
    digest = hashlib.sha1(name.encode('utf-8')).hexdigest()[:16]
    return f'section-{digest}'


def public_card_key(section_key, card):
    key = _clean_text((card or {}).get('key') or (card or {}).get('id'))
    if key:
        return key[:128]
    name = _clean_text((card or {}).get('name')) or 'card'
    digest = hashlib.sha1(f'{section_key}:{name}'.encode('utf-8')).hexdigest()[:16]
    return f'card-{digest}'


def public_card_preference_key(section_key, card_key):
    return f'{section_key}/{card_key}'


def default_public_section_visibility(section):
    """没有后台配置时的兼容策略；历史上移除的板块保持默认隐藏。"""
    return not _is_default_hidden_public_root(section)


def ensure_wow_today_section_settings(sections):
    """发现快照中的板块并补齐配置；已有管理员选择永不被抓取覆盖。"""
    public_sections = filter_public_sections(sections)
    existing = {
        row.section_key: row
        for row in WowTodaySectionSetting.objects.filter(
            section_key__in=[public_section_key(section) for section in public_sections]
        )
    }
    records = []
    for index, section in enumerate(public_sections):
        section_key = public_section_key(section)
        source_name = _clean_text(section.get('name'))[:150]
        row = existing.get(section_key)
        if row is None:
            row = WowTodaySectionSetting.objects.create(
                section_key=section_key,
                source_name=source_name,
                is_visible=default_public_section_visibility(section),
                sort_order=(index + 1) * 10,
            )
        elif row.source_name != source_name:
            row.source_name = source_name
            row.save(update_fields=('source_name', 'updated_at'))
        records.append(row)
    return records


def ensure_wow_today_card_settings(sections):
    """发现每张卡片并补齐长期配置；后续抓取不会覆盖管理员选择。"""
    public_sections = filter_public_sections(sections)
    section_keys = [public_section_key(section) for section in public_sections]
    existing = {
        (row.section_key, row.card_key): row
        for row in WowTodayCardSetting.objects.filter(section_key__in=section_keys)
    }
    records = []
    for section in public_sections:
        section_key = public_section_key(section)
        for index, card in enumerate(section.get('modules') or []):
            card_key = public_card_key(section_key, card)
            source_name = _clean_text(card.get('name'))[:150]
            identity = (section_key, card_key)
            row = existing.get(identity)
            if row is None:
                row = WowTodayCardSetting.objects.create(
                    section_key=section_key,
                    card_key=card_key,
                    source_name=source_name,
                    sort_order=(index + 1) * 10,
                )
            elif row.source_name != source_name:
                row.source_name = source_name
                row.save(update_fields=('source_name', 'updated_at'))
            records.append(row)
    return records


def sync_wow_today_card_snapshots(snapshot, sections):
    """把层级快照规范化为一张卡片一行，并同步发现长期卡片配置。"""
    public_sections = filter_public_sections(sections)
    rows = []
    for section_index, section in enumerate(public_sections):
        section_key = public_section_key(section)
        section_name = _clean_text(section.get('name'))[:150]
        for card_index, card in enumerate(section.get('modules') or []):
            card_key = public_card_key(section_key, card)
            rows.append(WowTodayCardSnapshot(
                snapshot=snapshot,
                section_key=section_key,
                section_name=section_name,
                section_order=(section_index + 1) * 10,
                card_key=card_key,
                source_name=_clean_text(card.get('name'))[:150],
                kind=_clean_text(card.get('kind'))[:40] or 'lines',
                source_url=_clean_text(card.get('url'))[:500],
                payload_json=dict(card),
                card_order=(card_index + 1) * 10,
            ))
    with transaction.atomic():
        WowTodayCardSnapshot.objects.filter(snapshot=snapshot).delete()
        if rows:
            WowTodayCardSnapshot.objects.bulk_create(rows)
        ensure_wow_today_card_settings(public_sections)
    return rows


def wow_today_sections_for_snapshot(snapshot):
    """优先从逐卡片记录重建板块，兼容尚未回填的旧快照。"""
    card_rows = list(snapshot.card_snapshots.all())
    if not card_rows:
        raw_sections = snapshot.sections_json if isinstance(snapshot.sections_json, list) else []
        return filter_public_sections(raw_sections)
    grouped = {}
    for row in card_rows:
        section = grouped.setdefault(row.section_key, {
            'key': row.section_key,
            'name': row.section_name,
            'modules': [],
        })
        card = dict(row.payload_json) if isinstance(row.payload_json, dict) else {}
        card['key'] = row.card_key
        card['name'] = row.source_name
        card['kind'] = row.kind or card.get('kind') or 'lines'
        if row.source_url:
            card['url'] = row.source_url
        section['modules'].append(card)
    return list(grouped.values())


def apply_wow_today_section_settings(sections):
    """把板块与逐卡片配置投影到公开内容，不在公开读取请求中产生写操作。"""
    public_sections = filter_public_sections(sections)
    settings = {
        row.section_key: row
        for row in WowTodaySectionSetting.objects.filter(
            section_key__in=[public_section_key(section) for section in public_sections]
        )
    }
    card_settings = {
        (row.section_key, row.card_key): row
        for row in WowTodayCardSetting.objects.filter(
            section_key__in=[public_section_key(section) for section in public_sections]
        )
    }
    projected = []
    for index, section in enumerate(public_sections):
        section_key = public_section_key(section)
        setting = settings.get(section_key)
        if setting is not None:
            if not setting.is_visible:
                continue
            display_name = _clean_text(setting.display_name) or _clean_text(section.get('name'))
            sort_order = setting.sort_order
        else:
            if not default_public_section_visibility(section):
                continue
            display_name = _clean_text(section.get('name'))
            sort_order = (index + 1) * 10
        projected_cards = []
        for card_index, card in enumerate(section.get('modules') or []):
            card_key = public_card_key(section_key, card)
            card_setting = card_settings.get((section_key, card_key))
            if card_setting is not None:
                if not card_setting.is_visible:
                    continue
                card_name = _clean_text(card_setting.display_name) or _clean_text(card.get('name'))
                card_sort_order = card_setting.sort_order
            else:
                card_name = _clean_text(card.get('name'))
                card_sort_order = (card_index + 1) * 10
            projected_card = dict(card)
            projected_card['key'] = card_key
            projected_card['name'] = card_name
            projected_card['preference_key'] = public_card_preference_key(section_key, card_key)
            projected_cards.append((card_sort_order, card_index, projected_card))
        projected_cards.sort(key=lambda row: (row[0], row[1]))
        if not projected_cards:
            continue
        item = dict(section)
        item['key'] = section_key
        item['name'] = display_name
        item['modules'] = [row[2] for row in projected_cards]
        projected.append((sort_order, index, item))
    projected.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in projected]


class WowTodayTranslator:
    def __init__(self, translation_service=None):
        self.translation_service = translation_service if translation_service is not None else build_translation_service()

    def _builtin(self, source):
        text = _clean_text(source)
        if not text:
            return ''
        if text in BUILTIN_ZH:
            return BUILTIN_ZH[text]
        if ': ' in text:
            left, right = text.split(': ', 1)
            left_zh = BUILTIN_ZH.get(left, '')
            right_zh = BUILTIN_ZH.get(right, '')
            if left_zh and right_zh:
                return f'{left_zh}：{right_zh}'
        return text if not _has_untranslated_english(text) else ''

    def translate_many(self, values):
        ordered = []
        for value in values or []:
            text = _clean_text(value)
            if text and text not in ordered:
                ordered.append(text)
        translated = {text: self._builtin(text) for text in ordered}
        missing = [text for text in ordered if not translated.get(text)]
        if missing and self.translation_service and self.translation_service.available():
            try:
                raw = self.translation_service.translate_content('\n'.join(missing))
                pairs = json.loads(raw or '[]')
                if isinstance(pairs, list):
                    for pair in pairs:
                        if not isinstance(pair, dict):
                            continue
                        source = _clean_text(pair.get('original'))
                        target = _clean_text(pair.get('translated'))
                        if source in translated and target and not _has_untranslated_english(target):
                            translated[source] = target
            except Exception as exc:
                logger.warning('[WowTodayTranslator] 批量翻译失败: %s', str(exc))
        return translated


def _collect_translatable_strings(roots):
    values = []
    for root in roots:
        values.append(root.get('name'))
        for group in root.get('groups') or []:
            values.append(group.get('name'))
            content = group.get('content') or {}
            for line in content.get('lines') or []:
                if not isinstance(line, dict):
                    continue
                for key in ('name', 'iconLabel', 'label', 'subtitle', 'difficulty', 'mode'):
                    values.append(line.get(key))
    return values


def _line_to_public_item(line, group_name_zh, translations, fallback_icon=''):
    if not isinstance(line, dict):
        return None
    source_name = _clean_text(line.get('name'))
    name_zh = translations.get(source_name, '') if source_name else group_name_zh
    if not name_zh:
        return None
    icon_label_source = _clean_text(line.get('iconLabel'))
    item = {
        'name': name_zh,
        'url': _absolute_wowhead_url(line.get('url')),
        'icon_url': _icon_url(line.get('icon') or fallback_icon),
        'icon_label': translations.get(icon_label_source, '') if icon_label_source else '',
        'starts_at': _safe_int(line.get('startingUt')),
        'ends_at': _safe_int(line.get('endingUt')),
        'quantity': _safe_int(line.get('qty')),
        'extra': _clean_text(line.get('extraText')) if str(line.get('extraText') or '').isdigit() else '',
    }
    return {key: value for key, value in item.items() if value not in ('', 0, None)}


def build_public_sections(roots, translator=None):
    translator = translator or WowTodayTranslator()
    translations = translator.translate_many(_collect_translatable_strings(roots))
    sections = []
    missing = set()
    for root in roots:
        source_section_name = _clean_text(root.get('name'))
        section_name = translations.get(source_section_name, '')
        if not section_name:
            missing.add(source_section_name)
            continue
        modules = []
        for group in root.get('groups') or []:
            source_module_name = _clean_text(group.get('name'))
            module_name = translations.get(source_module_name, '')
            if not module_name:
                missing.add(source_module_name)
                continue
            content = group.get('content') if isinstance(group.get('content'), dict) else {}
            kind = _clean_text(group.get('type')).lower() or 'lines'
            module = {
                'key': _clean_text(group.get('id')),
                'name': module_name,
                'kind': kind,
                'url': _absolute_wowhead_url(group.get('url')),
                'items': [],
            }
            if kind == 'token':
                price_html = str(content.get('priceHtml') or '')
                price_text = _clean_text(BeautifulSoup(price_html, 'html.parser').get_text(' ', strip=True))
                if price_text:
                    module['items'] = [{'name': module_name, 'value': price_text}]
            elif kind == 'mythic-progression':
                module['metrics'] = {
                    'defeated_bosses': _safe_int(content.get('defeatedBosses')),
                    'total_bosses': _safe_int(content.get('totalBosses')),
                    'top_guild_count': _safe_int(content.get('topGuildCount')),
                }
            else:
                for line in content.get('lines') or []:
                    item = _line_to_public_item(
                        line,
                        module_name,
                        translations,
                        fallback_icon=group.get('wowIcon'),
                    )
                    if item:
                        module['items'].append(item)
                    elif isinstance(line, dict) and _clean_text(line.get('name')):
                        missing.add(_clean_text(line.get('name')))
            if not module.get('items') and not module.get('metrics'):
                continue
            module = {key: value for key, value in module.items() if value not in ('', [], None)}
            modules.append(module)
        if modules:
            sections.append({
                'key': _clean_text(root.get('id')),
                'name': section_name,
                'modules': modules,
            })
    return sections, len([value for value in missing if value])


def snapshot_payload_from_html(html_text, translator=None):
    today_json = extract_today_json(html_text)
    if today_json is None:
        raise ValueError('Wowhead 页面缺少 Today in WoW 数据')
    roots, expansion_id = select_current_na_roots(today_json)
    if not roots:
        raise ValueError('Wowhead 页面没有北美正式服当前版本内容')
    _reject_incomplete_placeholder_lines(roots)
    sections, translation_missing = build_public_sections(roots, translator=translator)
    if not sections:
        raise ValueError('Wowhead 当前版本内容没有可公开的中文模块')
    return {
        'expansion_id': expansion_id,
        'expansion_name': EXPANSION_NAMES_ZH.get(expansion_id, '当前版本'),
        'sections': sections,
        'raw_roots': roots,
        'translation_missing': translation_missing,
    }


class WowTodayService:
    def __init__(self, request_client, translator=None, sleep_func=time.sleep):
        self.request_client = request_client
        self.translator = translator or WowTodayTranslator()
        self.sleep_func = sleep_func

    def _response_html(self, response):
        if response is False or response is None:
            return ''
        if isinstance(response, bytes):
            return response.decode('utf-8', 'ignore')
        if isinstance(response, str):
            return response
        return str(getattr(response, 'text', '') or '')

    def fetch_html(self):
        response = self.request_client.get(
            WOWHEAD_DATA_URL,
            'Response',
            0,
            '',
            WOWHEAD_REQUEST_HEADERS,
        )
        html_text = self._response_html(response)
        status = _safe_int(getattr(response, 'status_code', 200), 200)
        if status < 400 and extract_today_json(html_text) is not None:
            return html_text

        driver = self.request_client.get(
            WOWHEAD_DATA_URL,
            'RespByChrome',
            0,
            '',
            is_origin=1,
        )
        for _ in range(6):
            html_text = self._response_html(getattr(driver, 'html', driver))
            if extract_today_json(html_text) is not None:
                return html_text
            self.sleep_func(0.8)
        raise RuntimeError('无法取得 Wowhead Today in WoW 页面数据')

    def sync(self):
        html_text = self.fetch_html()
        payload = snapshot_payload_from_html(html_text, translator=self.translator)
        now = timezone.now()
        snapshot_date = now.astimezone(ZoneInfo('America/Los_Angeles')).date()
        sections = payload['sections']
        content_hash = hashlib.sha256(
            json.dumps(sections, ensure_ascii=False, sort_keys=True).encode('utf-8')
        ).hexdigest()
        snapshot, created = WowTodaySnapshot.objects.update_or_create(
            snapshot_date=snapshot_date,
            region='na',
            game_version='retail',
            defaults={
                'source_region': 'US',
                'expansion_id': payload['expansion_id'],
                'expansion_name': payload['expansion_name'],
                'source_url': WOWHEAD_SOURCE_URL,
                'content_hash': content_hash,
                'sections_json': sections,
                'raw_json': payload['raw_roots'],
                'translation_missing': payload['translation_missing'],
                'fetched_at': now,
            },
        )
        sync_wow_today_card_snapshots(snapshot, sections)
        ensure_wow_today_section_settings(sections)
        return {
            'created': created,
            'snapshot_id': snapshot.id,
            'snapshot_date': snapshot.snapshot_date.isoformat(),
            'section_count': len(sections),
            'module_count': sum(len(section.get('modules') or []) for section in sections),
            'translation_missing': payload['translation_missing'],
            'content_hash': content_hash,
        }


def unix_timestamp_to_iso(value):
    timestamp = _safe_int(value)
    if not timestamp:
        return ''
    try:
        return datetime.fromtimestamp(timestamp, tz=datetime_timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ''
