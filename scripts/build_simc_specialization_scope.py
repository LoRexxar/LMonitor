"""按固定版本复核专精固有光环，生成公共分量补充契约及可重放补丁。"""
import argparse
from collections import defaultdict
import difflib
import hashlib
import json
from pathlib import Path
import re
import subprocess

from scripts.simc_scope_resolution import (
    CATEGORY_AURAS, CATEGORY_SELECTORS, COMMON_SELECTORS, REVIEWED_REVISION,
    aggregate, load_dbc,
)
from botend.constants.wow import SPEC_IDENTITY_MAP
from scripts.build_simc_global_damage_review import KEYS, SPEC_LABELS

BUILD = '12.1.5.70575'
# 固有光环身份来自该版 specialization_spells；名称只用于展示，不用于分类。
AURAS = {
    71:137049,72:137050,73:137048,65:137029,66:137028,70:137027,
    253:137015,254:137016,255:137017,259:137037,260:137036,261:137035,
    256:137032,257:137031,258:137033,250:137008,251:137006,252:137007,
    262:137040,263:137041,264:137039,62:137021,63:137019,64:137020,
    265:137043,266:137044,267:137046,268:137023,270:137024,269:137025,
    102:137013,103:137011,104:137010,105:137012,577:212612,581:212613,
    1480:1213636,1467:356809,1468:356810,1473:396186,
}
# 本次补充复核的类别选择器，仅适用于上面的固有光环；不会扩大旧天赋分类规则。
# 集合包含该类别下的技能及其派生分量，复核文件保留完整集合以检测后续漂移。
EXTRA_SELECTORS = {
    8: [(1, 4194304, '潜行者通用伤害技能')],
    10: [(1, 16384, '圣骑士通用伤害技能'), (0, 134217728, '圣骑士精通适用伤害类别')],
    107: [(2, 64, '复仇专精通用伤害技能')],
}


# Historical reviews are immutable flags-only projections, not current DBC facts.
LEGACY_PROJECTION = ('1d3897ef4adefc8e47a43989eed81b6618ce7bef', '12.1.0.69814')


def legacy_projection(revision, build):
    return (revision, build) == LEGACY_PROJECTION


class SupplementAffectedScope:
    """Match the native DBC selector dispatch, shared by both supplements.

    107/108 retain family flags; 218/219/320 use misc2 labels. 271/537
    use spell_data_t::affected_by_all (flags OR label OR category).
    Selection does not imply that a modifier changes damage.
    """
    def __init__(self, source, spells, legacy=False):
        self.spells, self.legacy = spells, legacy
        self.labels = defaultdict(set)
        if legacy:
            return
        header = (source / 'engine/dbc/spell_data.hpp').read_text(encoding='utf-8')
        body = header.split('struct spelllabel_data_t\n{', 1)[1].split('unsigned id_spell()', 1)[0]
        fields = re.findall(r'\b(_\w+)\s*;', re.sub(r'//[^\n]*', '', body))
        if set(fields) != {'_id_spell', '_label'}:
            raise ValueError('DBC label 结构漂移')
        active = found = False
        for line in (source / 'engine/dbc/generated/sc_spell_data.inc').read_text(encoding='utf-8').splitlines():
            if re.match(r'static (?:constexpr std::array<spelllabel_data_t,|spelllabel_data_t )', line):
                active = found = True
            elif active and line.startswith('}'):
                break
            elif active and line.startswith('  {'):
                values = aggregate(line)
                if len(values) != len(fields):
                    raise ValueError('DBC label 数据与同版声明不匹配')
                row = dict(zip(fields, values))
                self.labels[row['_label']].add(row['_id_spell'])
        if not found:
            raise ValueError('DBC label 表缺失')

    def affected(self, effect):
        family = self.spells[effect['_spell_id']]['_class_flags_family']
        subtype = effect['_subtype']
        def flags():
            return {s['_id'] for s in self.spells.values()
                    if s['_class_flags_family'] == family and any(
                        a & b for a, b in zip(s['_class_flags'], effect['_class_flags']))}
        if self.legacy or subtype in (107, 108, 220, 416, 417):
            return sorted(flags())
        if subtype in (218, 219, 320):
            return sorted(self.labels[effect['_misc_value_2']])
        if subtype in (271, 537):
            # affected_by_label uses misc1 for 537, misc2 for 271.
            label = effect['_misc_value'] if subtype == 537 else effect['_misc_value_2']
            category = effect['_misc_value']
            return sorted(flags() | self.labels[label] | {
                s['_id'] for s in self.spells.values()
                if category > 0 and s['_category'] == category})
        return []


def local_damage_component(part, legacy=False):
    # 218 is percent; 219 is flat (P_EFFECT_N is not a damage multiplier).
    # 320 is hasted GCD, not damage. Duration/tick-time must never leak here.
    subtypes = (107, 108) if legacy else (107, 108, 218, 219)
    return (part['子类型'] in subtypes and part['参数'] in (0, 22, 7, 15)
            and bool(part['完整DBC集合']))


def specialization_token(spec_id):
    value = SPEC_IDENTITY_MAP[spec_id][1]
    return {'BeastMastery':'beast_mastery'}.get(value, value.lower())


def classify_component(family, effect):
    """只接纳明确的伤害类别与公共掩码；治疗、技能专属与未知分量保持原样。"""
    flags = effect['_class_flags']
    subtype = effect['_subtype']
    if effect['_type'] not in (6, 35, 65):
        return None
    if subtype in CATEGORY_AURAS and not any(flags):
        return CATEGORY_AURAS[subtype]
    if subtype not in (107, 108) or effect['_misc_value'] not in (0, 22, 7, 15):
        return None
    selectors = list(CATEGORY_SELECTORS.get(family, [])) + EXTRA_SELECTORS.get(family, [])
    if family in COMMON_SELECTORS:
        selectors.append(COMMON_SELECTORS[family])
    for word, bit, label in selectors:
        if flags[word] & bit:
            return label
    return None


def collect_review(source):
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    build_text = (source / 'engine/dbc/generated/client_data_version.inc').read_text(encoding='utf-8')
    if revision != REVIEWED_REVISION or f'"{BUILD}"' not in build_text:
        raise ValueError('专精补充复核的源码版本或客户端版本不匹配。')
    spells, effects = load_dbc(source)
    scope = SupplementAffectedScope(source, spells, legacy=legacy_projection(revision, BUILD))
    by_spell = defaultdict(list)
    for effect in effects.values():
        by_spell[effect['_spell_id']].append(effect)
    identities = set()
    for line in (source / 'engine/dbc/generated/specialization_spells.inc').read_text(encoding='utf-8').splitlines():
        if line.startswith('  {'):
            row = aggregate(line)
            if row[0]:
                identities.add((row[1], row[2]))
    rows = []
    for spec, sid in sorted(AURAS.items()):
        if (spec, sid) not in identities:
            raise ValueError(f'专精固有光环身份漂移：{spec}/{sid}')
        spell = spells[sid]
        family = spell['_class_flags_family']
        parts = []
        for effect in sorted(by_spell[sid], key=lambda e:e['_index']):
            category = classify_component(family, effect)
            affected = scope.affected(effect)
            parts.append({
                '效果ID':effect['_id'], '效果编号':effect['_index']+1,
                '类型':effect['_type'], '子类型':effect['_subtype'],
                '参数':effect['_misc_value'], '参数二':effect['_misc_value_2'],
                '基础值':effect['_base_value'], '选择位':effect['_class_flags'],
                '处理':'剔除公共分量' if category else '保持原样',
                '类别':category, '完整DBC集合':affected,
            })
        rows.append({'专精ID':spec, '专精':specialization_token(spec),
                     '职业':SPEC_IDENTITY_MAP[spec][0].lower(), '法术ID':sid,
                     '名称':spell['_name'], '职业族':family, '分量':parts})
    return {'源码提交':revision, '客户端版本':BUILD,
            '依据':'固定版本专精法术表、公共类别选择器及完整DBC集合；不按光环名称删除整条法术。',
            '条目':rows}


def extend_contract(contract, review):
    """合并逐效果事实和专精展示信息，摘要同时覆盖原契约与本次复核。"""
    if review.get('draft'):
        raise ValueError('专精草稿不可编入正式导出器契约。')
    if (f'"{review["源码提交"]}"' not in contract or
            f'"{review["客户端版本"]}"' not in contract):
        raise ValueError('基础契约与专精复核版本不一致。')
    if 'specialization_aura:' in contract:
        raise ValueError('基础契约已包含专精补充，拒绝重复应用。')
    additions, displays = [], []
    current = {(int(s), int(i)): (int(e), int(f), g) for s,i,e,f,g in re.findall(
        r'\{(\d+), (\d+), (\d+), (\d+), (true|false)\}', contract)}
    for row in review['条目']:
        parts = [p for p in row['分量'] if p['处理'] == '剔除公共分量']
        if not parts:
            continue
        local = [p for p in row['分量'] if p not in parts and local_damage_component(
            p, legacy=legacy_projection(review['源码提交'], review['客户端版本']))]
        def identity(part):
            return {'spell_id':row['法术ID'], 'effect_index':part['效果编号'], 'effect_id':part['效果ID']}
        for part in parts:
            key = (row['法术ID'], part['效果编号'])
            value = (part['效果ID'], row['职业族'], 'true')
            if key in current:
                if current[key] != value:
                    raise ValueError(f'专精补充与已有分量分类冲突：{key}')
            else:
                additions.append(f'  {{{key[0]}, {key[1]}, {value[0]}, {value[1]}, true}},')
        details = []
        for p in parts:
            label = {0:'直接伤害',22:'周期伤害',7:'暴击率',15:'暴击伤害'}.get(p['参数']) if p['子类型'] in (107,108) else p['类别']
            details.append({'label':f'{p["类别"]}：{label}' if p['子类型'] in (107,108) else label,
                            'source_spell_id':row['法术ID'], 'effect_index':p['效果编号'],
                            'base_value':p['基础值'], 'value_kind':'percentage_points' if p['参数']==7 and p['子类型'] in (107,108) else 'percent',
                            'conditional':False, 'evidence':'dbc_native'})
        display_name = f'{SPEC_LABELS[row["专精ID"]]}{KEYS[row["职业"]]}专精光环'
        fact = {'effect_id':f'specialization_aura:{row["法术ID"]}:{row["专精ID"]}',
                'source_type':'reviewed_scope', 'source_kind':'passive', 'source_name':row['名称'],
                'display_name':display_name, 'source_spell_ids':[row['法术ID']],
                'source_class':row['职业'], 'specializations':[row['专精']],
                'scope_evidence':'reviewed_dbc_native_effect_scope', 'excluded_before_probe':True,
                'partial_state':bool(local), 'projections':[],
                'global_components':[identity(p) for p in parts],
                'local_components':[identity(p) for p in local],
                'local_skill_bindings':[{**identity(p), 'skill_spell_ids':p['完整DBC集合'],
                                         'evidence':'专精固有光环的明确技能选择器'} for p in local],
                'lower_skill_policy':'exclude_global_keep_explicit_local',
                'local_scope_evidence':'专精公共分量按编号排除，其余技能专属分量保持原生计算。',
                'effect_details':details, 'runtime_condition':'当前专精固有光环'}
        literal = json.dumps(json.dumps(fact, ensure_ascii=False, separators=(',', ':')), ensure_ascii=True)
        displays.append(f'  {{{row["职业族"]}, "{row["专精"]}", {literal}}},')
    digest = hashlib.sha256((contract + json.dumps(review, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
    result = re.sub(r'(skill_damage_scope_sha256 = ")[a-f0-9]+', lambda m:m[1]+digest, contract)
    for marker, lines in [('skill_damage_scope_effects[] = {', additions),
                          ('skill_damage_scope_display[] = {', displays)]:
        start = result.index(marker)
        end = result.index('\n};', start)
        result = result[:end] + '\n' + '\n'.join(lines) + result[end:]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--base-contract', type=Path, required=True)
    parser.add_argument('--patch', type=Path, required=True)
    parser.add_argument('--record-review', action='store_true', help='显式生成待复核证据；通常重放时必须逐项匹配已有证据。')
    args = parser.parse_args()
    review = collect_review(args.source)
    if args.record_review:
        args.review.write_text(json.dumps(review, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    elif json.loads(args.review.read_text(encoding='utf-8')) != review:
        raise ValueError('专精复核证据发生漂移，拒绝生成契约。')
    before = args.base_contract.read_text(encoding='utf-8')
    after = extend_contract(before, review)
    path = 'engine/skill_damage_scope_contract.inc'
    patch = f'diff --git a/{path} b/{path}\n' + ''.join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True), fromfile='a/'+path, tofile='b/'+path))
    args.patch.write_text(patch, encoding='utf-8', newline='\n')
    print('专精补充契约已生成，逐效果证据已校验。')


if __name__ == '__main__':
    main()
