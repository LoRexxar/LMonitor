"""从正式服公开数据源生成职业配装器的规范化目录。"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode

import requests

from botend.constants.wow import SPEC_IDENTITY_MAP, localize_gear_source
from botend.services.article_image_service import _get_configured_proxies


WAGO_DB2_HOME = 'https://wago.tools/db2'
RAIDBOTS_LIVE_ROOT = 'https://www.raidbots.com/static/data/live'
WOWHEAD_TOOLTIP = 'https://nether.wowhead.com/tooltip/item/{item_id}'

# 当前正式服赛季的合法装备等级。版本变化时必须显式更新并通过审计，不能静默猜测。
SEASON_LEVEL_PROFILES = {
    'mid2': {
        'tracks': {
            'champion': (292, 295, 298, 302, 305, 308),
            'hero': (305, 308, 311, 315, 318, 321),
            'myth': (318, 321, 324, 328, 331, 334),
        },
        'crafted': {
            'hero': (305, 309, 312, 315, 318),
            'myth': (318, 322, 325, 328, 331),
        },
    },
}

INVENTORY_SLOTS = {
    1: ('head',), 2: ('neck',), 3: ('shoulders',), 5: ('chest',),
    6: ('waist',), 7: ('legs',), 8: ('feet',), 9: ('wrists',),
    10: ('hands',), 11: ('finger',), 12: ('trinket',),
    13: ('main_hand', 'off_hand'), 14: ('off_hand',), 15: ('main_hand',),
    16: ('back',), 17: ('main_hand',), 20: ('chest',),
    21: ('main_hand',), 22: ('off_hand',), 23: ('off_hand',),
    25: ('main_hand',), 26: ('main_hand',), 28: ('off_hand',),
}

STAT_NAMES_ZH = {
    '力量': 'strength', '敏捷': 'agility', '智力': 'intellect',
    '耐力': 'stamina', '护甲': 'armor', '额外护甲': 'bonus_armor',
    '爆击': 'crit', '暴击': 'crit', '急速': 'haste', '精通': 'mastery',
    '全能': 'versatility', '吸血': 'leech', '闪避': 'avoidance', '速度': 'speed',
}
RAIDBOTS_STATS = {
    'str': 'strength', 'agi': 'agility', 'int': 'intellect', 'sta': 'stamina',
    'crit': 'crit', 'haste': 'haste', 'mastery': 'mastery',
    'vers': 'versatility', 'leech': 'leech', 'avoidance': 'avoidance',
    'runspeed': 'speed',
}
PRIMARY_STATS_BY_ITEM_MOD = {
    3: ('agility',), 4: ('strength',), 5: ('intellect',),
    71: ('strength', 'agility', 'intellect'), 72: ('strength', 'agility'),
    73: ('agility', 'intellect'), 74: ('strength', 'intellect'),
}
ARMOR_TYPE_NAMES = {1: '布甲', 2: '皮甲', 3: '锁甲', 4: '板甲', 6: '盾牌'}
WEAPON_TYPE_NAMES = {
    0: '单手斧', 1: '双手斧', 2: '弓', 3: '枪械', 4: '单手锤', 5: '双手锤',
    6: '长柄武器', 7: '单手剑', 8: '双手剑', 9: '战刃', 10: '法杖',
    13: '拳套', 15: '匕首', 18: '弩', 19: '魔杖',
}


class CatalogSourceError(RuntimeError):
    """远端目录无法被安全构建。"""


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _plain_text(value):
    value = str(value or '').replace('\b', '')
    value = re.sub(r'<br\s*/?>', '\n', value, flags=re.I)
    value = re.sub(r'<[^>]+>', '', value)
    value = html.unescape(value)
    return '\n'.join(line.strip() for line in value.splitlines() if line.strip())


def _tooltip_details(payload):
    tooltip = _plain_text((payload or {}).get('tooltip'))
    stats = {}
    for amount, label in re.findall(
        r'\+?([0-9][0-9,.]*)\s*(额外护甲|力量|敏捷|智力|耐力|护甲|爆击|暴击|急速|精通|全能|吸血|闪避|速度)',
        tooltip,
    ):
        key = STAT_NAMES_ZH[label]
        stats[key] = max(stats.get(key, 0), _safe_int(amount.replace(',', '').replace('.', '')))
    effects = []
    for line in tooltip.splitlines():
        if line.startswith(('装备：', '使用：', '提供下列属性：')) or re.match(r'^\([24]\)\s*(?:组合|套装)', line):
            effects.append({'description_zh': line})
    effects = list({row['description_zh']: row for row in effects}.values())
    primary_options = {}
    for amount, labels in re.findall(r'\+?([0-9][0-9,.]*)\s*\[([^\]]*(?:力量|敏捷|智力)[^\]]*)\]', tooltip):
        parsed = _safe_int(amount.replace(',', '').replace('.', ''))
        for label, key in (('力量', 'strength'), ('敏捷', 'agility'), ('智力', 'intellect')):
            if label in labels:
                primary_options[key] = parsed
    random_secondaries = [
        _safe_int(value.replace(',', '').replace('.', ''))
        for value in re.findall(r'\+?([0-9][0-9,.]*)\s*随机属性\d+', tooltip)
    ]
    return {
        'name_zh': str((payload or {}).get('name') or ''),
        'icon': str((payload or {}).get('icon') or ''),
        'quality': _safe_int((payload or {}).get('quality')),
        'description_zh': '\n'.join(row['description_zh'] for row in effects),
        'stats': stats,
        'effects': effects,
        'primary_options': primary_options,
        'secondary_total': sum(random_secondaries),
    }


class CurrentGearCatalogSource:
    """锁定 Wago 构建，并将 Raidbots/Wowhead 数据投影为导入目录。"""

    def __init__(self, *, cache_dir='.cache/gear_builder', workers=8, timeout=45, no_proxy=False, progress=None):
        self.cache_root = Path(cache_dir).expanduser().resolve()
        self.workers = max(1, min(24, int(workers or 8)))
        self.timeout = max(5, int(timeout or 45))
        self.progress = progress or (lambda _message: None)
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; LMonitor-GearBuilder/1.0)'})
        self.session.trust_env = not no_proxy
        if not no_proxy:
            configured_proxies = _get_configured_proxies()
            if configured_proxies:
                self.session.proxies.update(configured_proxies)

    def build(self, *, season_key='', include_wowhead=True):
        self.progress('正在读取 Wago 正式服构建号……')
        game_build = self._wago_current_build()
        metadata = self._get_json(f'{RAIDBOTS_LIVE_ROOT}/metadata.json')
        raidbots_build = str(metadata.get('wowBuild') or metadata.get('wow_build') or '')
        if raidbots_build != game_build:
            raise CatalogSourceError(
                f'Wago 构建 {game_build} 与 Raidbots 构建 {raidbots_build or "未知"} 不一致，拒绝激活混合批次。'
            )

        self.progress(f'已锁定正式服构建 {game_build}，正在下载结构化目录……')
        seasons = self._get_json(f'{RAIDBOTS_LIVE_ROOT}/seasons.json')
        instances = self._get_json(f'{RAIDBOTS_LIVE_ROOT}/instances.json')
        encounter_items = self._get_json(f'{RAIDBOTS_LIVE_ROOT}/encounter-items.json')
        catalyst_items = self._get_json(f'{RAIDBOTS_LIVE_ROOT}/export-items-catalyst.json')
        item_sets = self._get_json(f'{RAIDBOTS_LIVE_ROOT}/item-sets.json')
        crafting = self._get_json(f'{RAIDBOTS_LIVE_ROOT}/crafting.json')
        enchantments = self._get_json(f'{RAIDBOTS_LIVE_ROOT}/enchantments.json')
        active = next((row for row in seasons if row.get('active')), None)
        if not active:
            raise CatalogSourceError('Raidbots 未声明当前正式服赛季。')
        short_name = str(active.get('shortName') or '')
        profile = SEASON_LEVEL_PROFILES.get(short_name)
        if not profile:
            raise CatalogSourceError(f'当前赛季 {short_name or active.get("name")} 尚未配置合法装等范围。')

        normalized_season_key = season_key.strip() or self._season_key(active)
        items, season_info = self._build_items(
            active, profile, instances, encounter_items, catalyst_items, item_sets, crafting, enchantments,
        )
        if include_wowhead:
            self._enrich_wowhead(items, game_build)
        else:
            self.progress('已跳过 Wowhead 中文 Tooltip 与变体属性补全。')

        content = json.dumps(items, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        batch_key = f'gear-{game_build}-{hashlib.sha256(content).hexdigest()[:12]}'
        return {
            'season_key': normalized_season_key,
            'season_name': str(active.get('name') or normalized_season_key),
            'season_info': season_info,
            'batch_key': batch_key,
            'game_build': game_build,
            'provider': {
                'structure': 'wago_db2',
                'normalized_projection': 'raidbots_static',
                'display': 'wowhead_zhcn',
                'wago_build': game_build,
                'raidbots_build': raidbots_build,
            },
            'rules': {
                'socket_additions': self._socket_addition_rules(active),
                'add_socket_item': active.get('addSocketItem') or {},
            },
            'items': items,
        }

    @staticmethod
    def _socket_addition_rules(active):
        slot_aliases = {'wrist': 'wrists', 'rings': 'finger'}
        rules = []
        for row in active.get('sockets') or []:
            slot = slot_aliases.get(str(row.get('slot') or ''), str(row.get('slot') or ''))
            maximum = _safe_int(row.get('extraSockets') or row.get('vault'))
            if slot and maximum:
                rules.append({
                    'slot': slot,
                    'max_additional': maximum,
                    'source': 'great_vault' if row.get('vault') else 'socket_item',
                })
        return rules

    def _get(self, url, **kwargs):
        try:
            response = self.session.get(url, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            raise CatalogSourceError(f'下载失败：{url}；{exc}') from exc

    def _get_json(self, url):
        try:
            return self._get(url).json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise CatalogSourceError(f'远端返回的不是合法 JSON：{url}') from exc

    def _wago_current_build(self):
        response = self._get(WAGO_DB2_HOME)
        match = re.search(r'data-page=(?:"([^"]+)"|\'([^\']+)\')', response.text or '')
        if not match:
            raise CatalogSourceError('无法从 Wago DB2 页面解析当前构建号。')
        try:
            page = json.loads(html.unescape(match.group(1) or match.group(2) or ''))
        except json.JSONDecodeError as exc:
            raise CatalogSourceError('Wago DB2 页面中的构建元数据无法解析。') from exc
        props = page.get('props') or {}
        build = str(props.get('currentVersion') or props.get('current_version') or '')
        if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+', build):
            raise CatalogSourceError(f'Wago 返回了异常构建号：{build or "空"}')
        return build

    @staticmethod
    def _season_key(active):
        short_name = str(active.get('shortName') or '').lower()
        expansion = re.sub(r'[^a-z0-9]+', '-', str(active.get('name') or '').split(' Season ', 1)[0].lower()).strip('-')
        number = re.search(r'(\d+)$', short_name)
        return f'{expansion}-s{number.group(1)}' if expansion and number else short_name or 'current-season'

    def _build_items(self, active, profile, instances, encounter_items, catalyst_items, item_sets, crafting, enchantments):
        instance_by_id = {_safe_int(row.get('id')): row for row in instances}
        short_name = str(active.get('shortName') or '')
        season_number = (re.search(r'(\d+)$', short_name) or [None, ''])[1]
        mplus = next((row for row in instances if row.get('type') == 'mplus-chest'), None)
        delve = next((row for row in instances if row.get('type') == f'delve-{short_name}'), None)
        raid_group = next((row for row in instances if row.get('type') == 'raid' and row.get('id', 0) < 0 and str(row.get('name')) == f'Season {season_number} Raids'), None)
        catalyst = next((row for row in instances if row.get('type') == 'catalyst' and str(row.get('name')) == f'Catalyst Season {season_number}'), None)
        if not mplus or not delve or not raid_group:
            raise CatalogSourceError('无法定位当前赛季的大秘境、团本或地下堡来源集合。')
        raid_ids = sorted({_safe_int(row.get('sourceInstanceId')) for row in raid_group.get('encounters') or [] if _safe_int(row.get('sourceInstanceId')) > 0})
        raid_encounter_meta = {
            (_safe_int(row.get('sourceInstanceId')), _safe_int(row.get('id'))): row
            for row in raid_group.get('encounters') or []
            if _safe_int(row.get('sourceInstanceId')) > 0 and _safe_int(row.get('id')) > 0
        }
        final_sequence = max((_safe_int(row.get('itemSequenceLevel')) for row in raid_encounter_meta.values()), default=0)
        profession_type = f'profession{str(active.get("name") or "").split(" Season ", 1)[0].replace(" ", "")}Epic'
        profession = next((row for row in instances if row.get('type') == profession_type), None)
        if not profession:
            raise CatalogSourceError(f'无法定位当前赛季制造装备集合：{profession_type}')

        source_groups = {
            _safe_int(mplus['id']): ('mythic_plus', mplus),
            _safe_int(delve['id']): ('delve', delve),
            _safe_int(profession['id']): ('crafted', profession),
        }
        for raid_id in raid_ids:
            source_groups[raid_id] = ('raid', instance_by_id.get(raid_id) or raid_group)
        encounter_names = {}
        for instance in instances:
            for encounter in instance.get('encounters') or []:
                encounter_names[(_safe_int(instance.get('id')), _safe_int(encounter.get('id')))] = str(encounter.get('name') or '')

        by_id = {}
        for raw in encounter_items:
            matched = []
            for source in raw.get('sources') or []:
                instance_id = _safe_int(source.get('instanceId'))
                if instance_id not in source_groups:
                    continue
                source_type, instance = source_groups[instance_id]
                raid_meta = raid_encounter_meta.get((instance_id, _safe_int(source.get('encounterId'))), {})
                matched.append(localize_gear_source({
                    'type': source_type,
                    'instance_id': instance_id,
                    'instance': str(instance.get('name') or ''),
                    'encounter_id': _safe_int(source.get('encounterId')),
                    'encounter': encounter_names.get((instance_id, _safe_int(source.get('encounterId'))), ''),
                    'encounter_order': _safe_int(raid_meta.get('order')),
                    'item_sequence_level': _safe_int(raid_meta.get('itemSequenceLevel')),
                    'very_rare': bool(source.get('veryRare')),
                }))
            if not matched:
                continue
            slots = list(INVENTORY_SLOTS.get(_safe_int(raw.get('inventoryType')), ()))
            if not slots:
                continue
            item = self._base_item(raw, 'equipment', slots)
            for source_type in sorted({row['type'] for row in matched}):
                sources = [row for row in matched if row['type'] == source_type]
                if source_type == 'crafted':
                    self._add_crafted_variants(item, profile, sources)
                else:
                    special_mythic = source_type == 'raid' and any(
                        row.get('very_rare') or (
                            final_sequence and row.get('item_sequence_level') == final_sequence
                            and row.get('encounter_order', 0) >= 7
                        )
                        for row in sources
                    )
                    self._add_drop_variants(item, profile, source_type, sources, special_mythic=special_mythic)
            by_id[item['item_id']] = item

        self._add_tier_set_items(
            by_id, catalyst_items, item_sets, profile, catalyst, raid_ids, instance_by_id,
        )

        for reagent in self._highest_quality_embellishments(crafting.get('reagents') or []):
            limit = reagent.get('itemLimit') or {}
            item_id = _safe_int(reagent.get('id') or reagent.get('itemId'))
            if not item_id:
                continue
            item = by_id.setdefault(item_id, self._base_item(reagent, 'embellishment', []))
            item['unique_group'] = 'embellishment-limit'
            item['variants'].append({
                'key': f'embellishment-q{_safe_int(reagent.get("craftingQuality")) or 1}',
                'type': 'embellishment',
                'crafting_quality': _safe_int(reagent.get('craftingQuality')),
                'bonus_ids': reagent.get('craftingBonusIds') or [],
                'compatible_slots': [],
                'unique_group': 'embellishment-limit',
                'max_equipped': _safe_int(limit.get('quantity'), 2),
                'sources': [localize_gear_source({'type': 'profession', 'profession': 'Crafting'})],
                'metadata': {'reagent_slot_ids': reagent.get('reagentSlotIds') or []},
            })

        for raw in self._highest_quality_enhancements(enchantments):
            item_id = _safe_int(raw.get('itemId'))
            if _safe_int(raw.get('expansion')) != 11 or not item_id:
                continue
            is_gem = raw.get('slot') == 'socket'
            if not is_gem and str(raw.get('categoryName') or '') in ('Tool Enchants', 'Bots'):
                continue
            slots = ['head', 'neck', 'shoulders', 'back', 'chest', 'wrists', 'hands', 'waist', 'legs', 'feet', 'finger', 'weapon'] if is_gem else self._enchant_slots(raw)
            if not slots:
                continue
            catalog_type = 'gem' if is_gem else 'enchant'
            item = by_id.setdefault(item_id, self._base_item({
                'id': item_id, 'name': raw.get('itemName'), 'icon': raw.get('itemIcon'), 'quality': raw.get('quality'),
            }, catalog_type, slots))
            item['enchantment_id'] = _safe_int(raw.get('id'))
            stats, primary = self._raidbots_stats(raw.get('stats') or [])
            item['variants'].append({
                'key': f'{catalog_type}-q{_safe_int(raw.get("craftingQuality")) or 1}-e{_safe_int(raw.get("id"))}',
                'type': catalog_type,
                'crafting_quality': _safe_int(raw.get('craftingQuality')),
                'compatible_slots': slots,
                'socket_types': [str(raw.get('socketType') or 'prismatic').lower()] if is_gem else [],
                'stats': stats,
                'effects': [{'description': str(raw.get('displayName') or '')}] if not stats else [],
                'unique_group': f'item-limit-{_safe_int(raw.get("itemLimitCategory"))}' if raw.get('unique') else '',
                'max_equipped': 1 if raw.get('unique') else 0,
                'sources': [localize_gear_source({'type': 'profession', 'profession': 'Jewelcrafting' if is_gem else 'Enchanting'})],
                'metadata': {'enchantment_id': _safe_int(raw.get('id')), 'simc_name': raw.get('tokenizedName') or '', 'primary_stat_amount': primary},
            })

        items = []
        for item in by_id.values():
            unique = {}
            for variant in item.pop('variants', []):
                unique[variant['key']] = variant
            item['variants'] = list(unique.values())
            if item['variants']:
                items.append(item)
        items.sort(key=lambda row: row['item_id'])
        raid_names = [str((instance_by_id.get(value) or {}).get('name') or value) for value in raid_ids]
        return items, {
            'mplus_zone_id': _safe_int(mplus.get('id')),
            'mplus_zone_name': str(mplus.get('name') or ''),
            'mplus_encounters': mplus.get('encounters') or [],
            'raid_zone_id': raid_ids[0] if raid_ids else _safe_int(raid_group.get('id')),
            'raid_zone_name': ' / '.join(raid_names),
            'raid_zones': [{'zone_id': value, 'zone_name': str((instance_by_id.get(value) or {}).get('name') or value)} for value in raid_ids],
            'raid_encounters': raid_group.get('encounters') or [],
            'delve_sources': delve.get('encounters') or [],
        }

    @staticmethod
    def _base_item(raw, catalog_type, slots):
        item_id = _safe_int(raw.get('id') or raw.get('itemId'))
        slot_key = slots[0] if len(slots) == 1 else ('weapon' if any(value.endswith('hand') for value in slots) else '')
        item_class = _safe_int(raw.get('itemClass'))
        item_subclass = _safe_int(raw.get('itemSubClass'))
        inventory_type = _safe_int(raw.get('inventoryType'))
        primary_options = sorted({
            primary
            for stat in (raw.get('stats') or []) if isinstance(stat, dict)
            for primary in PRIMARY_STATS_BY_ITEM_MOD.get(_safe_int(stat.get('id')), ())
        })
        allowable_classes = raw.get('allowableClasses') or raw.get('allowableClassMask')
        if isinstance(allowable_classes, (list, tuple, set)):
            allowable_class_mask = sum(
                1 << (_safe_int(class_id) - 1)
                for class_id in allowable_classes
                if _safe_int(class_id) > 0
            )
        else:
            allowable_class_mask = _safe_int(allowable_classes)
        return {
            'item_id': item_id,
            'name': str(raw.get('name') or raw.get('itemName') or ''),
            'name_zh': '',
            'description_zh': '',
            'icon': str(raw.get('icon') or raw.get('itemIcon') or ''),
            'quality': _safe_int(raw.get('quality')),
            'catalog_type': catalog_type,
            'inventory_type': inventory_type,
            'slot_key': slot_key,
            'item_class_id': item_class,
            'item_subclass_id': item_subclass,
            'armor_type': ARMOR_TYPE_NAMES.get(item_subclass, '') if item_class == 4 and inventory_type in {1, 3, 5, 6, 7, 8, 9, 10, 14, 20} else '',
            'weapon_type': WEAPON_TYPE_NAMES.get(item_subclass, '') if item_class == 2 else '',
            'allowable_class_mask': allowable_class_mask,
            'eligible_specs': [
                f'{identity[0]}:{identity[1]}'
                for spec_id in (raw.get('specs') or [])
                for identity in [SPEC_IDENTITY_MAP.get(_safe_int(spec_id))]
                if identity
            ],
            'unique_group': f'item-{item_id}' if raw.get('uniqueEquipped') else '',
            'simc_token': re.sub(r'[^a-z0-9]+', '_', str(raw.get('name') or raw.get('itemName') or '').lower()).strip('_'),
            'metadata': {
                'raidbots_stats_alloc': raw.get('stats') or [],
                'primary_stat_options': primary_options,
                'two_handed': inventory_type == 17,
                'native_socket_types': [
                    str(row.get('type') or 'PRISMATIC').lower()
                    for row in ((raw.get('socketInfo') or {}).get('sockets') or [])
                ],
            },
            'variants': [],
        }

    @classmethod
    def _add_tier_set_items(cls, by_id, catalyst_items, item_sets, profile, catalyst, raid_ids, instance_by_id):
        """把当前赛季化生目录中的职业套装映射为团本可获取装备。"""
        if not catalyst:
            return
        catalyst_id = _safe_int(catalyst.get('id'))
        sets_by_id = {_safe_int(row.get('id')): row for row in item_sets}
        sources = []
        for raid_id in raid_ids:
            raid = instance_by_id.get(raid_id) or {}
            localized = localize_gear_source({
                'type': 'raid',
                'instance_id': raid_id,
                'instance': str(raid.get('name') or ''),
                'encounter': 'Tier Set / Catalyst',
                'encounter_zh': '职业套装（首领兑换或化生）',
                'difficulty': 'Normal / Heroic / Mythic',
            })
            sources.append(localized)
        for raw in catalyst_items:
            set_id = _safe_int(raw.get('itemSetId'))
            if not set_id or _safe_int(raw.get('expansion')) != 11:
                continue
            if not any(_safe_int(row.get('instanceId')) == catalyst_id for row in raw.get('sources') or []):
                continue
            slots = list(INVENTORY_SLOTS.get(_safe_int(raw.get('inventoryType')), ()))
            if not slots:
                continue
            item = by_id.setdefault(_safe_int(raw.get('id')), cls._base_item(raw, 'equipment', slots))
            item_set = sets_by_id.get(set_id) or {}
            item.setdefault('metadata', {}).update({
                'is_tier_set': True,
                'item_set_id': set_id,
                'item_set_name': str(item_set.get('name') or ''),
            })
            item['effect_refs'] = [
                {
                    'spell_id': _safe_int(row.get('spellId')),
                    'required_items': _safe_int(row.get('reqItems')),
                    'spec_id': _safe_int(row.get('specId')),
                }
                for row in item_set.get('spells') or []
                if _safe_int(row.get('spellId'))
            ]
            cls._add_drop_variants(item, profile, 'raid', sources)

    @staticmethod
    def _highest_quality_embellishments(reagents):
        """同名美化只保留效果数值最高的制造品质。"""
        selected = {}
        for raw in reagents:
            limit = raw.get('itemLimit') or {}
            if _safe_int(raw.get('expansion')) != 11 or _safe_int(limit.get('category')) != 512:
                continue
            name = re.sub(r'\s+', ' ', str(raw.get('name') or raw.get('itemName') or '')).strip().casefold()
            key = (name or str(raw.get('craftingCategoryId') or raw.get('id')), _safe_int(raw.get('craftingCategoryId')))
            score = (_safe_int(raw.get('craftingQuality')), _safe_int(raw.get('quality')), _safe_int(raw.get('id') or raw.get('itemId')))
            previous = selected.get(key)
            if not previous or score > previous[0]:
                selected[key] = (score, raw)
        return [value[1] for value in selected.values()]

    @staticmethod
    def _add_drop_variants(item, profile, source_type, sources, special_mythic=False):
        socket_types = list((item.get('metadata') or {}).get('native_socket_types') or [])
        allowed_tracks = ('champion', 'hero') if source_type == 'delve' else tuple(profile['tracks'])
        for track, levels in profile['tracks'].items():
            if track not in allowed_tracks:
                continue
            for rank, item_level in enumerate(levels, 1):
                item['variants'].append({
                    'key': f'{source_type}-{track}-{rank}-{item_level}',
                    'type': 'drop_equipment', 'item_level': item_level,
                    'upgrade_track': track, 'track_rank': rank, 'track_max_rank': len(levels),
                    'compatible_slots': list(INVENTORY_SLOTS.get(item['inventory_type'], ())),
                    'socket_count': len(socket_types), 'socket_types': socket_types,
                    'sources': sources,
                })
        if special_mythic:
            item['variants'].append({
                'key': f'{source_type}-myth-9-344',
                'type': 'drop_equipment', 'item_level': 344,
                'upgrade_track': 'myth', 'track_rank': 9, 'track_max_rank': 6,
                'compatible_slots': list(INVENTORY_SLOTS.get(item['inventory_type'], ())),
                'socket_count': len(socket_types), 'socket_types': socket_types,
                'sources': [localize_gear_source(dict(row, difficulty='Mythic · Myth 9/6')) for row in sources],
                'metadata': {'special_mythic_drop': True},
            })

    @staticmethod
    def _add_crafted_variants(item, profile, sources):
        socket_types = list((item.get('metadata') or {}).get('native_socket_types') or [])
        for tier, levels in profile['crafted'].items():
            for quality, item_level in enumerate(levels, 1):
                item['variants'].append({
                    'key': f'crafted-{tier}-q{quality}-{item_level}',
                    'type': 'crafted_equipment', 'item_level': item_level, 'crafting_quality': quality,
                    'compatible_slots': list(INVENTORY_SLOTS.get(item['inventory_type'], ())),
                    'socket_count': len(socket_types), 'socket_types': socket_types,
                    'crafting_options': {'stat_count': 2, 'stat_pool': ['crit', 'haste', 'mastery', 'versatility']},
                    'sources': sources,
                })

    @staticmethod
    def _highest_quality_enhancements(enchantments):
        """只保留当前资料片成品的高阶品质及最高制造星级。"""
        selected = {}
        for raw in enchantments:
            if _safe_int(raw.get('expansion')) != 11 or not _safe_int(raw.get('itemId')):
                continue
            if _safe_int(raw.get('quality')) < 3:
                continue
            token = re.sub(r'_\d+$', '', str(raw.get('tokenizedName') or raw.get('itemName') or raw.get('itemId')).lower())
            key = ('gem' if raw.get('slot') == 'socket' else 'enchant', token, str(raw.get('categoryName') or ''))
            score = (_safe_int(raw.get('craftingQuality')), _safe_int(raw.get('quality')), _safe_int(raw.get('itemId')))
            previous = selected.get(key)
            if not previous or score > previous[0]:
                selected[key] = (score, raw)
        return [value[1] for value in selected.values()]

    @staticmethod
    def _raidbots_stats(rows):
        stats = defaultdict(int)
        primary = 0
        for row in rows:
            amount = _safe_int(row.get('amount'))
            stat_type = str(row.get('type') or '')
            if stat_type in ('stragiint', 'stragi'):
                primary = max(primary, amount)
            elif stat_type in RAIDBOTS_STATS:
                stats[RAIDBOTS_STATS[stat_type]] += amount
        return dict(stats), primary

    @staticmethod
    def _enchant_slots(raw):
        category = str(raw.get('categoryName') or '').lower()
        by_category = {
            'chest enchants': ['chest'], 'helm enchants': ['head'], 'boot enchants': ['feet'],
            'rings enchants': ['finger'], 'shoulder enchants': ['shoulders'],
            'weapon enchants': ['weapon'],
        }
        if category in by_category:
            return by_category[category]
        mask = _safe_int((raw.get('equipRequirements') or {}).get('invTypeMask'))
        slots = []
        for inventory_type, values in INVENTORY_SLOTS.items():
            if mask & (1 << inventory_type):
                slots.extend(values)
        return list(dict.fromkeys(slots))

    def _enrich_wowhead(self, items, game_build):
        requests_needed = {}
        for item in items:
            for variant in item.get('variants') or []:
                item_level = _safe_int(variant.get('item_level'))
                requests_needed[(item['item_id'], item_level)] = None
        total = len(requests_needed)
        self.progress(f'正在从 Wowhead 补全 {total} 组中文 Tooltip/装等属性（结果会缓存）……')
        cache_dir = self.cache_root / game_build / 'wowhead'
        cache_dir.mkdir(parents=True, exist_ok=True)
        completed = 0
        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {executor.submit(self._wowhead_tooltip, item_id, item_level, cache_dir): (item_id, item_level) for item_id, item_level in requests_needed}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    requests_needed[key] = future.result()
                except CatalogSourceError:
                    requests_needed[key] = {}
                completed += 1
                if completed % 250 == 0 or completed == total:
                    self.progress(f'Wowhead 补全进度 {completed}/{total}')
        for item in items:
            fallback = {}
            for variant in item.get('variants') or []:
                details = requests_needed.get((item['item_id'], _safe_int(variant.get('item_level')))) or {}
                fallback = fallback or details
                if details.get('stats'):
                    variant['stats'] = details['stats']
                if details.get('effects'):
                    variant['effects'] = details['effects']
                if details.get('primary_options'):
                    variant.setdefault('metadata', {})['primary_stat_values'] = details['primary_options']
                if details.get('secondary_total') and variant.get('type') == 'crafted_equipment':
                    variant.setdefault('crafting_options', {})['secondary_total'] = details['secondary_total']
            details = fallback or requests_needed.get((item['item_id'], 0)) or {}
            item['name_zh'] = details.get('name_zh') or item.get('name_zh') or ''
            item['description_zh'] = details.get('description_zh') or item.get('description_zh') or ''
            item['icon'] = details.get('icon') or item.get('icon') or ''
            item['quality'] = details.get('quality') or item.get('quality') or 0

    def _wowhead_tooltip(self, item_id, item_level, cache_dir):
        path = cache_dir / f'{item_id}-{item_level or "base"}.json'
        if path.is_file():
            try:
                return _tooltip_details(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, json.JSONDecodeError):
                pass
        params = {'locale': 'zhcn'}
        if item_level:
            params['ilvl'] = item_level
        url = f'{WOWHEAD_TOOLTIP.format(item_id=item_id)}?{urlencode(params)}'
        payload = self._get_json(url)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return _tooltip_details(payload)
