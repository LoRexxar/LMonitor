"""复核固定版本的全部精通分量，将有公共范围证据的伤害分量补入归一化契约。"""
import argparse
import difflib
import hashlib
import json
from pathlib import Path
import re
import subprocess

from scripts.build_simc_specialization_scope import BUILD, classify_component, specialization_token
from scripts.simc_scope_resolution import REVIEWED_REVISION, load_dbc
from botend.constants.wow import SPEC_IDENTITY_MAP
from scripts.build_simc_global_damage_review import KEYS, SPEC_LABELS

# 哑光环没有静态技能选择器；以下身份逐项来自当前版本公共动作实现。
MANUAL = {
    362980: ('engine/class_modules/sc_evoker.cpp', 'affected_by_giantkiller', 'cache.mastery_value() * std::max( 0.3, t->health_percentage() / 100 )', '目标血量伤害加成', '目标满血时；随目标血量降低，最低为所列加成的30%'),
    343690: ('engine/class_modules/priest/sc_priest.cpp', 'priest_t::shadow_weaving_multiplier', 'dots * cache.mastery_value()', '暗影伤害', '每个符合暗影交织条件的持续伤害；按实际数量叠加'),
    77220: ('engine/class_modules/warlock/sc_warlock_actions.cpp', 'destruction() && affected_by.chaotic_energies', 'chaotic_energies_rng * p()->cache.mastery_value()', '毁灭伤害随机加成', '每次取精通加成的50%至100%；相关天赋可提高下限'),
    115636: ('engine/class_modules/monk/sc_monk.cpp', 'p()->baseline.windwalker.mastery->effectN( 1 )', 'mastery_parse_entry( base_t::ta_multiplier_effects )', '连击伤害', '触发连击时'),
}


def collect_review(source):
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != REVIEWED_REVISION or f'"{BUILD}"' not in (source/'engine/dbc/generated/client_data_version.inc').read_text():
        raise ValueError('精通复核的源码或客户端版本不匹配。')
    spells, effects = load_dbc(source)
    identities = re.findall(r'\{\s*(\d+),\s*(\d+)\s*\}', (source/'engine/dbc/generated/mastery_spells.inc').read_text())
    if len(identities) != 40 or len({int(s) for s, _ in identities}) != 40:
        raise ValueError('精通专精目录不完整或重复。')
    grouped = {}
    for effect in effects.values():grouped.setdefault(effect['_spell_id'], []).append(effect)
    rows = []
    for spec, sid in identities:
        spec, sid = int(spec), int(sid)
        spell = spells[sid]; family = spell['_class_flags_family']
        parts = []
        for e in sorted(grouped[sid], key=lambda x:x['_index']):
            category = classify_component(family, e)
            proof = None
            if sid in MANUAL and e['_index'] == 0:
                file, marker, calculation, category, condition = MANUAL[sid]
                code = (source/file).read_text(encoding='utf-8')
                if marker not in code or calculation not in code:
                    raise ValueError(f'手写精通实现漂移：{sid}')
                proof = {'文件':file, '摘要':hashlib.sha256(code.encode()).hexdigest(),
                         '入口':marker, '计算':calculation, '条件':condition}
            affected = sorted(s['_id'] for s in spells.values() if s['_class_flags_family'] == family and
                              any(a & b for a,b in zip(s['_class_flags'], e['_class_flags'])))
            parts.append({'效果ID':e['_id'], '效果编号':e['_index']+1,
                          '类型':e['_type'], '子类型':e['_subtype'], '参数':e['_misc_value'],
                          '选择位':e['_class_flags'], '基础值':e['_base_value'],
                          '精通系数':e['_sp_coeff'] / 100, '完整DBC集合':affected,
                          '处理':'剔除公共分量' if category else '保持原样', '类别':category,
                          '手写证据':proof})
        rows.append({'专精ID':spec, '专精':specialization_token(spec),
                     '职业':SPEC_IDENTITY_MAP[spec][0].lower(), '法术ID':sid,
                     '名称':spell['_name'], '职业族':family, '分量':parts})
    return {'源码提交':revision, '客户端版本':BUILD,
            '依据':'全部精通法术表；公共类别选择器与明确手写公共动作路径；不因精通名称剔除整条法术。',
            '条目':rows}


def extend_contract(contract, review):
    if f'"{review["源码提交"]}"' not in contract or f'"{review["客户端版本"]}"' not in contract:
        raise ValueError('精通复核与基础契约版本不一致。')
    if 'mastery_aura:' in contract:raise ValueError('拒绝重复应用精通补充。')
    current = {(int(s),int(i)):(int(e),int(f),g) for s,i,e,f,g in re.findall(r'\{(\d+), (\d+), (\d+), (\d+), (true|false)\}',contract)}
    additions, displays = [], []
    for row in review['条目']:
        parts = [p for p in row['分量'] if p['处理'] == '剔除公共分量']
        if not parts:continue
        local = [p for p in row['分量'] if p not in parts and p['完整DBC集合'] and p['子类型'] in (107,108) and p['参数'] in (0,22,7,15)]
        def identity(p):return {'spell_id':row['法术ID'], 'effect_index':p['效果编号'], 'effect_id':p['效果ID']}
        details=[]
        first=row['分量'][0]['精通系数']
        for p in parts:
            key=row['法术ID'],p['效果编号'];value=p['效果ID'],row['职业族'],'true'
            if key in current:
                if current[key]!=value:raise ValueError(f'精通范围分类冲突：{key}')
            else:additions.append(f'  {{{key[0]}, {key[1]}, {value[0]}, {value[1]}, true}},')
            proof=p['手写证据']
            label=p['类别']
            if p['子类型'] in (107,108):label+='：'+{0:'直接伤害',22:'周期伤害',7:'暴击率',15:'暴击伤害'}[p['参数']]
            detail={'label':label,'source_spell_id':row['法术ID'],'effect_index':p['效果编号'],
                    'base_value':p['基础值'],'coefficient':p['精通系数'],'value_kind':'mastery',
                    'conditional':bool(proof),'evidence':'dbc_native'}
            if first:detail['normalized_mastery_percent']=50*p['精通系数']/first+p['基础值']
            if proof:detail['runtime_condition']=proof['条件']
            details.append(detail)
        fact={'effect_id':f'mastery_aura:{row["法术ID"]}:{row["专精ID"]}',
              'source_type':'reviewed_scope','source_kind':'passive','source_name':row['名称'],
              'display_name':f'{SPEC_LABELS[row["专精ID"]]}{KEYS[row["职业"]]}精通',
              'source_spell_ids':[row['法术ID']], 'source_class':row['职业'],'specializations':[row['专精']],
              'scope_evidence':'reviewed_dbc_native_effect_scope','excluded_before_probe':True,
              'partial_state':bool(local),'projections':[],
              'global_components':[identity(p) for p in parts], 'local_components':[identity(p) for p in local],
              'local_skill_bindings':[{**identity(p),'skill_spell_ids':p['完整DBC集合'],'evidence':'精通的明确局部技能选择器'} for p in local],
              'lower_skill_policy':'exclude_global_keep_explicit_local','effect_details':details,
              'runtime_condition':'当前专精精通；按下列类别与条件生效'}
        literal=json.dumps(json.dumps(fact,ensure_ascii=False,separators=(',',':')),ensure_ascii=True)
        displays.append(f'  {{{row["职业族"]}, "{row["专精"]}", {literal}}},')
    digest=hashlib.sha256((contract+json.dumps(review,sort_keys=True,ensure_ascii=False)).encode()).hexdigest()
    result=re.sub(r'(skill_damage_scope_sha256 = ")[a-f0-9]+',lambda m:m[1]+digest,contract)
    for marker,rows in [('skill_damage_scope_effects[] = {',additions),('skill_damage_scope_display[] = {',displays)]:
        end=result.index('\n};',result.index(marker));result=result[:end]+'\n'+'\n'.join(rows)+result[end:]
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','review','base-contract','patch'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--record-review',action='store_true')
    args=parser.parse_args();review=collect_review(args.source)
    if args.record_review:args.review.write_text(json.dumps(review,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    elif review!=json.loads(args.review.read_text(encoding='utf-8')):raise ValueError('精通证据漂移，拒绝生成。')
    before=args.base_contract.read_text(encoding='utf-8');after=extend_contract(before,review)
    path='engine/skill_damage_scope_contract.inc'
    args.patch.write_text(f'diff --git a/{path} b/{path}\n'+''.join(difflib.unified_diff(before.splitlines(True),after.splitlines(True),fromfile='a/'+path,tofile='b/'+path)),encoding='utf-8',newline='\n')
    print('精通复核：',len(review['条目']),'专精，',sum(p['处理']=='剔除公共分量' for r in review['条目'] for p in r['分量']),'公共分量。')


if __name__=='__main__':main()
