const assert=require('node:assert/strict');
const vm=require('node:vm');
const {test}=require('node:test');
const {JSDOM}=require('jsdom');
const {readSource}=require('./vm_harness');
const tick=()=>new Promise(r=>setImmediate(r));
async function setup(t){
 const dom=new JSDOM(`<div id="simc-workbench"></div><section id="simc-workflow" data-simc-page="workflow" class="content-section"><div data-simc-panel="import" class="simc-workbench-panel"></div><div data-simc-panel="talent-strings" class="simc-workbench-panel"><select id="simc-talent-string-spec-filter"><option value="">All</option><option value="mage_fire">Fire</option></select><button id="simc-talent-string-refresh"></button><div id="simc-talent-string-list"></div><div id="simc-talent-string-pagination"></div></div></section><section id="simc-history" data-simc-page="history" class="content-section"></section><button data-simc-workflow-entry="import"></button>`,{runScripts:'outside-only',url:'https://example.test/'});
 t.after(()=>dom.window.close());await new Promise(r=>dom.window.addEventListener('load',r,{once:true}));
 const w=dom.window,requests=[];w.document.cookie='csrftoken=test';w.fetch=(url,options={})=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject}));
 vm.runInContext(readSource('static/dashboard/js/main.js'),dom.getInternalVMContext());
 w.showMessage=()=>{};w.stopSimcAttributeSearch=()=>{};w.stopSimcCandidateComparisonPolling=()=>{};
 return {w,context:dom.getInternalVMContext(),requests,el:id=>w.document.getElementById(id)};
}
const reply=(request,page,rows=[{id:page,name:`PAGE ${page}`}],total=51)=>request.resolve({ok:true,json:async()=>({success:true,data:rows,pagination:{page,page_size:25,total,total_pages:Math.max(1,Math.ceil(total/25))}})});
test('home binds without SimC requests; direct history loads history only; import starts catalogs once',async t=>{
 const h=await setup(t),panels=[];h.w.simcWorkbenchLoadPanel=p=>panels.push(p);
 h.w.initSimcWorkbench();assert.equal(h.requests.length,0);
 h.el('simc-history').classList.add('active');h.w.switchSimcWorkbenchL1Tab('history');assert.deepEqual(panels,['tasks']);assert.equal(h.requests.length,0);
 h.w.document.querySelector('[data-simc-workflow-entry]').click();assert.ok(h.requests.some(r=>r.url.includes('simc-spec-options')));const count=h.requests.length;
 h.w.document.querySelector('[data-simc-workflow-entry]').click();assert.equal(h.requests.length,count);
});
test('current page only, same-spec page race, filter resets, refresh retains, failed retry',async t=>{
 const h=await setup(t);h.w.loadSimcSpecOptions=async()=>[{value:'mage_fire',label:'Fire'}];h.w.bindSimcTalentStringControls();await tick();
 const first=h.w.loadSimcTalentStrings();assert.equal(new URL(h.requests[0].url,'https://x').searchParams.get('page_size'),'25');assert.equal(h.el('simc-talent-string-list').getAttribute('aria-busy'),'true');reply(h.requests[0],1);await first;assert.equal(h.requests.length,1);
 h.el('simc-talent-string-pagination').querySelector('[data-talent-page="2"]').click();const old=h.requests[1];
 const fresh=h.w.loadSimcTalentStrings(3);reply(h.requests[2],3);await fresh;reply(old,2);await tick();assert.match(h.el('simc-talent-string-list').textContent,/PAGE 3/);assert.doesNotMatch(h.el('simc-talent-string-list').textContent,/PAGE 2/);
 h.el('simc-talent-string-refresh').click();assert.equal(new URL(h.requests[3].url,'https://x').searchParams.get('page'),'3');h.requests[3].reject(new Error('RETRY ME'));await tick();assert.match(h.el('simc-talent-string-list').textContent,/RETRY ME/);h.el('simc-talent-string-list').querySelector('button').click();reply(h.requests[4],3);await tick();
 h.el('simc-talent-string-spec-filter').value='mage_fire';h.el('simc-talent-string-spec-filter').dispatchEvent(new h.w.Event('change'));assert.equal(new URL(h.requests[5].url,'https://x').searchParams.get('page'),'1');reply(h.requests[5],1);await tick();assert.match(h.el('simc-talent-string-pagination').textContent,/51/);
});
test('deleted final page falls back safely and leaving panel fences late response',async t=>{
 const h=await setup(t);const p=h.w.loadSimcTalentStrings(3);reply(h.requests[0],3,[],50);await tick();assert.equal(new URL(h.requests[1].url,'https://x').searchParams.get('page'),'2');reply(h.requests[1],2);await p;
 const old=h.w.loadSimcTalentStrings();h.w.deactivateSimcWorkbench();reply(h.requests[2],2,[{id:99,name:'STALE'}]);await old;assert.doesNotMatch(h.el('simc-talent-string-list').textContent,/STALE/);
});
test('real history loader starts only history and its filter, then navigation cancels it',async t=>{
 const h=await setup(t);
 h.el('simc-history').innerHTML='<select data-task-history-spec></select><div id="simc-wb-task-list"></div>';
 vm.runInContext(readSource('static/dashboard/js/simc-workbench.js'),h.context);
 h.w.initSimcWorkbench();assert.equal(h.requests.length,0);
 h.el('simc-history').classList.add('active');h.w.switchSimcWorkbenchL1Tab('history');
 assert.equal(h.requests.length,2);
 assert.ok(h.requests.some(r=>r.url==='/api/simc-spec-options/'));
 const history=h.requests.find(r=>r.url.startsWith('/api/simc-workbench/history'));
 assert.ok(history);assert.match(h.el('simc-wb-task-list').textContent,/加载/);
 h.w.deactivateSimcWorkbench();assert.equal(history.options.signal.aborted,true);
});

test('profile query scopes only explicit spec and retains every matching record',async t=>{
 const h=await setup(t);const p=h.w.simcWbFetchProfilesForWorkbench(undefined,'mage_fire');assert.equal(h.requests[0].url,'/api/simc-profile/?spec=mage_fire');h.requests[0].resolve({ok:true,json:async()=>({success:true,data:[{id:1},{id:2}]})});assert.equal((await p).length,2);
 const all=h.w.simcWbFetchProfilesForWorkbench();assert.equal(h.requests[1].url,'/api/simc-profile/');h.requests[1].resolve({ok:true,json:async()=>({success:true,data:[]})});await all;
});
