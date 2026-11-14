const assert = require('node:assert/strict');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');
const {readSource, betweenMarkers, beforeMarker} = require('./vm_harness');
const plain = value => JSON.parse(JSON.stringify(value));

(async () => {
  const dom = new JSDOM('<div id="simc-talent-string-list"></div><select id="simc-talent-string-spec-filter"></select><select id="simc-sim-talent-string"></select><h2 id="simc-dialog-title"></h2><div id="simc-dialog-body"></div>');
  try {
    const row = {id:17, name:'[Icy Veins] ![icon](https://example.test/icon.png) 原始名称', display_name:'[Icy Veins] 可读名称 · 单体', spec:'warrior_fury', talent:'FROZEN_CODE', hero_talent_names:['山丘领主'], can_edit:true};
    const before = JSON.stringify(row), requests = [];
    const c = vm.createContext({window:dom.window, document:dom.window.document, console, AbortController,
      escapeHtml:value=>String(value ?? '').replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;'),
      getCSRFToken:()=>'', openSimcWorkbenchDialog:()=>{}, closeSimcWorkbenchDialog:()=>{},
      showMessage:(message,kind)=>{assert.notEqual(kind,'error',message);},
      loadSimcSpecOptions:async()=>[{value:row.spec,label:'狂怒'}],
      fetch:async(url,options={})=>{requests.push({url,...options}); return {ok:true,json:async()=>({success:true,data:[row]}),text:async()=>JSON.stringify({success:true})};}});
    const source = readSource('static/dashboard/js/main.js');
    vm.runInContext(betweenMarkers(source,'let simcTalentStringEditId = null;','function mountTalentThumbnail('), c);
    vm.runInContext(betweenMarkers(source,'let simcResolvedBaseTemplateId = 0;','function renderSimcBattlenetLoadState('), c);
    vm.runInContext(`simcResolvedCanonicalSpec = ${JSON.stringify(row.spec)};`, c);
    vm.runInContext(betweenMarkers(source,'async function loadSimcTalentStringCandidates(','function currentSimcScenario('), c);
    c.loadSimcTalentStrings();
    await new Promise(resolve=>setImmediate(resolve));
    assert.ok(dom.window.document.querySelector('#simc-talent-string-list td').textContent.includes(row.display_name));
    assert.equal(c.window.simcTalentStringRows[row.id].name,row.name);
    await c.loadSimcTalentStringCandidates(row.spec);
    const select = dom.window.document.getElementById('simc-sim-talent-string');
    assert.equal(select.options[1].value,String(row.id));
    assert.equal(select.options[1].textContent,`${row.display_name} · 山丘领主`);
    c.simcTalentStringOpenEditor(row);
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(dom.window.document.getElementById('simc-talent-string-name').value,row.name);
    await c.saveSimcTalentString();
    const saved = requests.find(item=>item.method==='PUT');
    assert.deepEqual(JSON.parse(saved.body),{id:row.id,name:row.name,spec:row.spec,talent:row.talent});
    assert.equal(JSON.stringify(row),before);
    delete row.display_name;
    await c.loadSimcTalentStringCandidates(row.spec, {force:true});
    assert.equal(select.options[1].textContent,`${row.name} · 山丘领主`);
    console.log('PASS talent list/picker use display label; editor PUT retains original name/code/id; old payload fallback');
  } finally {dom.window.close();}

  const dom2 = new JSDOM('<div id="card"><input name="spec_key" value="warrior_fury"><label>APL<select name="apl_id"></select></label><label>Template<select name="template_id"></select></label><label>Profile<select name="profile_id"></select></label><div data-talent-picker></div><div data-variant-apl-editor></div></div>');
  try {
    const data={resources:{profiles:[{id:4,name:'Raw profile',canonical_spec:'warrior_fury'}],talent_strings:[{id:17,name:'Raw talent',display_name:'Readable talent',canonical_spec:'warrior_fury',hero_talent_names:[]}],apls:[],templates:[]}};
    const c=vm.createContext({window:dom2.window,document:dom2.window.document,console,fixture:data});
    vm.runInContext(beforeMarker(readSource('static/dashboard/js/simc-benchmark-dashboard.js'),"if(typeof module==='object'&&module.exports)")+"resources=fixture;globalThis.api={updateSpecResources,selectedProfiles,renderOptionGainDetails};})();",c);
    const before=JSON.stringify(data), card=dom2.window.document.getElementById('card');
    c.api.updateSpecResources(card,4,[17]);
    assert.equal(card.querySelector('[data-talent-picker] strong').textContent,'Readable talent');
    assert.equal(card.querySelector('[data-variant-apl-row] span').textContent,'Readable talent');
    assert.deepEqual(plain(c.api.selectedProfiles(card)),[{profile_id:4,talent_string_id:17,label:'Raw profile · Raw talent',is_enabled:true,display_order:0}]);
    assert.equal(JSON.stringify(data),before);
    card.querySelector('[data-talent-included]').checked=false;
    assert.deepEqual(plain(c.api.selectedProfiles(card)),[]);
    console.log('PASS Benchmark visible labels change, checked IDs and outbound raw labels remain unchanged');
    const detail={talents:{name:'Frozen original',display_name:'Frozen readable <label>'}};
    const beforeDetail=JSON.stringify(detail);
    const rendered=c.api.renderOptionGainDetails(detail,{},{});
    assert.ok(rendered.textContent.includes('Frozen readable <label>'));
    assert.equal(rendered.querySelector('label'),null);
    assert.equal(JSON.stringify(detail),beforeDetail);
  } finally {dom2.window.close();}

  const dom3 = new JSDOM('<div id="simc-wb-task-list"></div>');
  try {
    dom3.window.escapeHtml=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
    const historyRow={id:91,status:2,name:'Task',talent_name:'Frozen original',talent_display_name:'Frozen readable <label>'};
    const before=JSON.stringify(historyRow);
    const c=vm.createContext({window:dom3.window,document:dom3.window.document,console,AbortController,
      fetch:async()=>({ok:true,json:async()=>({success:true,data:[historyRow]})})});
    vm.runInContext(beforeMarker(readSource('static/dashboard/js/simc-workbench.js'),"    document.addEventListener('DOMContentLoaded', () => {")+"state.activePanel='tasks';globalThis.loadTasks=loadTasks;})();",c);
    await c.loadTasks();
    const label=dom3.window.document.querySelector('.simc-task-card__resources span:last-child');
    assert.equal(label.querySelector('em').textContent,historyRow.talent_display_name);
    assert.equal(label.title,`天赋字符串：${historyRow.talent_display_name}`);
    assert.equal(JSON.stringify(historyRow),before);
    delete historyRow.talent_display_name;
    await c.loadTasks();
    assert.equal(dom3.window.document.querySelector('.simc-task-card__resources span:last-child em').textContent,historyRow.talent_name);
    vm.runInContext(beforeMarker(readSource('static/portal/js/simc-benchmarks.js'),'  document.addEventListener("DOMContentLoaded", loadBenchmarks);')+'globalThis.renderProfileDetails=renderProfileDetails;})();',c);
    const detail={talents:{name:'Frozen original',display_name:'Frozen readable <label>'}};
    const beforeDetail=JSON.stringify(detail), rendered=c.renderProfileDetails(detail,{},{});
    assert.ok(rendered.textContent.includes('Frozen readable <label>'));
    assert.equal(rendered.querySelector('label'),null);
    assert.equal(JSON.stringify(detail),beforeDetail);
    console.log('PASS history and Dashboard/Portal Benchmark result labels are escaped read-only projections with fallback');
  } finally {dom3.window.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
