"""从实际技能伤害产物生成条件核对表，不把静态天赋或状态候选当作伤害证据。"""
import argparse
from collections import Counter
import copy
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_simc_global_damage_review import KEYS, SPEC_LABELS
from audit_simc_global_damage_initialization import SPECS


def same_damage(left, right):
    """比较伤害结果及分量，避免期望相等掩盖暴击、多目标或分量差异。"""
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_damage(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same_damage(a, b) for a, b in zip(left, right))
    return left == right


def damage(row):
    p = row['product']
    fields = ('normalized_base_damage', 'final_normalized_damage', 'noncrit_damage', 'crit_damage',
              'final_normalized_damage_by_target')
    result = {k: p.get(k) for k in fields}
    result['components'] = [{k: c.get(k) for k in (
        'token', 'spell_id', 'component', 'damage_equivalent_count', 'normalized_base_damage',
        'final_normalized_damage', 'noncrit_damage', 'crit_damage', 'crit_chance')}
        for c in row.get('components', [])]
    result['contributions'] = [{k: c.get(k) for k in (
        'noncrit_contribution_by_target', 'crit_contribution_by_target')}
        for c in p.get('formula_components', [])]
    return result


def state_key(row):
    v = row.get('variant', {})
    return (row['token'], row['spell_id'], '血量低于35%' in v.get('runtime_condition', ''),
            json.dumps(v.get('runtime_conditions', []), sort_keys=True))


def build_rows(actor, *, config, label, source, reference_actor=None, talent=None):
    actions = actor['actions']
    baseline = {(a['token'], a['spell_id'], '血量低于35%' in a['variant'].get('runtime_condition', '')): a
                for a in actions if not a['variant'].get('runtime_conditions')}
    references = {state_key(a): a for a in (reference_actor or {}).get('actions', [])}
    rows = []
    for action in actions:
        if action.get('supported') is not True or action['product'].get('final_normalized_damage', 0) <= 0:
            continue
        variant = action['variant']
        low = '血量低于35%' in variant.get('runtime_condition', '')
        conditions = variant.get('runtime_conditions', [])
        ident = (action['token'], action['spell_id'])
        before, relation = None, '基础伤害'
        if reference_actor is not None:
            before = references.get(state_key(action))
            relation = '单天赋对照' if before else '对照未提供该技能条件'
        elif conditions:
            before = baseline.get((*ident, low)) or baseline.get((*ident, False))
            relation = '状态对照'
        elif low:
            before = baseline.get((*ident, False))
            relation = '目标血量对照'
        if before and same_damage(damage(before), damage(action)):
            # 无差异状态不能进入天赋 / buff 影响表；基础技能仍保留。
            if reference_actor is not None or conditions or low:
                continue
        if conditions and before is None and reference_actor is None:
            # 缺少数值对照不能声称已证实影响。
            relation = '缺少同配置对照'
        rows.append({'职业': KEYS[actor['class']], '专精': label,
                     '配置': config, '技能': action.get('name_zh') or action.get('name') or action['token'],
                     '技能ID': action['spell_id'], '技能标记': action['token'],
                     '天赋': talent, '条件': conditions, '目标血量': 34 if low else 100,
                     '关系': relation, '前': damage(before) if before else None,
                     '后': damage(action), '公式分量': action['product'].get('formula_components', []),
                     '来源': source, '等值条件': variant.get('equivalent_runtime_conditions', []),
                     '原始条件': variant})
    return rows


def paired_products(path, catalog):
    """单天赋初始化原始结果有独立前置对照，只使用实际测试的满血条件。"""
    from types import SimpleNamespace
    from botend.services.simc_skill_damage import (
        SimcSkillDamageSnapshotService, _mark_empty_runtime_amount_components_unresolved,
        _discard_empty_runtime_amount_components, classify_global_skill_effects,
        flatten_single_talent_damage_variants, project_skill_damage_product_payload)
    raw = json.loads(path.read_text(encoding='utf-8'))
    service = SimcSkillDamageSnapshotService(SimpleNamespace(
        simc_revision=catalog['simc_revision'], game_build=catalog['game_build']), backend=SimpleNamespace())
    marked = _mark_empty_runtime_amount_components_unresolved(raw)
    service._validate_export(raw, expected_actor_names={a['name'] for a in raw['actors']})
    _discard_empty_runtime_amount_components(marked)
    service._validate_export(raw, expected_actor_names={a['name'] for a in raw['actors']})
    before = next(a for a in raw['actors'] if a['name']=='skill_damage_base')
    after = next(a for a in raw['actors'] if a['name'].startswith('skill_damage_talent_'))
    products = []
    for actor in (before, after):
        effects = classify_global_skill_effects(actor, actor, [])
        effects = [e for e in effects if not any(p.get('kind')=='crit_chance' for p in e.get('projections', []))]
        product = copy.copy(actor)
        product['global_skill_effects'] = effects
        product['actions'] = flatten_single_talent_damage_variants(actor, actor, [], global_effects=effects)
        products.append(project_skill_damage_product_payload({'actors':[product]})['actors'][0])
    return products


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, action='append', required=True)
    parser.add_argument('--initialization', type=Path, action='append', default=[])
    parser.add_argument('--names', type=Path, required=True)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    catalog = json.loads((args.audit[0]/'global-scope-catalog.json').read_text(encoding='utf-8'))
    manifests = [json.loads((d/'manifest.json').read_text(encoding='utf-8')) for d in args.audit]
    hashes = {m['二进制_SHA256'] for m in manifests}
    if len(hashes)!=1:
        raise ValueError('拒绝混用不同二进制的伤害产物。')
    spell_names = {int(m[2]):m[1].replace('\\"','"') for m in re.finditer(
        r'^\s*\{\s*"((?:\\.|[^"\\])*)"\s*,\s*(\d+),',
        (args.source/'engine/dbc/generated/sc_spell_data.inc').read_text(encoding='utf-8'), re.M)}
    local = json.loads(args.names.read_text(encoding='utf-8'))
    by_id = {r[1]:r[3] for r in local if r[1] and r[3]}
    by_name = {r[2].casefold():r[3] for r in local if r[2] and r[3]}
    def name(sid, fallback):
        en = spell_names.get(sid, fallback)
        return by_id.get(sid) or by_name.get(en.casefold()) or en
    spec_names = {value: SPEC_LABELS[key] for key, value in SPECS.items()}
    rows, coverage, missing, configurations = [], [], [], []
    for directory, manifest in zip(args.audit, manifests):
        current = json.loads((directory/'global-scope-catalog.json').read_text(encoding='utf-8'))
        if current!=catalog:
            raise ValueError('全局效果目录不一致。')
        summaries = json.loads((directory/'summary.json').read_text(encoding='utf-8'))
        for summary in summaries:
            key = summary['class']+'_'+summary['spec']
            label = spec_names[summary['spec']]
            config = summary['talents_input']
            path = directory/(key+'-product.json')
            coverage.append({'职业':KEYS[summary['class']], '专精':label, '配置':config,
                             '状态':summary['status'], '行数':summary.get('rows',0)})
            for u in summary.get('unresolved_damage', []):
                missing.append({'职业':KEYS[summary['class']], '专精':label, '配置':config, **u})
            if not path.exists():
                continue
            actor = json.loads(path.read_text(encoding='utf-8'))['actors'][0]
            if actor.get('global_damage_policy')!='exclude_before_probe':
                raise ValueError('产物未声明在探针前去除全局效果。')
            configurations.append({'职业':KEYS[summary['class']], '专精':label, '配置':config,
                                    '已选节点':actor.get('selected_trait_ids', []),
                                    '输入文件':str((directory/(key+'.simc')).resolve())})
            rows.extend(build_rows(actor, config=config, label=label, source=str(path.resolve())))
    paired_count = 0
    rejected = []
    for directory in args.initialization:
        summary = json.loads((directory/'summary.json').read_text(encoding='utf-8'))
        if summary['二进制_SHA256'] not in hashes:
            raise ValueError('单天赋结果二进制版本不同。')
        for item in summary['节点']:
            if item['状态']!='通过':
                continue
            path = directory/f'{item["节点"]}.json'
            try:
                before, after = paired_products(path, catalog)
            except ValueError as exc:
                rejected.append({'职业':KEYS[item['职业']], '专精':spec_names[item['专精']],
                                 '节点':item['节点'], '名称':item['名称'], '原因':str(exc),
                                 '来源':str(path.resolve())})
                continue
            pair_rows = build_rows(after, reference_actor=before, config='单天赋实测',
                label=spec_names[item['专精']], source=str(path.resolve()),
                talent={'节点':item['节点'], '法术ID':item['法术ID'], '名称':name(item['法术ID'], item['名称'])})
            rows.extend(pair_rows)
            paired_count += 1
            print('单天赋伤害对照', item['节点'], len(pair_rows), flush=True)
    # 缺少对照的实际正伤害仍属于技能表，但不能作为已证实的效果差分。
    unpaired = [r for r in rows if r['关系'] in ('对照未提供该技能条件', '缺少同配置对照')]
    for index, row in enumerate(rows, 1):
        row['序号'] = index
        row['技能'] = name(row['技能ID'], row['技能'])
        for condition in row['条件']:
            condition['名称'] = name(condition['spell_id'], condition['token'])
    effects = {}
    for row in rows:
        # 只有独立天赋差分能够归因到天赋，整套天赋配置不拆分归因。
        items = [('天赋', row['天赋']['节点'], row['天赋']['名称'])] if row['天赋'] and row['前'] is not None else []
        if row['关系']=='状态对照':
            # 当前独立状态探针只有一个状态；组合只作为组合条件展示。
            if len(row['条件'])==1:
                c = row['条件'][0]
                items.append(('buff / debuff', (c['scope'], c['spell_id'], c['token']), c['名称']))
        for kind, identity, text in items:
            k = (row['职业'], row['专精'], kind, identity)
            effect = effects.setdefault(k, {'职业':row['职业'], '专精':row['专精'], '类型':kind,
                                            '名称':text, '标识':identity, '伤害行':[]})
            effect['伤害行'].append(row['序号'])
    result = {'客户端版本':catalog['game_build'], '源码提交':catalog['simc_revision'],
              '二进制摘要':next(iter(hashes)), '技能伤害':rows, '伤害相关效果':list(effects.values()),
              '覆盖':coverage, '配置':configurations, '未解析伤害':missing, '缺少同条件对照':unpaired,
              '单天赋实测数':paired_count, '拒收的导出':rejected,
              '边界':'覆盖现有基础配置、上游完整天赋模板和指定单天赋实测；未穷举所有天赋与状态组合。'}
    output = args.output
    output.with_suffix('.json').write_text(json.dumps(result, ensure_ascii=False, separators=(',',':')), encoding='utf-8')
    output.with_suffix('.html').write_text(render(result), encoding='utf-8')
    counts = Counter(r['关系'] for r in rows)
    output.with_suffix('.md').write_text(
        '# 全职业技能归一化伤害条件核对\n\n'
        f'客户端 {result["客户端版本"]}；{len(rows)} 条技能伤害记录，{len(effects)} 个职业专精内伤害相关效果。\n\n'
        '只按真实技能伤害和数值差分收录，无伤害声明候选不作为表格条目。'
        '全局效果已在探针前排除；相同条件下无数值变化的天赋和状态不列为伤害影响。\n\n'
        + result['边界'] + f' 单天赋实测 {paired_count} 项。\n\n'
        '完整表格、数值、公式分量及原始证据见同名 HTML 和 JSON。\n\n'
        + '\n'.join(f'- {k}：{v}' for k,v in counts.items())+'\n', encoding='utf-8')
    print(json.dumps({'伤害行':len(rows),'相关效果':len(effects),'关系':counts,
                      '缺少对照':len(unpaired),'未解析伤害':len(missing),'拒收导出':len(rejected)},ensure_ascii=False))


def render(data):
    payload = json.dumps(data, ensure_ascii=False, separators=(',',':')).replace('<','\\u003c')
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>全职业技能归一化伤害 · 条件核对</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f7fa;color:#172437;font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1680px;margin:auto;padding:34px 30px}h1{font-size:30px;margin:6px 0}h2{font-size:18px}p{margin:8px 0;color:#536277}.eyebrow{font-size:13px;color:#226254}.notice{background:#fff3d8;padding:13px 17px;border-left:4px solid #c38b1a;margin:20px 0}.stats{display:flex;gap:15px;margin:22px 0}.stat{background:white;border:1px solid #dce2e8;border-radius:10px;padding:14px 22px;flex:1}.stat strong{display:block;font-size:26px}.filters{display:flex;gap:12px;flex-wrap:wrap;margin:18px 0}select,input,button{font:inherit;padding:8px 12px;border:1px solid #cdd5df;border-radius:6px;background:white}input{flex:1;min-width:240px}button{cursor:pointer}table{width:100%;border-collapse:collapse;background:white;font-size:13px}th,td{border-bottom:1px solid #e3e7ed;padding:12px 13px;text-align:left;vertical-align:top}th{background:#eaf0f5;white-space:nowrap}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}small{display:block;color:#68788c}a{color:#1b665d}details{cursor:pointer}pre{white-space:pre-wrap;word-break:break-word;font-size:12px;max-width:700px}#table{overflow:auto;border:1px solid #dce2e8;border-radius:8px}.pager{display:flex;align-items:center;gap:14px;margin:18px 0}.tag{font-size:12px;color:#236457}.positive{color:#116350}@media(max-width:700px){main{padding:18px 12px}.stats{flex-wrap:wrap}.stat{min-width:40%}h1{font-size:24px}}</style>
<main><div class="eyebrow">SIMC · 已去除全局效果 · 实测结果</div><h1>全职业技能归一化伤害</h1>
<p>从技能出发，查看天赋、buff、目标血量及目标数量改变后的伤害。天赋和 buff 索引只收录已有伤害差分的条目。</p>
<div class="notice">当前是已测试配置的伤害表，尚未完成所有天赋及组合。基础配置与整套天赋模板之间的变化不能归因给单个天赋。未取得伤害结果的项目在“覆盖与缺项”中显示。</div>
<div class="stats" id="stats"></div><div class="filters">
<select aria-label="视图" id="view"><option value="damage">技能伤害表</option><option value="effects">伤害相关天赋与 buff</option><option value="coverage">覆盖与缺项</option></select>
<select aria-label="职业" id="cls"><option value="">全部职业</option></select><select aria-label="专精" id="spec"><option value="">全部专精</option></select>
<select aria-label="配置" id="config"><option value="">全部配置</option></select>
<select aria-label="目标数" id="targets"><option>1</option><option>2</option><option>5</option><option>10</option><option>20</option></select>
<input aria-label="搜索" id="query" placeholder="搜索技能、天赋、buff、法术 ID"><button id="reset">重置</button></div>
<p id="caption"></p><div id="table"></div><div class="pager"><button id="prev">上一页</button><span id="page"></span><button id="next">下一页</button></div>
<p>归一化口径：攻击强度 / 法术强度以 100 为基准；基础暴击率 20%，具体分量采用实测暴击率。期望伤害 = 未暴击贡献 + 暴击贡献。多目标列为对应目标数的合计伤害。</p>
<p id="version"></p></main><script id="data" type="application/json">''' + payload + '''</script><script>
const D=JSON.parse(document.querySelector('#data').textContent),$=s=>document.querySelector(s),esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=x=>x==null?'—':Number(x).toLocaleString('zh-CN',{maximumFractionDigits:4}), sel=id=>$(id).value;let page=0,matched=[],focus=null;
for(const [id,key]of[['#cls','职业'],['#spec','专精'],['#config','配置']])for(const v of [...new Set(D.技能伤害.map(r=>r[key]))].sort())$(id).add(new Option(v,v));
const supported=new Set(D.覆盖.filter(r=>r.行数>0).map(r=>r.职业+r.专精));
$('#stats').innerHTML=[['已生成专精',supported.size+' / 40'],['实测伤害行',D.技能伤害.length],['伤害相关效果',D.伤害相关效果.length],['单天赋实测',D.单天赋实测数]].map(([a,b])=>`<div class="stat">${a}<strong>${b}</strong></div>`).join('');
$('#version').textContent=`客户端 ${D.客户端版本} · 源码 ${D.源码提交.slice(0,12)} · ${D.边界}`;
function condition(r){return [r.天赋?.名称,...r.条件.map(c=>`${c.名称}${c.stacks?' '+c.stacks+' 层':''}${c.min_stacks?' '+c.min_stacks+'–'+c.max_stacks+' 层':''}`),r.目标血量===34?'目标血量 34%':''].filter(Boolean).join('；')||'无额外条件（目标血量 100%）'}
function value(p){return p?.final_normalized_damage_by_target?.[sel('#targets')]}
function detail(r){return `<details><summary>伤害分量与证据</summary><pre>${esc(JSON.stringify({'对照':r.前,'当前':r.后,'公式分量':r.公式分量,'原始条件':r.原始条件,'来源':r.来源},null,2))}</pre></details>`}
function render(){let view=sel('#view'),q=sel('#query').trim().toLowerCase();matched=(view==='effects'?D.伤害相关效果:view==='coverage'?D.覆盖:D.技能伤害).filter(r=>(!sel('#cls')||r.职业===sel('#cls'))&&(!sel('#spec')||r.专精===sel('#spec'))&&(view!=='damage'||!sel('#config')||r.配置===sel('#config'))&&(!focus||view!=='damage'||focus.has(r.序号))&&(!q||JSON.stringify(r).toLowerCase().includes(q)));let pages=Math.max(1,Math.ceil(matched.length/50));page=Math.min(page,pages-1);const rs=matched.slice(page*50,page*50+50);$('#caption').textContent=`共 ${matched.length} 条${focus?' · 当前仅显示所选效果对应的技能':''}`;$('#page').textContent=`${page+1} / ${pages}`;$('#prev').disabled=!page;$('#next').disabled=page===pages-1;
if(view==='damage')$('#table').innerHTML=`<table><thead><tr><th>职业 / 专精</th><th>技能</th><th>配置与条件</th><th class="num">对照期望</th><th class="num">当前期望（${sel('#targets')} 目标）</th><th class="num">变化</th><th>核对</th></tr></thead><tbody>${rs.map(r=>{let a=value(r.前),b=value(r.后);return `<tr><td>${esc(r.职业)}<small>${esc(r.专精)}</small></td><td>${esc(r.技能)}<small>${r.技能ID} · ${esc(r.技能标记)}</small></td><td>${esc(condition(r))}<small>${esc(r.配置)} · ${esc(r.关系)}</small></td><td class="num">${fmt(a)}</td><td class="num"><b>${fmt(b)}</b></td><td class="num positive">${a!=null?fmt(b-a):'—'}<small>${a?fmt((b/a-1)*100)+'%':''}</small></td><td>${detail(r)}</td></tr>`}).join('')}</tbody></table>`;
else if(view==='effects')$('#table').innerHTML=`<table><thead><tr><th>职业 / 专精</th><th>天赋或 buff</th><th>类型</th><th>伤害证据</th></tr></thead><tbody>${rs.map(r=>`<tr><td>${esc(r.职业)} / ${esc(r.专精)}</td><td>${esc(r.名称)}<small>${esc(JSON.stringify(r.标识))}</small></td><td>${r.类型}</td><td><button data-rows="${r.伤害行.join(',')}">查看 ${r.伤害行.length} 条技能伤害差分</button></td></tr>`).join('')}</tbody></table>`;
else $('#table').innerHTML=`<p>未解析伤害记录 ${D.未解析伤害.length} 条；缺少同条件对照 ${D.缺少同条件对照.length} 条。它们不计入已证实的伤害相关效果。</p><table><thead><tr><th>职业 / 专精</th><th>配置</th><th>结果</th><th>已导出行数</th></tr></thead><tbody>${rs.map(r=>`<tr><td>${esc(r.职业)} / ${esc(r.专精)}</td><td>${esc(r.配置)}</td><td>${esc(r.状态)}</td><td>${r.行数}</td></tr>`).join('')}</tbody></table><details><summary>未解析伤害记录</summary><pre>${esc(JSON.stringify(D.未解析伤害.filter(r=>(!sel('#cls')||r.职业===sel('#cls'))&&(!sel('#spec')||r.专精===sel('#spec'))),null,2))}</pre></details><details><summary>缺少同条件对照的伤害</summary><pre>${esc(JSON.stringify(D.缺少同条件对照.filter(r=>(!sel('#cls')||r.职业===sel('#cls'))&&(!sel('#spec')||r.专精===sel('#spec'))).map(r=>({职业:r.职业,专精:r.专精,技能:r.技能,天赋:r.天赋,条件:r.条件,关系:r.关系})),null,2))}</pre></details>`;
if(view==='coverage')$('#table').insertAdjacentHTML('afterbegin',`<details open><summary>拒收的导出 ${D.拒收的导出.length} 份</summary><pre>${esc(JSON.stringify(D.拒收的导出,null,2))}</pre></details>`);
}
document.querySelector('.filters').addEventListener('change',()=>{page=0;focus=null;render()});$('#query').addEventListener('input',()=>{page=0;focus=null;render()});$('#table').addEventListener('click',e=>{const b=e.target.closest('[data-rows]');if(!b)return;focus=new Set(b.dataset.rows.split(',').map(Number));$('#view').value='damage';$('#query').value='';$('#config').value='';page=0;render()});$('#prev').onclick=()=>{page--;render()};$('#next').onclick=()=>{page++;render()};$('#reset').onclick=()=>{for(const s of ['#cls','#spec','#config','#query'])$(s).value='';focus=null;page=0;render()};render();
</script></html>'''


if __name__=='__main__':
    main()
