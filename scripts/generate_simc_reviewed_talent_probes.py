"""从完整 DBC 天赋目录逐项实算，仅跳过已确认纯全局的被动天赋。"""
import argparse
import concurrent.futures
import copy
import hashlib
from collections import defaultdict
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'LMonitor.settings_test_sqlite')
import django
django.setup()
from botend.services.simc_skill_damage import (
    SimcSkillDamageSnapshotService, build_single_talent_actor_input,
    _mark_empty_runtime_amount_components_unresolved, _discard_empty_runtime_amount_components,
)
from scripts.audit_simc_global_damage_initialization import CLASSES, SPECS


def talent_probe_catalog(catalog, review):
    """复核表只排除纯全局被动；未收录、主动技能和混合效果均保留。"""
    reviewed = {row['节点']: row for row in review['条目'] if row['类型'] == '天赋'}
    result = []
    for row in catalog['talent_catalog']:
        # 只探测当前导出器支持的职业、专精、英雄树；不混入 PvP 等其他目录。
        if row.get('class_id', 1) not in CLASSES or row.get('tree_index', 1) not in (1, 2, 3):
            continue
        if row.get('spell_id', 1) <= 0:
            continue
        fact = reviewed.get(row['trait_entry_id'], {})
        parts = fact.get('分量') or []
        if row.get('passive') is True and parts and all(p['处理结论'] == '应剔除' for p in parts):
            continue
        result.append({**row, 'display_name': fact.get('名称') or row['name']})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=6)
    parser.add_argument('--batch-size', type=int, default=12, choices=range(1,25), help='每次进程中的独立角色数量')
    parser.add_argument('--spec', help='仅生成指定 class_spec')
    parser.add_argument('--limit', type=int, help='仅生成前若干项，用于批次一致性检查')
    parser.add_argument('--resume', action='store_true', help='仅复用同一构建下已通过的配置，其余写入新的原始批次')
    parser.add_argument('--partition',type=int,nargs=2,metavar=('序号','总数'),help='按完整计划分片，序号从零开始，各分片使用独立输出目录')
    args = parser.parse_args()
    cached_results = []
    binary_hash = hashlib.sha256(args.binary.read_bytes()).hexdigest()
    if args.resume:
        previous = json.loads((args.output/'summary.json').read_text(encoding='utf-8'))
        build = json.loads((args.output.parent/'build-manifest.json').read_text(encoding='utf-8'))
        if build.get('二进制_SHA256') != binary_hash:
            parser.error('断点数据与当前二进制不一致，必须使用新的输出目录。')
        cached_results = [r for r in previous['结果'] if r['status'] == '通过' and all(
            (args.output/f'{r["class"]}_{r["spec"]}--{r["entry"]}--{h}.json').is_file() for h in (100,34))]
    elif args.output.exists() and any(args.output.iterdir()):
        parser.error('请使用空输出目录；原始导出保留，不覆盖既有实算文件。')
    args.output.mkdir(parents=True, exist_ok=True)
    review = json.loads(args.review.read_text(encoding='utf-8'))
    catalog = json.loads((args.base / 'global-scope-catalog.json').read_text(encoding='utf-8'))
    supported = [(r['class'], r['spec']) for r in json.loads((args.base/'summary.json').read_text(encoding='utf-8'))
                 if r.get('rows') and not r.get('validation_errors')]
    tasks = []
    for row in talent_probe_catalog(catalog, review):
        cls = CLASSES.get(row['class_id'])
        if cls is None:
            continue
        for c, spec in supported:
            if c == cls and (not row['spec_ids'] or spec in [SPECS.get(s) for s in row['spec_ids']]):
                tasks.append((c, spec, row))

    if args.spec:
        tasks = [t for t in tasks if '_'.join(t[:2]) == args.spec]
    if args.limit:
        tasks = tasks[:args.limit]
    total_planned_count=len(tasks)
    if args.partition:
        part,count=args.partition
        if count<1 or not 0<=part<count:parser.error('分片参数必须满足 0 <= 序号 < 总数。')
        tasks=[task for i,task in enumerate(tasks) if i%count==part]
    planned_count = len(tasks)
    planned_keys = {(c,s,r['trait_entry_id']) for c,s,r in tasks}
    cached_results = [r for r in cached_results if (r['class'],r['spec'],r['entry']) in planned_keys]
    cached_keys = {(r['class'],r['spec'],r['entry']) for r in cached_results}
    tasks = [t for t in tasks if (t[0],t[1],t[2]['trait_entry_id']) not in cached_keys]
    attempt = uuid.uuid4().hex[:8]

    def run(task):
        cls, spec, row = task
        entry = row['trait_entry_id']
        key = f'{cls}_{spec}--{entry}'
        trait = SimpleNamespace(pk=entry, node_id=entry, talent_id=entry, max_points=row['max_ranks'],
                                tree_type={1:'class',2:'spec',3:'hero'}[row['tree_index']], db2_subtree_id=row['subtree_id'])
        actor_name = f'skill_damage_talent_{entry}_trait_{entry}'
        text = build_single_talent_actor_input((args.base/f'{cls}_{spec}.simc').read_text(encoding='utf-8'),
                cls, [], actor_plan=[{'name': actor_name, 'selected_talents': [trait]}])
        inp = args.output / (key+'.simc')
        inp.write_text(text, encoding='utf-8')
        result = {'class':cls, 'spec':spec, 'entry':entry, 'name':row['display_name'],
                  'spell_id':row['spell_id'], 'tree_type':trait.tree_type,
                  'hero_subtree_id':row['subtree_id'] or None, 'status':'通过'}
        service = SimcSkillDamageSnapshotService(SimpleNamespace(simc_revision=catalog['simc_revision'],
                    game_build=catalog['game_build']), backend=SimpleNamespace())
        for health in (100,34):
            dst = args.output / f'{key}--{health}.json'
            raw_dst = args.output / f'{key}--{attempt}--{health}.raw.json'
            try:
                process = subprocess.run([str(args.binary.resolve()), str(inp.resolve()),
                    f'skill_damage_export={raw_dst.resolve()}', f'skill_damage_revision={catalog["simc_revision"]}',
                    f'skill_damage_game_build={catalog["game_build"]}', f'skill_damage_target_health_percentage={health}'],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
                (args.output/f'{key}--{health}.log').write_text(process.stdout+process.stderr, encoding='utf-8')
                if process.returncode:
                    raise RuntimeError((process.stdout+process.stderr)[-1000:])
                payload = json.loads(raw_dst.read_text(encoding='utf-8'))
                dst.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
                marked = _mark_empty_runtime_amount_components_unresolved(payload)
                service._validate_export(payload, profile=SimpleNamespace(class_name=cls, spec=cls+'_'+spec))
                _discard_empty_runtime_amount_components(marked)
                service._validate_export(payload)
                actor = payload['actors'][0]
                if entry not in actor['selected_trait_ids'] or actor['talent_effectiveness'] != 'active':
                    result['status'] = '当前专精未激活'
            except Exception as exc:
                result.update(status='未生成', error=str(exc), health=health)
                break
        return result

    def run_batch(batch):
        if len(batch) == 1:
            return [run(batch[0])]
        cls, spec, _ = batch[0]
        key = f'{cls}_{spec}--batch-{batch[0][2]["trait_entry_id"]}-{attempt}'
        plan, records = [], []
        for _, _, row in batch:
            entry = row['trait_entry_id']
            trait = SimpleNamespace(pk=entry, node_id=entry, talent_id=entry, max_points=row['max_ranks'],
                    tree_type={1:'class',2:'spec',3:'hero'}[row['tree_index']], db2_subtree_id=row['subtree_id'])
            plan.append({'name':f'skill_damage_talent_{entry}_trait_{entry}', 'selected_talents':[trait]})
            records.append({'class':cls,'spec':spec,'entry':entry,'name':row['display_name'],
                    'spell_id':row['spell_id'],'tree_type':trait.tree_type,'hero_subtree_id':row['subtree_id'] or None,'status':'通过'})
        inp = args.output / (key+'.simc')
        inp.write_text(build_single_talent_actor_input((args.base/f'{cls}_{spec}.simc').read_text(encoding='utf-8'),
                       cls, [], actor_plan=plan), encoding='utf-8')
        service = SimcSkillDamageSnapshotService(SimpleNamespace(simc_revision=catalog['simc_revision'],
                    game_build=catalog['game_build']), backend=SimpleNamespace())
        try:
            for health in (100,34):
                dst = args.output / f'{key}--{health}.json'
                process = subprocess.run([str(args.binary.resolve()),str(inp.resolve()),
                    f'skill_damage_export={dst.resolve()}', f'skill_damage_revision={catalog["simc_revision"]}',
                    f'skill_damage_game_build={catalog["game_build"]}',f'skill_damage_target_health_percentage={health}'],
                    capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=180)
                (args.output/f'{key}--{health}.log').write_text(process.stdout+process.stderr,encoding='utf-8')
                if process.returncode:
                    raise RuntimeError((process.stdout+process.stderr)[-1000:])
                payload = json.loads(dst.read_text(encoding='utf-8'))
                raw = copy.deepcopy(payload)
                marked = _mark_empty_runtime_amount_components_unresolved(payload)
                service._validate_export(payload,expected_actor_names=[a['name'] for a in plan])
                _discard_empty_runtime_amount_components(marked)
                service._validate_export(payload,expected_actor_names=[a['name'] for a in plan])
                actors = {a['name']:a for a in raw['actors']}
                for item in records:
                    entry = item['entry']
                    actor = actors[f'skill_damage_talent_{entry}_trait_{entry}']
                    if entry not in actor['selected_trait_ids'] or actor['talent_effectiveness'] != 'active':
                        item['status'] = '当前专精未激活'
                    split = {**raw,'actors':[actor], 'batch_source':dst.name,
                             'batch_sha256':hashlib.sha256(dst.read_bytes()).hexdigest()}
                    (args.output/f'{cls}_{spec}--{entry}--{health}.json').write_text(json.dumps(split,ensure_ascii=False),encoding='utf-8')
            return records
        except Exception as exc:
            (args.output/f'{key}.fallback.log').write_text(str(exc),encoding='utf-8')
            return [run(task) for task in batch]

    grouped = defaultdict(list)
    for task in tasks:
        grouped[task[:2]].append(task)
    batches = [items[i:i+args.batch_size] for items in grouped.values() for i in range(0,len(items),args.batch_size)]
    manifest = {'源码提交':catalog['simc_revision'], '客户端版本':catalog['game_build'],
                '二进制_SHA256':binary_hash,'分片':args.partition,'完整计划数':total_planned_count,
                '说明':'从完整 DBC 职业、专精与英雄天赋目录生成；仅排除已确认纯全局被动。每个配置为基础技能框架加一个天赋，不穷举多个可选天赋组合。', '计划数':planned_count, '批次大小':args.batch_size}
    results = list(cached_results)
    (args.output/'summary.json').write_text(json.dumps({**manifest,'结果':results}, ensure_ascii=False, indent=2), encoding='utf-8')
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_batch, batch) for batch in batches]
        for future in concurrent.futures.as_completed(futures):
            results.extend(future.result())
            if results:
                (args.output/'summary.json').write_text(json.dumps({**manifest,'结果':results}, ensure_ascii=False, indent=2), encoding='utf-8')
                print(f'已完成 {len(results)} / {planned_count}，未生成 {sum(r["status"] == "未生成" for r in results)}', flush=True)


if __name__ == '__main__':
    main()
