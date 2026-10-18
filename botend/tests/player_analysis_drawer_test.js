const {test}=require('node:test');
const assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const {mount}=require('../../static/dashboard/js/player-analysis-management.js');
const tick=()=>new Promise(r=>setImmediate(r));
async function setup({initialState='idle',initialToken,postFailure,postToken,pollInterval=10}={}){
 const dom=new JSDOM('<section id="player-analyses"><div id="app"></div></section><meta name="csrf-token" content="csrf">',{url:'https://example.com',pretendToBeVisual:true});
 const row={id:7,revision:3,player_id:42,player:{character_name:'角色'},status:'ready',review_status:'approved',is_published:false,source_kind:'ai',manual:null,ai:{tags:[{label:'队伍',value:'固定',evidence:[]}],conclusion:'AI冻结',evidence:[]}};
 const calls=[];let state=initialState,token=initialToken,accept=true,failPost=false;
 const app=mount(dom.window.document.querySelector('#app'),{confirm:()=>accept,pollInterval,fetch:async(url,opt)=>{calls.push([url,opt]);let data=row;
 if(url.endsWith('/recheck/')){
  if(opt.method==='POST'){
   if(postFailure===409)return {ok:false,status:409,json:async()=>({status:'error',message:'conflict'})};
   if(postFailure==='network-old')throw Error('connection lost');
   state='queued';token=postToken;if(failPost)throw Error('connection lost');
  }
  data={state,message:state,...(token?{token}:{})};
 }
 else if(url.includes('?'))data={items:[row],page:1,total:1,total_pages:1};
 return {ok:true,status:200,json:async()=>({status:'success',data:structuredClone(data)})};}});
 await app.ready;const doc=dom.window.document;
 const click=async(s)=>{doc.querySelector(s).click();await app.settled();await tick();};
 return {dom,doc,app,row,calls,click,setState:v=>state=v,setToken:v=>token=v,setAccept:v=>accept=v,failPost:()=>failPost=true};
}
for(const postFailure of [409,'network-old'])for(const initialToken of [undefined,'previous-job']){
 test(`rejected/uncertain POST (${postFailure}, token=${initialToken}) never adopts historical success`,async()=>{
  const f=await setup({initialState:'succeeded',initialToken,postFailure,pollInterval:60000});try{
   await f.click('[data-action="open"]');
   f.doc.querySelector('[data-field="conclusion"]').value='未保存草稿';
   const before=f.calls.length;
   await f.click('.pa-detail [data-action="recheck"]');
   await f.click('[data-action="refresh-check"]');
   f.app.setActive(false);f.app.setActive(true);await f.app.settled();await tick();
   const status=f.doc.querySelector('[data-check-status]').textContent;
   assert.doesNotMatch(status,/检查完成/);
   assert.match(status,postFailure===409?/提交被拒绝/:/本次提交未确认/);
   assert.equal(f.calls.filter(([,o])=>o.method==='POST').length,1);
   assert.equal(f.calls.slice(before).filter(([u])=>u.endsWith('/7/')).length,0,'old terminal must not trigger result readback');
   assert.equal(f.doc.querySelector('[data-field="conclusion"]').value,'未保存草稿');
   assert.equal(f.doc.querySelector('.pa-detail [data-action="recheck"]').disabled,false);
  }finally{f.app.destroy();f.dom.window.close();}
 });
}
for(const uncertain of [false,true]){
 test(`${uncertain?'uncertain':'accepted'} POST follows new token to success without replay or draft loss`,async()=>{
  const f=await setup({initialState:'succeeded',initialToken:'old',postToken:'new',pollInterval:60000});try{
   await f.click('[data-action="open"]');f.doc.querySelector('[data-field="conclusion"]').value='保留';
   if(uncertain)f.failPost();
   await f.click('.pa-detail [data-action="recheck"]');
   assert.match(f.doc.querySelector('[data-check-status]').textContent,/已排队/);
   // A stale read must not override the token returned by POST/recovered GET.
   f.setToken('old');f.setState('succeeded');await f.click('[data-action="refresh-check"]');
   assert.doesNotMatch(f.doc.querySelector('[data-check-status]').textContent,/检查完成/);
   f.setToken('new');f.setState('running');await f.click('[data-action="refresh-check"]');
   assert.match(f.doc.querySelector('[data-check-status]').textContent,/正在检查/);
   f.setState('succeeded');await f.click('[data-action="refresh-check"]');
   assert.match(f.doc.querySelector('[data-check-status]').textContent,/检查完成/);
   assert.equal(f.calls.filter(([,o])=>o.method==='POST').length,1);
   assert.equal(f.doc.querySelector('[data-field="conclusion"]').value,'保留');
  }finally{f.app.destroy();f.dom.window.close();}
 });
}
test('new terminal token reloads frozen detail and stale-review state',async()=>{
 const f=await setup({initialState:'succeeded',initialToken:'old',pollInterval:60000});try{
  await f.click('[data-action="open"]');const before=f.calls.length;
  f.row.ai.conclusion='NEW_AI';f.row.reviewed_at='2026-10-18T00:00:00Z';f.row.review_matches_ai=false;f.row.revision++;
  f.setToken('next');f.setState('succeeded');await f.click('[data-action="refresh-check"]');
  assert.match(f.doc.querySelector('.pa-projections').textContent,/NEW_AI/);
  assert.ok(f.doc.querySelector('[data-review-stale]'));
  assert.ok(f.calls.slice(before).some(([u])=>u.endsWith('/7/')));
 }finally{f.app.destroy();f.dom.window.close();}
});
test('successor task observed after hiding replaces unreachable accepted token without reposting',async()=>{
 const f=await setup({initialState:'succeeded',initialToken:'old',postToken:'job-A',pollInterval:60000});try{
  await f.click('[data-action="open"]');await f.click('.pa-detail [data-action="recheck"]');f.app.setActive(false);
  f.setToken('job-B');f.setState('succeeded');f.row.ai.conclusion='SUCCESSOR_AI';f.app.setActive(true);await f.app.settled();await tick();
  assert.equal(f.doc.querySelector('.pa-detail [data-action="recheck"]').disabled,false);
  assert.match(f.doc.querySelector('[data-check-status]').textContent,/最新|后续/);
  assert.match(f.doc.querySelector('.pa-projections').textContent,/SUCCESSOR_AI/);
  assert.equal(f.calls.filter(([,o])=>o.method==='POST').length,1);
 }finally{f.app.destroy();f.dom.window.close();}
});
test('lost POST followed by a new terminal token reads latest result without claiming request ownership',async()=>{
 const f=await setup({initialState:'succeeded',initialToken:'old',postFailure:'network-old',pollInterval:60000});try{
  await f.click('[data-action="open"]');await f.click('.pa-detail [data-action="recheck"]');
  f.setToken('changed');f.setState('succeeded');f.row.ai.conclusion='RECOVERED_AI';await f.click('[data-action="refresh-check"]');
  const status=f.doc.querySelector('[data-check-status]').textContent;
  assert.match(status,/提交.*未确认/);assert.match(status,/最新/);
  assert.match(f.doc.querySelector('.pa-projections').textContent,/RECOVERED_AI/);
  assert.equal(f.calls.filter(([,o])=>o.method==='POST').length,1);
 }finally{f.app.destroy();f.dom.window.close();}
});

test('drawer, direct manual fields, AI copy, Escape guard and focus return',async()=>{
 const f=await setup();try{
 const trigger=f.doc.querySelector('[data-action="open"]');trigger.focus();await f.click('[data-action="open"]');
 const drawer=f.doc.querySelector('[role="dialog"]');assert.ok(drawer);assert.equal(drawer.hidden,false);
 assert.ok(drawer.querySelector('[data-field="conclusion"]'));
 await f.click('[data-action="copy-ai"]');assert.equal(f.doc.querySelector('[data-field="conclusion"]').value,'AI冻结');
 f.doc.querySelector('[data-field="conclusion"]').value='人工草稿';assert.equal(f.row.ai.conclusion,'AI冻结');
 f.setAccept(false);f.doc.dispatchEvent(new f.dom.window.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));assert.equal(drawer.hidden,false);
 f.setAccept(true);f.doc.dispatchEvent(new f.dom.window.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));assert.equal(drawer.hidden,true);assert.equal(f.doc.activeElement,trigger);
 }finally{f.app.destroy();f.dom.window.close();}
});
test('failed task and uncertain POST preserve draft; visibility resumes GET only',async()=>{
 const f=await setup();try{
 await f.click('[data-action="open"]');f.doc.querySelector('[data-field="review_note"]').value='核验草稿';f.failPost();
 await f.click('.pa-detail [data-action="recheck"]');assert.equal(f.calls.filter(([,o])=>o.method==='POST').length,1);
 Object.defineProperty(f.doc,'visibilityState',{configurable:true,value:'hidden'});f.doc.dispatchEvent(new f.dom.window.Event('visibilitychange'));
 const count=f.calls.length;await new Promise(r=>setTimeout(r,30));assert.equal(f.calls.length,count);
 f.setState('failed');Object.defineProperty(f.doc,'visibilityState',{configurable:true,value:'visible'});f.doc.dispatchEvent(new f.dom.window.Event('visibilitychange'));await tick();await tick();
 assert.equal(f.calls.filter(([,o])=>o.method==='POST').length,1);assert.match(f.doc.querySelector('[data-check-status]').textContent,/检查失败/);assert.equal(f.doc.querySelector('[data-field="review_note"]').value,'核验草稿');
 }finally{f.app.destroy();f.dom.window.close();}
});
test('AI copy saves manual-only patch and invalidates approval without changing frozen source',async()=>{
 const f=await setup();try{
 await f.click('[data-action="open"]');await f.click('[data-action="copy-ai"]');await f.click('[data-action="save"]');
 const patch=JSON.parse(f.calls.find(([,o])=>o.method==='PATCH')[1].body);
 assert.equal(patch.manual.conclusion,'AI冻结');assert.equal(patch.review_status,'pending');assert.equal(patch.revision,3);assert.equal(patch.ai,undefined);assert.equal(f.row.ai.conclusion,'AI冻结');
 }finally{f.app.destroy();f.dom.window.close();}
});
test('recheck submits once with revision and CSRF; hidden pause and GET resume preserve draft',async()=>{
 const f=await setup();try{
 await f.click('[data-action="open"]');f.doc.querySelector('[data-field="conclusion"]').value='保留草稿';
 await f.click('.pa-detail [data-action="recheck"]');await f.click('.pa-detail [data-action="recheck"]');
 const posts=()=>f.calls.filter(([,o])=>o.method==='POST');assert.equal(posts().length,1);assert.deepEqual(JSON.parse(posts()[0][1].body),{revision:3});assert.equal(posts()[0][1].headers['X-CSRFToken'],'csrf');
 f.app.setActive(false);const count=f.calls.length;await new Promise(r=>setTimeout(r,40));assert.equal(f.calls.length,count);assert.equal(f.doc.querySelector('.pa-detail').hidden,true);
 f.setState('succeeded');f.app.setActive(true);await tick();await tick();assert.equal(posts().length,1);assert.equal(f.doc.querySelector('[data-field="conclusion"]').value,'保留草稿');
 assert.ok(f.calls.slice(count).some(([u,o])=>u.endsWith('/recheck/')&&o.method==='GET'));
 assert.ok(f.calls.slice(count).some(([u])=>u.endsWith('/7/')));
 }finally{f.app.destroy();f.dom.window.close();}
});
