"""将已复核的 DBC/原生分量表编译成导出器唯一作用域事实。"""
import argparse
import hashlib
import json
from pathlib import Path


def display_details(row):
    """保留每个加成的单位与作用类别；并列分量不能相乘或当成统一倍率。"""
    labels = {79:'学校伤害',163:'暴击伤害',270:'目标受到自身的伤害',271:'目标受到自身的伤害',
              290:'暴击率',319:'自动攻击速度',342:'自动攻击速度',344:'自动攻击伤害',
              380:'目标受到守护者的伤害',381:'目标受到宠物的伤害',429:'宠物伤害',531:'守护者伤害',
              303:'符合目标状态时的伤害'}
    schools = {1:'物理',2:'神圣',4:'火焰',8:'自然',16:'冰霜',32:'暗影',64:'奥术',126:'魔法',127:'全技能'}
    details = []
    for part in row['分量']:
        if part['处理结论'] != '应剔除':
            continue
        dbc = part['DBC']
        aura, prop = dbc.get('subtype'), dbc.get('misc1')
        label = labels.get(aura)
        if aura in (107,108,218,219):
            label = {0:'直接伤害',22:'周期伤害',7:'暴击率',15:'暴击伤害',1:'持续时间',19:'周期间隔'}.get(prop)
        if aura == 79:
            label = schools.get(prop, '指定学校') + '伤害'
        if not label:
            label = '条件加成'
        detail = {'label':label,'source_spell_id':part['源法术ID'],'effect_index':part['效果编号'],
                  'evidence':'dbc_native','base_value':dbc.get('base_value'),
                  'conditional':any(b.get('conditional') for b in part.get('原生实际应用',[]))}
        if dbc.get('mastery_scaled'):
            detail.update(value_kind='mastery', coefficient=dbc['mastery_coefficient'])
            references = {b.get('owner_mastery_coefficient') for b in part.get('原生实际应用', [])
                          if b.get('owner_mastery_spell_id') == part['源法术ID'] and b.get('owner_mastery_coefficient')}
            if len(references) == 1:
                detail['normalized_mastery_percent'] = 50 * dbc['mastery_coefficient'] / references.pop() + (dbc.get('base_value') or 0)
        elif aura in labels or aura in (107,108,218,219):
            detail['value_kind'] = 'percentage_points' if label == '暴击率' else 'percent'
            if not dbc.get('base_value'):
                detail['value_kind'] = 'dynamic'
        else:
            detail['value_kind'] = 'dynamic'
        details.append(detail)
    return details


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
            # 回调所引用的状态仍可开启局部天赋，不能因公共分量而禁用整个开关。
            flags[1] |= any(b.get('layer') == 'conditional_action_registry'
                           for c in row['分量'] for b in c.get('原生实际应用', []))
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
    lines += ['};', 'struct skill_damage_scope_display_t { unsigned family; const char* specialization; const char* json; };',
              'constexpr skill_damage_scope_display_t skill_damage_scope_display[] = {']
    try:
        from scripts.build_simc_global_damage_review import SPEC_LABELS
        from scripts.audit_simc_global_damage_initialization import SPECS
    except ModuleNotFoundError:
        from build_simc_global_damage_review import SPEC_LABELS
        from audit_simc_global_damage_initialization import SPECS
    from botend.constants.wow import SPEC_IDENTITY_MAP
    from botend.constants.simc_effect_ownership import NATIVE_STATE_OWNERS
    from scripts.build_simc_global_damage_review import KEYS
    class_keys = {zh: key for key, zh in KEYS.items()}
    talent_scopes = {}
    for row in review['条目']:
        if row['类型'] == '天赋':
            talent_scopes.setdefault((row.get('职业'), row['法术ID']), set()).update(row.get('专精', '').split('、'))
    for row in review['条目']:
        if not row.get('职业'):
            continue
        parts = [c for c in row['分量'] if c['处理结论'] == '应剔除']
        if not parts:
            continue
        cls = class_keys[row['职业']]
        labels = set(row.get('专精','').split('、'))
        # 状态可以在多个专精预创建，优先采用同 ID 天赋的可学习范围。
        if row['类型'] != '天赋':
            declared = talent_scopes.get((row['职业'], row['法术ID']))
            if declared and '职业通用' not in declared:
                labels &= declared
        spec_ids = [sid for sid, (owner, _) in SPEC_IDENTITY_MAP.items()
                    if owner.lower() == cls and ('职业通用' in labels or SPEC_LABELS[sid] in labels)]
        if not spec_ids:
            raise ValueError(f'全局展示条目缺少明确专精归属：{row["职业"]}/{row["法术ID"]}')
        specs = sorted(SPECS[sid] for sid in spec_ids)
        owner = NATIVE_STATE_OWNERS.get(row['法术ID'])
        if owner:
            if owner['class'] != cls:
                raise ValueError('原生施加来源与职业不一致。')
            specs = sorted(set(specs) & set(owner['specs']))
        # 原生导出按一个专精一条事实输出，避免同一职业下的跨专精事实泄漏。
        # JSON 内也使用单值专精范围，后端再次投影时可以继续做精确校验。
        for specialization in specs:
            fact = {'effect_id':f'reviewed_scope:{row["类型"]}:{row["法术ID"]}:{row.get("节点",0)}',
                'source_type':'reviewed_scope', 'source_name':row['名称'], 'display_name':row['名称'],
                'source_spell_ids':[row['法术ID']], 'specializations':[specialization],
                'source_class':cls, 'source_kind':{'天赋':'talent','目标减益':'debuff'}.get(row['类型'],'buff'),
                'scope_evidence':'reviewed_dbc_native_effect_scope', 'excluded_before_probe':True,
                'partial_state':any(c['处理结论']=='保留' for c in row['分量']), 'projections':[],
                'global_components':[{'spell_id':c['源法术ID'],'effect_index':c['效果编号'],'effect_id':c['效果ID']} for c in parts],
                'effect_details':display_details(row),
                'runtime_condition':'自身效果生效时' if row['类型']=='自身状态' else ('自身施加的目标效果生效时' if row['类型']=='目标减益' else '启用相应天赋时')}
            if row['类型'] != '天赋' and any(b.get('layer') == 'conditional_action_registry' for c in row['分量'] for b in c.get('原生实际应用', [])):
                fact['partial_state'] = True
            value = json.dumps(json.dumps(fact,ensure_ascii=False,separators=(',',':')),ensure_ascii=True)
            lines.append(f'  {{{parts[0]["DBC"]["class_family"]}, "{specialization}", {value}}},')
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
