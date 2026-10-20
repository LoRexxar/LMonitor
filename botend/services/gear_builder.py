"""职业配装器目录查询、制造解析与 SimC 映射服务。"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from math import floor

from django.db.models import Exists, OuterRef, Q

from botend.journal_models import JournalEncounter, JournalState

from botend.constants.wow import (
    CLASS_CN,
    CLASS_SPEC_MAP,
    SPEC_CN,
    SPEC_ICON,
    SPEC_ROLE,
    canonical_class_spec,
    localize_gear_source,
)
from botend.models import (
    SeasonMeta,
    SimcMasteryCoefficient,
    SimcSecondaryStatRule,
    WowItemVariantSnapshot,
    WowWagoMonitorState,
)
from botend.services.simc_player_config import (
    EQUIPMENT_SLOT_ALIASES as SIMC_EQUIPMENT_SLOT_ALIASES,
    canonical_simc_profile_key,
    parse_simc_player_profile,
    secondary_rule_class_key,
    simc_spec_slug,
)
from botend.services.wow_item_display import item_display_metadata
from botend.services.gear_builder_tier_sources import tier_set_source_catalog, tier_set_sources


EQUIPMENT_SLOTS = (
    ('head', '头部'),
    ('neck', '颈部'),
    ('shoulders', '肩部'),
    ('back', '背部'),
    ('chest', '胸部'),
    ('wrists', '腕部'),
    ('hands', '手部'),
    ('waist', '腰部'),
    ('legs', '腿部'),
    ('feet', '脚部'),
    ('finger1', '戒指1'),
    ('finger2', '戒指2'),
    ('trinket1', '饰品1'),
    ('trinket2', '饰品2'),
    ('main_hand', '主手'),
    ('off_hand', '副手'),
)
SLOT_LABELS = dict(EQUIPMENT_SLOTS)
# The shared SimC parser owns SimC spelling; the builder owns its UI slots.
# Adapt only at the builder boundary, without changing either canonical set.
SLOT_SIMC_KEYS = {key: SIMC_EQUIPMENT_SLOT_ALIASES.get(key, key) for key, _label in EQUIPMENT_SLOTS}
SLOT_ALIASES = {simc_key: key for key, simc_key in SLOT_SIMC_KEYS.items() if key != simc_key}


def canonical_equipment_slot(value):
    """Translate known SimC aliases only; callers still validate slot membership."""
    slot = str(value or '')
    return SLOT_ALIASES.get(slot, slot)


SLOT_FAMILIES = {
    'finger1': 'finger', 'finger2': 'finger',
    'trinket1': 'trinket', 'trinket2': 'trinket',
    'main_hand': 'weapon', 'off_hand': 'weapon',
}
STAT_KEYS = (
    'strength', 'agility', 'intellect', 'stamina', 'armor', 'bonus_armor',
    'crit', 'haste', 'mastery', 'versatility', 'leech', 'avoidance', 'speed',
    'weapon_dps', 'min_damage', 'max_damage',
)
STAT_LABELS = {
    'strength': '力量', 'agility': '敏捷', 'intellect': '智力', 'stamina': '耐力',
    'armor': '护甲', 'bonus_armor': '额外护甲', 'crit': '暴击', 'haste': '急速',
    'mastery': '精通', 'versatility': '全能', 'leech': '吸血',
    'avoidance': '闪避', 'speed': '速度', 'weapon_dps': '武器秒伤',
    'min_damage': '最低伤害', 'max_damage': '最高伤害',
}
UPGRADE_TRACK_LABELS = {'champion': '勇士', 'hero': '英雄', 'myth': '神话'}
ADDITIONAL_SOCKET_SLOTS = {'head', 'wrists', 'waist'}
JEWELRY_INVENTORY_TYPES = {2, 11}
CLASS_MASKS = {
    'warrior': 1, 'paladin': 2, 'hunter': 4, 'rogue': 8, 'priest': 16,
    'deathknight': 32, 'shaman': 64, 'mage': 128, 'warlock': 256,
    'monk': 512, 'druid': 1024, 'demonhunter': 2048, 'evoker': 4096,
}
INTELLECT_SPECS = {
    'Paladin:Holy', 'Priest:Discipline', 'Priest:Holy', 'Priest:Shadow',
    'Shaman:Elemental', 'Shaman:Restoration', 'Mage:Arcane', 'Mage:Fire', 'Mage:Frost',
    'Warlock:Affliction', 'Warlock:Demonology', 'Warlock:Destruction',
    'Monk:Mistweaver', 'Druid:Balance', 'Druid:Restoration',
    'DemonHunter:Devourer',
    'Evoker:Devastation', 'Evoker:Preservation', 'Evoker:Augmentation',
}
AGILITY_CLASSES = {'Hunter', 'Rogue', 'DemonHunter'}
AGILITY_SPECS = {
    'Shaman:Enhancement', 'Monk:Brewmaster', 'Monk:Windwalker',
    'Druid:Feral', 'Druid:Guardian',
}
PRIMARY_STAT_KEYS = {'strength', 'agility', 'intellect'}
PRIMARY_STATS_BY_ITEM_MOD = {
    3: {'agility'}, 4: {'strength'}, 5: {'intellect'},
    71: {'strength', 'agility', 'intellect'},
    72: {'strength', 'agility'}, 73: {'agility', 'intellect'}, 74: {'strength', 'intellect'},
}
ARMOR_SUBCLASS_BY_CLASS = {
    'Mage': 1, 'Priest': 1, 'Warlock': 1,
    'DemonHunter': 2, 'Druid': 2, 'Monk': 2, 'Rogue': 2,
    'Evoker': 3, 'Hunter': 3, 'Shaman': 3,
    'DeathKnight': 4, 'Paladin': 4, 'Warrior': 4,
}
PRIMARY_ARMOR_INVENTORY_TYPES = {1, 3, 5, 6, 7, 8, 9, 10, 20}
WEAPON_SUBCLASSES_BY_CLASS = {
    'DeathKnight': {0, 1, 4, 5, 6, 7, 8},
    'DemonHunter': {0, 7, 9, 13, 15},
    'Druid': {4, 5, 6, 10, 13, 15},
    'Evoker': {4, 7, 10, 15},
    'Hunter': {0, 1, 2, 3, 6, 7, 8, 10, 13, 15, 18},
    'Mage': {7, 10, 15, 19},
    'Monk': {0, 4, 6, 7, 10, 13},
    'Paladin': {0, 1, 4, 5, 6, 7, 8},
    'Priest': {4, 10, 15, 19},
    'Rogue': {0, 4, 7, 13, 15},
    'Shaman': {0, 4, 10, 13, 15},
    'Warlock': {7, 10, 15, 19},
    'Warrior': {0, 1, 4, 5, 6, 7, 8, 13, 15},
}
SPEC_WEAPON_INVENTORY_TYPES = {
    'Warrior:Arms': {17}, 'Warrior:Fury': {17}, 'Warrior:Protection': {13, 21},
    'Paladin:Holy': {13, 17, 21}, 'Paladin:Protection': {13, 21}, 'Paladin:Retribution': {17},
    'DeathKnight:Blood': {17}, 'DeathKnight:Frost': {13, 17, 21}, 'DeathKnight:Unholy': {17},
    'Hunter:BeastMastery': {15, 26}, 'Hunter:Marksmanship': {15, 26}, 'Hunter:Survival': {13, 17, 21},
    'Rogue:Assassination': {13, 21}, 'Rogue:Outlaw': {13, 21}, 'Rogue:Subtlety': {13, 21},
    'DemonHunter:Havoc': {13, 21}, 'DemonHunter:Vengeance': {13, 21}, 'DemonHunter:Devourer': {13, 21},
    'Shaman:Enhancement': {13, 21}, 'Shaman:Elemental': {13, 17, 21}, 'Shaman:Restoration': {13, 17, 21},
    'Monk:Brewmaster': {13, 17, 21}, 'Monk:Windwalker': {13, 17, 21}, 'Monk:Mistweaver': {13, 17, 21},
    'Druid:Balance': {13, 17, 21}, 'Druid:Feral': {17}, 'Druid:Guardian': {17}, 'Druid:Restoration': {13, 17, 21},
    'Mage:Arcane': {13, 17, 21, 26}, 'Mage:Fire': {13, 17, 21, 26}, 'Mage:Frost': {13, 17, 21, 26},
    'Priest:Discipline': {13, 17, 21, 26}, 'Priest:Holy': {13, 17, 21, 26}, 'Priest:Shadow': {13, 17, 21, 26},
    'Warlock:Affliction': {13, 17, 21, 26}, 'Warlock:Demonology': {13, 17, 21, 26}, 'Warlock:Destruction': {13, 17, 21, 26},
    'Evoker:Devastation': {13, 17, 21}, 'Evoker:Preservation': {13, 17, 21}, 'Evoker:Augmentation': {13, 17, 21},
}
# 仅靠职业武器白名单和单双手类型无法表达专精技能的实际要求。
# 例如狂徒主手不能使用匕首、奇袭需要双匕首，而《午夜》的生存猎同时支持
# 双持单手武器和传统双手武器。这里按专精与槽位收紧武器子类型，未列出的
# 专精继续沿用职业白名单。
SPEC_WEAPON_SUBCLASSES_BY_SLOT = {
    'DemonHunter:Havoc': {
        'main_hand': {0, 7, 9, 13}, 'off_hand': {0, 7, 9, 13},
    },
    'DemonHunter:Vengeance': {
        'main_hand': {0, 7, 9, 13}, 'off_hand': {0, 7, 9, 13},
    },
    'DemonHunter:Devourer': {
        'main_hand': {0, 7, 9, 13, 15}, 'off_hand': {0, 7, 9, 13, 15},
    },
    'Hunter:Survival': {
        'main_hand': {0, 1, 6, 7, 10, 15}, 'off_hand': {0, 7, 15},
    },
    'Rogue:Assassination': {
        'main_hand': {15}, 'off_hand': {15},
    },
    'Rogue:Outlaw': {
        'main_hand': {0, 4, 7, 13}, 'off_hand': {0, 4, 7, 13, 15},
    },
    'Rogue:Subtlety': {
        'main_hand': {15}, 'off_hand': {0, 4, 7, 13, 15},
    },
    'Shaman:Enhancement': {
        'main_hand': {0, 4, 13}, 'off_hand': {0, 4, 13},
    },
}
DUAL_WIELD_SPECS = {
    'Warrior:Fury', 'DeathKnight:Frost', 'Rogue:Assassination', 'Rogue:Outlaw', 'Rogue:Subtlety',
    'DemonHunter:Havoc', 'DemonHunter:Vengeance', 'DemonHunter:Devourer', 'Hunter:Survival',
    'Shaman:Enhancement',
    'Monk:Brewmaster', 'Monk:Windwalker',
}
SHIELD_SPECS = {'Warrior:Protection', 'Paladin:Holy', 'Paladin:Protection', 'Shaman:Elemental', 'Shaman:Restoration'}
HELD_OFFHAND_SPECS = INTELLECT_SPECS - {'Paladin:Holy', 'Shaman:Elemental', 'Shaman:Restoration'}
QUICK_SOURCE_FILTERS = {
    'mythic_plus': {'mythic_plus', 'great_vault'},
    'raid': {'raid'},
    'delve': {'delve'},
}
FILTERABLE_SECONDARY_STATS = {'crit', 'haste', 'mastery', 'versatility'}


class GearBuilderError(ValueError):
    def __init__(self, message, *, issues=None):
        super().__init__(message)
        self.issues = issues or []


def _number(value, default=0):
    try:
        parsed = float(value)
        return int(parsed) if parsed.is_integer() else parsed
    except (TypeError, ValueError):
        return default


def normalize_stats(raw):
    """将导入器允许的扁平或分组属性统一成前端可累加结构。"""
    raw = raw if isinstance(raw, dict) else {}
    result = OrderedDict()
    for key in STAT_KEYS:
        if key in raw:
            result[key] = _number(raw.get(key))
    for group_name in ('primary', 'secondary', 'tertiary', 'weapon'):
        group = raw.get(group_name)
        if not isinstance(group, dict):
            continue
        for key, value in group.items():
            if key not in STAT_KEYS:
                continue
            if isinstance(value, dict):
                value = value.get('rating', value.get('value', 0))
            result[key] = _number(value)
    return {key: value for key, value in result.items() if value}


def stats_for_identity(raw, metadata, class_name='', spec_name=''):
    """把可随职业变化的主属性映射到当前职业专精，避免同时累计三种主属性。"""
    stats = normalize_stats(raw)
    identity = f'{class_name}:{spec_name}'
    if identity in INTELLECT_SPECS:
        primary = 'intellect'
    elif class_name in AGILITY_CLASSES or identity in AGILITY_SPECS:
        primary = 'agility'
    else:
        primary = 'strength'
    for key in PRIMARY_STAT_KEYS - {primary}:
        stats.pop(key, None)
    metadata = metadata if isinstance(metadata, dict) else {}
    values = metadata.get('primary_stat_values') if isinstance(metadata.get('primary_stat_values'), dict) else {}
    amount = values.get(primary) or metadata.get('primary_stat_amount') or 0
    if amount:
        stats[primary] = _number(amount)
    return stats


def active_season():
    active = SeasonMeta.objects.filter(is_active=True).annotate(
        has_gear_catalog=Exists(
            WowItemVariantSnapshot.objects.filter(
                season_id=OuterRef('pk'),
                batch_key=OuterRef('gear_batch_key'),
            ),
        ),
    )
    # 多条历史记录被误标为 active 时，优先选真实存在的已激活装备批次；
    # 不再依赖 updated_at，避免赛季监控的普通刷新改变页面所读赛季。
    return (
        active.filter(has_gear_catalog=True).order_by('-gear_synced_at', '-id').first()
        or active.order_by('-id').first()
    )


def _catalog_exists(season):
    if not season or not season.gear_batch_key:
        return False
    annotated = getattr(season, 'has_gear_catalog', None)
    if annotated is not None:
        return bool(annotated)
    return WowItemVariantSnapshot.objects.filter(
        season_id=season.pk,
        batch_key=season.gear_batch_key,
    ).exists()


def catalog_context(season=None):
    season = season or active_season()
    monitor = WowWagoMonitorState.objects.filter(branch='wow', locale='enUS', is_active=True).first()
    if not season:
        return {
            'available': False, 'season': None, 'batch_key': '',
            'game_build': str(getattr(monitor, 'build', '') or ''),
            'sync_status': 'missing_season', 'synced_at': None, 'sync_report': {},
        }
    available = _catalog_exists(season)
    return {
        'available': available,
        'season': {
            'id': season.id,
            'key': season.season_key,
            'name': season.season_name,
        },
        'batch_key': season.gear_batch_key,
        'game_build': season.game_build or str(getattr(monitor, 'build', '') or ''),
        'sync_status': (
            season.gear_sync_status if available
            else ('missing_batch' if season.gear_batch_key else 'not_synced')
        ),
        'synced_at': season.gear_synced_at.isoformat() if season.gear_synced_at else None,
        'sync_report': season.gear_sync_report or {},
    }


def specs_payload():
    payload = []
    for class_name, spec_names in CLASS_SPEC_MAP.items():
        payload.append({
            'key': class_name,
            'name': CLASS_CN.get(class_name, class_name),
            'specs': [{
                'key': spec_name,
                'name': SPEC_CN.get(spec_name, spec_name),
                'role': SPEC_ROLE.get((class_name, spec_name), 'dps'),
                'icon': SPEC_ICON.get((class_name, spec_name), ''),
            } for spec_name in spec_names],
        })
    return payload


def secondary_stat_conversion_rules():
    """按职业/专精读取现有绿字比例及精通系数，生成前端只读换算表。"""
    fields = ('crit_per_percent', 'haste_per_percent', 'mastery_per_percent', 'versatility_per_percent')
    defaults = {
        field: float(SimcSecondaryStatRule._meta.get_field(field).get_default())
        for field in fields
    }
    class_rules = {
        str(row.class_name or '').casefold(): row
        for row in SimcSecondaryStatRule.objects.all()
    }
    mastery_rules = {
        str(row.spec or '').casefold(): float(row.mastery_coefficient or 1)
        for row in SimcMasteryCoefficient.objects.all()
    }
    payload = {}
    for class_name, spec_names in CLASS_SPEC_MAP.items():
        class_rule = class_rules.get(secondary_rule_class_key(class_name).casefold())
        ratios = {
            field: float(getattr(class_rule, field, defaults[field]) or defaults[field])
            for field in fields
        }
        for spec_name in spec_names:
            full_key = canonical_simc_profile_key(spec_name, class_name).casefold()
            short_key = simc_spec_slug(spec_name).casefold()
            payload[f'{class_name}:{spec_name}'] = {
                **ratios,
                'mastery_coefficient': mastery_rules.get(full_key, mastery_rules.get(short_key, 1.0)),
            }
    return payload


def raid_boss_numbers(season):
    """按团本分别编号，优先采用活动冒险手册，排除小怪等非首领来源。"""
    groups = defaultdict(dict)
    for row in (season.raid_encounters or []) if season else []:
        if not isinstance(row, dict):
            continue
        try:
            instance_id = int(row.get('sourceInstanceId') or season.raid_zone_id or 0)
            encounter_id = int(row.get('id') or 0)
            order = int(row.get('order', row.get('index', 0)) or 0)
        except (TypeError, ValueError):
            continue
        if instance_id > 0 and encounter_id > 0:
            groups[instance_id][encounter_id] = order
    journal_groups = defaultdict(dict)
    release = JournalState.objects.filter(pk='wow-zhCN').values('active_release_id')[:1]
    for row in JournalEncounter.objects.filter(
        instance__release_id=release, instance__kind='raid',
    ).values('instance__journal_id', 'journal_id', 'order'):
        journal_groups[row['instance__journal_id']][row['journal_id']] = row['order']
    groups.update(journal_groups)
    return {
        str(instance_id): {
            str(encounter_id): index
            for index, (encounter_id, _order) in enumerate(
                sorted(encounters.items(), key=lambda row: (row[1], row[0])), 1,
            )
        }
        for instance_id, encounters in groups.items()
    }


def bootstrap_payload():
    season = active_season()
    sync_report = season.gear_sync_report if season and isinstance(season.gear_sync_report, dict) else {}
    catalog_rules = sync_report.get('catalog_rules') if isinstance(sync_report.get('catalog_rules'), dict) else {}
    # 旧版 Tooltip 补齐命令会覆盖 catalog_rules；仅恢复当前已知版本的规则。
    # 显式配置的空列表仍表示禁用，不能被默认值覆盖。
    if 'socket_additions' not in catalog_rules and season and str(season.game_build or '').startswith('12.1.'):
        catalog_rules = {
            **catalog_rules,
            'socket_additions': [
                {'slot': slot, 'max_additional': 1, 'source': 'socket_item'}
                for slot in ('head', 'wrists', 'waist')
            ],
        }
    catalog_rules = {
        **catalog_rules,
        'socket_additions': [
            row for row in (catalog_rules.get('socket_additions') or [])
            if isinstance(row, dict) and str(row.get('slot') or '') in ADDITIONAL_SOCKET_SLOTS
        ],
    }
    return {
        'catalog': catalog_context(season),
        'classes': specs_payload(),
        'raid_boss_numbers': raid_boss_numbers(season),
        'tier_set_sources': tier_set_source_catalog(),
        'slots': [
            {'key': key, 'label': label, 'family': SLOT_FAMILIES.get(key, key), 'simc_key': SLOT_SIMC_KEYS[key]}
            for key, label in EQUIPMENT_SLOTS
        ],
        'slot_aliases': dict(SLOT_ALIASES),
        'stats': [{'key': key, 'label': label} for key, label in STAT_LABELS.items()],
        'upgrade_tracks': [{'key': key, 'label': label} for key, label in UPGRADE_TRACK_LABELS.items()],
        'rules': {
            'state_version': 1,
            'share_version': 4,
            'max_share_length': 8000,
            'crafted_secondary_count': 2,
            'temporary_enchants_supported': False,
            'secondary_stat_conversion': secondary_stat_conversion_rules(),
            **catalog_rules,
        },
    }


def canonical_spec(class_name, spec_name):
    identity = canonical_class_spec(class_name, spec_name)
    if not identity:
        raise GearBuilderError('未知职业或专精')
    return identity


def _variant_item(variant):
    from botend.services.wow_item_identity import variant_identity, project_item_identity
    from botend.services.wow_data_branch import branch_item, variant_branch
    item = branch_item(variant.item, variant_branch(variant))
    if (variant.item.metadata or {}).get('branch_display', {}).get(variant_branch(variant)):
        return item
    return project_item_identity(item, variant_identity(item, variant))


def slot_matches(variant, slot, class_name='', spec_name=''):
    family = SLOT_FAMILIES.get(slot, slot)
    compatible = [str(value) for value in (variant.compatible_slots or []) if value]
    if variant.variant_type == WowItemVariantSnapshot.TYPE_EMBELLISHMENT:
        # Weapon reagents describe the weapon family, not the equipped hand.
        # The carrier's slot/spec and recipe intersection are checked separately.
        return bool(compatible) and (slot in compatible or family in compatible
            or (slot == 'off_hand' and 'main_hand' in compatible))
    item = _variant_item(variant)
    item_slot = str(item.slot_key or '')
    if getattr(item, '_resolved_identity', None):
        from botend.services.gear_builder_catalog_source import INVENTORY_SLOTS
        compatible = INVENTORY_SLOTS.get(item.inventory_type, ())
        matched = slot in compatible or family in compatible
    else:
        matched = not compatible or slot in compatible or family in compatible or item_slot in (slot, family)
    if not matched and slot == 'off_hand' and f'{class_name}:{spec_name}' == 'Warrior:Fury':
        matched = int(item.item_class_id or 0) == 2 and int(item.inventory_type or 0) == 17
    return matched


def embellishment_eligibility_reason(equipment, embellishment, slot, class_name='', spec_name=''):
    """Authorize from central recipe/reagent facts; None means proven compatible.

    Slot families alone cannot distinguish professions or recipes. Missing facts
    are unknown, never an implicit universal reagent. This performs no queries.
    """
    unknown = {'code': 'embellishment_unknown', 'reason': '美化或制造配方关系资料不足，无法确认适用性'}
    incompatible = {'code': 'embellishment_incompatible', 'reason': '所选美化与该制造装备不兼容'}
    if equipment is None or embellishment is None:
        return unknown
    if (equipment.variant_type != WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT
            or equipment.is_intrinsic_embellishment
            or embellishment.variant_type != WowItemVariantSnapshot.TYPE_EMBELLISHMENT):
        return incompatible
    from botend.services.wow_data_branch import variant_branch
    if variant_branch(equipment) != variant_branch(embellishment):
        return unknown
    if not embellishment.compatible_slots:
        return unknown
    if (not slot_matches(embellishment, slot, class_name, spec_name)
            or not slot_matches(equipment, slot, class_name, spec_name)):
        return incompatible
    item_metadata = equipment.item.metadata or {}
    reagent_metadata = embellishment.metadata or {}
    def ids(values):
        if not isinstance(values, (list, tuple)):
            return set()
        return {int(value) for value in values if str(value).isdigit() and int(value) > 0}
    recipe_slots = ids(item_metadata.get('crafting_reagent_slot_ids'))
    reagent_slots = ids(reagent_metadata.get('reagent_slot_ids'))
    if not recipe_slots or not reagent_slots:
        return unknown
    return None if recipe_slots.intersection(reagent_slots) else incompatible


def _expected_primary_stat(class_name, spec_name):
    identity = f'{class_name}:{spec_name}'
    if identity in INTELLECT_SPECS:
        return 'intellect'
    if class_name in AGILITY_CLASSES or identity in AGILITY_SPECS:
        return 'agility'
    return 'strength'


def _item_primary_options(item, variant=None):
    options = set()
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    options.update(str(value) for value in (metadata.get('primary_stat_options') or []) if value)
    for row in metadata.get('raidbots_stats_alloc') or []:
        if isinstance(row, dict):
            options.update(PRIMARY_STATS_BY_ITEM_MOD.get(int(row.get('id') or 0), set()))
    if variant:
        variant_metadata = variant.metadata if isinstance(variant.metadata, dict) else {}
        values = variant_metadata.get('primary_stat_values') if isinstance(variant_metadata.get('primary_stat_values'), dict) else {}
        options.update(key for key, value in values.items() if key in PRIMARY_STAT_KEYS and _number(value))
        options.update(key for key, value in normalize_stats(variant.stats_json).items() if key in PRIMARY_STAT_KEYS and _number(value))
    return options


def spec_matches(item, class_name, spec_name, variant=None, slot='', *, require_loot_spec=True):
    """Keep acquisition filtering optional; loot specs are not equip restrictions."""
    if not getattr(item, '_resolved_identity', None) and variant is not None:
        item = _variant_item(variant)
    class_mask = int(item.allowable_class_mask or 0)
    expected_mask = CLASS_MASKS.get(str(class_name or '').casefold(), 0)
    if class_mask > 0 and expected_mask and not class_mask & expected_mask:
        return False
    eligible = [str(value).casefold() for value in (item.eligible_specs or []) if value]
    identity = f'{class_name}:{spec_name}'
    if require_loot_spec and eligible:
        candidates = {
            identity.casefold(),
            f'{class_name}_{spec_name}'.casefold(),
            spec_name.casefold(),
        }
        if not candidates.intersection(eligible):
            return False

    item_class = int(item.item_class_id or 0)
    subclass = int(item.item_subclass_id or 0)
    inventory_type = int(item.inventory_type or 0)
    if item_class == 4 and inventory_type in PRIMARY_ARMOR_INVENTORY_TYPES:
        if subclass in {1, 2, 3, 4} and subclass != ARMOR_SUBCLASS_BY_CLASS.get(class_name):
            return False
    if item_class == 4 and subclass == 6 and identity not in SHIELD_SPECS:
        return False
    if item_class == 4 and inventory_type == 23 and identity not in HELD_OFFHAND_SPECS:
        return False
    if item_class == 2:
        if subclass not in WEAPON_SUBCLASSES_BY_CLASS.get(class_name, set()):
            return False
        allowed_inventory = SPEC_WEAPON_INVENTORY_TYPES.get(identity)
        if allowed_inventory and inventory_type not in allowed_inventory:
            return False
        allowed_subclasses = SPEC_WEAPON_SUBCLASSES_BY_SLOT.get(identity, {}).get(slot)
        if allowed_subclasses and subclass not in allowed_subclasses:
            return False
        if slot == 'off_hand' and inventory_type in {13, 17} and identity not in DUAL_WIELD_SPECS:
            return False

    primary_options = _item_primary_options(item, variant)
    if primary_options and _expected_primary_stat(class_name, spec_name) not in primary_options:
        return False
    return True


def _source_matches(variant, source_type):
    if not source_type or source_type == 'all':
        return True
    return any(str(row.get('type') or '').casefold() == source_type.casefold()
               for row in (variant.source_json or []) if isinstance(row, dict))


def _filter_values(raw_values, allowed):
    values = [raw_values] if isinstance(raw_values, str) else list(raw_values or [])
    parsed = {
        value.strip().casefold()
        for raw in values
        for value in str(raw or '').split(',')
        if value.strip()
    }
    return parsed.intersection(allowed)


def _source_is_excluded(variant, excluded_sources):
    source_types = {
        str(row.get('type') or '').casefold()
        for row in (variant.source_json or [])
        if isinstance(row, dict) and row.get('type')
    }
    return any(source_types.intersection(QUICK_SOURCE_FILTERS[group]) for group in excluded_sources)


def _secondary_stat_is_excluded(variant, excluded_stats):
    stats = normalize_stats(variant.stats_json)
    return any(_number(stats.get(key)) for key in excluded_stats)


def _source_track_is_valid(variant, *, source_types=None):
    """地下堡饰品不开放神话；其他部位仍须有本赛季解锁依据。"""
    if source_types is None:
        source_types = {
            str(row.get('type') or '').casefold()
            for row in (variant.source_json or [])
            if isinstance(row, dict) and row.get('type')
        }
    return (not (source_types == {'delve'} and str(variant.upgrade_track or '').casefold() == 'myth')
            or (int(_variant_item(variant).inventory_type or 0) != 12
                and bool((variant.metadata or {}).get('delve_myth'))))


def serialize_variant(variant, class_name='', spec_name=''):
    item = _variant_item(variant)
    metadata = {**(item.metadata or {}), **(variant.metadata or {})}
    metadata.setdefault('two_handed', int(item.inventory_type or 0) == 17)
    socket_types = list(variant.socket_types or [])
    socket_count = max(int(variant.socket_count or 0), len(socket_types))
    if int(item.inventory_type or 0) in JEWELRY_INVENTORY_TYPES:
        if not metadata.get('jewelry_socket_baseline_applied') and not (
            int(item.inventory_type or 0) == 2
            and variant.variant_type == WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT
        ):
            # 旧掉落数据只保存额外天然孔；制造项链源数据已包含基础孔。
            socket_count += 1
        socket_count = min(2, max(1, socket_count))
        while len(socket_types) < socket_count:
            socket_types.append('prismatic')
        socket_types = socket_types[:socket_count]
        metadata['jewelry_socket_baseline_applied'] = True
    stats = stats_for_identity(variant.stats_json, variant.metadata, class_name, spec_name)
    sources = tier_set_sources(metadata, item.slot_key)
    if sources is None:
        sources = [localize_gear_source(row) for row in (variant.source_json or []) if isinstance(row, dict)]
    display = item_display_metadata(
        item.item_id, item, item_level=variant.item_level, variant=variant,
        stats=stats, sources=sources,
    )
    return {
        'id': variant.id,
        'batch_key': variant.batch_key,
        'key': variant.variant_key,
        'type': variant.variant_type,
        'item_id': item.item_id,
        'item_level': variant.item_level,
        'track': variant.upgrade_track,
        'track_label': UPGRADE_TRACK_LABELS.get(variant.upgrade_track, variant.upgrade_track),
        'track_rank': variant.track_rank,
        'track_max_rank': variant.track_max_rank,
        'crafting_quality': variant.crafting_quality,
        'bonus_ids': variant.bonus_ids or [],
        'game_build': variant.game_build,
        'data_branch': variant.data_branch,
        'item_identity': getattr(item, '_resolved_identity', None),
        'compatible_slots': ([item.slot_key] if getattr(item, '_resolved_identity', None)
                             else variant.compatible_slots or []),
        'socket_types': socket_types,
        'socket_count': socket_count,
        'stats': stats,
        'effects': display['effect_details'],
        'effects_status': display['effects_status'],
        'text_schema_version': 2,
        'effects_missing': display['effects_missing'],
        'sources': sources,
        'crafting_options': variant.crafting_options or {},
        'unique_group': variant.unique_group or item.unique_group,
        'max_equipped': variant.max_equipped,
        'is_intrinsic_embellishment': variant.is_intrinsic_embellishment,
        'metadata': metadata,
        'tooltip': display['tooltip'],
        'tooltip_complete': display['tooltip_complete'],
    }


def serialize_item(item, variants, class_name='', spec_name=''):
    first_variant = variants[0] if variants else None
    if first_variant:
        item = _variant_item(first_variant)
    display = item_display_metadata(item.item_id, item, icon_size='medium', variant=first_variant,
        stats=stats_for_identity(first_variant.stats_json, first_variant.metadata, class_name, spec_name) if first_variant else None)
    return {
        'item_id': item.item_id,
        'name': display['display_name'],
        'name_en': item.name or '',
        'description': display['localized_description'],
        'text_schema_version': 2,
        'display_description': display['display_description'],
        'icon': item.icon or '',
        'icon_url': display['icon_url'],
        'quality': item.quality,
        'catalog_type': item.catalog_type,
        'slot': item.slot_key,
        'armor_type': item.armor_type,
        'weapon_type': item.weapon_type,
        'unique_group': item.unique_group,
        'simc_token': item.simc_token,
        'enchantment_id': item.enchantment_id,
        'metadata': item.metadata or {},
        'wowhead_url': display['wowhead_url'],
        'variants': [serialize_variant(variant, class_name, spec_name) for variant in variants],
    }


def _catalog_queryset(season, variant_types, query=''):
    qs = WowItemVariantSnapshot.objects.filter(
        season=season,
        batch_key=season.gear_batch_key,
        variant_type__in=variant_types,
    ).select_related('item')
    query = str(query or '').strip()
    if query:
        lookup = Q(item__name__icontains=query) | Q(item__name_zh__icontains=query)
        if query.isdigit():
            lookup |= Q(item__item_id=int(query))
        qs = qs.filter(lookup)
    return qs.order_by('-item_level', 'item__name_zh', 'item__name', 'variant_key')


def _is_ptr_equipment(variant):
    """识别 PTR，兼容分支字段及旧目录中的预览标记。"""
    metadata = variant.metadata or {}
    return (variant.data_branch == 'ptr' or metadata.get('data_branch') == 'ptr'
            or bool(metadata.get('ptr_preview')))


def _is_journal_display_variant(variant):
    """手册专用难度变体只服务掉落展示，不参与配装目录的分支推断与选择。"""
    return isinstance(variant.metadata, dict) and 'difficulty_drop' in variant.metadata


def _equipment_catalog_variants(season, class_name, spec_name, slot, query=''):
    """按物品 ID 选择一个分支：优先正式服，缺少正式服时保留 PTR。"""
    from botend.services.wow_data_branch import variant_branch
    selected = {}
    for variant in _catalog_queryset(season, (
        WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
        WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT,
    ), query).exclude(metadata__has_key='difficulty_drop').select_related(None).prefetch_related('item'):
        # 在 SQL 侧避免水合无关的大体积手册证据；Python 判据继续保守兜底。
        if _is_journal_display_variant(variant):
            continue
        if (not _source_track_is_valid(variant)
                or not slot_matches(variant, slot, class_name, spec_name)
                or not spec_matches(variant.item, class_name, spec_name, variant, slot)):
            continue
        branch = 'ptr' if _is_ptr_equipment(variant) else variant_branch(variant)
        priority = {'retail': 0, 'ptr': 1}.get(branch, 2)
        previous = selected.get(variant.item_id)
        if previous is None or priority < previous[0]:
            selected[variant.item_id] = (priority, [variant])
        elif priority == previous[0]:
            previous[1].append(variant)
    # 分支选择先于来源、绿字和分页筛选，避免筛选时重新出现同 ID 的 PTR 副本。
    return [variant for _priority, variants in selected.values() for variant in variants]


def catalog_items(
    *, class_name, spec_name, slot, source_type='all', query='', page=1, page_size=60,
    excluded_sources=(), excluded_stats=(),
):
    class_name, spec_name = canonical_spec(class_name, spec_name)
    if slot not in SLOT_LABELS:
        raise GearBuilderError('未知装备槽位')
    season = active_season()
    if not season or not season.gear_batch_key:
        return {'items': [], 'total': 0, 'page': 1, 'page_size': page_size, 'catalog': catalog_context(season)}

    excluded_sources = _filter_values(excluded_sources, set(QUICK_SOURCE_FILTERS))
    excluded_stats = _filter_values(excluded_stats, FILTERABLE_SECONDARY_STATS)
    grouped = defaultdict(list)
    for variant in _equipment_catalog_variants(season, class_name, spec_name, slot, query):
        if not _source_matches(variant, source_type):
            continue
        if _source_is_excluded(variant, excluded_sources):
            continue
        if _secondary_stat_is_excluded(variant, excluded_stats):
            continue
        grouped[variant.item_id].append(variant)

    rows = [serialize_item(variants[0].item, variants, class_name, spec_name) for variants in grouped.values()]
    rows.sort(key=lambda row: (-max((v['item_level'] for v in row['variants']), default=0), row['name']))
    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 60)))
    start = (page - 1) * page_size
    return {
        'items': rows[start:start + page_size],
        'total': len(rows),
        'page': page,
        'page_size': page_size,
        'catalog': catalog_context(season),
    }


def catalog_snapshot_items(*, class_name, spec_name, slot, season):
    """后台生成完整槽位，复用适配规则并保留每个变体的筛选与展示语义。"""
    grouped = defaultdict(list)
    positions = {}
    if season and season.gear_batch_key:
        for position, variant in enumerate(_equipment_catalog_variants(season, class_name, spec_name, slot)):
            grouped[variant.item_id].append(variant)
            positions[variant.pk] = position
    rows = []
    for variants in grouped.values():
        row = serialize_item(variants[0].item, variants, class_name, spec_name)
        row['_search'] = [variants[0].item.name, variants[0].item.name_zh]
        for variant, output in zip(variants, row['variants']):
            output['_order'] = positions[variant.pk]
            output['_filter'] = {
                'sources': sorted({str(s.get('type') or '').casefold()
                                   for s in (variant.source_json or []) if isinstance(s, dict)}),
                'stats': sorted(k for k, v in normalize_stats(variant.stats_json).items() if _number(v)),
            }
            # 来源筛选可能使第一条变体变化，名称、描述等必须与旧接口一致。
            display = serialize_item(variant.item, [variant], class_name, spec_name)
            output['_item'] = {k: v for k, v in display.items() if k != 'variants' and row.get(k) != v}
        rows.append(row)
    return rows


def enhancement_items(*, class_name, spec_name, slot, equipment_variant_id=None):
    class_name, spec_name = canonical_spec(class_name, spec_name)
    if slot not in SLOT_LABELS:
        raise GearBuilderError('未知装备槽位')
    season = active_season()
    if not season or not season.gear_batch_key:
        return {'groups': {'embellishments': [], 'gems': [], 'enchants': []}, 'catalog': catalog_context(season)}
    equipment_variant = None
    if equipment_variant_id:
        equipment_variant = WowItemVariantSnapshot.objects.filter(
            id=equipment_variant_id,
            season=season,
            batch_key=season.gear_batch_key,
        ).select_related('item').first()

    selected = _selected_enhancement_variants(
        _catalog_queryset(season, ('embellishment', 'gem', 'enchant')),
        class_name, spec_name, slot, equipment_variant,
    )
    groups = _serialize_enhancement_groups(selected, class_name, spec_name)
    return {'groups': groups, 'catalog': catalog_context(season)}


def _selected_enhancement_variants(variants, class_name, spec_name, slot, equipment_variant=None):
    """采集投影和内部目录共用资格、槽位及最高品质规则。"""
    type_to_group = {
        WowItemVariantSnapshot.TYPE_EMBELLISHMENT: 'embellishments',
        WowItemVariantSnapshot.TYPE_GEM: 'gems',
        WowItemVariantSnapshot.TYPE_ENCHANT: 'enchants',
    }
    grouped = defaultdict(list)
    highest_quality = {}
    for variant in variants:
        if not spec_matches(variant.item, class_name, spec_name, variant, slot):
            continue
        if variant.variant_type == WowItemVariantSnapshot.TYPE_EMBELLISHMENT:
            if embellishment_eligibility_reason(equipment_variant, variant, slot, class_name, spec_name):
                continue
        if not slot_matches(variant, slot, class_name, spec_name):
            continue
        if variant.variant_type in (
            WowItemVariantSnapshot.TYPE_EMBELLISHMENT,
            WowItemVariantSnapshot.TYPE_GEM,
            WowItemVariantSnapshot.TYPE_ENCHANT,
        ):
            if variant.variant_type != WowItemVariantSnapshot.TYPE_EMBELLISHMENT and int(variant.item.quality or 0) < 3:
                continue
            metadata = variant.metadata if isinstance(variant.metadata, dict) else {}
            family = str(
                metadata.get('simc_name') or variant.item.simc_token
                or variant.item.name_zh or variant.item.name or variant.item_id
            ).casefold()
            family = family.rsplit('_', 1)[0] if family.rsplit('_', 1)[-1].isdigit() else family
            key = (variant.variant_type, family, str(metadata.get('category_name') or ''))
            score = (int(variant.crafting_quality or 0), int(variant.item.quality or 0), int(variant.item_id))
            if key not in highest_quality or score > highest_quality[key][0]:
                highest_quality[key] = (score, variant)
            continue
        grouped[(variant.variant_type, variant.item_id)].append(variant)
    for _score, variant in highest_quality.values():
        grouped[(variant.variant_type, variant.item_id)].append(variant)
    groups = {'embellishments': [], 'gems': [], 'enchants': []}
    for (variant_type, _item_id), variants in grouped.items():
        groups[type_to_group[variant_type]].append(variants)
    return groups


def _serialize_enhancement_groups(selected, class_name, spec_name):
    groups = {key: [serialize_item(variants[0].item, variants, class_name, spec_name) for variants in rows]
              for key, rows in selected.items()}
    for rows in groups.values():
        rows.sort(key=lambda row: row['name'])
    return groups


def enhancement_snapshot_payload(*, class_name, spec_name, slot, season):
    """后台一次读取公共增强资料；配方关系只保存引用，正文不重复。"""
    variants = list(_catalog_queryset(season, ('embellishment', 'gem', 'enchant')).select_related(None).prefetch_related('item')) if season and season.gear_batch_key else []
    common = _selected_enhancement_variants(variants, class_name, spec_name, slot)
    groups = _serialize_enhancement_groups(common, class_name, spec_name)
    options, by_equipment = {}, {}
    if season and season.gear_batch_key:
        materials = [row for row in variants if row.variant_type == 'embellishment']
        carriers = _catalog_queryset(season, ('crafted_equipment',)).select_related(None).prefetch_related('item')
        for carrier in carriers:
            selected = _selected_enhancement_variants(materials, class_name, spec_name, slot, carrier)['embellishments']
            keys = []
            for rows in selected:
                key = ','.join(str(row.pk) for row in rows)
                keys.append(key)
                if key not in options:
                    options[key] = serialize_item(rows[0].item, rows, class_name, spec_name)
            if selected:
                by_equipment[str(carrier.pk)] = sorted(keys, key=lambda key: options[key]['name'])
    return {'groups': groups, 'embellishment_options': options, 'embellishments_by_equipment': by_equipment}


def filter_enhancement_snapshot(payload, equipment_variant_id=None):
    """兼容接口按同一发布文件中的配方引用筛选，不查装备表。"""
    groups = payload.get('groups') or {'embellishments': [], 'gems': [], 'enchants': []}
    keys = (payload.get('embellishments_by_equipment') or {}).get(str(equipment_variant_id or ''), [])
    options = payload.get('embellishment_options') or {}
    return {**groups, 'embellishments': [options[key] for key in keys if key in options]}


def _resolve_crafted_rows(variant, selected_stats, embellishment, class_name, spec_name, target_slot=''):
    options = variant.crafting_options or {}
    allowed = [str(value) for value in (options.get('stat_pool') or ('crit', 'haste', 'mastery', 'versatility'))]
    required_count = int(options.get('stat_count') or 2)
    selected = list(dict.fromkeys(str(value) for value in (selected_stats or []) if value))
    if len(selected) != required_count or any(value not in allowed for value in selected):
        raise GearBuilderError(f'请选择 {required_count} 项合法制造绿字')

    stats = stats_for_identity(variant.stats_json, variant.metadata, class_name, spec_name)
    total = _number(options.get('secondary_total') or (variant.stats_json or {}).get('secondary_total'))
    explicit = options.get('stat_values') if isinstance(options.get('stat_values'), dict) else {}
    if explicit:
        for key in selected:
            stats[key] = _number(explicit.get(key))
    elif total:
        base = floor(total / required_count)
        remainder = int(total - base * required_count)
        for index, key in enumerate(selected):
            stats[key] = base + (1 if index < remainder else 0)

    effects = list(serialize_variant(variant, class_name, spec_name)['effects'])
    if embellishment:
        slot = target_slot or _variant_item(variant).slot_key or (variant.compatible_slots or [''])[0]
        reason = embellishment_eligibility_reason(variant, embellishment, slot, class_name, spec_name)
        if reason:
            raise GearBuilderError(reason['reason'])
        effects.extend(serialize_variant(embellishment, class_name, spec_name)['effects'])
    return stats, selected, effects


def resolve_crafted_variant(*, variant_id, selected_stats=None, embellishment_variant_id=None,
                             class_name='Warrior', spec_name='Fury'):
    class_name, spec_name = canonical_spec(class_name, spec_name)
    season = active_season()
    if not season or not season.gear_batch_key:
        raise GearBuilderError('当前赛季装备目录尚未同步')
    variant = WowItemVariantSnapshot.objects.filter(
        id=variant_id,
        season=season,
        batch_key=season.gear_batch_key,
        variant_type=WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT,
    ).select_related('item').first()
    if not variant:
        raise GearBuilderError('制造装备变体不存在或已过期')
    embellishment = None
    if embellishment_variant_id:
        embellishment = WowItemVariantSnapshot.objects.filter(
            id=embellishment_variant_id,
            season=season,
            batch_key=season.gear_batch_key,
            variant_type=WowItemVariantSnapshot.TYPE_EMBELLISHMENT,
        ).select_related('item').first()
        if not embellishment:
            raise GearBuilderError('所选美化不存在或已过期')
    stats, selected, effects = _resolve_crafted_rows(
        variant, selected_stats or [], embellishment, class_name, spec_name,
    )

    return {
        'variant': serialize_item(variant.item, [variant], class_name, spec_name),
        'resolved_stats': stats,
        'selected_stats': selected,
        'effects': effects,
        'embellishment': serialize_item(embellishment.item, [embellishment], class_name, spec_name) if embellishment else None,
        'simc': {
            'item_id': variant.item.item_id,
            'ilevel': variant.item_level,
            'bonus_ids': variant.bonus_ids or [],
            'crafted_stats': selected,
            'crafting_quality': variant.crafting_quality,
        },
    }


def _state_item_and_variant(variant, class_name, spec_name):
    payload = serialize_item(variant.item, [variant], class_name, spec_name)
    variant_payload = payload.pop('variants')[0]
    payload.pop('metadata', None)
    return payload, variant_payload


def _parse_share_variant_ref(value, fallback_item_id=0):
    """解析 v4 稳定引用；装备为变体键，强化为 [物品 ID, 变体键]。"""
    try:
        fallback_item_id = int(fallback_item_id or 0)
    except (TypeError, ValueError):
        raise GearBuilderError('分享链接包含无效物品 ID')

    if value in (None, 0, ''):
        return None
    if fallback_item_id:
        if not isinstance(value, str) or not value or len(value) > 160:
            raise GearBuilderError('分享链接中的装备变体键无效')
        return fallback_item_id, value
    if not isinstance(value, list) or len(value) != 2:
        raise GearBuilderError('分享链接中的强化引用格式无效')
    try:
        item_id = int(value[0] or 0)
    except (TypeError, ValueError):
        raise GearBuilderError('分享链接包含无效物品 ID')
    variant_key = str(value[1] or '')
    if item_id <= 0 or not variant_key or len(variant_key) > 160:
        raise GearBuilderError('分享链接中的强化引用无效')
    return item_id, variant_key


def _resolve_share_variant(reference, expected_types, variants_by_reference):
    if not reference:
        return None
    variant = variants_by_reference.get(reference)
    if variant and variant.variant_type in set(expected_types):
        return variant
    return None


def current_variants(rows, season=None):
    """仅刷新相同物品、变体、装等、分支；不替换用户选择，不回写历史记录。"""
    rows = list(rows)
    season = season or active_season()
    if not season or not rows:
        return {row.pk: row for row in rows}
    from botend.services.wow_data_branch import variant_branch
    latest = list(WowItemVariantSnapshot.objects.filter(
        season=season, batch_key=season.gear_batch_key,
        item_id__in={row.item_id for row in rows},
    ).select_related('item'))
    def identity(row):
        branch = variant_branch(row)
        return (row.item_id, row.variant_key.removeprefix(f'{branch}:'), row.item_level, branch)
    lookup = {identity(row): row for row in latest}
    return {row.pk: lookup.get(identity(row), row) for row in rows}


def _share_integer(value, *, zero=False):
    # JSON booleans, numeric strings and fractional IDs are not identities.
    if type(value) is not int or value < (0 if zero else 1) or value > 2 ** 53 - 1:
        raise GearBuilderError('分享链接包含无效物品或变体 ID')
    return value


def _share_key(value):
    if not isinstance(value, str) or not value or len(value) > 160:
        raise GearBuilderError('分享链接中的变体键无效')
    return value


def _share_reference(value, version, item_id=0):
    if value is None or value == '' or type(value) is int and value == 0:
        return None
    if version == 2:
        return ('pk', _share_integer(value), item_id, '', None)
    if version == 3:
        if not isinstance(value, list) or len(value) != 4:
            raise GearBuilderError('分享链接中的历史变体引用格式无效')
        pk, ref_item, key, level = value
        _share_integer(pk)
        _share_integer(ref_item)
        _share_integer(level, zero=True)
        if item_id and ref_item != item_id:
            raise GearBuilderError('分享链接中的物品身份冲突')
        return ('pk', pk, ref_item, _share_key(key), level)
    if item_id:
        return ('key', item_id, _share_key(value))
    if not isinstance(value, list) or len(value) != 2:
        raise GearBuilderError('分享链接中的强化引用格式无效')
    return ('key', _share_integer(value[0]), _share_key(value[1]))


def share_payload_arguments(payload):
    """Adapt only formats actually emitted by the historical JS codecs.

    This projection never replaces the original encoded string in storage.
    """
    if not isinstance(payload, dict):
        raise GearBuilderError('配装字符串必须是对象')
    if 'v' in payload or 'e' in payload:
        return dict(share_version=payload.get('v'), class_name=payload.get('c'),
                    spec_name=payload.get('s'), batch_key=payload.get('b'), entries=payload.get('e'))
    if type(payload.get('version')) is not int or payload['version'] != 1:
        raise GearBuilderError('配装字符串版本无效')
    equipment = payload.get('equipment')
    if not isinstance(equipment, dict):
        raise GearBuilderError('配装字符串中的装备格式无效')
    return dict(share_version=4, class_name=payload.get('className'),
                spec_name=payload.get('specName'), batch_key=payload.get('batchKey'),
                entries=[[slot, 0, 0, [], 0, [], 0, 0, entry] for slot, entry in equipment.items()])


def _validated_share_snapshot(snapshot, slot, class_name, spec_name, batch_key):
    """Validate a self-contained historical state, not a guessed catalog item.

    Old compactItem omitted class masks; check supplied restrictions and any
    surviving central identity. Missing catalog rows do not invalidate a real
    snapshot, but contradictory identities or slots do.
    """
    from copy import deepcopy
    from botend.models import WowItemSnapshot

    equipment_types = {WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
                       WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT}

    def component(value, expected_types, equipment=False):
        if not isinstance(value, dict) or not isinstance(value.get('item'), dict):
            raise GearBuilderError('分享快照缺少有效物品身份')
        item = value['item']
        item_id = _share_integer(item.get('item_id'))
        variant = value.get('variant') if value.get('variant') is not None else {}
        if not isinstance(variant, dict):
            raise GearBuilderError('分享快照中的变体格式无效')
        # A snapshot may mix historical equipment with newer enhancements.
        # Only an absent origin falls back to the payload's legacy batch.
        component_batch = variant.get('batch_key', batch_key)
        if not isinstance(component_batch, str) or not component_batch or len(component_batch) > 160:
            raise GearBuilderError('分享快照中的装备批次无效')
        metadata = variant.get('metadata') if variant.get('metadata') is not None else {}
        if not isinstance(metadata, dict):
            raise GearBuilderError('分享快照元数据无效')
        if variant.get('type') is not None and not isinstance(variant['type'], str):
            raise GearBuilderError('分享快照中的物品类型无效')
        if variant.get('item_id') is not None and _share_integer(variant['item_id']) != item_id:
            raise GearBuilderError('分享快照中的物品身份冲突')
        pk = variant.get('id')
        if pk is not None:
            _share_integer(pk)
        if variant.get('key') is not None:
            _share_key(variant['key'])
        external = value.get('external') is True
        if variant.get('type') and variant['type'] not in expected_types and not (
                external and variant['type'] == 'external'):
            raise GearBuilderError('分享快照中的物品类型冲突')
        if variant.get('item_level') is not None:
            _share_integer(variant['item_level'], zero=True)
        for key in ('item_id', 'variant_id'):
            if value.get(key) is not None:
                actual = _share_integer(value[key])
                if actual != (item_id if key == 'item_id' else pk):
                    raise GearBuilderError('分享快照中的物品身份冲突')
        for key in ('slot', 'slot_key'):
            if value.get(key) and canonical_equipment_slot(value[key]) != slot:
                raise GearBuilderError('分享快照中的槽位冲突')
        family = SLOT_FAMILIES.get(slot, slot)
        item_slot = canonical_equipment_slot(item.get('slot') or item.get('slot_key') or '')
        compatible = variant.get('compatible_slots') if variant.get('compatible_slots') is not None else []
        if not isinstance(compatible, list) or not all(isinstance(v, str) for v in compatible):
            raise GearBuilderError('分享快照中的适用槽位无效')
        compatible = [canonical_equipment_slot(v) for v in compatible]
        if equipment and not item_slot and not compatible and not external:
            raise GearBuilderError('分享快照缺少装备槽位证据')
        # Enhancement item slots describe their category, not the carrier.
        # Their explicit compatible_slots and surviving central rules still apply.
        if (item_slot and (equipment or item_slot in {*SLOT_LABELS, *SLOT_FAMILIES.values()})
                and item_slot not in (slot, family)):
            # Fury two-handed off-hand is validated against central rules below.
            if not (slot == 'off_hand' and item_slot == 'main_hand'
                    and (class_name, spec_name) == ('Warrior', 'Fury')
                    and (variant.get('metadata') or {}).get('two_handed') is True):
                raise GearBuilderError('分享快照中的槽位冲突')
        if compatible and slot not in compatible and family not in compatible:
            if not (slot == 'off_hand' and 'main_hand' in compatible
                    and (class_name, spec_name) == ('Warrior', 'Fury')
                    and (variant.get('metadata') or {}).get('two_handed') is True):
                raise GearBuilderError('分享快照中的槽位冲突')
        branch = variant.get('data_branch', 'retail')
        branches = metadata.get('branch_display') if metadata.get('branch_display') is not None else {}
        if not isinstance(branch, str) or not isinstance(branches, dict):
            raise GearBuilderError('分享快照中的分支身份无效')
        display = branches.get(branch) if branches.get(branch) is not None else {}
        if not isinstance(display, dict):
            raise GearBuilderError('分享快照中的分支身份无效')
        facts = {**display, **item}
        numeric = {}
        for key in ('item_class_id', 'item_subclass_id', 'inventory_type', 'allowable_class_mask'):
            numeric[key] = _share_integer(0 if facts.get(key) is None else facts[key], zero=True)
        eligible = facts.get('eligible_specs') if facts.get('eligible_specs') is not None else []
        if not isinstance(eligible, list) or not all(isinstance(v, str) for v in eligible):
            raise GearBuilderError('分享快照中的专精限制无效')
        model_item = WowItemSnapshot(item_id=item_id, slot_key=item_slot, eligible_specs=eligible,
                                     **numeric)
        if not isinstance(variant.get('stats', {}), dict):
            raise GearBuilderError('分享快照中的属性格式无效')
        model_variant = WowItemVariantSnapshot(item=model_item,
            variant_type=variant.get('type') or next(iter(expected_types)), compatible_slots=compatible,
            stats_json=variant.get('stats') or {})
        if not spec_matches(model_item, class_name, spec_name, model_variant, slot):
            raise GearBuilderError('分享快照中的装备不适用于当前专精')
        existing = WowItemVariantSnapshot.objects.filter(pk=pk).select_related('item').first() if pk else None
        if not existing and variant.get('key'):
            existing = WowItemVariantSnapshot.objects.filter(item__item_id=item_id,
                variant_key=variant['key'], batch_key=component_batch).select_related('item').first()
        if existing:
            from botend.services.wow_data_branch import variant_branch
            if (existing.item.item_id != item_id or existing.batch_key != component_batch
                    or variant.get('key') and existing.variant_key != variant['key']
                    or variant.get('item_level') is not None and existing.item_level != variant['item_level']
                    or variant.get('type') and existing.variant_type != variant['type']
                    or variant.get('data_branch') is not None and variant_branch(existing) != variant['data_branch']
                    or existing.variant_type not in expected_types):
                raise GearBuilderError('分享快照与目录身份冲突')
            if not slot_matches(existing, slot, class_name, spec_name) or not spec_matches(
                    existing.item, class_name, spec_name, existing, slot):
                raise GearBuilderError('分享快照中的装备不适用于当前槽位或专精')
        return value

    component(snapshot, equipment_types, equipment=True)
    gems = snapshot.get('gems') if snapshot.get('gems') is not None else []
    if not isinstance(gems, list) or len(gems) > 3:
        raise GearBuilderError('分享快照中的宝石数量无效')
    for gem in gems:
        component(gem, {WowItemVariantSnapshot.TYPE_GEM})
    for field, expected in (('enchant', WowItemVariantSnapshot.TYPE_ENCHANT),
                            ('embellishment', WowItemVariantSnapshot.TYPE_EMBELLISHMENT)):
        if snapshot.get(field) is not None:
            component(snapshot[field], {expected})
    if snapshot.get('itemLevel') is not None:
        _share_integer(snapshot['itemLevel'], zero=True)
        level = (snapshot.get('variant') or {}).get('item_level')
        if level is not None and snapshot['itemLevel'] != level:
            raise GearBuilderError('分享快照中的装等冲突')
    if 'addedSocket' in snapshot and type(snapshot['addedSocket']) is not bool:
        raise GearBuilderError('分享快照中的插槽标记无效')
    if snapshot.get('addedSocket') and SLOT_FAMILIES.get(slot, slot) not in ADDITIONAL_SOCKET_SLOTS:
        raise GearBuilderError('分享快照中的槽位不支持额外插槽')
    stats = snapshot.get('selectedStats', [])
    if not isinstance(stats, list) or len(stats) > 4 or any(
            not isinstance(value, str) or value not in FILTERABLE_SECONDARY_STATS for value in stats):
        raise GearBuilderError('分享快照中的制造属性无效')
    return deepcopy(snapshot)


def _snapshot_matches_reference(component, reference):
    if reference is None:
        return True
    if not isinstance(component, dict):
        return False
    item, variant = component.get('item') or {}, component.get('variant') or {}
    if reference[0] == 'key':
        return (item.get('item_id'), variant.get('key')) == reference[1:]
    _, pk, item_id, key, level = reference
    return (variant.get('id') == pk and (not item_id or item.get('item_id') == item_id)
            and (not key or variant.get('key') == key)
            and (level is None or variant.get('item_level') == level))


def hydrate_shared_state(*, share_version, class_name, spec_name, batch_key, entries, current=True, strict=False):
    """Recover historical codecs without silently substituting identities.

    strict is opt-in for atomic UI replacement; legacy consumers still receive
    partial equipment plus warnings. Storage opts in but keeps the exact code.
    """
    import json

    if type(strict) is not bool:
        raise GearBuilderError('strict 必须是布尔值')
    if type(share_version) is not int or share_version not in (2, 3, 4):
        raise GearBuilderError('分享链接版本无效')
    if (not isinstance(class_name, str) or not isinstance(spec_name, str)
            or len(class_name) > 32 or len(spec_name) > 64):
        raise GearBuilderError('分享链接缺少有效职业专精')
    class_name, spec_name = canonical_spec(class_name, spec_name)
    if not isinstance(batch_key, str) or not batch_key or len(batch_key) > 160:
        raise GearBuilderError('分享链接缺少有效装备批次')
    if not isinstance(entries, list) or len(entries) > len(EQUIPMENT_SLOTS) + len(SLOT_ALIASES):
        raise GearBuilderError('分享链接中的装备槽位数量无效')
    raw_slots, rows, raw_contents = set(), {}, {}
    references, snapshots = {}, {}
    for row in entries:
        if not isinstance(row, list) or not 3 <= len(row) <= 9:
            raise GearBuilderError('分享链接中的装备引用格式无效')
        raw_slot = row[0]
        if not isinstance(raw_slot, str) or raw_slot not in {*SLOT_LABELS, *SLOT_ALIASES} or raw_slot in raw_slots:
            raise GearBuilderError('分享链接包含未知或重复装备槽位')
        raw_slots.add(raw_slot)
        slot = canonical_equipment_slot(raw_slot)
        # Only byte-equivalent JSON content (apart from the slot alias) merges.
        # Validate raw duplicates before canonicalization, never first/last wins.
        if slot in rows:
            if json.dumps(raw_contents[slot], sort_keys=True) != json.dumps(row[1:], sort_keys=True):
                raise GearBuilderError('分享链接包含冲突的重复装备槽位，请保留原字符串')
            continue
        item_id = _share_integer(row[1], zero=True)
        padded = row[:8] + [[], 0, [], 0, 0][len(row) - 3:] if len(row) < 8 else row[:8]
        stats, embellishment, gems, enchant, socket = padded[3:8]
        if not isinstance(stats, list) or len(stats) > 4 or any(
                not isinstance(value, str) or value not in FILTERABLE_SECONDARY_STATS for value in stats):
            raise GearBuilderError('分享链接中的制造属性无效')
        if not isinstance(gems, list) or len(gems) > 3:
            raise GearBuilderError('分享链接中的宝石数量无效')
        if type(socket) not in (bool, int) or socket not in (0, 1):
            raise GearBuilderError('分享链接中的插槽标记无效')
        refs = [_share_reference(row[2], share_version, item_id),
                _share_reference(embellishment, share_version),
                [_share_reference(value, share_version) for value in gems],
                _share_reference(enchant, share_version)]
        if refs[0] and not item_id:
            raise GearBuilderError('分享链接缺少有效物品 ID')
        if item_id and not refs[0]:
            raise GearBuilderError('分享链接缺少有效装备引用')
        if any(ref is None for ref in refs[2]):
            raise GearBuilderError('分享链接中的宝石引用无效')
        if len(row) == 9:
            snapshot = _validated_share_snapshot(row[8], slot, class_name, spec_name, batch_key)
            components = [snapshot, snapshot.get('embellishment'), snapshot.get('gems') or [], snapshot.get('enchant')]
            if refs[0] and (bool(components[1]) != bool(refs[1]) or bool(components[3]) != bool(refs[3])
                    or len(components[2]) != len(refs[2]) or snapshot.get('selectedStats', []) != stats
                    or bool(snapshot.get('addedSocket')) != bool(socket)):
                raise GearBuilderError('分享快照与强化配置冲突')
            if (not _snapshot_matches_reference(components[0], refs[0])
                    or not _snapshot_matches_reference(components[1], refs[1])
                    or not _snapshot_matches_reference(components[3], refs[3])
                    or refs[2] and (len(refs[2]) != len(components[2]) or any(
                        not _snapshot_matches_reference(comp, ref) for comp, ref in zip(components[2], refs[2])))):
                raise GearBuilderError('分享快照与引用身份冲突')
            snapshots[slot] = snapshot
        rows[slot], references[slot], raw_contents[slot] = padded, refs, row[1:]

    legacy_ids = {ref[1] for refs in references.values() for ref in [refs[0], refs[1], *refs[2], refs[3]]
                  if ref and ref[0] == 'pk'}
    legacy = {row.pk: row for row in WowItemVariantSnapshot.objects.filter(
        pk__in=legacy_ids, batch_key=batch_key).select_related('item')} if legacy_ids else {}
    issues, converted = [], []
    for slot, row in rows.items():
        # A verified self-contained snapshot is the historical fact. Do not
        # recalculate it from today's catalog, even when its references survive.
        if slot in snapshots:
            continue
        refs = references[slot]
        missing = []
        def convert(ref, field):
            if ref is None:
                return 0
            if ref[0] == 'key':
                return list(ref[1:])
            _, pk, item_id, key, level = ref
            variant = legacy.get(pk)
            if (not variant or item_id and variant.item.item_id != item_id
                    or key and variant.variant_key != key or level is not None and variant.item_level != level):
                missing.append({'slot': slot, 'field': field, 'code': 'unresolved_reference', 'reference': list(ref)})
                return 0
            return [variant.item.item_id, variant.variant_key]
        equip = convert(refs[0], 'equipment')
        embellishment = convert(refs[1], 'embellishment')
        gems = [convert(ref, f'gems.{index}') for index, ref in enumerate(refs[2])]
        enchant = convert(refs[3], 'enchant')
        if missing:
            issues.extend(missing)
        if not equip:
            if not missing and slot not in snapshots:
                issues.append({'slot': slot, 'field': 'equipment', 'code': 'missing_snapshot'})
            continue
        converted.append([slot, equip[0], equip[1], row[3], embellishment,
                          [value for value in gems if value], enchant, row[7]])
    result = _hydrate_catalog_shared_state(share_version=4, class_name=class_name, spec_name=spec_name,
        batch_key=batch_key, entries=converted, current=current)
    issues.extend(result['issues'])
    result['equipment'].update(snapshots)
    result['issues'] = issues
    result['warnings'] = [issue.get('message') or f"{SLOT_LABELS[issue['slot']]}的历史引用无法完整恢复" for issue in issues][:16]
    if strict and issues:
        raise GearBuilderError('配装无法完整恢复，请保留原字符串；未替换当前配装', issues=issues)
    return result


def _hydrate_catalog_shared_state(*, share_version, class_name, spec_name, batch_key, entries, current=True):
    """按批次和变体引用恢复紧凑分享数据，不保存任何用户配装。"""
    try:
        share_version = int(share_version or 0)
    except (TypeError, ValueError):
        share_version = 0
    if share_version != 4:
        raise GearBuilderError('分享链接版本已过期，请使用当前配装器重新生成')
    class_name, spec_name = canonical_spec(class_name, spec_name)
    batch_key = str(batch_key or '').strip()
    current_season = active_season() if current else None
    if not batch_key and current_season:
        batch_key = current_season.gear_batch_key
    if not batch_key or len(batch_key) > 160:
        raise GearBuilderError('分享链接缺少有效装备批次')
    if not isinstance(entries, list) or len(entries) > len(EQUIPMENT_SLOTS):
        raise GearBuilderError('分享链接中的装备槽位数量无效')

    parsed = []
    item_ids = set()
    seen_slots = set()
    for row in entries:
        if not isinstance(row, list) or len(row) < 3:
            raise GearBuilderError('分享链接中的装备引用格式无效')
        slot = canonical_equipment_slot(row[0])
        if slot not in SLOT_LABELS or slot in seen_slots:
            raise GearBuilderError('分享链接包含未知或重复装备槽位')
        seen_slots.add(slot)
        try:
            item_id = int(row[1] or 0)
        except (TypeError, ValueError):
            raise GearBuilderError('分享链接包含无效物品 ID')
        equipment_ref = _parse_share_variant_ref(row[2], item_id)
        embellishment_ref = _parse_share_variant_ref(row[4]) if len(row) > 4 else _parse_share_variant_ref(0)
        gem_values = row[5] if len(row) > 5 and isinstance(row[5], list) else []
        gem_refs = [_parse_share_variant_ref(value) for value in gem_values]
        enchant_ref = _parse_share_variant_ref(row[6]) if len(row) > 6 else _parse_share_variant_ref(0)
        if len(gem_refs) > 3:
            raise GearBuilderError('分享链接中的宝石数量无效')
        selected_stats = [str(value) for value in (row[3] or [])][:4] if len(row) > 3 and isinstance(row[3], list) else []
        added_socket = bool(row[7]) if len(row) > 7 else False
        parsed.append((slot, item_id, equipment_ref, selected_stats, embellishment_ref, gem_refs, enchant_ref, added_socket))
        item_ids.update(reference[0] for reference in [equipment_ref, embellishment_ref, enchant_ref, *gem_refs] if reference)

    selection = Q(batch_key=batch_key)
    if current_season:
        selection |= Q(season=current_season, batch_key=current_season.gear_batch_key)
    batch_variants = list(WowItemVariantSnapshot.objects.filter(
        selection, item__item_id__in=item_ids,
    ).select_related('item'))
    # Prefer original-batch facts; only current_variants may refresh a proven
    # same-item/key/level/branch identity, never queryset ordering by accident.
    variants_by_reference = {
        (row.item.item_id, row.variant_key): row
        for row in sorted(batch_variants, key=lambda row: row.batch_key == batch_key)
    }
    if current_season:
        replacements = current_variants(batch_variants, current_season)
        variants_by_reference = {key: replacements.get(row.pk, row) for key, row in variants_by_reference.items()}
    equipment = {}
    warnings = []
    issues = []
    def issue(slot, field, message, code='unresolved_reference'):
        warnings.append(message)
        issues.append({'slot': slot, 'field': field, 'code': code, 'message': message})
    for slot, item_id, equipment_ref, selected_stats, embellishment_ref, gem_refs, enchant_ref, added_socket in parsed:
        if not equipment_ref:
            continue
        variant = _resolve_share_variant(
            equipment_ref,
            (WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT, WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT),
            variants_by_reference,
        )
        if not variant:
            issue(slot, 'equipment', f'{SLOT_LABELS[slot]}的装备引用已失效')
            continue
        if variant.variant_type not in (
            WowItemVariantSnapshot.TYPE_DROP_EQUIPMENT,
            WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT,
        ) or not slot_matches(variant, slot, class_name, spec_name) or not spec_matches(
            variant.item, class_name, spec_name, variant, slot,
        ):
            issue(slot, 'equipment', f'{SLOT_LABELS[slot]}的装备不适用于当前专精，已置空', 'incompatible_reference')
            continue

        item_payload, variant_payload = _state_item_and_variant(variant, class_name, spec_name)
        enhancement_rows = {}
        resolved_enhancements = {}
        for key, reference, expected_type in (
            ('embellishment', embellishment_ref, WowItemVariantSnapshot.TYPE_EMBELLISHMENT),
            ('enchant', enchant_ref, WowItemVariantSnapshot.TYPE_ENCHANT),
        ):
            enhancement = _resolve_share_variant(
                reference, (expected_type,), variants_by_reference,
            )
            if (
                enhancement
                and enhancement.variant_type == expected_type
                and slot_matches(enhancement, slot, class_name, spec_name)
                and spec_matches(enhancement.item, class_name, spec_name, enhancement, slot)
            ):
                enhancement_item, enhancement_variant = _state_item_and_variant(enhancement, class_name, spec_name)
                enhancement_rows[key] = {'item': enhancement_item, 'variant': enhancement_variant}
                resolved_enhancements[key] = enhancement
            else:
                enhancement_rows[key] = None
                if reference:
                    issue(slot, key, f'{SLOT_LABELS[slot]}的{("美化" if key == "embellishment" else "附魔")}引用已失效')

        gems = []
        socket_family = SLOT_FAMILIES.get(slot, slot)
        if added_socket and socket_family not in ADDITIONAL_SOCKET_SLOTS:
            issue(slot, 'addedSocket', f'{SLOT_LABELS[slot]}不支持额外插槽', 'invalid_socket')
        added_socket = bool(added_socket and socket_family in ADDITIONAL_SOCKET_SLOTS)
        capacity = int(variant_payload.get('socket_count') or 0) + (1 if added_socket else 0)
        if len(gem_refs) > capacity:
            issue(slot, 'gems', f'{SLOT_LABELS[slot]}的宝石数量超过插槽容量', 'socket_capacity')
        for reference in gem_refs[:capacity]:
            gem = _resolve_share_variant(
                reference, (WowItemVariantSnapshot.TYPE_GEM,), variants_by_reference,
            )
            if (
                not gem
                or gem.variant_type != WowItemVariantSnapshot.TYPE_GEM
                or not slot_matches(gem, slot, class_name, spec_name)
                or not spec_matches(gem.item, class_name, spec_name, gem, slot)
            ):
                issue(slot, 'gems', f'{SLOT_LABELS[slot]}的宝石引用已失效')
                continue
            gem_item, gem_variant = _state_item_and_variant(gem, class_name, spec_name)
            gems.append({'item': gem_item, 'variant': gem_variant})

        resolved_stats = None
        resolved_effects = None
        if variant.variant_type == WowItemVariantSnapshot.TYPE_CRAFTED_EQUIPMENT and selected_stats:
            resolved_stats, selected_stats, resolved_effects = _resolve_crafted_rows(
                variant,
                selected_stats,
                resolved_enhancements.get('embellishment'),
                class_name,
                spec_name,
                slot,
            )
        equipment[slot] = {
            'item': item_payload,
            'variant': variant_payload,
            'itemLevel': variant.item_level,
            'selectedStats': selected_stats,
            'resolvedStats': resolved_stats,
            'resolvedEffects': resolved_effects,
            'embellishment': enhancement_rows['embellishment'],
            'gems': gems,
            'enchant': enhancement_rows['enchant'],
            'addedSocket': added_socket,
            'external': False,
        }
    return {'equipment': equipment, 'warnings': warnings[:16], 'issues': issues,
            'batch_key': current_season.gear_batch_key if current_season else batch_key}


def _simc_item_id(payload):
    if not isinstance(payload, dict):
        return 0
    return int(payload.get('item_id') or payload.get('id') or 0)


def _simc_enchantment_id(payload):
    if not isinstance(payload, dict):
        return 0
    return int(payload.get('enchantment_id') or payload.get('id') or payload.get('item_id') or 0)


def _canonical_crafted_stats(values):
    aliases = {
        '40': 'crit', '暴击': 'crit', 'crit': 'crit',
        '49': 'haste', '急速': 'haste', 'haste': 'haste',
        '32': 'mastery', '精通': 'mastery', 'mastery': 'mastery',
        '36': 'versatility', '全能': 'versatility', 'vers': 'versatility', 'versatility': 'versatility',
    }
    return [aliases[value] for value in map(str, values or []) if value in aliases]


def import_simc_profile(profile_text):
    if not str(profile_text or '').strip():
        raise GearBuilderError('请粘贴 SimC Profile')
    parsed = parse_simc_player_profile(profile_text)
    profile = parsed.get('profile') or {}
    identity = profile.get('identity') or {}
    normalized_identity = canonical_class_spec(identity.get('class_name'), identity.get('spec'))
    equipment = profile.get('equipment') or []
    bag_equipment = list((parsed.get('candidates') or {}).get('gear') or [])
    for row in bag_equipment:
        row['gems'] = [{'item_id': value, 'id': value} for value in (row.get('gem_ids') or [])]
        row['enchant'] = {'enchantment_id': row.get('enchant_id'), 'id': row.get('enchant_id')} if row.get('enchant_id') else None
    all_equipment = [*equipment, *bag_equipment]
    season = active_season()
    batch_key = season.gear_batch_key if season else ''
    item_ids = [_simc_item_id(row) for row in all_equipment]
    enhancer_ids = []
    for row in all_equipment:
        enhancer_ids.extend(_simc_item_id(gem) for gem in (row.get('gems') or []))
        enhancer_ids.append(_simc_enchantment_id(row.get('enchant')))
    requested_ids = [value for value in item_ids + enhancer_ids if value]
    variants = list(WowItemVariantSnapshot.objects.filter(
        Q(item__item_id__in=requested_ids) | Q(item__enchantment_id__in=requested_ids),
        season=season,
        batch_key=batch_key,
    ).select_related('item')) if season and batch_key else []
    by_item = defaultdict(list)
    by_enchantment = defaultdict(list)
    for variant in variants:
        by_item[int(variant.item.item_id)].append(variant)
        if variant.item.enchantment_id:
            by_enchantment[int(variant.item.enchantment_id)].append(variant)

    mapped = []
    warnings = []
    for row in all_equipment:
        slot = canonical_equipment_slot(row.get('slot'))
        item_id = _simc_item_id(row)
        candidates = by_item.get(item_id, [])
        requested_bonus = {str(value) for value in (row.get('bonus_ids') or [])}
        requested_level = int(row.get('item_level') or 0)
        selected = next((value for value in candidates if requested_bonus and set(map(str, value.bonus_ids or [])) == requested_bonus), None)
        selected = selected or next((value for value in candidates if requested_level and value.item_level == requested_level), None)
        selected = selected or (candidates[0] if candidates else None)
        external = selected is None
        if external:
            warnings.append(f"{SLOT_LABELS.get(slot, slot)} 的物品 #{item_id} 不在当前目录中，已作为外部装备保留。")
        enchant_payload = row.get('enchant') or {}
        enchant_id = _simc_enchantment_id(enchant_payload)
        enchant_candidates = by_item.get(enchant_id) or by_enchantment.get(enchant_id) or []
        gem_rows = []
        for gem in row.get('gems') or []:
            gem_id = _simc_item_id(gem)
            gem_variant = next((value for value in by_item.get(gem_id, []) if value.variant_type == WowItemVariantSnapshot.TYPE_GEM), None)
            gem_rows.append({
                'item_id': gem_id,
                'variant_id': gem_variant.id if gem_variant else None,
                'variant': serialize_variant(gem_variant) if gem_variant else None,
                'item': serialize_item(gem_variant.item, [gem_variant]) if gem_variant else None,
                'external': not bool(gem_variant),
                'name': gem.get('display_name') or f'#{gem_id}',
            })
        mapped.append({
            'slot': slot,
            'item_id': item_id,
            'name': row.get('display_name') or f'#{item_id}',
            'item_level': requested_level,
            'variant_id': selected.id if selected else None,
            'variant': serialize_variant(selected) if selected else None,
            'item': serialize_item(selected.item, [selected]) if selected else None,
            'external': external,
            'bonus_ids': row.get('bonus_ids') or [],
            'crafted_stats': _canonical_crafted_stats(row.get('crafted_stats')),
            'crafting_quality': row.get('crafting_quality') or 0,
            'gems': gem_rows,
            'enchant': {
                'item_id': enchant_id,
                'variant_id': enchant_candidates[0].id if enchant_candidates else None,
                'variant': serialize_variant(enchant_candidates[0]) if enchant_candidates else None,
                'item': serialize_item(enchant_candidates[0].item, [enchant_candidates[0]]) if enchant_candidates else None,
                'external': bool(enchant_id and not enchant_candidates),
                'name': enchant_payload.get('display_name') or (f'#{enchant_id}' if enchant_id else ''),
            } if enchant_id else None,
            'raw_value': row.get('raw_value') or '',
            'import_source': 'simc_bag' if row in bag_equipment else 'simc_equipped',
        })
    equipped_mapped = mapped[:len(equipment)]
    bag_mapped = mapped[len(equipment):]
    return {
        'identity': {
            'class_name': normalized_identity[0] if normalized_identity else '',
            'spec_name': normalized_identity[1] if normalized_identity else '',
            'class_cn': CLASS_CN.get(normalized_identity[0], '') if normalized_identity else '',
            'spec_cn': SPEC_CN.get(normalized_identity[1], '') if normalized_identity else '',
        },
        'equipment': equipped_mapped,
        'bag_equipment': bag_mapped,
        'owned_equipment': mapped,
        'warnings': warnings,
        'catalog': catalog_context(season),
    }
