"""汇总同构建的全部分片清单，保留原始导出位置及哈希，不移动或覆盖原始数据。"""
import argparse
import json
from pathlib import Path


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,action='append',required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    summaries=[json.loads((d/'summary.json').read_text(encoding='utf-8')) for d in args.input]
    first=summaries[0]
    count=first['分片'][1]
    assert {tuple(s['分片']) for s in summaries}=={(i,count) for i in range(count)},'分片缺失或重复'
    assert len(summaries)==count,'分片数量不一致'
    rows=[]
    for directory,s in zip(args.input,summaries):
        assert all(s[key]==first[key] for key in ('二进制_SHA256','源码提交','客户端版本','完整计划数')),'分片构建不一致'
        assert len(s['结果'])==s['计划数'],'分片尚未完成'
        relative=directory.resolve().relative_to(args.output.resolve().parent).as_posix()
        for row in s['结果']:
            if row['status']=='通过':
                assert all((directory/f'{row["class"]}_{row["spec"]}--{row["entry"]}--{h}.json').is_file() for h in (100,34))
            rows.append({**row,'export_directory':relative})
    assert len(rows)==first['完整计划数'],'完整计划未覆盖'
    assert len({(r['class'],r['spec'],r['entry']) for r in rows})==len(rows),'配置重复'
    result={**first,'分片':None,'计划数':len(rows),'结果':rows,'原始分片目录':[str(d) for d in args.input]}
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('已汇总',len(rows),'个配置')


if __name__=='__main__':main()
