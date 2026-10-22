const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {JSDOM} = require('jsdom');
const {beforeMarker} = require('./vm_harness');
const root = path.resolve(__dirname, '../..');
function load(file, marker, exports, html='') {
  const dom = new JSDOM(html, {url:'https://example.test/portal/simc-benchmarks/1/'});
  try {
    const context = vm.createContext({window:dom.window, document:dom.window.document, Intl, URL, URLSearchParams, AbortController, console});
    const source = fs.readFileSync(path.join(root,file),'utf8');
    vm.runInContext(beforeMarker(source,marker)+exports+'})();',context);
    return {dom, context};
  } catch (error) {
    dom.window.close();
    throw error;
  }
}
(async()=>{
  const {dom,context:c}=load('static/portal/js/simc-benchmarks.js','  document.addEventListener("DOMContentLoaded", loadBenchmarks);',
    'globalThis.api={renderProfileDetails,renderDeferredProfileDetails,renderSpecComparison,renderOptionGain,rawReportUrlForCoordinate};');
  try {
    const coordinate={spec_key:'mage_fire',talent_key:'1:2',profile_key:'1',scenario_key:'single',labels:{spec:'Fire',hero_talent:'Sun'},candidates:[{type:'base',dps:100,raw_report_url:'https://reports.example/report.html'}],profile_detail:{identity:{name:'<img src=x onerror=alert(1)>'},talents:{build_code:'BUILD'}},simulation_detail:{},audit:{apl_label:'APL <script>bad()</script>'}};
    const report=c.api.renderDeferredProfileDetails(coordinate,'/detail');
    assert.ok(report.textContent.includes('APL <script>bad()</script>'));
    assert.equal(report.querySelectorAll('script,[onerror]').length,0);
    const link=report.querySelector('.simc-benchmark-profile-report-link');
    assert.equal(link.href,'https://reports.example/report.html');
    assert.equal(link.target,'_blank'); assert.ok(link.rel.includes('noopener'));
    assert.equal(c.api.rawReportUrlForCoordinate({candidates:[{type:'base',raw_report_url:'javascript:alert(1)'}]}),null);
    const shell={body:dom.window.document.createElement('div')};
    c.api.renderSpecComparison(shell,{results:{coordinates:[coordinate]}});
    const toggle=shell.body.querySelector('button[aria-expanded]');
    assert.equal(toggle.getAttribute('aria-expanded'),'false'); toggle.click();
    assert.equal(toggle.getAttribute('aria-expanded'),'true');
    assert.equal(shell.body.querySelector('details').open,true);
    assert.ok(shell.body.textContent.includes('APL <script>bad()</script>'));
    toggle.click();assert.equal(shell.body.querySelector('details').open,false);
    console.log('PASS portal: frozen APL, safe report URL, escaped content, expandable baseline');
  } finally {dom.window.close();}
  // Replaces the retired Playwright scenario test's data roundtrip only.
  // Browser visibility, horizontal alignment and pixel/layout checks are retired
  // requirements, not equivalent coverage supplied by jsdom.
  const editor=load('static/dashboard/js/simc-benchmark-dashboard.js',"if(typeof module==='object'&&module.exports)",
    "root=document.querySelector('[data-simc-benchmark-root]');globalThis.api={addScenario,collectPayload};",
    '<div data-simc-benchmark-root><form data-benchmark-form></form><div data-editor-scenarios></div></div>');
  try {
    editor.context.api.addScenario({key:'st',name:'ST',simulation_params:{max_time:120,additional_simc_input:'seed=41719'}});
    const input=editor.dom.window.document.querySelector('textarea[name="additional_simc_input"]');
    const params=()=>editor.context.api.collectPayload().scenarios[0].simulation_params;
    assert.equal(input.value,'seed=41719'); assert.equal(params().max_time,120);
    assert.equal(params().additional_simc_input,'seed=41719');
    input.value='seed=41720\ntarget_health=1';assert.equal(params().additional_simc_input,input.value);
    for(const blank of ['', '  \n ']){input.value=blank;assert.equal('additional_simc_input' in params(),false);}
    editor.context.api.addScenario({key:'new',name:'New'});
    assert.equal('additional_simc_input' in editor.context.api.collectPayload().scenarios[1].simulation_params,false);
    console.log('PASS scenario: real DOM editor additional-input load/edit/blank/default roundtrip');
  } finally {editor.dom.window.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
