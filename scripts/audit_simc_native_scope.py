"""正常初始化全专精，导出 DBC 与原生应用关系；不计算伤害结果，不先清除全局效果。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import subprocess


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--binary',type=Path,required=True)
    parser.add_argument('--inputs',type=Path,action='append',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--workers',type=int,default=6)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    binary=args.binary.resolve()
    revision=subprocess.check_output(['git','-C',str(args.source),'rev-parse','HEAD'],text=True).strip()
    build=re.search(r'#define CLIENT_DATA_WOW_VERSION "([^"]+)"',
                    (args.source/'engine/dbc/generated/client_data_version.inc').read_text())[1]
    catalog=args.output/'global-scope-catalog.json'
    subprocess.run([str(binary),f'skill_damage_scope_export={catalog.resolve()}',f'skill_damage_revision={revision}',
                    f'skill_damage_game_build={build}'],capture_output=True,check=True)
    inputs=[(directory,path) for directory in args.inputs for path in sorted(directory.glob('*.simc'))]
    def run(item):
        directory,path=item
        identity=directory.name+'--'+path.stem
        output=args.output/(identity+'.json')
        command=[str(binary),str(path.resolve()),f'skill_damage_evidence_export={output.resolve()}',
                 'report_details=1']
        result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180)
        row={'输入':str(path),'输入摘要':hashlib.sha256(path.read_bytes()).hexdigest(),'结果文件':output.name,'退出码':result.returncode}
        if result.returncode:
            row['失败原因']=(result.stderr or result.stdout)[-3000:]
        else:
            data=json.loads(output.read_text(encoding='utf-8'))
            if data.get('normalized') is not False or data.get('evidence_schema')!=1:
                raise ValueError(f'{identity} 不是未经归一化的原生证据')
            row['原生关联数']=sum(len(a['bindings']) for a in data['actors'])
            row['已选天赋数']=sum(len(a['selected_trait_ids']) for a in data['actors'])
            row['结果摘要']=hashlib.sha256(output.read_bytes()).hexdigest()
        print(identity,'成功' if result.returncode==0 else '导出失败',flush=True)
        return row
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results=list(pool.map(run,inputs))
    sources={str(p.relative_to(args.source)):hashlib.sha256(p.read_bytes()).hexdigest()
             for p in [args.source/'engine/sc_main.cpp',args.source/'engine/player/player.cpp',
                       args.source/'engine/action/parse_effects.cpp',args.source/'engine/dbc/spell_data.cpp',
                       args.source/'engine/sim/sim.cpp',args.source/'engine/sim/sim.hpp']}
    manifest={'说明':'正常初始化的原生关系导出；未调用旧文本分类器清除效果。未选天赋和未执行手写分支不能视为无影响。',
              '源码提交':revision,'客户端版本':build,'本地修改摘要':hashlib.sha256(subprocess.check_output(['git','-C',str(args.source),'diff','--binary','HEAD'])).hexdigest(),'二进制摘要':hashlib.sha256(binary.read_bytes()).hexdigest(),
              '源码文件摘要':sources,'DBC目录摘要':hashlib.sha256(catalog.read_bytes()).hexdigest(),'结果':results}
    (args.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('成功',sum(r['退出码']==0 for r in results),'失败',sum(r['退出码']!=0 for r in results))
    return int(any(r['退出码'] for r in results))


if __name__=='__main__':raise SystemExit(main())
