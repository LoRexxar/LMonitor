const {test, createDOM, mount, initDashboard, closeDOM, response, tick} = require('./player_analysis_dom_harness');
// Real mount/DOM and the existing detail response envelope; no rendering mocks.
// NODE_PATH=/tmp/lmonitor-analysis-ui-test/node_modules node --test botend/tests/player_analysis_investigation_test.js
const assert=require('node:assert/strict');
const fixture=require('./player_analysis_investigation_fixture.json');
async function setup(change=()=>{}) {
    const row=structuredClone(fixture);change(row);
    const dom=createDOM('<section id="player-analyses"><div id="app"></div></section>',{url:'https://example.com/dashboard/',pretendToBeVisual:true});
    const doc=dom.window.document,calls=[];
    const app=mount(doc.querySelector('#app'),{confirm:()=>true,fetch:async(url,options)=>{
        calls.push([url,options]);
        const data=url.endsWith('/recheck/')?{state:'idle'}:url.includes('?')?{items:[row],page:1,total:1,total_pages:1}:row;
        return response(data);
    }});
    await app.ready;
    const click=async(selector)=>{doc.querySelector(selector).click();await app.settled();};
    await click('[data-action="open"]');
    return {row,doc,app,calls,click,close:()=>{closeDOM(dom,app);}};
}
// Server fixtures come through the actual validator and projection, not a fake API success.
const {execFileSync}=require('node:child_process');
const path=require('node:path');
function backend(version=8) {
    return JSON.parse(execFileSync(process.env.PYTHON || 'python3',['-c',`
import os,json
os.environ['DJANGO_SETTINGS_MODULE']='LMonitor.settings_test_sqlite'
import django
django.setup()
from botend.tests.test_player_analysis_presentation import publication,portal
from botend.services.player_analysis_management import frozen_ai
ai=frozen_ai(publication(${version}))
print(json.dumps({'ai': ai, 'html': portal(ai['rendered_investigation'])},ensure_ascii=False))
`],{cwd:path.resolve(__dirname,'../..'),encoding:'utf8',timeout:30000}));
}
for(const version of [7,8])test(`v${version} real frozen projection → management and Portal DOM, escaped exactly once`,async()=>{
    const {ai,html}=backend(version);
    assert.equal(ai.validation_status,'valid');
    const f=await setup(row=>{row.ai=ai;row.prompt_version=version;});
    try {
        const view=f.doc.querySelector('[data-investigation]');assert.ok(view);
        assert.match(view.textContent,/A & B < C <img src=x onerror=alert\(1\)> 中文三场/);
        assert.equal(view.querySelectorAll('img,script,iframe,[onerror]').length,0);
        assert.equal(view.querySelectorAll('[data-investigation-hypothesis]').length,ai.rendered_investigation.hypotheses.length);
        for(const h of ai.rendered_investigation.hypotheses) {
            const card=view.querySelector(`[data-investigation-hypothesis="${h.id}"]`);
            for(const key of ['evidence','counter_evidence','unknown_evidence']) {
                const group=card.querySelector(`[data-evidence-kind="${key}"]`);
                assert.equal(group?.querySelectorAll('li').length || 0,h[key].length);
                for(const e of h[key])assert.ok(group.textContent.includes(e.text));
            }
        }
        for(const a of view.querySelectorAll('a'))assert.match(a.href,/^https?:\/\//);
        assert.ok(f.doc.querySelector('[data-field="conclusion"]'));
        assert.ok(f.doc.querySelector('[data-action="recheck"]'));
        assert.equal(f.doc.querySelector('.pa-results [data-investigation]'),null);
        if(version===7)assert.match(f.doc.querySelector('[data-publication-validation]').textContent,/无需.*重新生成/);
        const dom=createDOM(html);try {
            const portal=dom.window.document.querySelector('[data-player-investigation]');assert.ok(portal);
            assert.match(portal.textContent,/A & B < C <img src=x onerror=alert\(1\)> 中文三场/);
            assert.equal(portal.querySelectorAll('img,script,[onerror]').length,0);
            for(const h of ai.rendered_investigation.hypotheses)for(const e of h.unknown_evidence)assert.ok(portal.textContent.includes(e.text));
        }finally{dom.window.close();}
        assert.ok(f.calls.every(([,opt])=>opt.method==='GET'));
    }finally{f.close();}
});
const expectedLimitations=[
    '仅比较所引公开样本；未选择的假设不等于被否定。',
    '当前观测不能升级为跑前记录，本人事实不能推广为全队事实；团队通关不是独立完成的证明。未知不作为反证。',
    '钥匙归属与交易未知，不能据此确认或否定交易。',
    '所有判断均为低置信候选，不证明私人动机。',
];
for(const surface of ['Portal','management'])test(`v8 limitations remain complete paragraphs in ${surface} DOM`,async()=>{
    const {ai,html}=backend();
    assert.equal(ai.validation_status,'valid');
    let dom,f;
    try {
        let paragraphs;
        if(surface==='Portal') {
            dom=createDOM(html);
            paragraphs=[...dom.window.document.querySelectorAll('[data-player-investigation] p.player-observation__note')];
        } else {
            f=await setup(row=>{row.ai=ai;row.prompt_version=8;});
            paragraphs=[...f.doc.querySelectorAll('[data-investigation] > p.pa-muted')];
        }
        const texts=paragraphs.map(p=>p.textContent);
        for(const text of expectedLimitations)assert.ok(texts.includes(text),`missing complete limitation: ${text}`);
        assert.ok(texts.every(text=>text.length>1),'limitations must not render as single-character paragraphs');
    }finally{if(dom)dom.window.close();if(f)f.close();}
});
for(const validation of ['invalid',undefined,'missing'])test(`no raw prose fallback for ${validation}`,async()=>{
    const f=await setup(row=>{
        row.ai.validation_status=validation;
        row.ai.result_json={investigation:{summary:'RAW MUST STAY IN AUDIT'}};
        row.ai.rendered_investigation={summary:'INVALID PROJECTION'};
    });try {
        assert.equal(f.doc.querySelector('[data-investigation]'),null);
        assert.doesNotMatch(f.doc.querySelector('.pa-detail').textContent,/RAW MUST STAY|INVALID PROJECTION/);
        assert.ok(f.doc.querySelector('[data-publication-validation]'));
        assert.equal(f.doc.querySelector('[data-action="copy-ai"]'),null);
        const raw=[...f.doc.querySelectorAll('details')].find(d=>d.textContent.includes('原始 JSON'));
        raw.open=true;raw.dispatchEvent(new f.doc.defaultView.Event('toggle'));
        assert.match(raw.querySelector('pre').textContent,/RAW MUST STAY IN AUDIT/);
        assert.ok(f.doc.querySelector('[data-field="conclusion"]'));
    }finally{f.close();}
});
