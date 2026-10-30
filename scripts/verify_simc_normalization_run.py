"""逐份核对本地全专精实算：上下表、物理分量、组合覆盖及五档目标数公式。"""
import argparse
from collections import Counter
import html
import json
import math
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'LMonitor.settings_test_sqlite')
import django
django.setup()
from botend.services.simc_global_display import normalize_reviewed_global_effects


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def verify(directory):
    results = []
    summaries = read(directory / 'summary.json')
    expected = set(read(directory / 'manifest.json')['专精范围'])
    identities = [s['class']+'_'+s['spec'] for s in summaries]
    if len(identities) != len(set(identities)) or set(identities) != expected:
        raise ValueError('生成配置缺失或重复，拒绝整体通过。')
    for summary in summaries:
        key = summary['class'] + '_' + summary['spec']
        row = {'专精': key, '目录': str(directory), '状态': summary['status'],
               '展示行': summary.get('rows', 0), '错误': [], '组合场景': 0,
               '未解析伤害': len(summary.get('unresolved_damage', []))}
        row['有限层数覆盖动作'] = []
        results.append(row)
        if summary.get('validation_errors'):
            row['错误'].extend(summary['validation_errors'])
        if summary['status'] != '已生成，待逐项审计':
            row['执行说明'] = summary.get('error', '')
            if 'not currently supported' not in row['执行说明'] and not summary.get('validation_errors'):
                row['错误'].append(['配置未完成，拒绝整体通过', row['执行说明']])
            continue
        product = read(directory / (key + '-product.json'))['actors'][0]
        upper = normalize_reviewed_global_effects(product, class_name=summary['class'])
        lower = product['actions']
        for health in (100, 34):
            actor = read(directory / (key + f'-{health}.json'))['actors'][0]
            normalized = {(e['spell_id'], e['effect_index']): e for e in actor['normalized_scope_effects']}
            for e in normalized.values():
                if e['actual_base_value'] != 0 or e['actual_mastery_coefficient'] != 0:
                    row['错误'].append(['全局分量未归零', e])
            declared = {(e['spell_id'], e['scope']): e for e in actor['global_damage_states']}
            for state in declared.values():
                if not state['available']:
                    continue
                if not any(state['spell_id'] in e.get('source_spell_ids', []) for e in upper):
                    row['错误'].append(['全局状态未进入上表', health, state['token']])
            for action in actor['actions']:
                coverage = action.get('state_coverage')
                if (coverage and coverage.get('stack_coverage') == 'all_pairs_and_min_max_presence'
                        and action['supported'] and not action['baseline'].get('unresolved_reason') and health == 100):
                    row['有限层数覆盖动作'].append(action['token'])
                if action['supported'] and not action['baseline'].get('unresolved_reason') and not coverage:
                    row['错误'].append(['缺少组合覆盖记录', action['token']])
                for amount in [action['baseline'], *(s['values'] for s in action['scenarios'])]:
                    for kind in ('direct', 'tick'):
                        for source in (amount.get(kind) or {}).get('runtime_factor_sources', []):
                            if ((source['source_spell_id'], source['effect_index']) in normalized
                                    and not math.isclose(source['factor'], 1, abs_tol=1e-12)):
                                row['错误'].append(['已剔除分量仍参与乘算', action['token'], source])
                for scenario in action['scenarios']:
                    conditions = scenario['buffs']
                    row['组合场景'] += len(conditions) > 1
                    if len({(b['token'], b['scope']) for b in conditions}) != len(conditions):
                        row['错误'].append(['同一状态同时出现不同层数', action['token']])
                    for b in conditions:
                        state = declared.get((b['spell_id'], b['scope']))
                        if state and not state['partial_state']:
                            row['错误'].append(['纯全局状态泄漏到下表', action['token'], b])
            for e in upper:
                for part in e.get('global_components', []):
                    if (part['spell_id'], part['effect_index']) not in normalized:
                        row['错误'].append(['上表效果未实际剔除', part])
        for action in lower:
            for count in ('1', '2', '5', '10', '20'):
                formulas = action['product']['formula_components']
                calculated = sum(f['noncrit_contribution_by_target'][count] + f['crit_contribution_by_target'][count] for f in formulas)
                actual = action['product']['final_normalized_damage_by_target'][count]
                if not math.isclose(calculated, actual, rel_tol=1e-9, abs_tol=1e-8):
                    row['错误'].append(['公式重算不一致', action['token'], count])
        esc = lambda x: html.escape(str(x))
        def table(headers, rows):
            return '<table><thead><tr>'+''.join('<th>'+esc(v)+'</th>' for v in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in values)+'</tr>' for values in rows)+'</tbody></table>'
        content = '<!doctype html><meta charset="utf-8"><style>body{font:14px system-ui;margin:24px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ccc;padding:6px}th{position:sticky;top:0;background:#eee}</style>'
        content += '<h1>'+esc(key)+' 技能归一化表</h1><p>上表为排除的公共效果；下表为本次参考配置中原生快照实际计算的条件伤害。</p>'
        if row['有限层数覆盖动作']:
            content += '<p>层数覆盖说明：部分动作覆盖全部双状态层数，以及多状态集合同时首层、同时满层；其余高阶层数组合未穷举。</p>'
        content += '<h2>全局效果</h2>'+table(['来源', '法术', '物理分量', '效果数值'], ((e.get('display_name') or e.get('source_name'), e.get('source_spell_ids'), e.get('global_components'), e.get('effect_details')) for e in upper))
        content += '<h2>技能与组合条件</h2>'+table(['技能', '法术', '条件', '单目标', '双目标', '五目标', '十目标', '二十目标'], ((a.get('display_name') or a['token'], a['spell_id'], a.get('variant', {}).get('runtime_conditions') or a.get('variant', {}).get('runtime_condition') or '基础', *(round(a['product']['final_normalized_damage_by_target'][str(n)], 6) for n in (1,2,5,10,20))) for a in lower))
        (directory / (key + '-table.html')).write_text(content, encoding='utf-8')
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    identities = {(m['二进制_SHA256'], m['源码提交'], m['客户端版本'])
                  for m in (read(d/'manifest.json') for d in args.run)}
    if len(identities) != 1:
        raise ValueError('输入目录的二进制、源码或客户端版本不一致。')
    results = [r for directory in args.run for r in verify(directory)]
    report = {'配置数': len(results), '成功配置': sum(r['状态']=='已生成，待逐项审计' for r in results),
              '展示行': sum(r['展示行'] for r in results), '组合场景': sum(r['组合场景'] for r in results),
              '错误数': sum(len(r['错误']) for r in results), '结果': results,
              '有限层数覆盖动作数': sum(len(r['有限层数覆盖动作']) for r in results),
              '范围': '全专精基础及上游参考天赋配置；未解析伤害单独保留。组合穷举已声明维度，不能证明未建模的原生互斥或隐藏触发。'}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    body = '<!doctype html><meta charset="utf-8"><title>全专精本地验证</title><style>body{font:15px system-ui;margin:30px}table{border-collapse:collapse}td,th{padding:8px;border:1px solid #ccc}</style><h1>全专精归一化表与验证结果</h1>'
    body += '<p>'+html.escape(report['范围'])+'</p><p>'+html.escape(str({k:v for k,v in report.items() if k not in {'结果','范围'}}))+'</p>'
    body += '<table><tr><th>配置</th><th>专精</th><th>结果</th><th>展示行</th><th>组合场景</th><th>未解析伤害</th><th>错误</th></tr>'
    for r in results:
        path=Path(r['目录'])/(r['专精']+'-table.html')
        label=html.escape(r['专精'])
        if path.exists():
            label='<a href="'+html.escape(path.resolve().relative_to(args.output.parent.resolve()).as_posix())+'">'+label+'</a>'
        body += '<tr><td>'+html.escape(Path(r['目录']).name)+'</td><td>'+label+'</td>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in (r['状态'],r['展示行'],r['组合场景'],r['未解析伤害'],r['错误']))+'</tr>'
    args.output.with_suffix('.html').write_text(body+'</table>',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='结果'}, ensure_ascii=False))
    if report['错误数']: sys.exit(1)


if __name__ == '__main__':
    main()
