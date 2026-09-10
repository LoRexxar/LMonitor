"""比较修复前技能身份与新表的技能及合并分量，避免把正常合并误报为技能丢失。"""
import argparse
import json
from pathlib import Path


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--before',type=Path,required=True)
    p.add_argument('--preview',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    read=lambda p:json.loads(p.read_text(encoding='utf-8'))
    before,after=read(args.before),read(args.preview.with_suffix('.single.json'))
    for key in ('simc_revision','game_build'):
        assert before['identity'][key]==after['identity'][key],'技能覆盖比较必须使用相同 SimC 与 DBC 版本'
    old={(a['class'],a['specialization']):{tuple(s[:2]):s[2] for s in a['skills']} for a in before['actors']}
    result={'旧技能身份数':sum(map(len,old.values())),'遗漏':[],'已合并进其他技能':[],'已补全法术身份':[]}
    for a in after['actors']:
        key=(a['class'],a['specialization'])
        direct={(r['token'],r['spell_id']) for r in a['actions']}
        components={(c['token'],c['spell_id']) for r in a['actions'] for c in r['components']}
        for identity in old[key].keys()-direct:
            entry={'职业':key[0],'专精':key[1],'技能':old[key][identity],'token':identity[0],'spell_id':identity[1]}
            resolved=sorted(sid for token,sid in direct|components if token==identity[0] and sid>0)
            if identity[1]==0 and resolved:
                result['已补全法术身份'].append({**entry,'当前法术ID':resolved})
                continue
            result['已合并进其他技能' if identity in components else '遗漏'].append(entry)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    assert not result['遗漏'],'技能覆盖回归未通过'


if __name__=='__main__':main()
