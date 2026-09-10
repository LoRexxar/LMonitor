"""将已复核的 DBC/原生分量表编译成导出器唯一作用域事实。"""
import argparse
import hashlib
import json
from pathlib import Path


def compile_contract(review):
    effects, parents, buffs = {}, set(), {}
    for row in review['条目']:
        for component in row['分量']:
            decision = component['处理结论']
            if decision not in {'保留', '应剔除'}:
                raise ValueError('存在未完成的分量判定，拒绝生成导出器契约。')
            dbc = component['DBC']
            key = (component['源法术ID'], component['效果编号'])
            value = (component['效果ID'], dbc['class_family'], decision == '应剔除')
            if key in effects and effects[key] != value:
                raise ValueError(f'同一 DBC 分量存在冲突：{key}')
            if (dbc['source_spell_id'], dbc['effect_index'], dbc['effect_id']) != (*key, value[0]):
                raise ValueError(f'DBC 身份不一致：{key}')
            effects[key] = value
            if row['类型'] == '天赋' and value[2]:
                parents.add((row['法术ID'], *key))
        if row['类型'] != '天赋':
            flags = buffs.setdefault(row['法术ID'], [False, False])
            flags[0] |= any(c['处理结论'] == '应剔除' for c in row['分量'])
            flags[1] |= any(c['处理结论'] == '保留' for c in row['分量'])
    # 同一法术可同时是主动天赋和目标状态；自身分量必须与统一事实保持一致。
    talent_spells={row['法术ID'] for row in review['条目'] if row['类型']=='天赋'}
    for (sid,index),(_,_,global_) in effects.items():
        if global_ and sid in talent_spells:
            parents.add((sid,sid,index))
    return effects, parents, buffs


def render_contract(review, digest):
    effects, parents, buffs = compile_contract(review)
    lines = [
        '// 自动生成：来源为已复核的 DBC 与 SimC 原生分量，禁止手改。',
        '#pragma once',
        f'constexpr const char* skill_damage_scope_revision = "{review["源码提交"]}";',
        f'constexpr const char* skill_damage_scope_build = "{review["客户端版本"]}";',
        f'constexpr const char* skill_damage_scope_sha256 = "{digest}";',
        'struct skill_damage_scope_effect_t { unsigned spell, index, id, family; bool global; };',
        'constexpr skill_damage_scope_effect_t skill_damage_scope_effects[] = {',
    ]
    lines += [f'  {{{spell}, {index}, {eid}, {family}, {str(global_).lower()}}},'
              for (spell, index), (eid, family, global_) in sorted(effects.items())]
    lines += ['};', 'struct skill_damage_scope_parent_t { unsigned parent, spell, index; };',
              'constexpr skill_damage_scope_parent_t skill_damage_scope_parents[] = {']
    lines += ['  {' + ', '.join(map(str, item)) + '},' for item in sorted(parents)]
    lines += ['};', 'struct skill_damage_scope_buff_t { unsigned spell; bool global, local; };',
              'constexpr skill_damage_scope_buff_t skill_damage_scope_buffs[] = {']
    lines += [f'  {{{spell}, {str(flags[0]).lower()}, {str(flags[1]).lower()}}},'
              for spell, flags in sorted(buffs.items())]
    lines += ['};', 'struct skill_damage_scope_display_t { unsigned family; const char* json; };',
              'constexpr skill_damage_scope_display_t skill_damage_scope_display[] = {']
    try:
        from scripts.build_simc_global_damage_review import SPEC_LABELS
        from scripts.audit_simc_global_damage_initialization import SPECS
    except ModuleNotFoundError:
        from build_simc_global_damage_review import SPEC_LABELS
        from audit_simc_global_damage_initialization import SPECS
    spec_labels = {SPEC_LABELS[key]: set() for key in SPEC_LABELS}
    for key, label in SPEC_LABELS.items():
        spec_labels[label].add(SPECS[key])
    for row in review['条目']:
        if not row.get('职业'):
            continue
        parts = [c for c in row['分量'] if c['处理结论'] == '应剔除']
        if not parts:
            continue
        specs = sorted({spec for label in row.get('专精','').split('、') for spec in spec_labels.get(label, [])})
        fact = {'effect_id':f'reviewed_scope:{row["类型"]}:{row["法术ID"]}:{row.get("节点",0)}',
                'source_type':'reviewed_scope', 'source_name':row['名称'], 'display_name':row['名称'],
                'source_spell_ids':[row['法术ID']], 'specializations':specs,
                'scope_evidence':'reviewed_dbc_native_effect_scope', 'excluded_before_probe':True,
                'partial_state':any(c['处理结论']=='保留' for c in row['分量']), 'projections':[],
                'global_components':[{'spell_id':c['源法术ID'],'effect_index':c['效果编号'],'effect_id':c['效果ID']} for c in parts],
                'runtime_condition':'；'.join(dict.fromkeys(c['处理说明'] for c in parts))}
        value = json.dumps(json.dumps(fact,ensure_ascii=False,separators=(',',':')),ensure_ascii=True)
        lines.append(f'  {{{parts[0]["DBC"]["class_family"]}, {value}}},')
    lines += ['};', '']
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    raw = args.review.read_bytes()
    result = render_contract(json.loads(raw), hashlib.sha256(raw).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result, encoding='utf-8')
    print(args.output)
