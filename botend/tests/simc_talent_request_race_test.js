const assert = require('node:assert/strict');
const vm = require('node:vm');
const {test} = require('node:test');
const {JSDOM} = require('jsdom');
const {readSource} = require('./vm_harness');
const tick = () => new Promise(resolve => setImmediate(resolve));
const response = (data, success = true) => ({ok:success, json:async()=>({success,data,error:success ? undefined : 'candidate unavailable'})});
async function harness(t) {
    const options = '<option value="mage_fire">Fire</option><option value="rogue_outlaw">Outlaw</option>';
    const dom = new JSDOM(`<div id="simc-talent-string-list"></div><select id="simc-talent-string-spec-filter">${options}</select><select id="simc-sim-spec">${options}</select><input type="radio" name="simc-sim-player-source" value="specified_spec" checked><select id="simc-sim-talent-string"></select><select id="simc-sim-apl-list"></select><select id="simc-sim-profile-select"><option value="default">Default</option></select><button id="simc-sim-submit-btn" disabled></button>`, {runScripts:'outside-only',url:'https://example.test/'});
    t.after(()=>dom.window.close());
    await new Promise(resolve=>dom.window.addEventListener('load',resolve,{once:true}));
    const w = dom.window, requests = [], messages = [];
    w.document.cookie = 'csrftoken=test-csrf';
    w.fetch = (url, options={}) => new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
    vm.runInContext(readSource('static/dashboard/js/main.js'),dom.getInternalVMContext());
    // Only unrelated presentation/catalog dependencies are replaced; the source,
    // APL, profile list, talent loaders and history hydration remain production code.
    for (const name of ['applyImplicitSimcRaidBuffDefaults','renderSimcInstantPlayerDetail','renderSimcComparisonCandidates','stopSimcCandidateComparisonPolling','syncSimcRaidBuffSummary','updateSimcHomeMode','syncSimcFightPresetFromInputs']) w[name] = ()=>{};
    for (const name of ['loadSimcBackendOptions','loadSimcRaidBuffOptions','loadSimcExtraOptions','loadSimcConsumableOptions','loadSimcSpecOptions']) w[name] = async()=>{};
    w.showMessage = (message,kind)=>messages.push({message,kind});
    const el = id => w.document.getElementById(id);
    const spec = value => {el('simc-sim-spec').value=value;vm.runInContext(`simcResolvedCanonicalSpec = ${JSON.stringify(value)}`,dom.getInternalVMContext());};
    return {w,requests,messages,el,spec};
}
for (const kind of ['management','candidate']) {
    test(`${kind}: current failure survives late old success`,async t=>{
        const h=await harness(t), candidate=kind==='candidate';
        const start = spec => {h.spec(spec);h.el('simc-talent-string-spec-filter').value=spec;return candidate ? h.w.loadSimcTalentStringCandidates(spec) : h.w.loadSimcTalentStrings();};
        const old=start('mage_fire'), fresh=start('rogue_outlaw');
        h.requests[1].reject(new Error('NEW FAILURE'));await fresh;await tick();
        h.requests[0].resolve(response([{id:1,name:'FIRE'}]));await old;await tick();
        assert.match(h.el(candidate ? 'simc-sim-talent-string' : 'simc-talent-string-list').textContent,/NEW FAILURE/);
    });
    for (const staleFailure of [false,true]) test(`${kind}: reverse completion / stale ${staleFailure ? 'failure' : 'success'} cannot overwrite current`, async t=>{
        const h=await harness(t), candidate=kind==='candidate';
        const start = spec => {h.spec(spec);h.el('simc-talent-string-spec-filter').value=spec;return candidate ? h.w.loadSimcTalentStringCandidates(spec) : h.w.loadSimcTalentStrings();};
        const old=start('mage_fire'), fresh=start('rogue_outlaw');
        h.requests[1].resolve(response([{id:2,name:'OUTLAW'}]));await fresh;await tick();
        if(staleFailure) h.requests[0].reject(new Error('OLD FAILURE')); else h.requests[0].resolve(response([{id:1,name:'FIRE'}]));
        await old;await tick();
        const text=h.el(candidate ? 'simc-sim-talent-string' : 'simc-talent-string-list').textContent;
        assert.match(text,/OUTLAW/);assert.doesNotMatch(text,/FIRE|OLD FAILURE/);
        assert.equal(h.requests[0].options.signal?.aborted,true);
    });
}
test('candidate loading, current error and clear are visible and fenced',async t=>{
    const h=await harness(t);h.spec('mage_fire');
    const pending=h.w.loadSimcTalentStringCandidates('mage_fire');
    assert.equal(h.el('simc-sim-talent-string').disabled,true);
    assert.match(h.el('simc-sim-talent-string').textContent,/加载/);
    h.requests[0].resolve(response([],false));await pending;
    assert.match(h.el('simc-sim-talent-string').textContent,/candidate unavailable/);
    assert.equal(h.el('simc-sim-talent-string').disabled,true);
    const next=h.w.loadSimcTalentStringCandidates('mage_fire',{force:true});
    h.w.clearSimcResolvedResources();
    h.requests[1].resolve(response([{id:1,name:'LATE'}]));await next;
    assert.doesNotMatch(h.el('simc-sim-talent-string').textContent,/LATE/);
    assert.equal(h.el('simc-sim-talent-string').disabled,true);
    assert.equal(h.requests[1].options.signal.aborted,true);
});
async function finishSource(h, label = 'CURRENT') {
    await tick();
    const apl = h.requests.filter(r=>r.url.startsWith('/api/simc-apl-candidates/')).at(-1);
    apl.resolve({ok:true,json:async()=>({success:true,default_template_id:3,data:[{id:5,name:'APL',is_default:true}]})});
    await tick();
    const candidate = h.requests.filter(r=>r.url.startsWith('/api/simc-talent-string-candidates/')).at(-1);
    h.requests.filter(r=>r.url==='/api/simc-profile/').at(-1).resolve(response([]));
    await tick();
    assert.equal(h.el('simc-sim-submit-btn').disabled,true,'history lock remains until candidates succeed');
    candidate.resolve(response([{id:8,name:label}]));
    await tick();
}
function startHistory(h, form, taskId = 42) {
    return h.w.hydrateSimcRerunDependencies(form, taskId, h.w.applySimcRerunSelectionShell(form, taskId));
}
test('history superseded by source recovers submit; late hydration cannot refill',async t=>{
    const h=await harness(t);
    const old=startHistory(h,{spec:'mage_fire',talent_string_id:7});
    await tick();
    const oldApl=h.requests[0];
    h.el('simc-sim-spec').value='rogue_outlaw';
    const current=h.w.resolveSimcPlayerSource();
    await finishSource(h);await current;
    assert.equal(h.el('simc-sim-submit-btn').disabled,false);
    h.el('simc-sim-talent-string').value='8';
    oldApl.resolve({ok:true,json:async()=>({success:true,default_template_id:3,data:[{id:5,is_default:true}]})});
    await old;
    assert.equal(h.el('simc-sim-talent-string').value,'8');
    assert.equal(h.el('simc-sim-submit-btn').disabled,false);
});
test('late old hydration cannot unlock a newer history lock',async t=>{
    const h=await harness(t);
    const old=startHistory(h,{spec:'mage_fire',talent_string_id:7});
    await tick();
    h.requests[0].resolve({ok:true,json:async()=>({success:true,default_template_id:3,data:[{id:5,is_default:true}]})});
    await tick();
    const oldCandidate=h.requests.find(r=>r.url.startsWith('/api/simc-talent-string-candidates/'));
    h.requests.find(r=>r.url==='/api/simc-profile/').resolve(response([]));
    await tick();
    h.el('simc-sim-spec').value='rogue_outlaw';
    const newer=startHistory(h,{spec:'rogue_outlaw',talent_string_id:8},43);
    await tick();
    oldCandidate.resolve(response([{id:7,name:'OLD'}]));
    await old;
    assert.equal(h.el('simc-sim-submit-btn').disabled,true);
    assert.doesNotMatch(h.el('simc-sim-talent-string').textContent,/OLD/);
    await finishSource(h);await newer;
    assert.equal(h.el('simc-sim-submit-btn').disabled,false);
    assert.equal(h.el('simc-sim-talent-string').value,'8');
});
test('source recovery never unlocks an in-flight submission',async t=>{
    const h=await harness(t);
    const old=startHistory(h,{spec:'mage_fire',talent_string_id:7});
    await tick();const oldApl=h.requests[0];
    h.w.requireSimcRunReferences=()=>{};
    let finishSubmission;
    h.w.createSimcSimulationTask=()=>new Promise(resolve=>{finishSubmission=resolve;});
    const submission=h.w.submitSimcHomeCreation();
    h.el('simc-sim-spec').value='rogue_outlaw';
    const current=h.w.resolveSimcPlayerSource();
    await finishSource(h);await current;
    assert.equal(h.el('simc-sim-submit-btn').disabled,true);
    oldApl.resolve({ok:true,json:async()=>({success:true,default_template_id:3,data:[{id:5,is_default:true}]})});
    await old;
    assert.equal(h.el('simc-sim-submit-btn').disabled,true);
    finishSubmission();await submission;
    assert.equal(h.el('simc-sim-submit-btn').disabled,false);
});
for(const early of [false,true,'error']) test(`history reuses ${early==='error'?'failed':early?'completed':'pending'} candidate load and preserves selection/error`,async t=>{
    const h=await harness(t);
    const hydration=startHistory(h,{spec:'mage_fire',talent_string_id:7,apl_id:5});
    const outcome=hydration.then(()=>null,error=>error);
    await tick();h.requests.find(r=>r.url.startsWith('/api/simc-apl-candidates/')).resolve({ok:true,json:async()=>({success:true,default_template_id:3,data:[{id:5,name:'APL',is_default:true}]})});
    await tick();
    const candidate=h.requests.find(r=>r.url.startsWith('/api/simc-talent-string-candidates/'));
    const profile=h.requests.find(r=>r.url==='/api/simc-profile/');
    assert.ok(profile,'profile request must start without waiting for talents');
    if(early) {candidate.resolve(response([{id:7,name:'HISTORY'}],early!=='error'));await tick();}
    profile.resolve(response([]));await tick();
    const talents=h.requests.filter(r=>r.url.startsWith('/api/simc-talent-string-candidates/'));
    assert.equal(talents.length,1,'one candidate HTTP request through actual source/APL/history chain');
    if(!early) candidate.resolve(response([{id:7,name:'HISTORY'}]));
    const error=await outcome;await tick();
    if(early==='error') {
        assert.match(error.message,/历史天赋字符串加载失败/);
        assert.match(h.el('simc-sim-talent-string').textContent,/candidate unavailable/);
        assert.equal(h.el('simc-sim-talent-string').disabled,true);
        assert.equal(h.el('simc-sim-submit-btn').disabled,true);
        assert.equal(h.messages.some(item=>item.kind==='success'),false);
        const retry=h.w.resolveSimcPlayerSource();
        await finishSource(h,'RETRY');await retry;
        assert.equal(h.el('simc-sim-submit-btn').disabled,false,'successful source retry releases failed history lock');
        assert.match(h.el('simc-sim-talent-string').textContent,/RETRY/);
        return;
    }
    assert.equal(error,null);
    assert.equal(h.el('simc-sim-talent-string').value,'7');
    assert.equal(h.el('simc-sim-submit-btn').disabled,false);
});
