// Real mount/DOM and the existing detail response envelope; no rendering mocks.
// NODE_PATH=/tmp/lmonitor-analysis-ui-test/node_modules node --test botend/tests/player_analysis_investigation_test.js
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const fixture=require('./player_analysis_investigation_fixture.json');
const {mount}=require('../../static/dashboard/js/player-analysis-management.js');
async function setup(change=()=>{}) {
    const row=structuredClone(fixture);change(row);
    const dom=new JSDOM('<section id="player-analyses"><div id="app"></div></section>',{url:'https://example.com/dashboard/',pretendToBeVisual:true});
    const doc=dom.window.document,calls=[];
    const app=mount(doc.querySelector('#app'),{confirm:()=>true,fetch:async(url,options)=>{
        calls.push([url,options]);
        const data=url.endsWith('/recheck/')?{state:'idle'}:url.includes('?')?{items:[row],page:1,total:1,total_pages:1}:row;
        return {ok:true,status:200,json:async()=>({status:'success',data:structuredClone(data)})};
    }});
    await app.ready;
    const click=async(selector)=>{doc.querySelector(selector).click();await app.settled();};
    await click('[data-action="open"]');
    return {row,doc,app,calls,click,close:()=>{app.destroy();dom.window.close();}};
}
test('frozen investigation shows grounds, competing explanations, references and remaining questions in drawer only',async()=>{
    const f=await setup();try{
        const view=f.doc.querySelector('[data-investigation]');assert.ok(view,'investigation must be rendered by real mount');
        assert.ok(view.querySelector('[data-investigation-hypotheses]'));
        assert.match(view.textContent,/为什么开始调查/);
        assert.match(view.textContent,/已有线索、尚未证实/);
        assert.match(view.textContent,/当前成绩.*不代表.*当场加分/);
        assert.match(view.textContent,/付款.*钥匙归属.*未知/);
        const facts=view.querySelector('[data-investigation-facts]');
        assert.match(facts.textContent,/已有本人低于当前同本/);
        assert.match(facts.textContent,/2026-10-17T00:00:00Z/);
        assert.match(facts.textContent,/2026-10-16T00:00:00Z/);
        assert.doesNotMatch(facts.textContent,/朋友互助也是可能解释/);
        const hypotheses=view.querySelectorAll('[data-investigation-hypothesis]');assert.equal(hypotheses.length,2);
        assert.match(hypotheses[0].textContent,/支持依据[\s\S]*e-core/);
        assert.match(hypotheses[0].textContent,/反证[\s\S]*e-counter/);
        assert.match(hypotheses[0].textContent,/未知[\s\S]*e-unknown/);
        assert.match(hypotheses[1].textContent,/missing-evidence.*未提供/);
        const model=view.querySelector('[data-investigation-model]');assert.match(model.textContent,/AI.*解释.*不是已验证事实/);
        assert.match(model.textContent,/朋友互助也是可能解释/);
        assert.equal(model.querySelectorAll('[data-model-hypothesis]').length,2);
        const questions=view.querySelector('[data-investigation-questions]');
        assert.equal(questions.querySelector('[data-investigation-question="q-fifth"]').dataset.identityDigest,'fifth-identity');
        assert.match(questions.textContent,/问题 1/);assert.doesNotMatch(questions.textContent,/fifth-identity/);
        assert.match(questions.textContent,/101.*102/);assert.match(questions.textContent,/预算耗尽/);
        assert.match(questions.textContent,/不会.*采集/);
        assert.doesNotMatch(f.doc.querySelector('.pa-results').textContent,/已有本人|朋友互助|旧版冻结结论/);
        assert.equal(view.querySelectorAll('script,img,iframe').length,0);
        assert.match(view.textContent,/<script>不执行<\/script>/);
        assert.match(view.textContent,/<img src=x onerror=alert\(1\)>/);
        assert.ok(view.querySelector('a[href="https://raider.io/mythic-plus-runs/season-test/101"]'));
        for(const a of view.querySelectorAll('a')) { assert.match(a.href,/^https?:/);assert.equal(a.rel,'noopener noreferrer'); }
        assert.equal(view.querySelectorAll('input,button,textarea,select').length,0,'investigation is read-only');
        assert.ok(f.calls.every(([url,opt])=>url.startsWith('/dashboard/api/player-analyses/') && opt.method==='GET'));
        f.doc.querySelector('[data-field="conclusion"]').value='人工草稿';f.app.setActive(false);f.app.setActive(true);await f.app.settled();
        assert.equal(f.doc.querySelector('[data-field="conclusion"]').value,'人工草稿');
        assert.deepEqual(f.row,fixture,'rendering must not mutate the frozen record');
    }finally{f.close();}
});
for(const value of [undefined,null,{},'invalid'])test(`old/missing investigation (${JSON.stringify(value)}) keeps original drawer`,async()=>{
    const f=await setup(row=>{row.ai.evidence_json={metrics:{investigation:value}};row.ai.result_json={};});try{
        assert.equal(f.doc.querySelector('.pa-investigation'),null);
        assert.match(f.doc.querySelector('.pa-projections').textContent,/旧版冻结结论仍保留/);
        assert.ok(f.doc.querySelector('[data-field="conclusion"]'));
        assert.equal(f.doc.querySelector('.pa-message').textContent,'');
    }finally{f.close();}
});
test('partial investigation and dangling model references remain unknown, never resolved or omitted',async()=>{
    const f=await setup(row=>{
        row.ai.evidence_json.metrics.investigation={version:1,triggered:true,hypotheses:[{id:'help',label:'协助',status:'supported',support_ids:['gone'],counter_ids:null,unknown_ids:['q-gone']}],evidence:[null,{},'bad'],questions:[null]};
        row.ai.result_json.investigation.hypotheses.push({id:'model-only',assessment:'supported',explanation:'仅模型解释',evidence_ids:['other']});
        row.ai.result_json.investigation.next_question_ids=['q-gone'];
    });try{
        const view=f.doc.querySelector('.pa-investigation');assert.ok(view);
        assert.match(view.textContent,/触发原因未提供/);assert.match(view.textContent,/停止原因未提供/);
        assert.match(view.textContent,/gone.*未提供/);assert.match(view.textContent,/other.*未提供/);
        assert.match(view.textContent,/冻结问题未提供/);
        assert.match(view.textContent,/有支持线索.*尚未证实/);
        assert.match(view.textContent,/仅模型解释/);
        assert.match(view.textContent,/不能据此判定调查已完成/);
        assert.doesNotMatch(view.textContent,/线索状态：已证实|模型判断：已证实|没有反证|没有问题/);
        assert.equal(f.doc.querySelector('.pa-message').textContent,'');
    }finally{f.close();}
});
test('model-only investigation is explicitly uncorroborated; malformed optional collections do not break editing',async()=>{
    const f=await setup(row=>{
        row.ai.evidence_json={};row.ai.result_json.investigation={summary:'仅保存了模型解释',hypotheses:[null,{id:'help',evidence_ids:{bad:true},counter_evidence_ids:[null]}],next_question_ids:'bad'};
    });try{
        const view=f.doc.querySelector('.pa-investigation');assert.ok(view);
        assert.match(view.textContent,/冻结调查依据未提供/);assert.match(view.textContent,/仅保存了模型解释/);
        assert.ok(f.doc.querySelector('[data-field="conclusion"]'));
        assert.equal(f.doc.querySelector('.pa-message').textContent,'');
    }finally{f.close();}
});
test('backend planner → frozen_ai detail contract → mounted DOM retains every hypothesis and question',async()=>{
    const {execFileSync}=require('node:child_process');
    const path=require('node:path');
    // Invoke the real pure planner and the management API's actual frozen_ai
    // projection; the backend fixture supplies only bounded validated inputs.
    // No DB migration, network request or generation is performed here.
    const ai=JSON.parse(execFileSync(process.env.PYTHON || 'python3',['-c',`
import os, json
from types import SimpleNamespace
os.environ['DJANGO_SETTINGS_MODULE']='LMonitor.settings_test_sqlite'
import django
django.setup()
from botend.tests.test_player_observation_investigation import InvestigationTests
from botend.services.player_analysis_management import frozen_ai
from botend.services.player_observation import _validate_investigation
fixture=InvestigationTests()
fixture.setUp()
inquiry=fixture.build()
result={'investigation': {'summary': '契约测试的模型候选解释，非事实确认', 'confidence': '低',
 'hypotheses': [{'id': h['id'], 'assessment': 'possible', 'evidence_ids': h['unknown_ids'][:1],
 'counter_evidence_ids': h['counter_ids'], 'explanation': '仍需区分朋友互助等可能性'} for h in inquiry['hypotheses'][:2]],
 'next_question_ids': [q['id'] for q in inquiry['questions'][:2]]}}
_validate_investigation(result['investigation'], inquiry, bounded=True)
print(json.dumps(frozen_ai(SimpleNamespace(evidence_json={'metrics': {'investigation': inquiry}},result_json=result)),ensure_ascii=False))
`],{cwd:path.resolve(__dirname,'../..'),encoding:'utf8',timeout:30000}));
    const f=await setup(row=>{row.ai=ai;});try{
        const inquiry=ai.evidence_json.metrics.investigation;
        const view=f.doc.querySelector('[data-investigation]');assert.ok(view);
        assert.equal(view.querySelectorAll('[data-investigation-hypothesis]').length,inquiry.hypotheses.length);
        assert.equal(view.querySelectorAll('[data-investigation-question]').length,inquiry.questions.length);
        assert.equal(view.querySelectorAll('[data-model-hypothesis]').length,ai.result_json.investigation.hypotheses.length);
        for(const item of ai.result_json.investigation.hypotheses) {
            assert.equal(item.evidence_ids.length,1,'real validator accepts an unknown-only reference');
            const card=view.querySelector(`[data-model-hypothesis="${item.id}"]`);
            const groups=Array.from(card.querySelectorAll('.pa-investigation-refs'));
            const support=groups.find(group=>group.firstElementChild.textContent==='模型引用的支持依据');
            const unknown=groups.find(group=>group.firstElementChild.textContent==='模型引用的未知 / 缺口');
            assert.ok(unknown,'unknown model references must have their own gap group');
            for(const id of item.evidence_ids) {
                assert.ok(unknown.textContent.includes(id));
                assert.ok(!support.textContent.includes(id),'unknown must never become support');
            }
        }
        for(const h of inquiry.hypotheses)assert.match(view.textContent,new RegExp(h.label));
        for(const e of inquiry.evidence)assert.ok(view.textContent.includes(e.text));
        assert.match(view.textContent,/模型置信度：低/);
        assert.match(view.textContent,/本人重复低于当前同本成绩上限/);
        assert.match(view.textContent,/仍需定向核查相关成员当前计分记录/);
        assert.doesNotMatch(view.textContent,/[0-9a-f]{64}|\[object Object\]/);
        assert.ok(f.calls.every(([,opt])=>opt.method==='GET'));
    }finally{f.close();}
});

test('model references are classified per frozen hypothesis; unrecognized references cannot become support',async()=>{
    const f=await setup(row=>{
        const plan=row.ai.evidence_json.metrics.investigation;
        plan.hypotheses[0].support_ids.push('missing-support','e-unknown');
        plan.evidence.push({id:'unclassified',text:'有记录但不属于该假设'});
        row.ai.result_json.investigation.hypotheses[0].evidence_ids=['e-core','e-unknown','e-counter','unclassified','missing-support','gone'];
        row.ai.result_json.investigation.hypotheses[1].evidence_ids=['e-counter','e-core','missing-evidence'];
        row.ai.result_json.investigation.hypotheses.push({id:'model-only',evidence_ids:['e-core']});
    });try{
        for(const [id,supportIDs,unknownIDs,unrecognizedIDs] of [
            ['help',['e-core'],['e-unknown'],['e-counter','unclassified','missing-support','gone']],
            ['key',['e-counter'],['missing-evidence'],['e-core']],
            ['model-only',[],[],['e-core']],
        ]) {
            const card=f.doc.querySelector(`[data-model-hypothesis="${id}"]`);
            const groups=Array.from(card.querySelectorAll('.pa-investigation-refs'));
            const group=title=>groups.find(node=>node.firstElementChild.textContent===title);
            const support=group('模型引用的支持依据');
            const unknown=group('模型引用的未知 / 缺口');
            const unrecognized=group('未识别 / 无法核验引用（不作为支持或反证）');
            assert.ok(unknown);assert.ok(unrecognized);
            assert.equal(support.querySelectorAll('li').length,supportIDs.length);
            for(const key of supportIDs)assert.ok(support.textContent.includes(key));
            for(const key of unknownIDs)assert.ok(unknown.textContent.includes(key));
            for(const key of unrecognizedIDs)assert.ok(unrecognized.textContent.includes(key));
            for(const key of [...unknownIDs,...unrecognizedIDs])assert.ok(!support.textContent.includes(key));
        }
        assert.equal(f.doc.querySelectorAll('[data-investigation-model] img, [data-investigation-model] script').length,0);
    }finally{f.close();}
});

test('top-level frozen behavior references resolve safely and legacy trigger display uses only frozen leads',async()=>{
    const f=await setup(row=>{
        const plan=row.ai.evidence_json.metrics.investigation;
        plan.trigger_reason='no_recent_signal';plan.seed_run_ids=[];plan.core_members=[];
        row.ai.evidence_json.evidence=[
            {id:'behavior:key_recovery_unknown:101',text:'跑前保护未知 <img src=x onerror=alert(1)>',run_url:'javascript:alert(1)'},
            {id:'behavior:lead:101',text:'本人已有同本较高层观测',run_url:'https://raider.io/mythic-plus-runs/season-test/101'}];
        row.ai.evidence_json.metrics.behavior_review={triggered:true,evidence:row.ai.evidence_json.evidence,witnesses:{joint_progress:[102]}};
        plan.hypotheses[0].unknown_ids.push('behavior:key_recovery_unknown:101');
        row.ai.result_json.investigation.hypotheses[0].evidence_ids=['e-core','behavior:key_recovery_unknown:101'];
    });try{
        const view=f.doc.querySelector('[data-investigation]');
        const card=view.querySelector('[data-model-hypothesis="help"]');
        assert.match(card.textContent,/跑前保护未知/);
        assert.doesNotMatch(card.textContent,/behavior:key_recovery_unknown:101 · 冻结引用未提供/);
        assert.match(card.textContent,/e-core/); // Nested legacy evidence still resolves.
        assert.match(view.textContent,/missing-evidence · 冻结引用未提供，无法核验/);
        const facts=view.querySelector('[data-investigation-facts]');
        assert.match(facts.textContent,/行为复核发现定向调查线索/);
        assert.match(facts.textContent,/起始场次：101、102/);
        assert.doesNotMatch(facts.textContent,/未触发调查|核心成员：5/);
        assert.match(facts.textContent,/兼容展示/);
        assert.equal(view.querySelectorAll('img,script').length,0);
        assert.ok([...view.querySelectorAll('a')].every(a=>/^https?:/.test(a.href)));
        assert.equal(f.row.ai.evidence_json.metrics.investigation.trigger_reason,'no_recent_signal');
        assert.deepEqual(f.row.ai.evidence_json.metrics.investigation.seed_run_ids,[]);
        assert.ok(f.calls.every(([,opt])=>opt.method==='GET'));
    }finally{f.close();}
});

test('not-triggered is distinct from unrecorded, with no claims of resolved hypotheses',async()=>{
    const f=await setup(row=>{row.ai.evidence_json.metrics.investigation={version:1,triggered:false,trigger_reason:'未达到调查触发条件',stop_reason:'not_triggered'};row.ai.result_json={};});try{
        const text=f.doc.querySelector('.pa-investigation').textContent;
        assert.match(text,/未触发定向调查/);assert.match(text,/未达到调查触发条件/);
        assert.match(text,/未保存候选解释/);assert.match(text,/未提供待核查问题.*不代表.*已解决/);
    }finally{f.close();}
});
