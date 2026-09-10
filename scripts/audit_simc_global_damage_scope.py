"""分别复核处理链路一致性与独立作用域预期，二者均不冒充完整性证明。"""
import argparse
import json
import re
from collections import Counter
from pathlib import Path
from simc_scope_evidence import verify_independent_expectations


def audit_directory(directory):
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    summary = json.loads((directory / 'summary.json').read_text(encoding='utf-8'))
    rows, errors = [], []
    for spec in manifest['专精范围']:
        result = next(row for row in summary if f"{row['class']}_{row['spec']}" == spec)
        if result.get('status') == '导出失败':
            rows.append({'专精': spec, '状态': '未验证', '原因': result.get('error', '')})
            continue
        expected, declared, candidate_ids = {}, set(), set()
        count = 0
        normalized_count = 0
        selected_global = set()
        catalog_path = directory / 'global-scope-catalog.json'
        catalog = {row['trait_entry_id']: row for row in json.loads(catalog_path.read_text(encoding='utf-8'))['talents']} if catalog_path.exists() else {}
        for health in (100, 34):
            payload = json.loads((directory / f'{spec}-{health}.json').read_text(encoding='utf-8'))
            if payload['schema_version'] != 18:
                errors.append(f'{spec}：不是新版作用域协议')
            for actor in payload['actors']:
                candidates = actor.get('global_scope_candidates', [])
                selected_ids = set(actor.get('selected_trait_ids', []))
                selected_global.update(selected_ids & set(catalog))
                observed = {(row['trait_entry_id'], row['spell_id'], row['effect_index'])
                            for row in actor.get('normalized_talent_effects', []) if row.get('excluded')}
                required = {(entry, part['spell_id'], index) for entry in selected_ids & set(catalog)
                            for part in catalog[entry]['global_components'] for index in part['effect_indices']}
                if observed != required:
                    errors.append(f'{spec}/{health}：初始化排除证据缺失 {required-observed}；多出 {observed-required}')
                for row in actor.get('normalized_talent_effects', []):
                    if row.get('excluded'):
                        normalized_count += 1
                        if row.get('actual_base_value') != 0:
                            errors.append(f'{spec}/{health}：初始化恢复了增伤 {row}')
                        if row.get('damage_scaling_removed') is not True:
                            errors.append(f'{spec}/{health}：派生全局伤害系数未清除 {row}')
                count += len(candidates)
                candidate_ids.update((row['scope'], row['spell_id']) for row in candidates)
                classified = {(row['token'], row['scope'], row['spell_id'])
                              for row in candidates if row.get('scope_basis')}
                states = actor.get('global_damage_states', [])
                if classified != {(row['token'], row['scope'], row['spell_id']) for row in states}:
                    errors.append(f'{spec}/{health}：声明与完整候选目录不一致')
                declared.update((row['scope'], row['spell_id']) for row in states)
                for row in states:
                    if row.get('available'):
                        expected[(row['scope'], row['spell_id'])] = {
                            '法术ID': row['spell_id'], '名称': row['name'],
                            '状态标记': row['token'], '作用域': row['scope'],
                            '识别依据': row['scope_basis'],
                        }
                for action in actor['actions']:
                    for scenario in action.get('scenarios', []):
                        if any((row.get('scope'), row.get('spell_id')) in declared
                               for row in scenario.get('buffs', [])):
                            errors.append(f"{spec}/{health}：原生场景残留 {action['token']}")
        product = json.loads((directory / f'{spec}-product.json').read_text(encoding='utf-8'))
        displayed, damage_rows, displayed_talents = set(), 0, set()
        for actor in product['actors']:
            for effect in actor.get('global_skill_effects', []):
                if str(effect.get('effect_id', '')).startswith('dbc_global_talent:'):
                    displayed_talents.add(int(effect['effect_id'].split(':')[1]))
                if effect.get('source_type') != 'runtime_state':
                    continue
                displayed.update((row['scope'], row['spell_id'])
                                 for row in effect.get('runtime_conditions', []))
            for action in actor['actions']:
                damage_rows += 1
                if any((row.get('scope'), row.get('spell_id')) in declared
                       for row in action.get('variant', {}).get('runtime_conditions', [])):
                    errors.append(f"{spec}：产品列表残留 {action['token']}")
        if displayed != set(expected):
            errors.append(f'{spec}：全局表缺失 {set(expected) - displayed}；多出 {displayed - set(expected)}')
        if displayed_talents != selected_global:
            errors.append(f'{spec}：全局天赋表缺失 {selected_global-displayed_talents}；多出 {displayed_talents-selected_global}')
        if result.get('validation_errors'):
            errors.append(f'{spec}：导出协议校验失败')
        rows.append({'专精': spec, '状态': '已复核', '候选状态数': len(candidate_ids),
                     '两种血量扫描数': count, '伤害行数': damage_rows,
                     '初始化增伤清零证据数': normalized_count, '全局天赋节点': sorted(selected_global),
                     '全局效果': list(expected.values())})
    return {'目录': str(directory), '源码提交': manifest['源码提交'],
            '二进制摘要': manifest['二进制_SHA256'], '专精': rows, '错误': errors}


def classify_catalog(payload):
    """完整目录保留每个节点，未获得作用域证据的脚本效果单独待核对。"""
    rows = []
    damage_auras = {79, 108, 163, 168, 218, 255, 270, 271, 276, 295, 303, 344, 429, 501, 531, 537}
    for raw in payload['talent_catalog']:
        components = raw.get('global_components', [])
        text = re.sub(r'\s+', ' ', raw['description'])
        if not raw['class_id']:
            category = '非职业天赋目录'
        elif components:
            category = '含全局伤害分量'
        elif re.search(r'your damage|damage you deal|damage your (?:spells|abilities) deal|all .*damage (?:is )?increased', text, re.I) and not re.search(r'damage taken|damage you take|heal for|damage.*heals', text, re.I):
            category = '需结合原生状态核对的声明'
        elif any(e['subtype'] in damage_auras for e in raw['effects']):
            category = '未归全局的伤害或局部修正'
        elif re.search(r'\bdamage\b', text, re.I):
            category = '技能本体或其他伤害相关机制'
        else:
            category = '无直接伤害声明'
        rows.append({'节点': raw['trait_entry_id'], '职业': raw['class_id'],
                     '专精': raw['spec_ids'], '法术ID': raw['spell_id'], '名称': raw['name'],
                     '分类': category, '全局分量': components, '自身全局效果编号': raw['global_effect_indices'],
                     '说明原文': text, 'DBC效果': raw['effects']})
    return {'客户端版本': payload['game_build'], '源码提交': payload['simc_revision'],
            '完整节点数': len(rows), '分类统计': dict(Counter(row['分类'] for row in rows)),
            '职业节点统计': dict(Counter(row['职业'] for row in rows)), '节点': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, action='append', required=True, help='真实全专精审计目录，可重复')
    parser.add_argument('--output', type=Path, required=True, help='中文 JSON 审计记录')
    args = parser.parse_args()
    reports = [audit_directory(directory) for directory in args.audit]
    catalogs = [directory / 'global-scope-catalog.json' for directory in args.audit]
    result = {
        '完整目录': classify_catalog(json.loads(catalogs[0].read_text(encoding='utf-8'))) if catalogs[0].exists() else None,
        '说明': '链路一致性只检查已声明效果；独立预期能发现漏声明，但有限样本不证明全目录已筛净。未支持的专精和未遍历的组合不视为通过。',
        '完整性状态':'未证明全部全局效果已筛净；本审计不签发完整性结论',
        '审计': reports,
    }
    fixture_path=Path(__file__).resolve().parents[1]/'botend/tests/fixtures/simc_global_damage_scope.json'
    fixture=json.loads(fixture_path.read_text(encoding='utf-8'))
    independent=[]
    for path in catalogs:
        if not path.exists():
            independent.append({'目录':str(path.parent),'错误':['缺少分类目录，不能验证独立预期']})
        else:
            independent.append({'目录':str(path.parent),'样本数':len(fixture['cases']),
                '错误':verify_independent_expectations(json.loads(path.read_text(encoding='utf-8')),fixture)})
    result['独立作用域校验']=independent
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    errors = sum(len(report['错误']) for report in reports)
    scope_errors=sum(len(report['错误']) for report in independent)
    print(f'审计目录 {len(reports)} 个，链路一致性错误 {errors} 个，独立作用域预期错误 {scope_errors} 个；不代表全目录已筛净。')
    return bool(errors or scope_errors)


if __name__ == '__main__':
    raise SystemExit(main())
