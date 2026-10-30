"""将真实参考配点挂到全专精每行，核对当前行关联过滤、解锁证据与数值不变。"""
import argparse
from collections import Counter
import html
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE','LMonitor.settings_test_sqlite')
import django
django.setup()
from botend.services.simc_skill_damage import flatten_single_talent_damage_variants, project_skill_damage_product_payload


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def signature(row):
    return (row['token'],row['spell_id'],json.dumps((row.get('variant') or {}).get('runtime_conditions'),sort_keys=True),
            json.dumps(row['product'],sort_keys=True))


def audit(directory, output, specs=()):
    catalog={r['trait_entry_id']:r for r in read(directory/'global-scope-catalog.json')['talent_catalog']}
    stats=[]
    for summary in read(directory/'summary.json'):
        key=summary['class']+'_'+summary['spec']
        if specs and key not in specs:
            continue
        if summary['status']!='已生成，待逐项审计':
            stats.append({'专精':key,'状态':summary['status']})
            continue
        high=read(directory/(key+'-100.json'))['actors'][0]
        low=read(directory/(key+'-34.json'))['actors'][0]
        # 名单完全来自实算 actor 的实际选择，不用人为造出的空前置代替。
        entries=high['selected_trait_ids']
        traits=[{'trait_entry_id':e,'name':catalog.get(e,{}).get('name') or f'TraitEntry {e}'} for e in entries]
        reference={'source':'native_reference_selection','trait_entry_ids':entries,'traits':traits}
        evidence=[{'trait_entry_id':e,'replacement_spell_id':int(f['value'])}
                  for e in entries for f in catalog.get(e,{}).get('effects',[])
                  if f['type']==6 and f['subtype']==332 and f['value']>0 and int(f['value'])==f['value']]
        context={'trait_entry_ids':entries,'traits':traits,'evidence':evidence,
                 'action_spell_ids':sorted({e['replacement_spell_id'] for e in evidence})}
        raw=flatten_single_talent_damage_variants(high,low,[])
        # 相同 raw 施法/条件只增添真实配置元数据，伤害与目标数完全相同。
        baseline=project_skill_damage_product_payload({'actors':[{'actions':raw}]})['actors'][0]['actions']
        for action in raw:
            action['variant']['reference_context']=reference
            action['variant']['activation_context']=context
        rows=project_skill_damage_product_payload({'actors':[{'actions':raw}]})['actors'][0]['actions']
        if Counter(map(signature,rows))!=Counter(map(signature,baseline)):
            raise ValueError(key+' 前置过滤改变了技能数值或施法/条件分组。')
        counts=Counter()
        for row in rows:
            variant=row.get('variant') or {}
            if 'reference_context' in variant or 'activation_context' in variant:
                raise ValueError(key+' 整套前置名单仍在展示响应中。')
            component_ids={p['spell_id'] for p in row['components']}|{row['spell_id']}
            sources={(s.get('source_spell_id'),s.get('effect_index'))
                     for f in row['product']['formula_components'] for s in f.get('runtime_factor_sources',[])
                     if isinstance(s,dict)}
            sources_by_skill={}
            for part,formula in zip(row['components'],row['product']['formula_components']):
                sources_by_skill.setdefault(part['spell_id'],set()).update(
                    (s.get('source_spell_id'),s.get('effect_index'))
                    for s in formula.get('runtime_factor_sources',[]) if isinstance(s,dict))
            for link in variant.get('related_talents',[]):
                if link['trait_entry_id'] not in entries or not link['evidence']:
                    raise ValueError(key+' 未选择或缺少证据的前置。')
                for proof in link['evidence']:
                    if proof['skill_spell_id'] not in component_ids:
                        raise ValueError(key+' 关联了其他技能的证据。')
                    if proof['kind']=='native_action_unlock':
                        if not any(e['trait_entry_id']==link['trait_entry_id'] and e['replacement_spell_id']==row['spell_id'] for e in evidence):
                            raise ValueError(key+' 解锁关系不匹配当前行。')
                    elif proof['kind']=='native_modifier':
                        if (catalog.get(link['trait_entry_id'],{}).get('spell_id')!=proof['source_spell_id']
                                or (proof['source_spell_id'],proof['effect_index']) not in sources_by_skill.get(proof['skill_spell_id'],set())):
                            (output/('失败-'+key+'.json')).write_text(json.dumps({'row':row,'proof':proof},ensure_ascii=False),encoding='utf-8')
                            raise ValueError(key+' 前置缺少公式实际来源：'+str(proof)+'；公式来源：'+str(sorted(sources)))
                    else:
                        raise ValueError(key+' 未知天赋关联理由。')
                    counts[proof['kind']]+=1
        item={'专精':key,'状态':'通过','原始参考天赋数':len(entries),'检查行数':len(rows),
              '显示关联数':sum(len((r.get('variant') or {}).get('related_talents',[])) for r in rows),
              '被过滤的逐行名单项':sum(len(entries)-len((r.get('variant') or {}).get('related_talents',[])) for r in rows),
              '证据数':dict(counts)}
        stats.append(item)
        (output/(directory.name+'-'+key+'.json')).write_text(json.dumps({'检查':item,'actions':rows},ensure_ascii=False),encoding='utf-8')
        print(directory.name,key,'通过',len(rows),dict(counts),flush=True)
    return stats


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,action='append',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--spec',action='append',default=[])
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    results=[]
    for directory in args.run:
        results.extend(audit(directory,args.output,args.spec))
    passed=[r for r in results if r['状态']=='通过']
    report={'配置数':len(results),'通过配置数':len(passed),'检查行数':sum(r['检查行数'] for r in passed),
            '显示关联数':sum(r['显示关联数'] for r in passed),
            '过滤名单项数':sum(r['被过滤的逐行名单项'] for r in passed),'结果':results,
            '范围':'实际全专精基础/上游参考配置；每行挂载真实选择后验证过滤。另有包含单天赋差分与跨技能解锁的自动回归。'}
    if not report['显示关联数']:
        raise ValueError('没有有效正例，不能把全部隐藏当成验证通过。')
    (args.output/'检查结果.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    title='<meta charset="utf-8"><h1>技能行前置关联检查</h1><p>'+html.escape(str({k:v for k,v in report.items() if k!='结果'}))+'</p>'
    body='<table border="1"><tr><th>专精</th><th>状态</th><th>行数</th><th>显示关联</th><th>过滤项</th></tr>'
    for r in results:
        body+='<tr>'+''.join('<td>'+html.escape(str(r.get(k,'')))+'</td>' for k in ('专精','状态','检查行数','显示关联数','被过滤的逐行名单项'))+'</tr>'
    (args.output/'检查结果.html').write_text(title+body+'</table>',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='结果'},ensure_ascii=False))


if __name__=='__main__':
    main()
