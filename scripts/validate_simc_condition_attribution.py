"""用真实原始导出及最终投影核验减益剔除、天赋归因和多段增伤条件。"""
import argparse
import json
import math
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--preview',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    read=lambda p:json.loads(p.read_text(encoding='utf-8'))
    raw=read(args.run/'all-talents/warrior_fury-100.json')['actors'][0]
    zero=[e for e in raw['normalized_scope_effects'] if e['spell_id']==445836]
    assert len(zero)==1 and zero[0]['actual_base_value']==0,zero
    assert not any(b['spell_id']==445836 for a in raw['actions'] for s in a['scenarios'] for b in s['buffs'])
    result={'不堪一击计算前归零':zero,'剑刃风暴原生次数':{},'归因检查':{}}
    for token in ('bladestorm_mh','bladestorm_oh'):
        action=next(a for a in raw['actions'] if a['token']==token)
        baseline=action['baseline']['direct']['damage_equivalent_count']
        counts={b['stacks']:s['values']['direct']['damage_equivalent_count']
                for s in action['scenarios'] for b in s['buffs'] if b['spell_id']==445606}
        assert counts.keys()=={1,2,3},counts
        assert all(math.isclose(count,baseline+stack) for stack,count in counts.items())
        result['剑刃风暴原生次数'][token]={'基础':baseline,'殒命在即层数对应次数':counts}
    del raw
    for mode in ('single','profile'):
        data=read(args.preview.with_suffix('.'+mode+'.json'))
        fury=next(a for a in data['actors'] if a['class']=='warrior' and a['specialization']=='fury')
        assert any(445836 in e.get('source_spell_ids',[]) for e in fury['global_skill_effects'])
        for actor in data['actors']:
            for row in actor['actions']:
                if row['token']=='demolish':
                    assert row['spell_id']>0,'未启用的崩摧父技能不能进入产品表'
                conditions=row.get('variant',{}).get('runtime_conditions',[])
                assert not any(c['spell_id'] in (445836,208086) for c in conditions)
                if row['token']=='slayers_strike':
                    assert row['variant'].get('talent_id')!=117385,row['variant']
                    assert not any(c['spell_id']==445606 for c in conditions),row['variant']
            if actor['class']=='druid' and actor['specialization'] in ('feral','restoration') and mode=='single':
                vines=[r for r in actor['actions'] if r['spell_id']==439531]
                assert vines,(actor['specialization'],'全局修正不能连带删除缠藤自身伤害')
                assert any(any(c['spell_id']==439531 for c in r['variant']['runtime_conditions']) for r in vines)
        blade=[r for r in fury['actions'] if r['token']=='bladestorm']
        buff_rows=[r for r in blade if any(c['spell_id']==445606 for c in r['variant']['runtime_conditions'])]
        if mode=='profile':
            assert len(buff_rows)==3,len(buff_rows)
            assert all(r['component_count']==2 and {c['hand'] for c in r['components']}=={'main_hand','off_hand'} for r in buff_rows)
        result['归因检查'][mode]={'不堪一击上表存在':True,'屠戮者打击无殒命在即关联':True,
                                 '剑刃风暴殒命在即状态行':len(buff_rows)}
        arms=next(a for a in data['actors'] if a['class']=='warrior' and a['specialization']=='arms')
        assert any(208086 in e.get('source_spell_ids',[]) for e in arms['global_skill_effects'])
        result['归因检查'][mode]['手写巨人打击减益已提取']=True
        del data
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
