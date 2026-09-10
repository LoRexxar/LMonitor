"""用单天赋真实导出核对全局分量清零，保留原始输入和逐分量证据。"""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'LMonitor.settings_test_sqlite')
import django
django.setup()
from botend.services.simc_skill_damage import build_single_talent_actor_input, _validate_global_scope_catalog

CLASSES = dict(enumerate(['warrior', 'paladin', 'hunter', 'rogue', 'priest',
                         'deathknight', 'shaman', 'mage', 'warlock', 'monk',
                         'druid', 'demonhunter', 'evoker'], 1))
SPECS = dict(zip([71,72,73,65,66,70,253,254,255,259,260,261,256,257,258,
                 250,251,252,262,263,264,62,63,64,265,266,267,268,270,269,
                 102,103,104,105,577,581,1480,1467,1468,1473],
                ['arms','fury','protection','holy','protection','retribution',
                 'beast_mastery','marksmanship','survival','assassination','outlaw','subtlety',
                 'discipline','holy','shadow','blood','frost','unholy','elemental','enhancement',
                 'restoration','arcane','fire','frost','affliction','demonology','destruction',
                 'brewmaster','mistweaver','windwalker','balance','feral','guardian','restoration',
                 'havoc','vengeance','devourer','devastation','preservation','augmentation']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--audit', type=Path, required=True, help='全职业导出目录，提供目录与基线输入')
    parser.add_argument('--previous-catalog', type=Path, help='只检测相较旧目录新增或变化的节点')
    parser.add_argument('--spell-id', action='append', type=int, default=[], help='额外检测的法术，可重复')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, choices=range(1,5), default=2)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    catalog = json.loads((args.audit / 'global-scope-catalog.json').read_text(encoding='utf-8'))
    facts = {r['trait_entry_id']: r for r in catalog['talents']}
    old = {} if not args.previous_catalog else {
        r['trait_entry_id']: r for r in json.loads(args.previous_catalog.read_text(encoding='utf-8'))['talents']}
    selected = [r for r in catalog['talent_catalog'] if r['spell_id'] in args.spell_id or (
        r['trait_entry_id'] in facts and (not args.previous_catalog or
        old.get(r['trait_entry_id'], {}).get('global_components') != r['global_components']))]

    def run(row):
        entry = row['trait_entry_id']
        cls = CLASSES[row['class_id']]
        specs = [SPECS[s] for s in row['spec_ids'] if s in SPECS]
        if not specs:
            specs = [p.stem[len(cls)+1:] for p in args.audit.glob(cls + '_*.simc')]
        specs = [s for s in specs if (cls, s) not in {('paladin','holy'),('monk','mistweaver')}]
        result = {'节点':entry, '法术ID':row['spell_id'], '名称':row['name'], '职业':cls, '错误':[]}
        if not specs:
            return {**result, '状态':'上游不支持', '证据':[]}
        spec = specs[0]
        result['专精'] = spec
        trait = SimpleNamespace(pk=entry,node_id=entry,talent_id=entry,max_points=row['max_ranks'],
                                tree_type={1:'class',2:'spec',3:'hero'}[row['tree_index']],
                                db2_subtree_id=row['subtree_id'])
        text = build_single_talent_actor_input(
            (args.audit / f'{cls}_{spec}.simc').read_text(encoding='utf-8'), cls, [trait])
        inp, dst = args.output / f'{entry}.simc', args.output / f'{entry}.json'
        inp.write_text(text,encoding='utf-8')
        process = subprocess.run([str(args.binary.resolve()),str(inp.resolve()),
            f'skill_damage_export={dst.resolve()}',f'skill_damage_revision={catalog["simc_revision"]}',
            f'skill_damage_game_build={catalog["game_build"]}'],capture_output=True,text=True,
            encoding='utf-8',errors='replace',timeout=180)
        if process.returncode:
            return {**result,'状态':'导出失败','错误':[(process.stderr or process.stdout)[-2000:]]}
        actors = json.loads(dst.read_text(encoding='utf-8'))['actors']
        actor = next(a for a in actors if a['name'].startswith('skill_damage_talent_'))
        evidence = [e for e in actor['normalized_talent_effects'] if e['trait_entry_id']==entry]
        wanted = {(part['spell_id'],i) for part in facts.get(entry,{}).get('global_components',[]) for i in part['effect_indices']}
        observed = {(e['spell_id'],e['effect_index']) for e in evidence if e['excluded']}
        if observed != wanted:
            result['错误'].append(f'排除集合不一致：缺少 {wanted-observed}，多出 {observed-wanted}')
        _validate_global_scope_catalog(actor)
        if entry not in facts:
            base = next(a for a in actors if a['name']=='skill_damage_base')
            state_keys = lambda a: {(s['scope'],s['spell_id']) for s in a['global_damage_states'] if s['available']}
            added = state_keys(actor)-state_keys(base)
            result['新增全局状态'] = sorted(added)
            if not added:
                result['错误'].append('指定运行时天赋没有新增全局状态证据')
        for e in evidence:
            if e['excluded'] and (e['actual_base_value'] != 0 or not e['damage_scaling_removed']):
                result['错误'].append(f'初始化恢复增伤：{e}')
            if not e['excluded'] and e['dbc_base_value'] and not e['actual_base_value']:
                result['错误'].append(f'非全局分量被清零：{e}')
        result.update(状态='失败' if result['错误'] else '通过',证据=evidence)
        print(cls,spec,row['name'],result['状态'],flush=True)
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(run,selected))
    report = {'源码提交':catalog['simc_revision'],'客户端版本':catalog['game_build'],
              '二进制_SHA256':hashlib.sha256(args.binary.read_bytes()).hexdigest(),
              '说明':'每个选中节点使用一个支持专精的单天赋配置，未穷举天赋组合。', '节点':rows}
    (args.output / 'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return int(any(r['错误'] for r in rows))


if __name__ == '__main__':
    raise SystemExit(main())
