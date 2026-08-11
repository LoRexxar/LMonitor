"""为无 Spell ID 的 SimC APL token 生成可审计的中文本地化覆盖。"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re
from collections import Counter, defaultdict
from pathlib import Path


OVERRIDE_SCHEMA_VERSION = 1
RESOLVER_VERSION = 2

CLASS_MASKS = {
    'warrior': 1,
    'paladin': 2,
    'hunter': 4,
    'rogue': 8,
    'priest': 16,
    'deathknight': 32,
    'shaman': 64,
    'mage': 128,
    'warlock': 256,
    'monk': 512,
    'druid': 1024,
    'demonhunter': 2048,
    'evoker': 4096,
}

CONTROL_ACTION_LABELS = {
    'call_action_list': '调用动作列表',
    'cycling_variable': '循环变量',
    'pool_resource': '积攒资源',
    'run_action_list': '执行动作列表',
    'snapshot_stats': '快照属性',
    'variable': '变量',
    'wait': '等待',
    'auto_attack': '自动攻击',
    'potion': '使用药水',
    'use_item': '使用物品',
    'use_items': '使用物品组',
    'invoke_external_buff': '调用外部增益',
    'apply_poison': '施加毒药',
    'summon_pet': '召唤宠物',
    'retarget_auto_attack': '重新选择自动攻击目标',
    'cancel_buff': '取消增益',
    'strict_sequence': '严格技能序列',
}

# Wowhead 无精确同名记录的 SimC 内部状态与历史缩写。这里保留 token 作为数据库身份，
# 中文只负责解释语义，不能把通用机器翻译误当作官方技能名。
SEMANTIC_TOKEN_LABELS = {
    'acquired_wand': '博取之杖',
    'allied_virtual_cd_time': '盟友虚拟冷却时间',
    'ancient_madness_extension': '远古疯狂（延长时间）',
    'any_dnd': '任意枯萎凋零',
    'art_of_the_glaive_first': '战刃绝技（第一段）',
    'art_of_the_glaive_second_glaive_flurry': '战刃绝技（第二段：战刃乱舞）',
    'art_of_the_glaive_second_rending_strike': '战刃绝技（第二段：撕裂打击）',
    'brilliance_vers': '辉煌（全能）',
    'cancel_action': '取消当前动作',
    'cancel_autoattack': '取消自动攻击',
    'cancelform': '取消形态',
    'casting': '施法中',
    'celestial_alignment_only': '仅超凡之盟',
    'chains_of_ice_trollbane_debuff': '冰链术（托尔贝恩减益）',
    'chains_of_ice_trollbane_slow': '冰链术（托尔贝恩减速）',
    'charge_movement': '冲锋（位移）',
    'clearcasting_tree': '节能施法（天赋树状态）',
    'combo_strikes': '连击',
    'damage_taken': '承受伤害',
    'deadly_momentum': '致命动能',
    'death_and_madness_death_check': '死亡与疯狂（死亡判定）',
    'demonsurge_demonsurge': '恶魔涌动（自身状态）',
    'divine_arbiter_divine_storm': '神圣仲裁者（神圣风暴）',
    'divine_arbiter_hammer_of_light': '神圣仲裁者（圣光之锤）',
    'divine_arbiter_verdict': '神圣仲裁者（裁决）',
    'doomsayer_in_combat': '末日预言者（战斗中）',
    'doomsayer_out_of_combat': '末日预言者（脱离战斗）',
    'dreadstalkers': '恐惧猎犬',
    'ebon_might_self': '黑檀之力（自身）',
    'embrace_of_the_cinderbee_orb': '烬蜂之拥（宝珠）',
    'emerald_trance_stacking': '翡翠入定（叠加）',
    'entropy_in_combat': '熵能（战斗中）',
    'entropy_out_of_combat': '熵能（脱离战斗）',
    'evoker_augmentation_tww1_4pc': '唤魔师增辉（地心之战第1赛季四件套）',
    'expel_harm_accumulator': '移花接木（累积量）',
    'fake_solidarity': '团结（模拟状态）',
    'fake_solidarity_bulwark': '团结壁垒（模拟状态）',
    'feed_the_flames_pyre': '火上浇油（葬火）',
    'fel_rush_movement': '邪能冲撞（位移）',
    'flask_of_alchemical_chaos_vers': '炼金混沌合剂（全能）',
    'frozen_in_time': '时间冻结',
    'hatred': '仇恨',
    'heart_of_the_jade_serpent_unity_within': '青玉之心（内在团结）',
    'heart_of_the_jade_serpent_yulons_avatar': '青玉之心（玉珑化身）',
    'heroic_charge': '英勇冲锋',
    'heroic_leap_movement': '英勇飞跃（位移）',
    'in_firestorm': '火焰风暴中',
    'incarnation_avatar_of_ashamane_prowl': '化身：阿莎曼之灵（潜行）',
    'ingenious_mana_battery_recovery': '精巧法力电池（恢复）',
    'ingenious_mana_battery_stored': '精巧法力电池（储存）',
    'intervene_movement': '援护（位移）',
    'invoke_power_infusion_0': '调用灌注能量（编号0）',
    'iridescence_blue_disintegrate': '虹彩（蓝色：裂解）',
    'item_cd_1141': '物品冷却组1141',
    'maximum_stagger': '醉拳最大值',
    'metamorphosis_movement': '恶魔变形（位移）',
    'mid1_4pc_buff': '中期第1套四件套（增益）',
    'mid2_assassination_2pc': '中期第2套刺杀两件套',
    'moons': '月相',
    'never_say_die_leech': '绝不言败（吸血）',
    'none_stagger': '无醉拳',
    'out_of_range': '超出范围',
    'overcharge_tier': '过载层级',
    'pet_movement': '宠物移动',
    'pick_up_fragment': '拾取灵魂残片',
    'scars_of_fraternal_strife_1': '兄弟之争的伤痕（第1层）',
    'scars_of_fraternal_strife_2': '兄弟之争的伤痕（第2层）',
    'scars_of_fraternal_strife_3': '兄弟之争的伤痕（第3层）',
    'scars_of_fraternal_strife_4': '兄弟之争的伤痕（第4层）',
    'scars_of_fraternal_strife_5': '兄弟之争的伤痕（第5层）',
    'seed_of_corruption_is_out_dnt': '腐蚀之种已出手（内部状态）',
    'seething_rage': '沸腾之怒',
    'sentinel_decay': '哨兵层数衰减',
    'shadowform_state': '暗影形态状态',
    'shield_charge_movement': '盾牌冲锋（位移）',
    'singularity_supreme_lockout': '至高奇点（锁定）',
    'smite': '惩击',
    'soul_of_the_forest_tree': '丛林之魂（天赋树状态）',
    'spoils_of_neltharus_vers': '奈萨鲁斯战利品（全能）',
    't31_2pc_proc': '第31套装两件套（触发）',
    't31_2pc_stacks': '第31套装两件套（层数）',
    'thundercharge': '雷霆充能',
    'tigereye_brew_1_accumulator': '虎眼酒（第1组累积量）',
    'tigereye_brew_3': '虎眼酒（第3组）',
    'time_convergence_intellect': '时间汇聚（智力）',
    'time_thiefs_gambit': '时光窃贼的豪赌',
    'touch_of_death_ww': '轮回之触（踏风）',
    'twist_of_fate_can_trigger_on_ally_heal': '命运多舛（盟友治疗可触发）',
    'twist_of_fate_can_trigger_on_self_heal': '命运多舛（自身治疗可触发）',
    'tyrant': '恶魔暴君',
    'unbound_surge_mid1_evoker_devastation_sc': '无拘奔涌（中期第1套：湮灭单体）',
    'unbound_surge_runtime_evoker_augmentation': '无拘奔涌（运行时：增辉）',
    'vengeful_retreat_movement': '复仇回避（位移）',
    'vilefiend': '邪犬',
    'void_volley_set_bonus': '虚空齐射（套装加成）',
    'void_volley_set_bonus_effectiveness': '虚空齐射（套装加成效果系数）',
    'voidstep': '虚空步',
    'whirling_dragon': '旋风之龙',
    'wild_charge_movement': '野性冲锋（位移）',
    'wild_imps': '野生小鬼',
}

# 从长到短匹配，避免 `_rating` 抢先截断 `_haste_rating`。
TOKEN_SUFFIXES = (
    ('_versatility_rating', '全能等级'),
    ('_mastery_rating', '精通等级'),
    ('_haste_rating', '急速等级'),
    ('_crit_rating', '暴击等级'),
    ('_shadowfrost', '暗影冰霜'),
    ('_internal_cooldown', '内置冷却'),
    ('_cooldown', '冷却'),
    ('_debuff', '减益'),
    ('_buff', '增益'),
    ('_damage', '伤害'),
    ('_healing', '治疗'),
    ('_heal', '治疗'),
    ('_versatility', '全能'),
    ('_mastery', '精通'),
    ('_haste', '急速'),
    ('_crit', '暴击'),
    ('_stacks', '层数'),
    ('_stack', '层数'),
    ('_counter', '计数器'),
    ('_tracker', '追踪器'),
    ('_driver', '驱动状态'),
    ('_trigger', '触发状态'),
    ('_marker', '标记'),
    ('_builder', '积累状态'),
    ('_passive', '被动'),
    ('_active', '激活'),
    ('_proc', '触发'),
    ('_icd', '内置冷却'),
    ('_dnt', '内部状态'),
    ('_tt', '内部状态'),
    ('_fwf', '冰霜状态'),
)

LISTVIEW_PRIORITY = {
    'talents': 100,
    'specializations': 98,
    'pvp-talents': 96,
    'artifact-traits': 92,
    'azerite-essences': 90,
    'item-effects': 88,
    'uncategorized-spells': 70,
    'npc-abilities': 40,
}

_SEARCH_DATA_RE = re.compile(
    r'<script[^>]+type="application/json"[^>]+id="data\.([^"]+)"[^>]*>'
    r'(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def normalized_name(value):
    """比较 Wowhead 英文名时忽略大小写、空格、连字符和所有格标点。"""
    return re.sub(r'[^a-z0-9]+', '', html.unescape(str(value or '')).casefold())


def token_to_search_name(token):
    text = str(token or '').strip().lower().replace('_', ' ')
    replacements = {
        ' dnd ': ' death and decay ',
        ' aoe ': ' area of effect ',
    }
    padded = f' {text} '
    for source, target in replacements.items():
        padded = padded.replace(source, target)
    return re.sub(r'\s+', ' ', padded).strip()


def split_token_suffixes(token):
    base = str(token or '').strip().lower()
    labels = []
    changed = True
    while changed and base:
        changed = False
        for suffix, label in TOKEN_SUFFIXES:
            if base.endswith(suffix) and len(base) > len(suffix):
                base = base[:-len(suffix)]
                labels.insert(0, label)
                changed = True
                break
    tier_match = re.search(r'_(?:t|tier)?(\d+)_([24])pc$', base)
    if tier_match:
        base = base[:tier_match.start()]
        labels.insert(0, f'{tier_match.group(1)}级套装{tier_match.group(2)}件套')
    else:
        piece_match = re.search(r'_([24])pc$', base)
        if piece_match:
            base = base[:piece_match.start()]
            labels.insert(0, f'{piece_match.group(1)}件套')
    return base or str(token or '').strip().lower(), labels


def search_queries_for_token(token):
    token = str(token or '').strip().lower()
    base, _labels = split_token_suffixes(token)
    queries = [token_to_search_name(token)]
    base_query = token_to_search_name(base)
    if base_query and base_query not in queries:
        queries.append(base_query)
    return tuple(query for query in queries if query)


def parse_wowhead_search_html(text):
    """解析搜索页中的 Spell Listview JSON，不执行远端 JavaScript。"""
    datasets = {}
    for key, body in _SEARCH_DATA_RE.findall(str(text or '')):
        try:
            value = json.loads(html.unescape(body))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, list):
            datasets[key] = value

    candidates = []
    for block in str(text or '').split('new Listview({')[1:]:
        head = block.split('});', 1)[0]
        template = re.search(r'template:\s*"([^"]+)"', head)
        listview_id = re.search(r'id:\s*"([^"]+)"', head)
        data_key = re.search(r'WH\.getPageData\("([^"]+)"\)', head)
        if not (template and listview_id and data_key) or template.group(1) != 'spell':
            continue
        for row in datasets.get(data_key.group(1), []):
            if not isinstance(row, dict):
                continue
            spell_id = row.get('id')
            if not isinstance(spell_id, int) or isinstance(spell_id, bool) or spell_id <= 0:
                continue
            candidates.append({
                'spell_id': spell_id,
                'name_en': str(row.get('name') or row.get('displayName') or '').strip(),
                'listview': listview_id.group(1),
                'category': row.get('cat'),
                'class_mask': row.get('chrclass') or row.get('reqclass'),
                'search_popularity': int(row.get('searchpopularity') or 0),
            })
    unique = {}
    for candidate in candidates:
        key = (candidate['spell_id'], candidate['listview'])
        unique[key] = candidate
    return sorted(unique.values(), key=lambda item: (
        -LISTVIEW_PRIORITY.get(item['listview'], 50),
        -item['search_popularity'], item['spell_id'],
    ))


def exact_search_candidates(query, candidates):
    expected = normalized_name(query)
    return [
        candidate for candidate in candidates
        if normalized_name(candidate.get('name_en')) == expected
    ]


def _class_compatible(candidate, class_name):
    mask = candidate.get('class_mask')
    expected = CLASS_MASKS.get(str(class_name or '').strip().lower())
    if not isinstance(mask, int) or isinstance(mask, bool) or not expected:
        return True
    return bool(mask & expected)


def resolve_wowhead_search(query, candidates, class_name):
    """只接受最高可信类别内中文唯一的精确英文名候选。"""
    exact = [
        candidate for candidate in exact_search_candidates(query, candidates)
        if _class_compatible(candidate, class_name) and candidate.get('name_zh')
    ]
    if not exact:
        return None
    best_priority = max(LISTVIEW_PRIORITY.get(row.get('listview'), 50) for row in exact)
    best = [
        row for row in exact
        if LISTVIEW_PRIORITY.get(row.get('listview'), 50) == best_priority
    ]
    names = {str(row.get('name_zh') or '').strip() for row in best if row.get('name_zh')}
    if len(names) != 1:
        return None
    name_zh = names.pop()
    return {
        'name_zh': name_zh,
        'query': query,
        'candidate_spell_ids': sorted({row['spell_id'] for row in best}),
        'candidate_listviews': sorted({row['listview'] for row in best}),
        'candidate_english_names': sorted({row['name_en'] for row in best}),
    }


def _unique_name_map(rows, key):
    values = defaultdict(set)
    for row in rows:
        name_zh = str(row.get('name_zh') or '').strip()
        if name_zh:
            values[key(row)].add(name_zh)
    return {identity: next(iter(names)) for identity, names in values.items() if len(names) == 1}


def fact_identity(fact):
    return (
        str(fact.get('class_name') or ''), str(fact.get('spec') or ''),
        str(fact.get('hero_tree') or ''), str(fact.get('token') or ''),
        str(fact.get('symbol_kind') or ''),
    )


def _semantic_with_suffix(base_name, labels):
    if not labels:
        return base_name
    return f"{base_name}（{'、'.join(dict.fromkeys(labels))}）"


def _resolution(name_zh, source, status, evidence):
    return {
        'name_zh': str(name_zh or '').strip(),
        'localization_source': source,
        'localization_status': status,
        'evidence': evidence,
    }


def _official_localization_indexes(official_facts):
    return {
        'exact': _unique_name_map(
            official_facts, lambda row: (row.get('symbol_kind'), row.get('token')),
        ),
        'class_token': _unique_name_map(
            official_facts, lambda row: (row.get('class_name'), row.get('token')),
        ),
        'token': _unique_name_map(official_facts, lambda row: row.get('token')),
    }


def resolve_fact_localization(
        fact, *, official_facts, search_records, translations, official_indexes=None):
    token = str(fact.get('token') or '').strip().lower()
    kind = str(fact.get('symbol_kind') or '').strip().lower()
    class_name = str(fact.get('class_name') or '').strip().lower()
    indexes = official_indexes or _official_localization_indexes(official_facts)
    exact_names = indexes['exact']
    name = exact_names.get((kind, token))
    if name:
        return _resolution(name, 'catalog_exact', 'inferred_exact', {
            'method': 'same_kind_token_unique', 'token': token, 'symbol_kind': kind,
        })

    class_token_names = indexes['class_token']
    name = class_token_names.get((class_name, token))
    if name:
        return _resolution(name, 'catalog_cross_kind', 'inferred_exact', {
            'method': 'same_class_token_unique', 'token': token,
        })
    token_names = indexes['token']
    name = token_names.get(token)
    if name:
        return _resolution(name, 'catalog_cross_kind', 'inferred_exact', {
            'method': 'global_token_unique', 'token': token,
        })

    if kind == 'action' and token in CONTROL_ACTION_LABELS:
        return _resolution(
            CONTROL_ACTION_LABELS[token], 'simc_control_dictionary', 'inferred_semantic',
            {'method': 'control_action_dictionary', 'token': token},
        )

    queries = search_queries_for_token(token)
    for query in queries:
        record = search_records.get(query) or {}
        resolved = resolve_wowhead_search(query, record.get('candidates') or [], class_name)
        if resolved and query == queries[0]:
            return _resolution(
                resolved['name_zh'], 'wowhead_name_search', 'inferred_official',
                {'method': 'exact_english_name', **resolved},
            )
        if resolved:
            _base, labels = split_token_suffixes(token)
            return _resolution(
                _semantic_with_suffix(resolved['name_zh'], labels),
                'wowhead_base_semantic', 'inferred_semantic',
                {'method': 'base_english_name_with_suffix', 'suffix_labels': labels, **resolved},
            )

    base_token, labels = split_token_suffixes(token)
    base_name = token_names.get(base_token)
    if base_name and labels:
        return _resolution(
            _semantic_with_suffix(base_name, labels),
            'catalog_base_semantic', 'inferred_semantic',
            {'method': 'catalog_base_with_suffix', 'base_token': base_token,
             'suffix_labels': labels},
        )

    semantic_name = SEMANTIC_TOKEN_LABELS.get(token)
    if semantic_name:
        return _resolution(
            semantic_name, 'manual_semantic_dictionary', 'inferred_semantic',
            {'method': 'manual_semantic_dictionary', 'token': token},
        )

    phrase = token_to_search_name(token)
    translation_record = translations.get(phrase)
    if isinstance(translation_record, dict):
        translated = str(translation_record.get('name_zh') or '').strip()
    else:
        translated = str(translation_record or '').strip()
    if translated:
        return _resolution(
            translated, 'machine_translation', 'inferred_semantic',
            {'method': 'machine_translation', 'query': phrase},
        )
    return _resolution(
        f'内部状态：{phrase}', 'token_fallback', 'inferred_semantic',
        {'method': 'english_token_fallback', 'query': phrase},
    )


def build_localization_overrides(payload, *, search_cache=None, translation_cache=None):
    facts = payload.get('facts') if isinstance(payload, dict) else None
    if not isinstance(facts, list):
        raise ValueError('APL 元数据包 facts 无效')
    official_facts = [fact for fact in facts if str(fact.get('name_zh') or '').strip()]
    official_indexes = _official_localization_indexes(official_facts)
    search_records = (search_cache or {}).get('records') or {}
    translations = (translation_cache or {}).get('records') or {}
    records = []
    for fact in facts:
        if str(fact.get('name_zh') or '').strip():
            continue
        resolved = resolve_fact_localization(
            fact, official_facts=official_facts, search_records=search_records,
            translations=translations, official_indexes=official_indexes,
        )
        records.append({
            'class_name': fact.get('class_name'),
            'spec': fact.get('spec'),
            'hero_tree': fact.get('hero_tree'),
            'token': fact.get('token'),
            'symbol_kind': fact.get('symbol_kind'),
            **resolved,
        })
    result = {
        'schema_version': OVERRIDE_SCHEMA_VERSION,
        'package_type': 'simc_apl_localization_overrides',
        'resolver_version': RESOLVER_VERSION,
        'simc_revision': payload.get('simc_revision'),
        'game_build': payload.get('game_build'),
        'source_payload_sha256': payload.get('source_payload_sha256'),
        'records': records,
    }
    result['counts'] = {
        'record_count': len(records),
        'source_counts': dict(sorted(Counter(row['localization_source'] for row in records).items())),
        'status_counts': dict(sorted(Counter(row['localization_status'] for row in records).items())),
        'blank_count': sum(not row['name_zh'] for row in records),
    }
    return validate_localization_overrides(result)


def required_wowhead_queries(payload):
    """返回包内继承、控制词典和已知后缀仍无法解决的唯一英文查询。"""
    provisional = build_localization_overrides(payload, search_cache={}, translation_cache={})
    queries = set()
    for row in provisional['records']:
        if row['localization_source'] != 'token_fallback':
            continue
        queries.update(search_queries_for_token(row['token']))
    return tuple(sorted(queries))


def validate_localization_overrides(payload):
    if not isinstance(payload, dict) or payload.get('schema_version') != OVERRIDE_SCHEMA_VERSION:
        raise ValueError('本地化覆盖 schema_version 无效')
    if payload.get('package_type') != 'simc_apl_localization_overrides':
        raise ValueError('本地化覆盖 package_type 无效')
    if not re.fullmatch(r'[0-9a-f]{40}', str(payload.get('simc_revision') or '')):
        raise ValueError('本地化覆盖 simc_revision 无效')
    if not re.fullmatch(r'[0-9a-f]{64}', str(payload.get('source_payload_sha256') or '')):
        raise ValueError('本地化覆盖 source_payload_sha256 无效')
    records = payload.get('records')
    if not isinstance(records, list):
        raise ValueError('本地化覆盖 records 必须为数组')
    identities = set()
    for index, row in enumerate(records):
        if not isinstance(row, dict) or not str(row.get('name_zh') or '').strip():
            raise ValueError(f'本地化覆盖 records[{index}] 缺少中文')
        identity = fact_identity(row)
        if not identity[3] or not identity[4] or identity in identities:
            raise ValueError(f'本地化覆盖 records[{index}] 身份无效或重复')
        identities.add(identity)
        if not isinstance(row.get('evidence'), dict):
            raise ValueError(f'本地化覆盖 records[{index}].evidence 必须为对象')
    expected = {
        'record_count': len(records),
        'source_counts': dict(sorted(Counter(row['localization_source'] for row in records).items())),
        'status_counts': dict(sorted(Counter(row['localization_status'] for row in records).items())),
        'blank_count': sum(not row['name_zh'] for row in records),
    }
    if payload.get('counts') != expected:
        raise ValueError('本地化覆盖 counts 与 records 不一致')
    return payload


def apply_localization_overrides(payload, overrides):
    validate_localization_overrides(overrides)
    for field in ('simc_revision', 'game_build', 'source_payload_sha256'):
        if payload.get(field) != overrides.get(field):
            raise ValueError(f'本地化覆盖 {field} 与 APL 数据包不一致')
    result = copy.deepcopy(payload)
    by_identity = {fact_identity(row): row for row in overrides['records']}
    applied = set()
    for fact in result['facts']:
        row = by_identity.get(fact_identity(fact))
        if not row:
            continue
        if fact.get('name_zh'):
            raise ValueError(f'本地化覆盖试图替换已有官方中文: {fact_identity(fact)!r}')
        fact['name_zh'] = row['name_zh']
        fact['localization_source'] = row['localization_source']
        fact['localization_status'] = row['localization_status']
        fact.setdefault('metadata', {})['localization_evidence'] = row['evidence']
        applied.add(fact_identity(fact))
    missing = set(by_identity) - applied
    if missing:
        raise ValueError(f'本地化覆盖含 {len(missing)} 条数据包不存在的身份')
    serialized = json.dumps(overrides, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    result['localization_enrichment'] = {
        'resolver_version': overrides['resolver_version'],
        'override_sha256': hashlib.sha256(serialized.encode('utf-8')).hexdigest(),
        'counts': overrides['counts'],
    }
    facts = result['facts']
    result['counts']['localized_count'] = sum(bool(fact['name_zh']) for fact in facts)
    result['counts']['missing_zh_count'] = sum(not fact['name_zh'] for fact in facts)
    return result


def load_localization_overrides(path):
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'无法读取 APL 本地化覆盖 {path}: {exc}') from exc
    return validate_localization_overrides(payload)
