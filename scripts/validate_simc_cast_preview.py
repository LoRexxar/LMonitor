"""核对发布数据中的同次施法分量、主副手及完整全局目录。"""
import argparse
import json
import math
from pathlib import Path
from build_simc_scope_contract import compile_contract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preview',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--review',type=Path,default=Path('docs/reviews/2026-09-09-simc-retained-talents-buffs.json'))
    args=parser.parse_args()
    effects,_,_=compile_contract(json.loads(args.review.read_text(encoding='utf-8')))
    expected_globals={key for key,value in effects.items() if value[2]}
    result={'分量求和检查':0,'多段案例':[]}
    expected={('warrior','protection','demolish'):3,('warrior','fury','bladestorm'):2,
              ('demonhunter','havoc','blade_dance'):8,('monk','windwalker','whirling_dragon_punch'):2,
              ('priest','shadow','void_volley_swm'):2,('paladin','retribution','hammer_of_light'):2,
              ('deathknight','frost','exterminate'):2}
    seen=set()
    for mode in ('single','profile'):
        data=json.loads(args.preview.with_suffix('.'+mode+'.json').read_text(encoding='utf-8'))
        all_globals=set()
        for actor in data['actors']:
            for effect in actor['global_skill_effects']:
                all_globals.update((p['spell_id'],p['effect_index']) for p in effect['global_components'])
            for row in actor['actions']:
                parts=row['components']
                assert len(parts)==row['component_count']
                for targets in ('1','2','5','10','20'):
                    total=sum(p['final_normalized_damage_by_target'][targets] for p in parts)
                    assert math.isclose(total,row['product']['final_normalized_damage_by_target'][targets],rel_tol=1e-9,abs_tol=1e-8),(mode,row['token'],targets)
                    result['分量求和检查']+=1
                key=(actor['class'],actor['specialization'],row['token'])
                if mode=='profile' and key in expected:
                    assert len(parts)==expected[key],(key,len(parts))
                    if key not in seen:
                        seen.add(key)
                        result['多段案例'].append({'职业':key[0],'专精':key[1],'技能':row['display_name'],
                            '分量数':len(parts),'原生执行次数':[p['damage_equivalent_count'] for p in parts],
                            '各目标伤害':row['product']['final_normalized_damage_by_target']})
                    if key[2]=='bladestorm':
                        assert {p['hand'] for p in parts}=={'main_hand','off_hand'}
                    if key[2]=='blade_dance':
                        assert sum(p['final_normalized_damage_by_target']['1']==0 for p in parts)==4
        assert all_globals==expected_globals,(expected_globals-all_globals,all_globals-expected_globals)
        result[mode+'全局分量数']=len(all_globals)
    assert seen==set(expected),set(expected)-seen
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
