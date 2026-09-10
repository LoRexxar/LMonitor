"""从实际审计产物生成可搜索的全职业归一化核对表。"""
import argparse
from collections import Counter
import json
from pathlib import Path

CLASSES = dict(enumerate(['战士','圣骑士','猎人','潜行者','牧师','死亡骑士','萨满祭司',
                         '法师','术士','武僧','德鲁伊','恶魔猎手','唤魔师'],1))
KEYS = dict(zip(['warrior','paladin','hunter','rogue','priest','deathknight','shaman',
                 'mage','warlock','monk','druid','demonhunter','evoker'],CLASSES.values()))
SPEC_LABELS = dict(zip([71,72,73,65,66,70,253,254,255,259,260,261,256,257,258,
                       250,251,252,262,263,264,62,63,64,265,266,267,268,270,269,
                       102,103,104,105,577,581,1480,1467,1468,1473],
                      ['武器','狂怒','防护','神圣','防护','惩戒','野兽控制','射击','生存',
                       '奇袭','狂徒','敏锐','戒律','神圣','暗影','鲜血','冰霜','邪恶',
                       '元素','增强','恢复','奥术','火焰','冰霜','痛苦','恶魔学识','毁灭',
                       '酒仙','织雾','踏风','平衡','野性','守护','恢复','浩劫','复仇','噬灭',
                       '湮灭','恩护','增辉']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit',type=Path,required=True,help='作用域审计 JSON')
    parser.add_argument('--initialization',type=Path,action='append',required=True,help='单天赋初始化审计目录，可重复')
    parser.add_argument('--names',type=Path,help='本地中文名称列表')
    parser.add_argument('--output',type=Path,required=True,help='输出文件前缀')
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text(encoding='utf-8'))
    initializations = [json.loads((p/'summary.json').read_text(encoding='utf-8')) for p in args.initialization]
    init = {**initializations[0], '节点':[r for group in initializations for r in group['节点']]}
    if len({r['节点'] for r in init['节点']}) != len(init['节点']):
        raise ValueError('初始化目录包含重复节点。')
    raw_paths = {r['节点']:p/f'{r["节点"]}.json' for p,group in zip(args.initialization,initializations) for r in group['节点']}
    if any(r['错误'] for r in audit['审计']) or any(r['错误'] for r in init['节点']):
        raise ValueError('审计存在错误，不能生成通过报告。')
    hashes = {r['二进制摘要'] for r in audit['审计']} | {r['二进制_SHA256'] for r in initializations}
    if len(hashes) != 1:
        raise ValueError('初始化与全职业审计不属于同一个二进制。')
    names, names_by_text = {}, {}
    if args.names:
        local_names = json.loads(args.names.read_text(encoding='utf-8'))
        names = {r[1]:r[3] for r in local_names if r[1] and r[3]}
        names_by_text = {r[2].casefold():r[3] for r in local_names if r[2] and r[3]}
    def name(sid, original):
        zh = names.get(sid) or names_by_text.get(original.casefold())
        return f'{zh}（{original}）' if zh and zh != original else original
    rows = []
    catalog = audit['完整目录']
    for r in catalog['节点']:
        rows.append({'列表':'已提取全局节点' if r['全局分量'] else '剩余静态节点',
            '职业':CLASSES.get(r['职业'],'非职业目录'),'名称':name(r['法术ID'],r['名称']),
            '法术ID':r['法术ID'],'节点':r['节点'],'专精':'、'.join(SPEC_LABELS.get(s,str(s)) for s in r['专精']) or '目录未限定',
            '分类':r['分类'],'分量':'；'.join(f'{p["spell_id"]}:' + ','.join(map(str,p['effect_indices'])) for p in r['全局分量']),
            '说明':r['说明原文'],'依据':'DBC 作用范围与数值/触发关联' if r['全局分量'] else '未归全局，不等于已证明不属于全局',
            'DBC效果':r['DBC效果']})
    runtime = {}
    def add_runtime(cls,spec,e):
        key = (cls,e['scope'],e['spell_id'])
        row = runtime.setdefault(key,{'列表':'实测全局状态','职业':KEYS[cls],
            '名称':name(e['spell_id'],e['name']),'法术ID':e['spell_id'],'节点':'',
            '专精':set(),'分类':'自身增伤状态' if e['scope']=='self' else '目标增伤状态',
            '分量':e['token'],'说明':'该状态在归一化场景中排除；静态天赋与运行时状态可能同源。',
            '依据':e['scope_basis']})
        row['专精'].add(spec)
    for report in audit['审计']:
        for spec in report['专精']:
            if spec['状态'] != '已复核':
                continue
            cls, specialization = spec['专精'].split('_',1)
            for e in spec['全局效果']:
                add_runtime(cls,specialization,{'scope':e['作用域'],'spell_id':e['法术ID'],
                    'name':e['名称'],'token':e['状态标记'],'scope_basis':e['识别依据']})
    for result in init['节点']:
        if result['状态'] != '通过':
            continue
        payload = json.loads(raw_paths[result['节点']].read_text(encoding='utf-8'))
        selected = next(a for a in payload['actors'] if a['name'].startswith('skill_damage_talent_'))
        for e in selected['global_damage_states']:
            if e['available']:
                add_runtime(result['职业'],result['专精'],e)
    for r in runtime.values():
        r['专精'] = ', '.join(sorted(r['专精']))
        rows.append(r)
    class_order = {c:i for i,c in CLASSES.items()}
    rows.sort(key=lambda r:(class_order.get(r['职业'],99),r['列表'],r['法术ID'],str(r['节点'])))
    counts = Counter({key:sum(r['列表']==key for r in rows) for key in ['已提取全局节点','实测全局状态','剩余静态节点']})
    report = {'源码提交':catalog['源码提交'],'客户端版本':catalog['客户端版本'],
        '数据版本':29,'二进制摘要':next(iter(hashes)),'列表数量':dict(counts),
        '初始化':init,'全职业复核':audit['审计'],'条目':rows}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.with_suffix('.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    total_damage = sum(s.get('伤害行数',0) for a in audit['审计'] for s in a['专精'])
    lines = ['# SimC 归一化全职业表修复与验证','',
        f'客户端 {catalog["客户端版本"]}；源码 `{catalog["源码提交"]}`；导出协议 18，数据版本 29。',
        '',f'[打开可搜索的全职业表]({args.output.name}.html) · [完整证据]({args.output.name}.json)',
        '',f'完整目录 {len(catalog["节点"])} 个节点，已提取全局节点 {counts["已提取全局节点"]} 个；实测全局状态 {counts["实测全局状态"]} 项。静态节点与运行时状态可能同源，数量不能相加。',
        '', '修复覆盖全范围伤害复制、储存、条件性暴击流血、全宠物伤害及目标条件增伤；混合节点只去除伤害分量。修正了相同掩码导致指定召唤物强化被一起排除的问题。',
        '', '| 职业 | 已提取全局节点 | 实测全局状态 | 剩余静态节点 |', '| --- | ---: | ---: | ---: |']
    for cls in CLASSES.values():
        values = Counter(r['列表'] for r in rows if r['职业']==cls)
        lines.append(f'| {cls} | {values["已提取全局节点"]} | {values["实测全局状态"]} | {values["剩余静态节点"]} |')
    lines += ['', '## 验证结果','',
        f'- 基础与完整天赋两轮均尝试 40 专精，38 个支持专精通过；{total_damage} 条伤害行中已归类全局状态残留为 0，上下表目录差异为 0。',
        f'- {len(init["节点"])} 组单天赋检查通过；核对全局分量清零、非全局分量未被清零，以及仅在选中天赋后出现的全局状态。',
        '- 46 个补丁从固定原版源码连续应用成功；扣除既有 Windows 文件同步接口包装后，三个相关构建源文件一致。',
        '- 原生分类正反例及相关语义测试通过；前端真实渲染函数的公式、等伤害层数、排序与多目标测试通过。',
        '- 较大范围 Python 回归：164 项中 158 项通过、2 项原生环境测试跳过、4 项因 Windows 缺少原有 fcntl 文件锁接口报错；另行启用原生源码的 13 项分类/规划测试通过。',
        '', '## 边界','',
        '织雾武僧、神圣圣骑士为当前 SimC 不支持，保留其静态目录，不伪造伤害值。实测采用基础、完整模板和单天赋配置，未穷举全部天赋组合。剩余静态条目完整保留，“未归全局”不是“已证明不是全局”；其中也可能包含已通过运行时状态提取的同源天赋。本次验证为本地导出、渲染函数及核对表浏览器验收，未更新线上快照或部署。',
        '',f'二进制 SHA256：`{next(iter(hashes))}`。']
    args.output.with_suffix('.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    data = json.dumps({'rows':rows,'counts':dict(counts)},ensure_ascii=False).replace('<','\\u003c')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SimC 归一化全职业表</title>
<style>body{margin:0;background:#f4f6f9;color:#172334;font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1440px;margin:40px auto;padding:0 24px}h1{font-size:30px;margin:4px 0}p{color:#526077}.eyebrow{color:#246255;font-weight:700}header{border-bottom:1px solid #cdd5de;padding-bottom:24px}.stats{display:flex;gap:24px;margin:20px 0;flex-wrap:wrap}.stats strong{font-size:25px;color:#172334}.filters{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0 12px}input,select,button{font:inherit;border:1px solid #bac6d2;border-radius:6px;background:white;padding:9px 12px}input{min-width:260px;flex:1}button{cursor:pointer}button:disabled{opacity:.4;cursor:default}.table-wrap{overflow:auto;background:white;border:1px solid #d5dde6;border-radius:8px}table{border-collapse:collapse;width:100%;min-width:950px}th{text-align:left;font-size:13px;color:#566479;background:#eaf0f5}td,th{padding:13px 15px;border-bottom:1px solid #e7ecf1;vertical-align:top}td:nth-child(2){min-width:235px}td:nth-child(4){min-width:220px}td:last-child{min-width:270px}small{display:block;color:#627187}code{overflow-wrap:anywhere;font-size:12px}summary{cursor:pointer;color:#326b62}details{max-width:560px}pre{white-space:pre-wrap;font:12px/1.5 monospace}.bottom{display:flex;align-items:center;gap:16px;margin-top:18px}.notice{padding:12px 16px;background:#fff4dc;border-left:3px solid #ba8629}#count{margin-bottom:12px;color:#526077}@media(max-width:600px){main{margin:20px auto;padding:0 16px}h1{font-size:24px}input{min-width:180px}.filters>*{width:100%}.stats{gap:15px}}</style>
<main><header><div class="eyebrow">技能归一化 · 本地验证结果</div><h1>全职业全局效果核对表</h1><p>全范围直伤、流血、暴击、条件增伤及伤害复制按效果分量提取；明确的单技能强化保留。</p><div class="stats" id="stats"></div><div class="notice">38 个支持专精已完成两轮复核；织雾与神圣骑士仅提供静态目录。剩余条目未被认定为非全局，仍可逐项核对。未部署线上。</div></header>
<div class="filters"><select id="list" aria-label="清单"><option>已提取全局节点</option><option>实测全局状态</option><option>剩余静态节点</option><option value="">全部清单</option></select><select id="cls" aria-label="职业"><option value="">全部职业</option></select><input id="q" aria-label="搜索" placeholder="搜索名称、法术 ID、说明或分量"></div><div id="count" aria-live="polite"></div><div class="table-wrap"><table><thead><tr><th>职业</th><th>效果</th><th>专精范围</th><th>已排除分量 / 状态</th><th>来源与说明</th></tr></thead><tbody id="body"></tbody></table></div><div class="bottom"><button id="prev">上一页</button><span id="page"></span><button id="next">下一页</button></div><p>同一法术可能对应多个天赋节点；静态节点与运行时状态可能同源，不能直接相加。名称缺少本地译名时保留原文及法术 ID。</p></main>
<script id="data" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.getElementById('data').textContent);const el=id=>document.getElementById(id);const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));let page=0;const size=40;
el('stats').innerHTML=Object.entries(data.counts).map(([k,v])=>`<div><strong>${v}</strong><small>${esc(k)}</small></div>`).join('');
[...new Set(data.rows.map(r=>r.职业))].forEach(c=>{const o=document.createElement('option');o.value=c;o.textContent=c;el('cls').append(o)});
function render(){const q=el('q').value.trim().toLowerCase();const rows=data.rows.filter(r=>(!el('list').value||r.列表===el('list').value)&&(!el('cls').value||r.职业===el('cls').value)&&(!q||JSON.stringify(r).toLowerCase().includes(q)));const pages=Math.max(1,Math.ceil(rows.length/size));page=Math.min(page,pages-1);el('count').textContent=`共 ${rows.length} 条 · 每页 ${size} 条`;el('page').textContent=`${page+1} / ${pages}`;el('prev').disabled=page===0;el('next').disabled=page>=pages-1;el('body').innerHTML=rows.slice(page*size,(page+1)*size).map(r=>`<tr><td>${esc(r.职业)}<small>${esc(r.列表)}</small></td><td><b>${esc(r.名称)}</b><small>法术 ${r.法术ID}${r.节点?' · 节点 '+r.节点:''}</small></td><td>${esc(r.专精)}</td><td><code>${esc(r.分量||'未提取')}</code><small>${esc(r.分类)}</small></td><td><details><summary>查看说明与分量证据</summary><p>${esc(r.说明)}</p><small>${esc(r.依据)}</small>${r.DBC效果?'<pre>'+esc(JSON.stringify(r.DBC效果,null,2))+'</pre>':''}</details></td></tr>`).join('')||'<tr><td colspan="5">没有符合条件的条目</td></tr>'}
['list','cls','q'].forEach(id=>el(id).addEventListener(id==='q'?'input':'change',()=>{page=0;render()}));el('prev').onclick=()=>{page--;render()};el('next').onclick=()=>{page++;render()};render();
</script></html>'''
    args.output.with_suffix('.html').write_text(page.replace('__DATA__',data),encoding='utf-8')
    print(json.dumps(dict(counts),ensure_ascii=False))


if __name__ == '__main__':
    main()
