"""实算单天赋的精确参考配置，复核下表主条件及前置与全局物理分量的交集。"""
import argparse
import concurrent.futures
from collections import Counter
import hashlib
import json
import math
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

from botend.services.simc_skill_damage import (
    build_single_talent_actor_input, classify_global_skill_effects,
    flatten_single_talent_damage_variants, project_skill_damage_product_payload,
    prune_global_damage_talents, _reviewed_global_fact_metadata,
    collect_skill_damage_unresolved,
)
from botend.services.simc_global_display import reviewed_static_source_matches_owner
from botend.services.simc_skill_state_projection import global_damage_state_identities, uses_global_damage_scenario
from scripts.audit_simc_scope_completeness import load_raw_inventory, load_raw_talents, expected_parts, inspect_actor, inspect_upper_projection


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def physical_parts(effects):
    return {(c['spell_id'], i) for e in effects for c in e.get('global_components') or []
            for i in c.get('effect_indices') or [c.get('effect_index')] if i}


def local_parameter_witness(row, entry, talent, global_definition, after, before):
    """验证间接参数链：全局分量归零、局部参数存活、精确干预后同一原生运算变化。"""
    expected = physical_parts([global_definition])
    parameters = {(talent['spell_id'], e['index']) for e in talent['effects']
                  if e['type'] == 6 and e['subtype'] in (107, 108)
                  and e['misc1'] in (3, 12, 23, 32, 33)}  # P_EFFECT_1 至 P_EFFECT_5
    if not expected or not parameters:
        return False
    for h in (100, 34):
        selected, reference = after[h], before[h]
        if (set(selected.get('selected_trait_ids') or []) - set(reference.get('selected_trait_ids') or []) != {entry}
                or set(reference.get('selected_trait_ids') or []) - set(selected.get('selected_trait_ids') or [])):
            return False
        for actor in (selected, reference):
            if actor.get('global_damage_policy') != 'exclude_before_probe':
                return False
            facts = [*(actor.get('normalized_scope_effects') or []), *(actor.get('normalized_talent_effects') or [])]
            for part in expected:
                matches = [f for f in facts if (f.get('spell_id'), f.get('effect_index')) == part]
                if not matches or any(f.get('actual_base_value') != 0 or f.get('actual_mastery_coefficient', 0) != 0 for f in matches):
                    return False
        if not any(f.get('trait_entry_id') == entry and f.get('excluded') is False
                   and (f.get('spell_id'), f.get('effect_index')) in parameters - expected
                   and type(f.get('actual_base_value')) in (int, float)
                   and math.isfinite(f['actual_base_value']) and f['actual_base_value'] != 0
                   for f in selected.get('normalized_talent_effects') or []):
            return False
    for h in (100, 34):
        actors = [{(a.get('token'), a.get('spell_id')): a for a in actor.get('actions') or []}
                  for actor in (after[h], before[h])]
        for part in row.get('components') or []:
            actions = [a.get((part.get('token'), part.get('spell_id'))) for a in actors]
            if not all(actions):
                continue
            operations = []
            for action in actions:
                amount = (action.get('baseline') or {}).get(part.get('component')) or {}
                operations.append({(s.get('source_file'), s.get('source_line'), s.get('layer')): s['factor']
                    for s in amount.get('runtime_factor_sources') or []
                    if s.get('source_kind') == 'native_operation' and s.get('source_file') and s.get('source_line', 0) > 0
                    and s.get('evidence') == 'native_multiplier_dataflow_v1'
                    and type(s.get('factor')) in (int, float) and math.isfinite(s['factor'])})
            if any(not math.isclose(operations[0][k], operations[1][k], rel_tol=1e-12, abs_tol=1e-12)
                   for k in operations[0].keys() & operations[1].keys()):
                return True
    return False


def audit_spec(args, key, records, catalog, global_catalog):
    cls, spec = key.split('_', 1)
    output = args.output / key
    output.mkdir(parents=True, exist_ok=True)
    base = {h: read(args.base / f'{key}-{h}.json')['actors'][0] for h in (100, 34)}
    paths = {None: {h: args.base / f'{key}-{h}.json' for h in (100, 34)}}
    selections = {frozenset(base[100]['selected_trait_ids']): None}
    for rec in records:
        entry = rec['entry']
        paths[entry] = {h: args.single / f'{key}--{entry}--{h}.json' for h in (100, 34)}
        selections.setdefault(frozenset(read(paths[entry][100])['actors'][0]['selected_trait_ids']), entry)

    def trait(entry):
        row = catalog[entry]
        return SimpleNamespace(pk=entry, node_id=entry, talent_id=entry,
            tree_type={1: 'class', 2: 'spec', 3: 'hero'}[row['tree_index']],
            max_points=row['max_ranks'], db2_subtree_id=row['subtree_id'],
            name=row['name'], name_zh='')

    static = prune_global_damage_talents([trait(r['entry']) for r in records], [], {}, global_catalog)[3]
    accepted = [e for e in static if reviewed_static_source_matches_owner(base[100], e, class_name=cls)]
    statistics = Counter()
    examples, failures, configs = [], [], []
    unresolved = {}
    inventory = None
    if args.raw_catalog:
        spells, families, racial_catalog = load_raw_inventory(str(args.raw_catalog.resolve()))
        family = families[catalog[records[0]['entry']]['class_id']]
        inventory = (spells, family, expected_parts(spells, family), racial_catalog)
    for rec in records:
        entry = rec['entry']
        after = {h: read(paths[entry][h])['actors'][0] for h in (100, 34)}
        for health, actor in after.items():
            for problem in collect_skill_damage_unresolved({'actors': [actor]}, target_health=health):
                action = problem['action']
                key_ = (action['token'], action['spell_id'], problem['reason'])
                unresolved.setdefault(key_, problem)
        if inventory:
            for health, actor in after.items():
                checked = inspect_actor(actor, *inventory, raw_traits=load_raw_talents(str(args.raw_catalog.resolve())))
                statistics['独立原始DBC配置'] += 1
                failures.extend({'天赋': entry, '目标血量': health, **error} for error in checked['错误'])
        selected = frozenset(after[100]['selected_trait_ids'])
        if entry not in selected:
            failures.append({'天赋': entry, '问题': '目标天赋没有激活'})
            continue
        reference = selected - {entry}
        if reference in selections:
            before_paths = paths[selections[reference]]
        else:
            digest = hashlib.sha256(','.join(map(str, sorted(reference))).encode()).hexdigest()[:16]
            before_paths = {h: output / f'reference-{digest}-{h}.json' for h in (100, 34)}
            inp = output / f'reference-{digest}.simc'
            text = build_single_talent_actor_input(
                (args.base / f'{key}.simc').read_text(encoding='utf-8'), cls, [],
                actor_plan=[{'name': 'skill_damage_reference', 'selected_talents': [
                    trait(e) for e in sorted(reference) if catalog[e]['tree_index'] in (1, 2, 3)]}])
            if inp.exists() and inp.read_text(encoding='utf-8') != text:
                raise ValueError('参考配置输入漂移：' + str(inp))
            inp.write_text(text, encoding='utf-8')
            for h, dst in before_paths.items():
                if not dst.exists():
                    process = subprocess.run([str(args.binary.resolve()), str(inp.resolve()),
                        f'skill_damage_export={dst.resolve()}',
                        f'skill_damage_revision={args.identity["simc_revision"]}',
                        f'skill_damage_game_build={args.identity["game_build"]}',
                        f'skill_damage_target_health_percentage={h}'], capture_output=True, timeout=180)
                    if process.returncode:
                        raise ValueError(process.stderr.decode(errors='replace')[-1000:])
            reference_key = f'reference-{digest}'
            paths[reference_key] = before_paths
            selections[reference] = reference_key
        before = {h: read(before_paths[h])['actors'][0] for h in (100, 34)}
        differences = {h: [sorted(set(after[h]['selected_trait_ids']) - set(before[h]['selected_trait_ids'])),
                           sorted(set(before[h]['selected_trait_ids']) - set(after[h]['selected_trait_ids']))]
                       for h in (100, 34)}
        if any(d != [[entry], []] for d in differences.values()):
            # 默认赠送节点即使显式不选也会被原生重建，不冒充可开关的单天赋差分。
            statistics['不可独立关闭的默认节点'] += 1
            configs.append({'天赋': entry, '状态': '不可独立关闭', '实际差异': differences})
            continue
        statistics['精确差分配置'] += 1
        if global_catalog.get(entry, {}).get('remove_talent'):
            statistics['规划前排除的纯全局天赋'] += 1
            continue
        tr = trait(entry)
        item = {'talent': {'id': entry, 'node_id': entry, 'name': tr.name, 'tree_type': tr.tree_type,
                          'spell_id': catalog[entry]['spell_id']},
                'high': after[100], 'low': after[34],
                'reference_high': before[100], 'reference_low': before[34],
                'reference_traits': [{'trait_entry_id': e, 'name': catalog[e]['name']} for e in reference]}
        effects = [*accepted, *classify_global_skill_effects(*base.values(), [item])]
        for effect in effects:
            if effect.get('source_spell_ids'):
                effect.update(_reviewed_global_fact_metadata(base[100], effect['source_spell_ids'][0]))
        # 主条件差分使用 item 中完整且精确的参考对；基础行已由另一个全专精检查覆盖，
        # 保留其作用域元数据，避免为每个天赋反复枚举并丢弃同一套基础场景。
        baseline_metadata = [{k: v for k, v in actor.items() if k != 'actions'} for actor in base.values()]
        rows = flatten_single_talent_damage_variants(*baseline_metadata, [item], global_effects=effects)
        rows = [r for r in rows if (r.get('variant') or {}).get('trait_entry_id') == entry]
        product = project_skill_damage_product_payload({
            'actors': [{**{k: v for k, v in after[100].items() if k != 'actions'}, 'actions': rows}]})
        result = product['actors'][0]['actions']
        failures.extend({'天赋': entry, **error} for error in inspect_upper_projection(after[100], product['actors'][0]))
        statistics['展示单天赋行'] += len(result)
        known_globals = physical_parts(after[100].get('reviewed_global_effects') or [])
        by_token = {a['token']: a for a in after[100].get('actions') or []}
        reference_actions = {(a.get('token'), a.get('spell_id')) for a in before[100].get('actions') or []}

        def owned_damage(row):
            action, seen = row, set()
            # 相同前置、仅增加目标天赋后新增的原生动作，是可复核的解锁证据；
            # 不根据技能后缀、同一英雄树或全职业技能列表猜测归属。
            components = [(p.get('token'), p.get('spell_id')) for p in row.get('components') or []]
            if any(identity not in reference_actions for identity in components):
                return True
            # 原生目录的 global_components 已沿 DBC trigger 闭包追溯归属；
            # 触发出来的疾病本体仍属于主动天赋自身伤害。
            owned_spells = {catalog[entry]['spell_id'], *(p['spell_id'] for p in
                            global_catalog.get(entry, {}).get('global_components') or [])}
            while action and action.get('token') not in seen:
                seen.add(action.get('token'))
                if owned_spells & {action.get('spell_id'), action.get('reporting_root_spell_id')}:
                    return True
                action = by_token.get(action.get('parent_token'))
            return False

        states = set().union(*(global_damage_state_identities(actor) for actor in after.values()))
        for row in result:
            if uses_global_damage_scenario(row, states):
                failures.append({'天赋': entry, '技能': row['token'], '问题': '全局状态仍作为下表伤害条件'})
            for formula in row['product'].get('formula_components') or []:
                for source in formula.get('runtime_factor_sources') or []:
                    if ((source.get('source_spell_id'), source.get('effect_index')) in known_globals
                            and abs(source.get('factor', 1) - 1) > 1e-12):
                        failures.append({'天赋': entry, '技能': row['token'], '问题': '全局分量仍参与数值', '证据': source})
            if entry in global_catalog:
                local_skills = {sid for effect in effects if effect.get('trait_entry_id') == entry
                                for binding in effect.get('local_skill_bindings') or []
                                for sid in binding.get('skill_spell_ids') or []}
                owner_spells = {catalog[entry]['spell_id'], *(p['spell_id'] for p in global_catalog[entry]['global_components'])}
                owner_metadata = [_reviewed_global_fact_metadata(base[100], spell) for spell in owner_spells]
                local_skills.update(sid for metadata in owner_metadata
                                    for binding in metadata.get('local_skill_bindings') or []
                                    for sid in binding.get('skill_spell_ids') or [])
                skills = {row['spell_id'], *(p['spell_id'] for p in row.get('components') or [])}
                local_sources = {(s.get('source_spell_id'), s.get('effect_index'))
                                 for f in row['product'].get('formula_components') or []
                                 for s in f.get('runtime_factor_sources') or []
                                 if (s.get('source_spell_id'), s.get('effect_index')) not in known_globals}
                target_sources = {(f['source_spell_id'], f['effect_index'] + 1)
                                  for action in after[100].get('actions') or []
                                  if action.get('spell_id') in skills
                                  for f in action.get('selected_trait_effects') or []
                                  if f.get('trait_entry_id') == entry}
                target_sources.update(source for source in local_sources if source[0] in owner_spells)
                if not (skills & local_skills or local_sources & target_sources
                        or owned_damage(row) or local_parameter_witness(
                            row, entry, catalog[entry], global_catalog[entry], after, before)):
                    failures.append({'天赋': entry, '技能': row['token'], '问题': '全局天赋主条件缺少局部或解锁证据'})
            for link in (row.get('variant') or {}).get('related_talents') or []:
                for proof in link.get('evidence') or []:
                    if (proof.get('source_spell_id'), proof.get('effect_index')) in known_globals:
                        failures.append({'天赋': entry, '技能': row['token'], '问题': '前置关联引用全局分量', '证据': proof})
        if result:
            examples.append({'天赋': entry, '名称': tr.name, '行数': len(result),
                             '全局目录': entry in global_catalog, '技能示例': [r['token'] for r in result[:3]]})
            write(output / f'talent-{entry}-product.json', product)
        configs.append({'天赋': entry, '状态': '精确差分', '展示行数': len(result)})
    report = {'专精': key, '探测配置': len(records), '探测节点': sorted(r['entry'] for r in records), '统计': dict(statistics),
              '失败': failures, '有差分的天赋': examples, '配置': configs,
              '未解析技能': list(unresolved.values()),
              '静态全局目录数': len(static), '通过所有权门禁数': len(accepted)}
    write(output / '检查结果.json', report)
    print(key, dict(statistics), '失败', len(failures), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('binary', 'base', 'single', 'output'):
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--spec', action='append')
    parser.add_argument('--allow-partial', action='store_true', help='仅供中途诊断；结果明确保留未完成计数')
    parser.add_argument('--resume', action='store_true', help='复用同版本检查器、二进制及相同完整节点集合的专精报告')
    parser.add_argument('--workers', type=int, default=1, choices=range(1, 5), help='以独立进程检查专精，避免共享投影缓存')
    parser.add_argument('--raw-catalog', type=Path, help='对每个单天赋配置执行全法术独立完整性复核')
    parser.add_argument('--require-stability', action='store_true', help='要求完整配置已由新进程重复核对')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    doc = read(args.base / 'global-scope-catalog.json')
    args.identity = {k: doc[k] for k in ('simc_revision', 'game_build')}
    digest = hashlib.sha256(args.binary.read_bytes()).hexdigest()
    summary = read(args.single / 'summary.json')
    if args.require_stability and summary.get('逐配置新进程稳定性核对') is not True:
        raise ValueError('完整验收需要启用逐配置新进程稳定性核对，不能复用未经重复实算的结果')
    if not args.allow_partial and len(summary['结果']) != summary['计划数']:
        raise ValueError('完整单天赋目录尚未完成，不得生成全量通过结论')
    if summary['二进制_SHA256'] != digest:
        raise ValueError('逐天赋实算与本次二进制身份不一致')
    manifest = args.output / 'manifest.json'
    identity = {**args.identity, '二进制_SHA256': digest,
                '检查器_SHA256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                '逐配置新进程稳定性核对': summary.get('逐配置新进程稳定性核对') is True}
    dependencies = [Path(__file__).with_name('audit_simc_scope_completeness.py')]
    dependencies.extend(ROOT / 'botend/services' / name for name in (
        'simc_skill_damage.py', 'simc_skill_state_projection.py', 'simc_global_display.py', 'simc_skill_talent_links.py'))
    identity['检查依赖_SHA256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dependencies}
    if args.raw_catalog:
        identity['原始DBC_SHA256'] = hashlib.sha256(args.raw_catalog.read_bytes()).hexdigest()
    if manifest.exists() and read(manifest) != identity:
        raise ValueError('不得复用不同二进制或版本的参考导出')
    write(manifest, identity)
    catalog = {r['trait_entry_id']: r for r in doc['talent_catalog']}
    globals_ = {r['trait_entry_id']: r for r in doc['talents']}
    # 独立核对未限定技能的公共结果光环，包含 family=0，不能用导出器自身的分类结论当预期。
    expected = {(t['spell_id'], e['index']) for t in catalog.values() for e in t['effects']
                if e['type'] in (6, 35, 65, 119, 128, 129, 143, 174, 202) and e['subtype'] in (52, 57, 71, 79, 163, 168, 193, 270, 276, 290, 303, 306, 308, 319, 342, 343, 344, 380, 381, 429, 501, 530, 531)
                and not any(e['flags']) and e['value']}
    missing = expected - physical_parts(globals_.values())
    if missing:
        raise ValueError('公共光环分量未进入全局目录：' + str(sorted(missing)))
    records = [r for r in summary['结果'] if r['status'] == '通过']
    specs = sorted({r['class'] + '_' + r['spec'] for r in records})
    reports = []
    pending = []
    for key in specs:
        if not args.spec or key in args.spec:
            current = [r for r in records if r['class'] + '_' + r['spec'] == key]
            saved = args.output / key / '检查结果.json'
            cached = read(saved) if args.resume and saved.exists() else None
            if cached and cached.get('探测节点') == sorted(r['entry'] for r in current):
                reports.append(cached)
                print(key, '复用相同节点集合的已验证报告', flush=True)
            else:
                pending.append((key, current))
    if args.workers == 1:
        for key, current in pending:
            reports.append(audit_spec(args, key, current, catalog, globals_))
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(audit_spec, args, key, current, catalog, globals_) for key, current in pending]
            for future in concurrent.futures.as_completed(futures):
                reports.append(future.result())
    reports.sort(key=lambda row: row['专精'])
    failure_count = sum(len(r['失败']) for r in reports)
    write(args.output / '检查结果.json', {'身份': identity, '专精数': len(reports), '失败数': failure_count,
        '原始配置计划数': summary['计划数'], '原始配置完成数': len(summary['结果']),
        '导出状态统计': dict(Counter(r['status'] for r in summary['结果'])),
        '独立公共光环分量数': len(expected),
        '未通过原始配置': [r for r in summary['结果'] if r['status'] != '通过'], '结果': reports})
    if failure_count and not args.allow_partial:
        raise ValueError(f'仍有 {failure_count} 项作用域异常，详见输出报告。')


if __name__ == '__main__':
    main()
