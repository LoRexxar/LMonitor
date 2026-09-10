"""按 DBC 选择器、正常初始化的原生应用关系和手写源码读取生成审计清单。"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import subprocess

from build_simc_global_damage_review import CLASSES, KEYS, SPEC_LABELS
from audit_simc_global_damage_initialization import SPECS
from simc_native_scope_evidence import (index_native_reads, binding_has_damage, relation_has_damage, structural_scope,
                                      validate_native_payload, native_damage_bindings)
from simc_scope_resolution import ScopeResolver
from simc_cpp_scope import classify_reads, read_scope, non_damage_read, referenced_dbc_scope
from simc_native_scope_evidence import source_class, CLASS_FILES


def review_decision(scope):
    """展示审计处理意见，不把已发现关联冒充已完成局部范围判定。"""
    if scope == '公共伤害乘区':
        return '应剔除', '影响角色或宠物的通用伤害修正，属于全局效果。'
    if scope == '伤害类别修正':
        return '应剔除', '影响整个伤害类别，按约定属于全局效果。'
    reasons = {
        '已解析技能应用关系': '已找到关联技能，尚未判清是具体技能集合还是整个伤害类别。',
        'DBC 范围已解析，原生应用未覆盖': 'DBC 完整集合已列出，但选择器代表具体技能还是整个类别尚未核实；不是仅因配置未选中而待确认。',
        '技能本体伤害': '该法术自身造成伤害，触发来源及作用范围尚未判清。',
        '需追踪手写逻辑': '已发现伤害相关证据，需要继续追踪代码中的条件和作用范围。',
    }
    return '待确认', reasons.get(scope, '现有证据尚不足以决定保留或剔除。')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--native',type=Path,required=True)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--names',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--merge-target-review',type=Path,help='保留同版既有复核，仅补充本次实际观察的自身来源目标减益')
    args=p.parse_args()
    manifest=json.loads((args.native/'manifest.json').read_text(encoding='utf-8'))
    current_revision=subprocess.check_output(['git','-C',str(args.source),'rev-parse','HEAD'],text=True).strip()
    if current_revision!=manifest['源码提交']:
        raise ValueError('原生证据与当前 SimC 提交不一致')
    for path,digest in manifest['源码文件摘要'].items():
        if hashlib.sha256((args.source/path).read_bytes()).hexdigest()!=digest:
            raise ValueError('原生证据与当前源码不一致：'+path)
    if manifest.get('DBC目录摘要')!=hashlib.sha256((args.native/'global-scope-catalog.json').read_bytes()).hexdigest():
        raise ValueError('DBC 目录摘要不匹配')
    catalog=json.loads((args.native/'global-scope-catalog.json').read_text(encoding='utf-8'))
    if manifest.get('本地修改摘要') and hashlib.sha256(subprocess.check_output(['git','-C',str(args.source),'diff','--binary','HEAD'])).hexdigest()!=manifest['本地修改摘要']:
        raise ValueError('原生证据与本地补丁不一致')
    successes=[r for r in manifest['结果'] if not r['退出码']]
    covered=len({Path(r['输入']).stem for r in successes})
    coverage=f'当前覆盖 {covered} 个专精、{len(successes)} 份配置；另有 {len(manifest["结果"])-len(successes)} 份配置导出失败。'
    names=json.loads(args.names.read_text(encoding='utf-8'))
    localized={r[2].casefold():r[3] for r in names if r[2] and r[3]} if isinstance(names,list) else {}
    names_by_id={int(k):v for k,v in names.items() if v} if isinstance(names,dict) else {}
    spell_names={int(m[2]):m[1].replace('\\"','"') for m in re.finditer(
        r'^\s*\{\s*"((?:\\.|[^"\\])*)"\s*,\s*(\d+),',
        (args.source/'engine/dbc/generated/sc_spell_data.inc').read_text(encoding='utf-8'),re.M)}
    def label(sid):
        name=spell_names.get(sid,str(sid))
        return names_by_id.get(sid) or localized.get(name.casefold(),name)
    reads=index_native_reads(args.source,catalog)
    resolver=ScopeResolver(args.source,current_revision,catalog)
    damage_names=defaultdict(set)
    for sid in resolver.damage:
        spell=resolver.spells[sid]
        token=re.sub(r'[^a-z0-9]','',spell['_name'].lower())
        damage_names[(spell['_class_flags_family'],token)].add(sid)
    bindings=defaultdict(list)
    selected=set()
    buff_bindings=defaultdict(list)
    target_states=defaultdict(dict)
    relations={e['effect_id']:e for r in catalog['talent_catalog'] for e in r['dbc_scope_effects']}
    raw_binding_count=0
    action_spells=defaultdict(set)
    for run in manifest['结果']:
        if run['退出码']:continue
        if run.get('结果摘要')!=hashlib.sha256((args.native/run['结果文件']).read_bytes()).hexdigest():
            raise ValueError('原生结果摘要不匹配：'+run['结果文件'])
        data=json.loads((args.native/run['结果文件']).read_text(encoding='utf-8'))
        validate_native_payload(data)
        for actor in data['actors']:
            spec_token=Path(run['输入']).stem.removeprefix(actor['class']+'_')
            spec_label=next((SPEC_LABELS[sid] for sid,token in SPECS.items() if token==spec_token and sid in SPEC_LABELS),actor['spec'])
            for state in actor.get('target_states',[]):
                if not state.get('self_owned'):continue
                key=(actor['class'],state['spell_id'])
                target_states[key][run['结果文件']]={**state,'专精':spec_label}
            for action in actor['damage_actions']:
                if action['spell_id']:
                    cid=next(cid for token,cid in CLASS_FILES.items() if token.replace('_','')==actor['class'].replace('_',''))
                    action_spells[(cid,action['token'])].add(action['spell_id'])
            selected.update(actor['selected_trait_ids'])
            effects={e['effect_id']:e for e in actor['source_effects']}
            relations.update(effects)
            damage_ids={a['spell_id'] for a in actor['damage_actions'] if a['spell_id']}
            raw_binding_count+=len(actor['bindings'])
            for b in native_damage_bindings(actor):
                spec_token=Path(run['输入']).stem.removeprefix(actor['class']+'_')
                spec_label=next((SPEC_LABELS[sid] for sid,token in SPECS.items() if token==spec_token and sid in SPEC_LABELS),actor['spec'])
                b={**b,'配置':run['结果文件'],'职业':actor['class'],'专精':spec_label}
                bindings[b['effect_id']].append(b)
                if b['buff_spell_id'] and b['self_owned'] and (not b['buff_class_family'] or b['buff_class_family']==b['owner_class_family']):
                    buff_bindings[(actor['class'],b['buff_spell_id'],b['buff_token'])].append(b)
                elif b['layer'] in ('target_registry','target_player_registry') and not b['buff_spell_id'] and effects[b['effect_id']]['class_family']==b['owner_class_family']:
                    # 目标条件也可能是自身疾病、流血或计数器，不能要求一定存在 Buff 对象。
                    buff_bindings[(actor['class'],b['source_spell_id'],'')].append(b)

    def source_site(file,marker):
        lines=(args.source/file).read_text(encoding='utf-8').splitlines()
        index=next(i for i,line in enumerate(lines) if marker in line)
        return {'文件':file,'行':index+1,'代码':'\n'.join(lines[index:index+10])}
    common=[source_site('engine/player/player.cpp','player_t::spells_affected_by_passive('),
            source_site('engine/action/action.cpp','auto base_dd_mod = player->get_passive_value'),
            source_site('engine/action/parse_effects.cpp','parse_action_base_t::get_effect_vector('),
            source_site('engine/action/parse_effects.cpp','parse_action_base_t::check_affected_list(')]

    def component(e,native,code):
        # 同一原生关系可来自多个配置；保留配置列表而不重复技能行。
        groups={}
        for b in native:
            key=tuple(b[k] for k in ('职业','专精','target_spell_id','field','layer','buff_spell_id','parse_flags','conditional','pet'))
            entry=groups.setdefault(key,{k:v for k,v in b.items() if k not in ('配置','entity')})
            entry.setdefault('配置',[]).append(b['配置'])
        native=list(groups.values())
        scope,basis=structural_scope(e,native)
        decision,reason=review_decision(scope)
        resolution=resolver.resolve(e,native,scope,code)
        scope_evidence={}
        if resolution:
            decision,reason,scope_evidence=resolution
        if decision == '待确认':
            code_resolution=classify_reads(args.source,code)
            if code_resolution:
                decision,reason,sites=code_resolution
                scope_evidence={'判定路径':'原生手写范围','源码':sites}
            else:
                referenced=referenced_dbc_scope(resolver,e,code)
                if referenced:
                    decision,reason,scope_evidence=referenced
        dbc_ids={s['spell_id'] for s in e['affected_spells']}
        native_ids={sid for b in native for sid in b.get('传递到的伤害技能',[]) if sid}
        code_ids=set(scope_evidence.get('完整DBC集合',[])) | set(scope_evidence.get('伤害技能',[]))
        for site in scope_evidence.get('源码',[]):
            cid=source_class(args.source/site['文件'])
            for name in [site.get('类型','')]+site.get('外层类型',[]):
                token=name.removesuffix('_t')
                while token:
                    code_ids.update(action_spells.get((cid,token),set()))
                    # 仅补齐技能身份展示；不使用名称推断保留或全局结论。
                    code_ids.update(damage_names.get((e['class_family'],re.sub(r'[^a-z0-9]','',token.lower())),set()))
                    shorter=re.sub(r'_(?:base|tick|damage|dmg|attack)$','',token)
                    if shorter==token:break
                    token=shorter
        if decision == '待确认' and native_ids - dbc_ids:
            reason='SimC 存在 DBC 集合以外的伤害关联，需核实附加技能、效果传递或手写分支的完整范围。'
        gap = ('代码扩展范围尚未核实' if native_ids-dbc_ids else
               '选择器的技能集合或类别含义尚未核实' if dbc_ids else
               '伤害来源或手写作用范围尚未核实') if decision == '待确认' else ''
        return {'效果编号':e['effect_index'],'效果ID':e['effect_id'],'源法术ID':e['source_spell_id'],
                '处理结论':decision,'处理说明':reason,
                '待确认原因':gap,
                '范围核验':scope_evidence,
                '范围判定':scope,'依据':basis,'DBC':e,'原生实际应用':native,'手写读取':code,
                '涉及技能':[{'法术ID':sid,'名称':label(sid),'原生应用':sid in native_ids} for sid in sorted(dbc_ids|native_ids|code_ids)],
                '仅原生出现的技能':sorted(native_ids-dbc_ids),'DBC中尚未观察应用的技能':sorted(dbc_ids-native_ids)}

    rows=[]
    for row in catalog['talent_catalog']:
        if row['class_id'] not in CLASSES:continue
        components=[]
        for e in row['dbc_scope_effects']:
            native=[b for b in bindings[e['effect_id']] if KEYS.get(b['职业'])==CLASSES[row['class_id']]]
            code=reads.get((row['spell_id'],e['effect_index']),[])
            code=[c for c in code if not c['伤害函数'] or not (read_scope(args.source,c) and non_damage_read(read_scope(args.source,c)))]
            if not (native or relation_has_damage(e) or any(c['伤害函数'] for c in code)):continue
            components.append(component(e,native,code))
        if not components:continue
        rows.append({'类型':'天赋','职业':CLASSES[row['class_id']],
            '专精':'、'.join(SPEC_LABELS[s] for s in row['spec_ids'] if s in SPEC_LABELS) or '职业通用',
            '名称':label(row['spell_id']),'法术ID':row['spell_id'],'节点':row['trait_entry_id'],
            '配置覆盖':'至少一个配置已选中' if row['trait_entry_id'] in selected else '当前配置未选中',
            '范围判定':'、'.join(sorted({c['范围判定'] for c in components})),
            '分量':components,'描述仅供参考':row['description'],
            '旧文本规则已提取分量_不作本次依据':row['global_effect_indices']})
    merged_states=defaultdict(list)
    state_tokens=defaultdict(set)
    for (cls,sid,token),native in buff_bindings.items():
        merged_states[(cls,sid)].extend(native)
        if token:state_tokens[(cls,sid)].add(token)
    for key,states in target_states.items():
        merged_states.setdefault(key,[])
        state_tokens[key].update(s['token'] for s in states.values())
    for (cls,sid),native in merged_states.items():
        token=' / '.join(sorted(state_tokens[(cls,sid)]))
        components=[]
        manual=target_states.get((cls,sid),{})
        for eid in sorted({b['effect_id'] for b in native}|{eid for s in manual.values() for eid in s['effect_ids']}):
            e=relations[eid]
            applied=[b for b in native if b['effect_id']==eid]
            code=reads.get((e['source_spell_id'],e['effect_index']),[])
            # 同版 DBC 的空标签没有任何命中，且原生无登记或伤害读取，不生成无关分量。
            if e['method']!='no_static_spell_selector' and not e['affected_spells'] and not applied and not any(c['伤害函数'] for c in code):
                continue
            components.append(component(e,applied,code))
        known={c['效果ID'] for c in components}
        for own in resolver.own_damage_relations(sid):
            if own['effect_id'] not in known:
                part=component(own,[],[])
                part['处理说明']='同源法术自身的伤害分量保留；公共增伤另行归零，状态是否影响本技能由原生伤害探针判断。'
                components.append(part)
        if not components:continue
        rows.append({'类型':'目标减益' if all(b['layer'] in ('target_registry','target_player_registry') for b in native) else '自身状态','职业':KEYS[cls],'专精':'、'.join(sorted({b['专精'] for b in native}|{s['专精'] for s in manual.values()})),
            '名称':label(sid),'法术ID':sid,'状态':token,'节点':None,
            '自身目标状态原生证据':manual,
            '配置覆盖':'实际自身来源状态与同职业来源通过检查' if token else '原生目标修正登记及同职业 DBC 来源；条件可由持续伤害或计数器提供',
            '范围判定':'、'.join(sorted({c['范围判定'] for c in components})),'分量':components})
    rows.sort(key=lambda r:(r['职业'],r['类型'],r['法术ID'],r.get('节点') or 0))
    unresolved=[{'法术ID':r['法术ID'],'效果ID':c['效果ID'],'名称':r['名称']}
                for r in rows for c in r['分量'] if c['处理结论']=='待确认'
                and (not args.merge_target_review or r['类型']=='目标减益')]
    if unresolved:
        raise ValueError('仍有未完成的作用域，拒绝将待确认清单覆盖结果表：'+json.dumps(unresolved,ensure_ascii=False))
    result={'标题':'天赋与 Buff：保留还是剔除','判断依据':'DBC 效果选择器、原生实际应用的技能字段、手写伤害函数中的读取。描述不参与分类。',
        '说明':coverage+'本表按部分技能保留、公共或整类伤害剔除的规则生成。DBC 掩码和标签直接解析完整集合；手写效果沿具体技能实现、开关及引用的其他 DBC 效果判定。没有人工局部记录不影响保留。原生关系表示初始化登记，不等于实战触发；DBC 集合包含未启用或历史变体。结论限定当前源码、DBC 和导出目录，不是全职业归一化伤害数值已经完成的声明。',
        '客户端版本':catalog['game_build'],'原生证据未归一化':True,'二进制摘要':manifest['二进制摘要'],'源码提交':manifest['源码提交'],
        '原生关联原始条数':raw_binding_count,'数量':dict(Counter(r['类型'] for r in rows)),
        '范围统计':dict(Counter(c['范围判定'] for r in rows for c in r['分量'])),
        '处理统计':dict(Counter(c['处理结论'] for r in rows for c in r['分量'])),
        '待确认原因统计':dict(Counter(c['待确认原因'] for r in rows for c in r['分量'] if c['待确认原因'])),
        '分类规则摘要':{file:hashlib.sha256((Path(__file__).parent/file).read_bytes()).hexdigest()
                         for file in ('simc_scope_resolution.py','simc_scope_skill_sets.json','simc_cpp_scope.py')},
        '导出失败':[r for r in manifest['结果'] if r['退出码']],'公共应用源码':common,'条目':rows}
    if args.merge_target_review:
        previous=json.loads(args.merge_target_review.read_text(encoding='utf-8'))
        if any(previous[key]!=result[key] for key in ('源码提交','客户端版本')):
            raise ValueError('不能合并不同 SimC 或 DBC 版本的作用域证据。')
        additions=[r for r in rows if r['类型']=='目标减益']
        replacement={(r['类型'],r['职业'],r['法术ID']) for r in additions}
        previous['条目']=[r for r in previous['条目'] if (r['类型'],r['职业'],r['法术ID']) not in replacement]+additions
        previous['目标减益补充审计']={'原生目录':str(args.native),'二进制摘要':manifest['二进制摘要'],
            '源码文件摘要':manifest['源码文件摘要'],'状态数':len(additions),
            '说明':'目标状态必须由本角色施加；依照与自身增益相同的 DBC 选择器和原生伤害登记判定。'}
        previous['数量']=dict(Counter(r['类型'] for r in previous['条目']))
        previous['处理统计']=dict(Counter(c['处理结论'] for r in previous['条目'] for c in r['分量']))
        previous['范围统计']=dict(Counter(c['范围判定'] for r in previous['条目'] for c in r['分量']))
        result=previous
    args.output.with_suffix('.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    payload=json.dumps(result,ensure_ascii=False).replace('<','\\u003c')
    html=(Path(__file__).parent/'templates/simc_scope_review.html').read_text(encoding='utf-8')
    args.output.with_suffix('.html').write_text(html.replace('__DATA__',payload),encoding='utf-8')
    args.output.with_suffix('.md').write_text('# '+result['标题']+'\n\n'+result['判断依据']+'\n\n'+result['说明']+'\n\n'+'\n'.join(f'- {k}：{v}' for k,v in result['数量'].items())+'\n',encoding='utf-8')
    print(result['数量'],result['范围统计'])


if __name__=='__main__':main()
