"""把本地真实 SimC 导出交给线上同一产品投影和表格渲染函数。"""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from collections import Counter
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'LMonitor.settings_test_sqlite')
import django
django.setup()
from botend.services.simc_skill_damage import (
    SimcSkillDamageSnapshotService, classify_global_skill_effects, flatten_single_talent_damage_variants,
    project_skill_damage_product_payload, collect_skill_damage_unresolved,
    _mark_empty_runtime_amount_components_unresolved, _discard_empty_runtime_amount_components,
    prune_global_damage_talents, hero_subtree_name_by_id, hero_subtree_name_zh,
    single_talent_reference_entry,
)
from build_simc_scope_contract import compile_contract
from build_simc_global_damage_review import KEYS, SPEC_LABELS
from audit_simc_global_damage_initialization import SPECS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--names', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    read = lambda p: json.loads(p.read_text(encoding='utf-8'))
    review = read(args.review)
    contract_hash = hashlib.sha256(args.review.read_bytes()).hexdigest()
    effects, _, _ = compile_contract(review)
    expected = {(spell,index) for (spell,index), fact in effects.items() if fact[2]}
    manifest = read(args.run/'all-base/manifest.json')
    catalog_payload = read(args.run/'all-base/global-scope-catalog.json')
    catalog = {r['trait_entry_id']:r for r in catalog_payload['talents']}
    all_traits = {r['trait_entry_id']:r for r in catalog_payload['talent_catalog']}
    summaries = read(args.run/'all-talents/summary.json')
    singles = read(args.run/'single-talents/summary.json')
    export_directories={(r['class'],r['spec'],r['entry']):args.run/r.get('export_directory','single-talents') for r in singles['结果']}
    if len(singles['结果']) != singles['计划数']:
        raise ValueError('单项天赋探测尚未结束。')
    identity = {'simc_revision':review['源码提交'],'game_build':review['客户端版本'],
                'schema_revision':SimcSkillDamageSnapshotService.DATASET_SCHEMA_REVISION}
    service = SimcSkillDamageSnapshotService(SimpleNamespace(**identity), backend=SimpleNamespace())
    observed, displayed_globals, checks, mixed = set(), set(), 0, Counter()
    validated_paths = set()
    unresolved, excluded_rows, single_unresolved = [], [], {}
    reference_audit = []
    local_names = read(args.names)
    names = {r[1]:r[3] for r in local_names if r[1] and r[3]}
    names_by_text = {r[2].casefold():r[3] for r in local_names if r[2] and r[3]}
    for row in review['条目']:
        names[row['法术ID']] = row['名称']
    def name(sid, fallback):
        return names.get(sid) or names_by_text.get(str(fallback).casefold()) or names_by_text.get(str(fallback).removeprefix('buff.').removeprefix('debuff.').replace('_',' ').casefold()) or fallback

    def load_actor(path):
        nonlocal checks
        payload = read(path)
        marked = _mark_empty_runtime_amount_components_unresolved(payload)
        if path in validated_paths:
            _discard_empty_runtime_amount_components(marked)
            return payload['actors'][0]
        service._validate_export(payload)
        _discard_empty_runtime_amount_components(marked)
        actor = payload['actors'][0]
        if actor.get('scope_contract_sha256') != contract_hash:
            raise ValueError(f'导出未使用当前复核契约：{path}')
        present = set()
        for item in actor['normalized_scope_effects']:
            key = (item['spell_id'],item['effect_index'])
            if key not in expected or item['actual_base_value'] != 0:
                raise ValueError(f'全局分量归零不一致：{path} {key}')
            present.add(key)
        if len(present) != len(actor['normalized_scope_effects']):
            raise ValueError('全局分量回读重复。')
        observed.update(present)
        checks += 1
        for item in actor.get('normalized_talent_effects',[]):
            key = (item['spell_id'],item['effect_index'])
            if key in effects and not effects[key][2] and item['dbc_base_value'] and not item['actual_base_value']:
                raise ValueError(f'局部分量被清零：{path} {key}')
        for state in actor['global_damage_states']:
            if state.get('partial_state'):
                mixed[state['spell_id']] += sum(1 for action in actor['actions'] for scenario in action['scenarios']
                    if any(b['spell_id']==state['spell_id'] for b in scenario['buffs']))
        validated_paths.add(path)
        return actor

    def variants_for(cls, spec, high, low):
        records = [item for item in singles['结果'] if
                   (item['class'],item['spec'],item['status']) == (cls,spec,'通过')]
        selected_by_entry = {None: high['selected_trait_ids']}
        for item in records:
            path = export_directories[(cls,spec,item['entry'])]/f'{cls}_{spec}--{item["entry"]}--100.json'
            selected_by_entry[item['entry']] = load_actor(path)['selected_trait_ids']
        references = {item['entry']: single_talent_reference_entry(item['entry'], selected_by_entry)
                      for item in records}
        for item in records:
            reference = references[item['entry']]
            if reference is not None:
                reference_audit.append({'职业':cls,'专精':spec,'天赋节点':item['entry'],
                    '前置配置节点':reference,'实际新增节点':sorted(set(selected_by_entry[item['entry']])-set(selected_by_entry[reference])),
                    '比较规则':'保留相同前置及英雄树选择节点，仅增加该天赋'})
        class Variants:
            # 与线上分批读取方式一致，避免完整目录的所有原始 actor 同时驻留内存。
            def __len__(self):
                return len(records)

            def __iter__(self):
                for item in records:
                    prefix = f'{cls}_{spec}--{item["entry"]}'
                    directory=export_directories[(cls,spec,item['entry'])]
                    a = load_actor(directory/f'{prefix}--100.json')
                    b = load_actor(directory/f'{prefix}--34.json')
                    reference = references[item['entry']]
                    ref_high, ref_low = high, low
                    if reference is not None:
                        ref_prefix = f'{cls}_{spec}--{reference}'
                        ref_dir=export_directories[(cls,spec,reference)]
                        ref_high = load_actor(ref_dir/f'{ref_prefix}--100.json')
                        ref_low = load_actor(ref_dir/f'{ref_prefix}--34.json')
                    for health, actor in [(100,a),(34,b)]:
                        for gap in collect_skill_damage_unresolved({'actors':[actor]}, target_health=health):
                            key = (cls,spec,gap['action']['token'],gap['action']['spell_id'],gap['reason'])
                            record = single_unresolved.setdefault(key,{**gap,'涉及天赋':[],'涉及血量':[]})
                            if item['entry'] not in record['涉及天赋']:
                                record['涉及天赋'].append(item['entry'])
                            if health not in record['涉及血量']:
                                record['涉及血量'].append(health)
                    yield {'talent':{'id':item['entry'],'node_id':item['entry'], 'spell_id':item['spell_id'],
                        'name':name(item['spell_id'],item['name']),'name_zh':name(item['spell_id'],item['name']),'tree_type':item['tree_type'],
                        'hero_subtree_id':item['hero_subtree_id']},'high':a,'low':b,
                        'reference_high':ref_high,'reference_low':ref_low}
        return Variants()

    def product_actor(cls, spec, high, low, variants, mode):
        global_effects = classify_global_skill_effects(high,low,variants)
        global_effects = [e for e in global_effects if not any(p.get('kind')=='crit_chance' for p in e.get('projections',[]))]
        selected = set(high['selected_trait_ids'])
        for variant in variants:
            selected.update(variant['high']['selected_trait_ids'])
        talents = [SimpleNamespace(pk=e,node_id=e,name=catalog[e]['name'],name_zh=name(catalog[e]['spell_id'],catalog[e]['name']),
                   tree_type={1:'class',2:'spec',3:'hero'}[all_traits[e]['tree_index']],db2_subtree_id=all_traits[e]['subtree_id'])
                   for e in selected if e in catalog]
        static = prune_global_damage_talents(talents,[],{},catalog)[3]
        actor = copy.deepcopy(high)
        actor['specialization'] = actor.pop('spec')
        actor['global_skill_effects'] = static+global_effects
        actor['actions'] = flatten_single_talent_damage_variants(high,low,variants,global_effects=global_effects)
        product = project_skill_damage_product_payload({'actors':[actor]})['actors'][0]
        displayed_globals.update((c['spell_id'],c['effect_index']) for e in product['global_skill_effects'] for c in e.get('global_components',[]))
        hero_ids = sorted({v['talent']['hero_subtree_id'] for v in variants if v['talent']['hero_subtree_id']})
        if not hero_ids:
            hero_ids = sorted({all_traits[e]['subtree_id'] for e in selected if e in all_traits and all_traits[e]['subtree_id']})
        product['hero_talent_trees'] = [{'id':h,'name_zh':hero_subtree_name_zh(hero_subtree_name_by_id(h)) or str(h)} for h in hero_ids]
        if not product['hero_talent_trees']:
            product['hero_talent_trees'] = [{'id':0,'name_zh':'基础伤害技能'}]
        display_actions = []
        for action in product['actions']:
            if any(value <= 0 for value in action['product']['final_normalized_damage_by_target'].values()):
                excluded_rows.append({'class':cls,'specialization':spec,'mode':mode,
                    'action':{'token':action['token'],'spell_id':action['spell_id'],'name':name(action['spell_id'],action['name'])},
                    'reason':('仅部分目标数有伤害，当前表要求五种目标数均有可展示结果' if any(v>0 for v in action['product']['final_normalized_damage_by_target'].values()) else '全部目标数均无伤害，缺少触发上下文，不作为完整归一化行展示'),
                    'values':action['product']['final_normalized_damage_by_target'], 'variant':action.get('variant')})
                continue
            action['display_name'] = name(action.get('spell_id'),action.get('name'))
            for component in action.get('components',[]):
                component['display_name'] = name(component.get('spell_id'), component.get('token'))
            for c in action.get('variant',{}).get('runtime_conditions',[]):
                c['display_name'] = name(c.get('spell_id'),c.get('name') or c.get('token'))
                c['name'] = c['display_name']
                variant = action['variant']
                token = c.get('token','').split('.')[-1]
                if token and c['name']:
                    variant['runtime_condition'] = variant.get('runtime_condition','').replace(token,c['name'])
            for count in ('1','2','5','10','20'):
                formulas = action['product']['formula_components']
                expected_damage = sum(f['noncrit_contribution_by_target'][count]+f['crit_contribution_by_target'][count] for f in formulas)
                actual = action['product']['final_normalized_damage_by_target'][count]
                if actual<=0 or not math.isclose(actual,expected_damage,rel_tol=1e-9,abs_tol=1e-8):
                    raise ValueError(f'伤害期望不一致：{cls}/{spec}/{action["token"]}')
            if any(f.get('status')=='incomplete' for f in action['product']['formula_components']):
                raise ValueError(f'公式未完成：{cls}/{spec}/{action["token"]}')
            display_actions.append(action)
        product['actions'] = display_actions
        for effect in product['global_skill_effects']:
            ids = effect.get('source_spell_ids') or []
            effect['display_name'] = name(ids[0] if ids else 0,effect.get('talent_name_zh') or effect.get('source_name') or effect.get('source_token'))
        # 页面只保留展示字段，完整探针和逐分量回读留在原始数据中。
        product = {k:product[k] for k in ['class','specialization','hero_talent_trees','actions','global_skill_effects']}
        for action in product['actions']:
            for k in list(action):
                if k not in {'token','name','display_name','spell_id','player_skill','variant','product','hero_subtree_ids',
                             'component_count','components','reporting_root_token','reporting_root_spell_id'}:
                    action.pop(k)
        return product

    snapshots = {mode:{'identity':identity,'preset':service.FIXED_PRESET,'actors':[], 'unresolved':[],
                       'completed_at':'2026-09-10 本地实算'} for mode in ['single','profile']}
    per_spec = []
    for row in summaries:
        cls,spec = row['class'],row['spec']
        key = cls+'_'+spec
        if not row.get('rows') or row.get('validation_errors'):
            continue
        high = load_actor(args.run/'all-base'/f'{key}-100.json')
        low = load_actor(args.run/'all-base'/f'{key}-34.json')
        variants = variants_for(cls,spec,high,low)
        single = product_actor(cls,spec,high,low,variants,'single')
        full_high = load_actor(args.run/'all-talents'/f'{key}-100.json')
        full_low = load_actor(args.run/'all-talents'/f'{key}-34.json')
        profile = product_actor(cls,spec,full_high,full_low,[],'profile')
        for mode, actor in [('single',single),('profile',profile)]:
            snapshots[mode]['actors'].append(actor)
        gaps = collect_skill_damage_unresolved({'actors':[full_high]})
        unresolved.extend(gaps)
        per_spec.append({'职业':cls,'专精':spec,'单项配置':len(variants),'单项表行数':len(single['actions']),
                         '完整配置行数':len(profile['actions']),'未解析伤害记录':len(gaps)})
        print(cls,spec,len(single['actions']),len(profile['actions']),flush=True)
    if observed != expected:
        raise ValueError(f'全局分量回读覆盖缺失：{expected-observed}')
    if displayed_globals != expected:
        raise ValueError(f'上方剔除目录覆盖不一致：缺少 {expected-displayed_globals}，多出 {displayed_globals-expected}')
    for mode, snapshot in snapshots.items():
        snapshot.update(spec_count=len(snapshot['actors']),action_count=sum(len(a['actions']) for a in snapshot['actors']))
        snapshot['unresolved'] = (list(single_unresolved.values()) if mode == 'single' else unresolved) + [r for r in excluded_rows if r['mode']==mode]
        args.output.with_suffix(f'.{mode}.json').write_text(json.dumps(snapshot,ensure_ascii=False),encoding='utf-8')
    report = {'源码提交':review['源码提交'],'客户端版本':review['客户端版本'],'二进制摘要':manifest['二进制_SHA256'],
        '分类表摘要':contract_hash,'已回读全局分量':len(observed),'上表已展示全局分量':len(displayed_globals),'已验证原始导出':checks,
        '单项配置统计':dict(Counter(r['status'] for r in singles['结果'])), '混合状态局部场景数量':dict(mixed),
        '单项表行数':snapshots['single']['action_count'],'完整配置表行数':snapshots['profile']['action_count'],
        '未支持专精':[r for r in summaries if not r.get('rows')], '专精明细':per_spec,
        '排除的非正数多目标行':excluded_rows,
        '单项表未解析机制数':len(single_unresolved),'完整配置未解析记录数':len(unresolved),
        '前置配置对照':reference_audit,
        '说明':'单项表为基础技能框架加一个天赋；完整配置表为上游配置。没有穷举多个可选天赋组合，未解析伤害不填零。'}
    args.output.with_suffix('.audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    template = (ROOT/'templates/dashboard/index.html').read_text(encoding='utf-8')
    start = template.index('<section id="simc-skill-damage-panel"')
    panel = template[start:template.index('</section>',start)+10]
    source = (ROOT/'static/dashboard/js/main.js').read_text(encoding='utf-8')
    renderer = source[source.index('function renderSimcSkillIdentity('):source.index('function initSimcSkillDamagePanel(')]
    labels = {spec:SPEC_LABELS[sid] for sid,spec in SPECS.items()}
    data = {'single':args.output.name+'.single.json','profile':args.output.name+'.profile.json'}
    html = (ROOT/'scripts/templates/simc_normalized_preview.html').read_text(encoding='utf-8')
    for marker,value in {'PANEL':panel,'RENDERER':renderer,'FILES':json.dumps(data),'CLASSES':json.dumps(KEYS,ensure_ascii=False),
                         'SPECS':json.dumps(labels,ensure_ascii=False),'REPORT':json.dumps(report,ensure_ascii=False)}.items():
        html = html.replace('__'+marker+'__',value)
    args.output.with_suffix('.html').write_text(html,encoding='utf-8')


if __name__ == '__main__':
    main()
