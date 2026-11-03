"""从未归一化的全法术全集独立推导结果类别，复核归零、上表明细和运算来源。"""
import argparse
from collections import Counter
from functools import lru_cache
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from simc_scope_resolution import COMMON_SELECTORS, CATEGORY_SELECTORS


# 原始 DBC 枚举；不读取导出器已声明的全局状态作为预期。
AURA_TYPES = {6, 35, 65, 119, 128, 129, 143, 174, 202}
RESULT_TYPES = {52, 57, 71, 79, 163, 168, 193, 270, 276, 290, 303,
                306, 308, 319, 342, 343, 344, 380, 381, 429, 501, 530, 531}
PLAYER_RESULT_FIELDS = {
    'passive_player': {'damage_multiplier', 'auto_attack_multiplier', 'guardian_damage_multiplier',
                       'pet_damage_multiplier', 'crit_damage_multiplier', 'all_crit', 'all_haste',
                       'attack_speed', 'attack_speed_modifier'},
    'player_registry': {'player_multiplier', 'crit_chance', 'auto_attack_speed', 'pet_multiplier'},
    'target_player_registry': {'target_multiplier', 'pet_multiplier'},
}


def expected_global(spell, effect):
    if effect['type'] not in AURA_TYPES:
        return False
    if not any(effect['flags']) and effect['subtype'] in RESULT_TYPES:
        return True
    numeric = (effect['subtype'] in (107, 108) and effect['misc1'] in (0, 7, 15, 22)) or effect['subtype'] in (271, 308, 501)
    if not numeric:
        return False
    selectors = list(CATEGORY_SELECTORS.get(spell['class_family'], []))
    if spell['class_family'] in COMMON_SELECTORS:
        selectors.append(COMMON_SELECTORS[spell['class_family']])
    return (any(effect['flags'][word] & mask for word, mask, _ in selectors)
            or spell['class_family'] == 10 and effect['flags'][1] & 16384 and effect['flags'][2] & 2)


def physical_parts(facts):
    return {(p['spell_id'], index) for fact in facts for p in fact.get('global_components', [])
            for index in p.get('effect_indices', [p.get('effect_index')]) if index}


@lru_cache(maxsize=2)
def load_raw_inventory(path):
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    if raw.get('raw_catalog_schema') != 2 or raw.get('normalized') is not False:
        raise ValueError('需要未归一化的全法术原始目录')
    spells = {s['spell_id']: s for s in raw['spell_catalog']}
    families = {}
    for trait in raw['talent_catalog']:
        family = spells.get(trait['spell_id'], {}).get('class_family', 0)
        if family:
            families.setdefault(trait['class_id'], set()).add(family)
    if any(len(values) != 1 for values in families.values()):
        raise ValueError('原始天赋目录无法确定唯一职业族')
    racial = raw.get('racial_catalog')
    if not isinstance(racial, list) or not racial:
        raise ValueError('缺少未归一化的种族能力原始目录')
    return spells, {key: next(iter(value)) for key, value in families.items()}, racial


def all_racial_sources(spells, catalog):
    """独立展开全部原始种族能力和触发边，不以已声明排除表作为预期。"""
    pending = [row['spell_id'] for row in catalog]
    seen = set()
    while pending:
        sid = pending.pop()
        if sid in seen:
            continue
        seen.add(sid)
        pending.extend(e['trigger'] for e in spells.get(sid, {}).get('effects', []) if e['trigger'])
    return seen


@lru_cache(maxsize=2)
def load_raw_talents(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))['talent_catalog']


_trait_authorities = {}


def trait_authority(raw_traits, spells):
    key = id(raw_traits), id(spells)
    if key in _trait_authorities:
        return _trait_authorities[key][2:]
    selections, entries, owners = {}, {}, {}
    for row in raw_traits:
        entries[row['trait_entry_id']] = row
        if row['tree_index'] == 4:
            selections.setdefault((row['class_id'], row['subtree_id']), set()).update(row['spec_ids'])
    for row in raw_traits:
        if row['tree_index'] not in (1, 2, 3) or not row['spell_id']:
            continue
        specs = selections.get((row['class_id'], row['subtree_id']), set()) if row['tree_index'] == 3 else (
            set(row['spec_ids']) | set(row.get('starter_spec_ids', [])) if row['spec_ids'] else set())
        for sid in all_racial_sources(spells, [{'spell_id': row['spell_id']}]):
            owner = owners.setdefault((row['class_id'], sid), {'specs': set(), 'shared': False})
            owner['specs'].update(specs)
            owner['shared'] |= row['tree_index'] != 3 and not specs
    _trait_authorities[key] = raw_traits, spells, selections, entries, owners
    return selections, entries, owners


def expected_parts(spells, family):
    return {(sid, e['index']) for sid, spell in spells.items()
                if spell['class_family'] in (0, family)
                for e in spell['effects'] if expected_global(spell, e)}


def inspect_actor(actor, spells, family, expected=None, racial_catalog=(), native_bindings=(), native_sources=None, native_racials=None, raw_traits=()):
    if expected is None:
        expected = expected_parts(spells, family)
    expected = set(expected)
    racials = all_racial_sources(spells, racial_catalog)
    auxiliary_racials = actor.get('native_racial_spell_ids', []) if native_racials is None else native_racials
    racials.update(all_racial_sources(spells, [{'spell_id': sid} for sid in auxiliary_racials]))
    expected.update((sid, e['index']) for sid in racials for e in spells.get(sid, {}).get('effects', [])
                    if expected_global(spells[sid], e))
    bank_parts = {(b['source_spell_id'], b['effect_index']) for b in native_bindings
                  if not b['pet'] and b['base_value'] and b['field'] in PLAYER_RESULT_FIELDS.get(b['layer'], set())}
    expected.update(bank_parts)
    readback = {(e['spell_id'], e['effect_index']): e for e in [*actor['normalized_scope_effects'], *actor.get('normalized_talent_effects', []), *actor.get('normalized_racial_effects', [])]}
    errors = []
    if actor.get('scope_inventory_version', 0) >= 5:
        selections, entries, owners = trait_authority(raw_traits, spells)
        class_id = int(actor.get('class_mask', 0)).bit_length()
        spec_id = actor.get('spec_id')
        valid = {subtree for (cls, subtree), specs in selections.items() if cls == class_id and spec_id in specs}
        if actor.get('hero_ownership_inventory_version') != 1 or not valid or set(actor.get('valid_hero_subtree_ids', [])) != valid:
            errors.append({'问题': '英雄树归属与原始选择节点不一致'})
        for entry in actor.get('selected_trait_ids', []):
            trait = entries.get(entry, {})
            if trait.get('tree_index') == 3 and trait.get('subtree_id') not in valid:
                errors.append({'问题': '实际选择了其他专精的英雄节点', '天赋': entry})
            elif trait.get('class_id') != class_id or trait.get('tree_index') in (1, 2) and trait.get('spec_ids') and spec_id not in set(trait['spec_ids']) | set(trait.get('starter_spec_ids', [])):
                errors.append({'问题': '实际选择了其他职业或专精的天赋', '天赋': entry})
        for fact in actor['reviewed_global_effects']:
            for sid in fact.get('source_spell_ids', []):
                owner = owners.get((class_id, sid))
                if owner and not owner['shared'] and spec_id not in owner['specs']:
                    errors.append({'问题': '上表借用了其他专精的天赋来源', '来源': sid})
    if actor.get('scope_inventory_version', 0) >= 2 and actor.get('probe_context_inventory_version') != 1:
        errors.append({'问题': '缺少原生执行上下文门禁版本'})
    if actor.get('scope_inventory_version', 0) >= 3 and (not racial_catalog or not actor.get('race_mask') or not actor.get('class_mask')):
        errors.append({'问题': '缺少可独立核对的种族身份或原始目录'})
    if racial_catalog:
        if actor.get('racial_policy') != 'exclude_all' or set(actor.get('excluded_racial_spell_ids', [])) != racials:
            errors.append({'问题': '种族排除清单与原始全集不一致'})
        racial_readback = {(e['spell_id'], e['effect_index']): e for e in actor.get('normalized_racial_effects', [])}
        for sid in racials:
            for original in spells.get(sid, {}).get('effects', []):
                part = sid, original['index']
                observed = racial_readback.get(part)
                if observed is None or any(observed.get(field) != 0 for field in (
                        'actual_base_value', 'actual_sp_coefficient', 'actual_ap_coefficient', 'actual_scale_coefficient')):
                    errors.append({'问题': '种族分量未完整归零回读', '分量': part})
        for action in actor['actions']:
            states = [*action.get('activation_conditions', []), *(s for scene in action.get('scenarios', []) for s in scene.get('buffs', []))]
            if (action.get('spell_id') in racials or action.get('reporting_root_spell_id') in racials
                    or any(s.get('spell_id') in racials for s in states)):
                errors.append({'问题': '种族能力仍进入技能或条件', '技能': action.get('token')})
    global_ids = {part[0] for part in expected}
    global_ids.update(s['spell_id'] for s in actor.get('global_damage_states', []))
    for action in actor['actions']:
        for scenario in action.get('scenarios', []):
            for state in scenario.get('buffs', []):
                if state.get('spell_id') in global_ids:
                    errors.append({'问题': '原生普通场景仍枚举全局状态',
                                   '技能': action.get('token'), '状态': state})
    for part in sorted(expected):
        e = readback.get(part)
        if e is None:
            errors.append({'问题': '独立发现未归一化分量', '分量': part})
        elif e['actual_base_value'] != 0 or e.get('actual_mastery_coefficient', 0) != 0:
            errors.append({'问题': '公共分量归零回读失败', '分量': part, '回读': e})
    facts = actor['reviewed_global_effects']
    parts = physical_parts(facts)
    sources = {sid for fact in facts for sid in fact.get('source_spell_ids', [])}
    if sources.intersection(racials) or {sid for sid, _ in parts}.intersection(racials):
        errors.append({'问题': '种族来源仍出现在上方全局表'})
    if actor.get('scope_inventory_version', 0) >= 4:
        registered = actor.get('native_passive_spell_ids') if native_sources is None else native_sources
        if actor.get('native_registration_inventory_version') != 1 or not isinstance(registered, list):
            errors.append({'问题': '缺少完整原生注册来源证据'})
        else:
            required = {(sid, e['index']) for sid in set(registered) - racials
                        for e in spells.get(sid, {}).get('effects', [])
                        if spells[sid]['class_family'] in (0, family) and expected_global(spells[sid], e)
                        and (e['value'] or e['mastery_coefficient'])}
            for part in sorted(required - parts):
                errors.append({'问题': '上表遗漏原生实际注册的整项来源', '分量': part})
    # 同一来源不能只展示已经复核的旧分量，遗漏新发现的结果类别。
    required_details = {(sid, e['index']) for sid in sources for e in spells.get(sid, {}).get('effects', [])
                        if expected_global(spells[sid], e) and (e['value'] or e['mastery_coefficient'])}
    for part in sorted(required_details - parts):
        errors.append({'问题': '上表来源缺少公共分量', '分量': part})
    details = {(d.get('source_spell_id'), d.get('effect_index'))
               for f in facts for d in f.get('effect_details', [])}
    for fact in facts:
        for detail in fact.get('effect_details', []):
            label = str(detail.get('label') or '')
            if not label or label in {'公共效果分量', '未标注效果类别'} or '掩码' in label:
                errors.append({'问题': '上表类别明细未完整解释', '明细': detail})
            if fact.get('scope_evidence') == 'dbc_semantic_effect_scope':
                spell = spells.get(detail.get('source_spell_id'), {})
                raw = next((e for e in spell.get('effects', []) if e['index'] == detail.get('effect_index')), None)
                if raw is None or any(detail.get(key) != raw[key] for key in ('type', 'subtype', 'misc1', 'misc2')):
                    errors.append({'问题': '新明细缺少匹配的原始效果属性', '明细': detail})
    for part in sorted(parts - details):
        errors.append({'问题': '上表缺少对应效果明细', '分量': part})
    # 独立类别预期继续对照实际运算链，不以字段自报已排除代替数值检查。
    checked = 0
    def walk(value):
        nonlocal checked
        if isinstance(value, dict):
            for source in value.get('runtime_factor_sources', []):
                part = (source.get('source_spell_id'), source.get('effect_index'))
                if part in expected:
                    checked += 1
                    if abs(source.get('factor', 1) - 1) > 1e-12:
                        errors.append({'问题': '公共分量仍参与原生运算', '证据': source})
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(actor['actions'])
    return {'独立公共分量数': len(expected), '排除种族来源数': len(racials), '原生玩家结果分量数': len(bank_parts),
            '上表分量数': len(parts), '运算来源回读数': checked, '错误': errors}


def inspect_upper_projection(actor, product_actor):
    expected = physical_parts(actor['reviewed_global_effects'])
    displayed = product_actor.get('global_skill_effects') or []
    details = {(d.get('source_spell_id'), d.get('effect_index'))
               for effect in displayed for d in effect.get('effect_details', [])}
    return [{'问题': '上表产品投影缺少冻结分量或对应明细', '分量': part}
            for part in sorted(expected - (physical_parts(displayed) & details))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', type=Path, required=True)
    parser.add_argument('--run', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    read = lambda path: json.loads(path.read_text(encoding='utf-8'))
    raw = read(args.native / 'raw-dbc-scope-catalog.json')
    if raw.get('raw_catalog_schema') != 2 or raw.get('normalized') is not False:
        raise ValueError('需要未归一化的全法术原始目录')
    spells = {s['spell_id']: s for s in raw['spell_catalog']}
    reports = []
    failures = []
    for directory in args.run:
        for summary in read(directory / 'summary.json'):
            key = summary['class'] + '_' + summary['spec']
            if summary['status'] != '已生成，待逐项审计':
                failures.append({'配置': directory.name + '/' + key, '原因': summary.get('error')})
                continue
            native_name = ('talents' if 'talents' in directory.name else 'base') + '--' + key + '.json'
            native = read(args.native / native_name)['actors'][0]
            families = {b['owner_class_family'] for b in native['bindings'] if not b['pet']}
            if len(families) != 1:
                raise ValueError(key + ' 缺少唯一原生职业族')
            family = next(iter(families))
            for health in (100, 34):
                actor = read(directory / f'{key}-{health}.json')['actors'][0]
                canonical = lambda value: re.sub(r'[^a-z0-9]', '', str(value).casefold())
                spec_matches = canonical(native.get('spec')) == canonical(actor.get('spec', '') + actor.get('class', ''))
                if not spec_matches or any(actor.get(k) != native.get(k) for k in ('class', 'race', 'race_mask', 'class_mask')) or set(actor['selected_trait_ids']) != set(native['selected_trait_ids']):
                    raise ValueError(key + ' 原生证据与归一化配置身份不一致')
                if actor.get('scope_inventory_version', 0) >= 4 and any(
                        not isinstance(native.get(field), list) for field in ('native_passive_spell_ids', 'native_racial_spell_ids')):
                    raise ValueError(key + ' 未归一化的独立证据缺少完整注册来源')
                result = inspect_actor(actor, spells, family, racial_catalog=raw['racial_catalog'], native_bindings=native['bindings'],
                                       native_sources=native.get('native_passive_spell_ids'), native_racials=native.get('native_racial_spell_ids'), raw_traits=raw['talent_catalog'])
                if health == 100:
                    product_actor = read(directory / f'{key}-product.json')['actors'][0]
                    result['错误'].extend(inspect_upper_projection(actor, product_actor))
                reports.append({'专精': key, '配置': directory.name, '目标血量': health, **result})
                print(key, directory.name, health, '错误', len(result['错误']), flush=True)
    result = {'原始法术数': len(spells), '原始效果数': sum(len(s['effects']) for s in spells.values()),
              '检查配置数': len(reports), '错误数': sum(len(r['错误']) for r in reports),
              '未生成配置': failures, '结果': reports}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    if result['错误数']:
        raise ValueError('独立完整性复核未通过，详见报告')


if __name__ == '__main__':
    main()
