"""隔离执行所有专精的真实技能伤害导出，不写入业务数据库。"""
import argparse
import hashlib
import concurrent.futures
import copy
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from prepare_simc_native_inputs import discover_profiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'LMonitor.settings_test_sqlite')
import django
django.setup()
from botend.constants.simc_specs import SIMC_KNOWN_SPECS
from botend.services.simc_skill_damage import (
    SimcSkillDamageSnapshotService, attach_runtime_product_metrics,
    build_single_talent_actor_input, project_skill_damage_product_payload,
    flatten_single_talent_damage_variants, classify_global_skill_effects, prune_global_damage_talents,
    _mark_empty_runtime_amount_components_unresolved,
    _discard_empty_runtime_amount_components,
    collect_skill_damage_unresolved,
)

parser = argparse.ArgumentParser(description='真实执行全部 40 个专精的技能伤害审计，不写业务数据库。')
parser.add_argument('--source', type=Path, required=True, help='已应用项目补丁的 SimC 源码目录')
parser.add_argument('--binary', type=Path, required=True, help='对应源码构建的 SimC 程序')
parser.add_argument('--output', type=Path, required=True, help='不存在的审计输出目录，保留原始输入和结果')
parser.add_argument('--talents', action='store_true', help='使用上游完整天赋；没有上游模板的专精仍记录基础输入')
parser.add_argument('--wait-apl', action='store_true', help='以等待循环检查技能目录对 APL 的依赖')
parser.add_argument('--alternate-apl', action='store_true', help='注入无效循环，验证导出目录仍不受输入 APL 影响')
parser.add_argument('--raw-input', type=Path, help='复验已有真实导出；必须与二进制摘要和生成输入完全一致')
parser.add_argument('--spec', action='append', default=[], help='可重复指定 class_spec；默认审计全部专精')
parser.add_argument('--workers', type=int, default=4, choices=range(1,9), help='并发数，默认 4')
args = parser.parse_args()
SOURCE, BINARY, OUT = args.source.resolve(), args.binary.resolve(), args.output.resolve()
OUT.mkdir(parents=True, exist_ok=False)
REVISION = subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], text=True).strip()
BUILD = re.search(r'#define CLIENT_DATA_WOW_VERSION "([^"]+)"', (SOURCE/'engine/dbc/generated/client_data_version.inc').read_text())[1]
with BINARY.open('rb') as binary_file:
    BINARY_HASH = hashlib.file_digest(binary_file, 'sha256').hexdigest()
if args.raw_input:
    args.raw_input = args.raw_input.resolve()
    previous = json.loads((args.raw_input/'manifest.json').read_text(encoding='utf-8'))
    if previous.get('二进制_SHA256') != BINARY_HASH or previous.get('源码提交') != REVISION:
        parser.error('原始导出不属于当前二进制和源码提交。')
catalog_path = OUT / 'global-scope-catalog.json'
subprocess.run([str(BINARY), f'skill_damage_scope_export={catalog_path}',
                f'skill_damage_revision={REVISION}', f'skill_damage_game_build={BUILD}'], check=True, capture_output=True)
catalog_service = SimcSkillDamageSnapshotService(SimpleNamespace(simc_revision=REVISION, game_build=BUILD), backend=SimpleNamespace())
catalog = catalog_service._load_global_damage_talent_catalog(json.loads(catalog_path.read_text(encoding='utf-8')))
profiles = discover_profiles(SOURCE)


def run(identity):
    class_name, spec = identity
    key = class_name + '_' + spec
    result = {'class': class_name, 'spec': spec, 'game_build': BUILD, 'source_revision': REVISION}
    profile = profiles.get(identity)
    result['upstream_profile'] = profile[0].name if profile else None
    result['talents_input'] = '上游完整天赋' if args.talents and profile else '基础技能与自动赠送天赋'
    source_text = profile[1] if profile else f'{class_name}="audit"\nspec={spec}\nlevel=90\n'
    # 保留合法武器形态，去掉消耗品、装备触发和循环，直接由导出器发现伤害技能。
    actor_lines = [line for line in source_text.splitlines() if re.match(r'^(?:'+class_name+r'|spec|level|race|main_hand|off_hand)=', line)]
    actor_lines.append('role=attack')
    text = '\n'.join(actor_lines) + '\n'
    text = build_single_talent_actor_input(text, class_name, [])
    if args.talents and profile:
        text += '\n'.join(line for line in source_text.splitlines() if line.startswith(('talents=', 'class_talents=', 'spec_talents=', 'hero_talents='))) + '\n'
    if args.wait_apl:
        text += 'actions=wait\n'
    if args.alternate_apl:
        text += 'actions=unknown_damage_audit_action\nactions.audit+=/another_unknown_action\n'
    text = 'iterations=1\nthreads=1\nallow_experimental_specializations=1\n' + text
    if identity == ('warlock', 'destruction'):
        text += 'warlock.normalize_destruction_mastery=1\n'
    input_path = OUT / (key + '.simc')
    input_path.write_text(text, encoding='utf-8')
    payloads = []
    for health in (100, 34):
        output_path = OUT / (key + f'-{health}.json')
        command = [str(BINARY), str(input_path), f'skill_damage_export={output_path}',
            f'skill_damage_revision={REVISION}', f'skill_damage_game_build={BUILD}',
            f'skill_damage_target_health_percentage={health}']
        try:
            if args.raw_input:
                if (args.raw_input/input_path.name).read_text(encoding='utf-8') != text:
                    raise ValueError('生成输入与原始审计不一致，拒绝复用。')
                if not (args.raw_input/output_path.name).exists():
                    previous_rows = json.loads((args.raw_input/'summary.json').read_text(encoding='utf-8'))
                    return next(row for row in previous_rows if (row['class'], row['spec']) == identity)
                shutil.copyfile(args.raw_input/output_path.name, output_path)
            else:
                proc = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
                (OUT/(key+f'-{health}.log')).write_text(proc.stdout + proc.stderr,encoding='utf-8')
                if proc.returncode or not output_path.exists():
                    result.update(status='导出失败', health=health, returncode=proc.returncode, error=(proc.stdout+proc.stderr)[-1600:])
                    return result
            payload = json.loads(output_path.read_text(encoding='utf-8'))
        except Exception as exc:
            result.update(status='执行异常', health=health, error=str(exc))
            return result
        snapshot = SimpleNamespace(simc_revision=REVISION,game_build=BUILD,schema_revision=SimcSkillDamageSnapshotService.DATASET_SCHEMA_REVISION)
        service = SimcSkillDamageSnapshotService(snapshot, backend=SimpleNamespace())
        marked = _mark_empty_runtime_amount_components_unresolved(payload)
        try:
            service._validate_export(payload, profile=SimpleNamespace(class_name=class_name, spec=class_name+'_'+spec))
            _discard_empty_runtime_amount_components(marked)
            service._validate_export(payload, profile=SimpleNamespace(class_name=class_name, spec=class_name+'_'+spec))
        except Exception as exc:
            result.setdefault('validation_errors',[]).append({'health':health,'error':str(exc)})
        payloads.append(payload)
    if not payloads[0].get('actors'):
        result['status']='缺少玩家 actor'
        return result
    base_high=payloads[0]['actors'][0]
    base_low=payloads[1]['actors'][0]
    result['actions']=len(base_high.get('actions',[]))
    result['raw_unresolved']=dict(Counter(action.get('unsupported_reason') or (action.get('baseline') or {}).get('unresolved_reason') for action in base_high.get('actions',[]) if action.get('unsupported_reason') or (action.get('baseline') or {}).get('unresolved_reason')))
    actor=copy.deepcopy(base_high)
    effects=classify_global_skill_effects(base_high,base_low,[])
    effects=[effect for effect in effects if not any(p.get('kind')=='crit_chance' for p in effect.get('projections',[]))]
    selected_talents = [SimpleNamespace(pk=entry, node_id=entry, name=catalog[entry]['name'],
                          name_zh='', tree_type='spec', db2_subtree_id=0)
                        for entry in base_high.get('selected_trait_ids', []) if entry in catalog]
    static_effects = prune_global_damage_talents(selected_talents, [], {}, catalog)[3]
    actor['global_skill_effects']=static_effects+effects
    actor['actions']=flatten_single_talent_damage_variants(base_high,base_low,[],global_effects=effects)
    result['unresolved_damage'] = collect_skill_damage_unresolved({'actors': [base_high]})
    product=project_skill_damage_product_payload({'actors':[actor]})
    rows=product['actors'][0]['actions']
    result['rows']=len(rows)
    result['skills']=len({(row.get('token'),row.get('spell_id')) for row in rows})
    result['global_effects']=[(e.get('effect_id'),e.get('source_spell_ids')) for e in effects]
    result['incomplete_formulas']=[{'token':r['token'],'condition':r.get('variant'),'formulas':r['product']['formula_components']} for r in rows if any(f.get('status')=='incomplete' for f in r['product']['formula_components'])]
    result['zero_damage_rows']=sum(r['product']['final_normalized_damage'] <= 0 for r in rows)
    result['crit_chances']=dict(Counter(str(round(f['crit_chance'],6)) for r in rows for f in r['product']['formula_components']))
    result['expectation_errors'] = []
    for row in rows:
        formulas = row['product']['formula_components']
        for count in ('1', '2', '5', '10', '20'):
            expected = sum(f['noncrit_contribution_by_target'][count] + f['crit_contribution_by_target'][count] for f in formulas)
            actual = row['product']['final_normalized_damage_by_target'][count]
            if not math.isclose(expected, actual, rel_tol=1e-9, abs_tol=1e-8):
                result['expectation_errors'].append({'skill': row['token'], 'targets': count})
    result['status']='协议校验失败' if result.get('validation_errors') else '无可展示伤害' if not rows else '已生成，待逐项审计'
    (OUT/(key+'-product.json')).write_text(json.dumps(product,ensure_ascii=False),encoding='utf-8')
    return result


if __name__ == '__main__':
    identities=sorted((c,s) for c, specs in SIMC_KNOWN_SPECS.items() for s in specs)
    if args.spec:
        unknown = set(args.spec) - {'_'.join(identity) for identity in identities}
        if unknown: parser.error('未知专精：' + ', '.join(sorted(unknown)))
        identities=[identity for identity in identities if '_'.join(identity) in args.spec]
    (OUT/'manifest.json').write_text(json.dumps({
        '源码提交': REVISION, '客户端版本': BUILD,
        '二进制_SHA256': BINARY_HASH,
        '原始导出目录': str(args.raw_input) if args.raw_input else None,
        '专精范围': ['_'.join(identity) for identity in identities],
        '完整天赋': args.talents, '替换循环': args.alternate_apl,
        '说明': '协议通过不代表所有机制已实现；必须复查未解析、缺失技能、公式和宠物贡献。',
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    results=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        tasks={pool.submit(run, identity):identity for identity in identities}
        for future in concurrent.futures.as_completed(tasks):
            identity=tasks[future]
            try: result=future.result()
            except Exception as exc: result={'class':identity[0],'spec':identity[1],'status':'审计异常','error':repr(exc)}
            results.append(result)
            print(identity, result['status'],result.get('rows'),result.get('error','')[:100],flush=True)
            (OUT/'summary.json').write_text(json.dumps(sorted(results,key=lambda r:(r['class'],r['spec'])),ensure_ascii=False,indent=2),encoding='utf-8')

    if any(r.get('validation_errors') or r.get('status') != '已生成，待逐项审计' or r.get('incomplete_formulas') or r.get('zero_damage_rows') or r.get('expectation_errors') for r in results):
        sys.exit(2)
