"""将原生动作工厂、逐天赋原始导出与独立 APL 初始化目录进行覆盖核对。"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import subprocess

try:
    from scripts.export_simc_apl_fields import extract_static_action_candidates
except ModuleNotFoundError:
    from export_simc_apl_fields import extract_static_action_candidates


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def damage_identity(action):
    return (action['token'].lower(), action['spell_id'])


def inspect_inventory(inventory, token, exported):
    """按原生子动作追踪，并将未解锁入口和治疗与伤害漏项区分。"""
    roots = [a for a in inventory if a.get('requested_action', '').split(',')[0] == token]
    by_identity = {damage_identity(a): a for a in inventory}
    pending, found, visited = roots[:], [], set()
    if roots and not any(a['spell_valid'] for a in roots):
        return {'结论': '无有效DBC入口下的伤害候选', '原生入口': roots, '缺少伤害动作': []}
    while pending:
        action = pending.pop()
        identity = damage_identity(action)
        if identity in visited:
            continue
        visited.add(identity)
        if action.get('damage_action', action.get('harmful', False)) and action['spell_valid'] and (action['direct'] or action['periodic']):
            found.append(action)
        pending.extend(by_identity[damage_identity(c)] for c in action['children']
                       if damage_identity(c) in by_identity)
    absent = [a for a in found if damage_identity(a) not in exported]
    if absent and not any(a['spell_valid'] for a in roots):
        conclusion = '无有效DBC入口下的伤害候选'
    else:
        conclusion = '原生伤害未覆盖' if absent else '由已有伤害动作承载' if found else '此配置无玩家伤害动作'
    return {'结论': conclusion, '原生入口': roots, '缺少伤害动作': absent}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--exports', type=Path, action='append', required=True)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    binary_hash = hashlib.sha256(args.binary.read_bytes()).hexdigest()
    manifest = args.output / 'audit-build.json'
    if manifest.exists() and read(manifest)['sha256'] != binary_hash:
        raise ValueError('审计目录属于另一构建，请使用新目录，避免复用旧探针。')
    manifest.write_text(json.dumps({'sha256': binary_hash}), encoding='utf-8')
    factories = [asdict(row) for row in extract_static_action_candidates(args.source)]
    seen, damage, coverage, unresolved = defaultdict(set), defaultdict(set), Counter(), defaultdict(set)
    unsupported = defaultdict(set)
    artifacts = 0
    revisions = set()
    for directory in args.exports:
        for path in sorted(directory.glob('*100.json')):
            if not re.fullmatch(r'[a-z]+_[a-z_]+(?:--\d+--|-)100\.json', path.name):
                continue
            payload = read(path)
            revisions.add((payload.get('simc_revision'), payload.get('game_build')))
            artifacts += 1
            for actor in payload.get('actors', []):
                cls = actor['class'].replace('_', '')
                spec_key = (cls, actor['spec'])
                for action in actor['actions']:
                    seen[cls].add(action['token'].lower())
                    if action.get('supported'):
                        damage[spec_key].add(damage_identity(action))
                        if action.get('baseline', {}).get('unresolved_reason'):
                            unresolved[cls].add((actor['spec'], action['token'], action['baseline']['unresolved_reason']))
                    else:
                        unsupported[cls].add((actor['spec'], action['token'], action['spell_id'],
                                              action.get('unsupported_reason') or '未提供原因'))
                for action in actor.get('action_coverage', []):
                    seen[cls].add(action['token'].lower())
                    if action.get('requested_action'):
                        seen[cls].add(action['requested_action'].split(',')[0].lower())
                    coverage[action['status']] += 1
    if len(revisions) != 1:
        raise ValueError('覆盖审计不能混合不同 SimC/DBC 版本。')
    revision, build = next(iter(revisions))
    missing = {(row['class_name'].replace('_', ''), row['token']) for row in factories
               if row['token'] not in seen[row['class_name'].replace('_', '')]}
    profiles = []
    for path in sorted(args.profiles.glob('*.simc')):
        key = path.stem
        cls = key.split('_')[0]
        if not (args.profiles / f'{key}-100.json').exists():
            continue
        profiles.append((cls, key, path))

    def probe(job):
        cls, key, profile, token = job
        prefix = args.output / f'{key}--{token}'
        text = profile.read_text(encoding='utf-8')
        text = '\n'.join(line for line in text.splitlines() if not line.startswith('actions'))
        text += f'\nactions={token}\n'
        source, output = prefix.with_suffix('.simc'), prefix.with_suffix('.json')
        source.write_text(text, encoding='utf-8')
        if output.exists():
            result = None
        else:
            try:
                result = subprocess.run([str(args.binary.resolve()), str(source.resolve()),
                    f'apl_metadata_export={output.resolve()}', f'apl_metadata_revision={revision}',
                    f'apl_metadata_game_build={build}'], capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=60)
            except subprocess.TimeoutExpired:
                return {'职业': cls, '配置': key, '入口': token, '结论': '原生初始化超时'}
        if result and result.returncode:
            prefix.with_suffix('.log').write_text(result.stdout + result.stderr, encoding='utf-8')
            return {'职业': cls, '配置': key, '入口': token, '结论': '原生初始化拒绝', '日志': str(prefix.with_suffix('.log'))}
        payload = read(output)
        inventory = payload.get('action_inventory')
        if not isinstance(inventory, list):
            raise ValueError('独立初始化目录缺失，请使用最新补丁构建。')
        return {'职业': cls, '配置': key, '入口': token,
                **inspect_inventory(inventory, token, damage[(cls, key.split('_', 1)[1])]),
                '证据': str(output)}

    jobs = [(cls, key, path, token) for cls, key, path in profiles
            for candidate_cls, token in sorted(missing)
            if cls == candidate_cls]
    probes = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(probe, job) for job in jobs]
        for future in as_completed(futures):
            probes.append(future.result())
    report = {'SimC版本': revision, 'DBC版本': build, '审计构建SHA256': binary_hash, '原始配置数': artifacts,
              '源码工厂数': len(factories), '工厂入口去重数': len({(r['class_name'], r['token']) for r in factories}),
              '未直接匹配入口数': len(missing), '原生动作处理统计': dict(coverage),
              '独立初始化结论统计': dict(Counter(p['结论'] for p in probes)),
              '边界': '独立初始化使用各专精预设天赋；未选择的组合、宠物和召唤物执行链不能据此宣称全部覆盖。',
              '源码工厂': factories, '逐入口复验': sorted(probes, key=lambda p: (p['职业'], p['入口'], p['配置'])),
              '伤害上下文缺口': {cls: sorted(values) for cls, values in unresolved.items()},
              '未生成伤害的DBC候选': {cls: sorted(values) for cls, values in unsupported.items()}}
    (args.output / 'coverage.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in ('源码工厂', '逐入口复验', '伤害上下文缺口', '未生成伤害的DBC候选')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
