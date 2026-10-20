// Install only this test dependency: npm install --prefix /tmp/lmonitor-analysis-ui-test jsdom@26
// Run: NODE_PATH=/tmp/lmonitor-analysis-ui-test/node_modules node --test botend/tests/player_analysis_management_test.js
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {JSDOM} = require('jsdom');
const {Client, mount, buildPatch, safeURL} = require('../../static/dashboard/js/player-analysis-management.js');
const record = () => ({id:7,revision:3,player_id:42,player:{id:42,character_name:'<img src=x onerror=bad>',realm:'Realm'},status:'ready',review_status:'pending',is_published:false,review_note:'',source_kind:'ai',manual:null,ai:{tags:[{label:'AI',value:'判断',reason:'依据',evidence:[]}],conclusion:'冻结结论',evidence:[{text:'冻结证据',url:'javascript:bad'}],result_json:{large:true}},effective:{}});
const response = data => ({ok:true,status:200,json:async()=>({status:'success',data:structuredClone(data)})});
const page = (items,number=1,total=1) => ({items,total,page:number,page_size:20,total_pages:Math.ceil(total/20)});
async function fixture({row=record(),confirm=()=>true,handler}={}) {
 const dom=new JSDOM('<main id="player-analysis-app"></main><meta name="csrf-token" content="token">',{url:'https://example.com/dashboard/player-analyses/'});
 const calls=[];
 const fetcher=async(url,options)=>{
  calls.push([url,options]);const overridden=handler && await handler(url,options,row);if(overridden)return overridden;
  if(options.method==='PATCH'){Object.assign(row,JSON.parse(options.body));row.revision++;}
  if(options.method==='DELETE'){row.is_published=false;row.revision++;}
  if(options.method==='POST'){Object.assign(row,JSON.parse(options.body),{id:8,revision:0,source_kind:'manual'});}
  return response(url.includes('?')?page([row]):row);
 };
 const doc=dom.window.document;
 const app=mount(doc.getElementById('player-analysis-app'),{fetch:fetcher,confirm});await app.ready;
 return {app,doc,calls,row,dom,click:async selector=>{doc.querySelector(selector).click();await app.settled();},submit:async selector=>{doc.querySelector(selector).dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true}));await app.settled();}};
}
test('list uses authoritative Chinese identity and season names, never a database season ordinal',async()=>{
 const row=record();row.player={...row.player,class_name:'Mage',spec_name:'Fire',class_label:'法师',spec_label:'火焰',season_id:3,season_name:'至暗之夜第1赛季'};
 const orphan={...record(),id:8,player:null,player_id:null,character:{character_name:'孤立角色',region:'cn',realm:'服务器',season_id:3,season_name:'地心之战第2赛季'}};
 const unknown={...record(),id:9,player:{id:99,character_name:'未知角色',class_name:'FutureClass',spec_name:'FutureSpec',season_id:876}};
 const f=await fixture({handler:async(url)=>url.includes('?')?response(page([row,orphan,unknown])):null});
 try {
  const rows=f.doc.querySelectorAll('.pa-list-row');
  assert.match(rows[0].textContent,/法师 火焰/);assert.match(rows[0].textContent,/至暗之夜第1赛季/);assert.doesNotMatch(rows[0].textContent,/Mage|Fire|赛季 3/);
  assert.match(rows[1].textContent,/孤立角色/);assert.match(rows[1].textContent,/地心之战第2赛季/);
  assert.match(rows[2].textContent,/FutureClass FutureSpec/);assert.match(rows[2].textContent,/赛季未知/);assert.doesNotMatch(rows[2].textContent,/876/);
 }finally{f.app.destroy();f.dom.window.close();}
});
test('list displays every tag safely, preserves confidence and never falls back to any conclusion',async()=>{
 const row={...record(),tags:[{label:'组队模式',value:'五人固定车队',confidence:'low',confidence_label:'低'},{label:'<img src=x>',value:'第二标签'}],conclusion:'LIST_SECRET',manual:{tags:[],conclusion:'MANUAL_SECRET',evidence:[]},effective:{conclusion:'EFFECTIVE_SECRET'}};
 const empty={...row,id:8,tags:[]};
 const f=await fixture({row,handler:async(url)=>url.includes('?')?response(page([row,empty])):null});
 try {
  const rows=f.doc.querySelectorAll('.pa-list-row');
  assert.deepEqual(Array.from(rows[0].querySelectorAll('.pa-list-tag')).map(n=>n.textContent),['组队模式 · 五人固定车队 · 置信度：低','<img src=x> · 第二标签']);
  assert.match(rows[1].textContent,/暂无标签/);assert.equal(rows[1].querySelectorAll('.pa-list-tag').length,0);
  assert.equal(f.doc.querySelector('.pa-results img'),null);assert.equal(f.doc.querySelector('.pa-list-summary'),null);
  assert.doesNotMatch(f.doc.querySelector('.pa-results').textContent,/LIST_SECRET|MANUAL_SECRET|EFFECTIVE_SECRET|冻结结论/);
  await f.click('[data-action="open"]');assert.match(f.doc.querySelector('.pa-projections').textContent,/冻结结论/);assert.equal(f.doc.querySelector('[data-field="conclusion"]').value,'MANUAL_SECRET');
 }finally{f.app.destroy();f.dom.window.close();}
});
test('tag options follow server metadata; exact selection persists through pagination, option refresh and empty results',async()=>{
 let options=['组队模式','其他页标签','<img src=x>'],items=[record()];
 const f=await fixture({handler:async(url)=>{
  if(!url.includes('?'))return null;
  const params=new URL(url,'https://example.com').searchParams;
  return response({...page(items,Number(params.get('page')),items.length?41:0),tag_options:options});
 }});
 try {
  const select=f.doc.querySelector('.pa-filters [data-field="tag"]');assert.ok(select);
  assert.deepEqual(Array.from(select.options).map(n=>n.value),['',...options]);
  select.value='组队模式';f.doc.querySelector('[name="q"]').value='角色 42';await f.submit('.pa-filters');await f.click('.pa-results [data-action="next"]');
  let params=new URL(f.calls.at(-1)[0],'https://example.com').searchParams;
  assert.equal(params.get('tag'),'组队模式');assert.equal(params.get('page'),'2');assert.equal(params.get('q'),'角色 42');assert.equal(select.value,'组队模式');
  options=['新选项'];items=[];await f.submit('.pa-filters');
  assert.deepEqual(Array.from(select.options).map(n=>n.value),['','新选项','组队模式']);assert.equal(select.value,'组队模式');
  assert.match(f.doc.querySelector('.pa-results').textContent,/没有匹配/);
  select.value='';await f.submit('.pa-filters');
  params=new URL(f.calls.at(-1)[0],'https://example.com').searchParams;assert.equal(params.get('tag'),'');assert.equal(params.get('page'),'1');
  assert.deepEqual(Array.from(select.options).map(n=>n.value),['','新选项']);assert.equal(f.doc.querySelector('.pa-filters img'),null);
 }finally{f.app.destroy();f.dom.window.close();}
});
test('PATCH only changes, preserves manual; explicit restoration and publication gate',()=>{
 const row=record();
 assert.deepEqual(buildPatch(row,{review_status:'approved',review_note:'',is_published:false}),{revision:3,review_status:'approved'});
 assert.deepEqual(buildPatch({...row,manual:{tags:[]}}, {manual:null}),{revision:3,manual:null});
 assert.throws(()=>buildPatch(row,{is_published:true}),/批准/);
 assert.equal(safeURL('javascript:alert(1)'), '');
 assert.equal(safeURL('//evil.example'), '');
 assert.equal(safeURL('https://example.com/a'), 'https://example.com/a');
});
test('write lock, CSRF, exact object GET readback and server revision',async()=>{
 const calls=[]; let release;
 const client=new Client(async(url,options)=>{calls.push([url,options]);if(options.method==='PATCH')await new Promise(r=>release=r);return response({...record(),revision:4});},()=> 'csrf-value');
 const pending=client.write('PATCH',7,{revision:3,review_status:'approved'});
 await assert.rejects(client.write('DELETE',7,{revision:3}),/进行中/);
 release(); assert.equal((await pending).revision,4);
 assert.deepEqual(calls.map(c=>[c[0],c[1].method]),[['/dashboard/api/player-analyses/7/','PATCH'],['/dashboard/api/player-analyses/7/','GET']]);
 assert.equal(calls[0][1].headers['X-CSRFToken'],'csrf-value');
 assert.equal(calls[0][1].credentials,'same-origin');
});
test('401/403/409 are explicit; readback failure is not success',async()=>{
 for(const [status,message] of [[401,/登录/],[403,/权限/],[409,/冲突/]]){
  const c=new Client(async()=>({status,ok:false,json:async()=>({})}),()=> 'x');
  await assert.rejects(c.request(''),message);
 }
 let count=0;const c=new Client(async()=> ++count===1?response({id:7}):{ok:false,status:503,json:async()=>({})},()=> 'x');
 await assert.rejects(c.write('POST',null,{}),/已提交.*重新读取/);
});
test('DOM list is lazy; detail safe text, edit form, unchanged manual omitted; restore confirmed',async()=>{
 const {doc,calls,click}=await fixture({confirm:()=>false});
 assert.equal(calls.length,1);assert.match(calls[0][0],/page_size=20/);
 assert.equal(doc.querySelector('img'),null);
 await click('[data-action="open"]');
 assert.equal(calls.length,3);assert.match(doc.body.textContent,/冻结证据/);
 assert.equal(doc.querySelector('a[href^="javascript"]'),null);
 doc.querySelector('.pa-edit-form [data-field="review_status"]').value='approved';
 await click('[data-action="save"]');
 const patch=JSON.parse(calls.find(c=>c[1].method==='PATCH')[1].body);
 assert.deepEqual(patch,{revision:3,review_status:'approved'});
 await click('[data-action="restore"]');
 assert.equal(calls.filter(c=>c[1].method==='PATCH').length,1);
 await click('[data-action="edit-manual"]');await click('[data-action="add-tag"]');
 assert.ok(doc.querySelector('[data-tag-field="label"]'));
 assert.equal(doc.querySelector('textarea[data-field="raw-json"]'),null);
});
test('all filters and list pagination go to server; never fetch detail on list',async()=>{
 const {doc,calls,click,submit}=await fixture({handler:async(url)=>url.includes('?')?response(page([record()],Number(new URL('https://example.com'+url).searchParams.get('page')),41)):null});
 doc.querySelector('[name="q"]').value='角色 42';
 for(const [key,value] of Object.entries({status:'ready',review_status:'approved',source:'manual',visibility:'hidden'}))doc.querySelector(`.pa-filters [data-field="${key}"]`).value=value;
 await submit('.pa-filters');await click('.pa-results [data-action="next"]');
 const params=new URL('https://example.com'+calls.at(-1)[0]).searchParams;
 assert.equal(params.get('page'),'2');assert.equal(params.get('q'),'角色 42');assert.equal(params.get('visibility'),'hidden');assert.equal(params.get('source'),'manual');assert.equal(params.get('status'),'ready');assert.equal(params.get('review_status'),'approved');
 assert.ok(calls.every(c=>c[0].includes('?')));
});
test('create uses a selected real paginated character and form evidence; defaults hidden draft',async()=>{
 const character={id:99,character_name:'真实角色',realm:'真实服务器',region:'cn',season_id:4};
 const {doc,calls,click,submit}=await fixture({handler:async(url)=>url.includes('characters/?')?response(page([character],Number(new URL('https://example.com'+url).searchParams.get('page')),21)):null});
 await click('[data-action="new"]');doc.querySelector('[data-field="conclusion"]').value='手工核验结论';
 await submit('.pa-edit-form');assert.match(doc.querySelector('[role="status"]').textContent,/真实角色/);assert.equal(calls.filter(c=>c[1].method==='POST').length,0);
 doc.querySelector('.pa-character-picker input').value='真实';await submit('.pa-character-picker form');
 await click('.pa-character-picker [data-action="next"]');assert.match(calls.at(-1)[0],/page=2/);
 await click('[data-action="select-character"]');await click('[data-action="add-tag"]');
 doc.querySelector('[data-tag-field="label"]').value='配合';doc.querySelector('[data-tag-field="value"]').value='固定队';doc.querySelector('[data-tag-field="reason"]').value='已核实';
 await click('.pa-tag-editor [data-action="add-evidence"]');
 doc.querySelector('[data-evidence-field="text"]').value='通关证据';doc.querySelector('[data-evidence-field="url"]').value='https://example.com/proof';
 await submit('.pa-edit-form');
 const post=JSON.parse(calls.find(c=>c[1].method==='POST')[1].body);
 assert.equal(post.player_id,99);assert.equal(post.review_status,'pending');assert.equal(post.is_published,false);assert.equal(post.review_note,'');assert.equal(post.manual.conclusion,'手工核验结论');assert.equal(post.manual.tags[0].evidence[0].url,'https://example.com/proof');
 const postIndex=calls.findIndex(c=>c[1].method==='POST');assert.equal(calls[postIndex+1][0],'/dashboard/api/player-analyses/8/');
});
test('manual edit invalidates old approval; approve/publish then hide/reject; DELETE carries revision',async()=>{
 const row={...record(),review_status:'approved',is_published:true,source_kind:'manual',manual:{tags:[],conclusion:'原文',evidence:[]}};
 const {doc,calls,click,submit}=await fixture({row});
 await click('[data-action="open"]');await click('[data-action="edit-manual"]');
 doc.querySelector('[data-field="conclusion"]').value='修改结论';await submit('.pa-edit-form');
 const writes=()=>calls.filter(c=>c[1].method!=='GET').map(c=>JSON.parse(c[1].body));
 assert.deepEqual(writes()[0],{revision:3,manual:{tags:[],conclusion:'修改结论',evidence:[]},review_status:'pending',is_published:false});
 doc.querySelector('.pa-edit-form [data-field="review_status"]').value='approved';doc.querySelector('.pa-edit-form [data-field="is_published"]').value='true';await submit('.pa-edit-form');
 assert.deepEqual(writes()[1],{revision:4,review_status:'approved',is_published:true});
 doc.querySelector('.pa-edit-form [data-field="review_status"]').value='rejected';doc.querySelector('.pa-edit-form [data-field="is_published"]').value='false';doc.querySelector('[data-field="review_note"]').value='证据不足';await submit('.pa-edit-form');
 assert.deepEqual(writes()[2],{revision:5,review_status:'rejected',is_published:false,review_note:'证据不足'});
 await click('[data-action="restore"]');assert.deepEqual(writes()[3],{revision:6,manual:null});
 await click('[data-action="archive"]');assert.deepEqual(writes()[4],{revision:7});assert.equal(calls.filter(c=>c[1].method==='DELETE').length,1);
});
test('unchanged manual with database-reordered object keys retains approval',async()=>{
 const row={...record(),manual:{evidence:[],conclusion:'人工结论',tags:[{evidence:[],reason:'理由',value:'值',label:'标签'}]},review_status:'approved',source_kind:'manual'};
 const {doc,calls,click,submit}=await fixture({row});
 await click('[data-action="open"]');doc.querySelector('[data-field="review_note"]').value='仅更新审核意见';await submit('.pa-edit-form');
 assert.deepEqual(JSON.parse(calls.find(c=>c[1].method==='PATCH')[1].body),{revision:3,review_note:'仅更新审核意见'});
});
test('409 preserves editor values and never renders successful save',async()=>{
 const {doc,calls,click,submit}=await fixture({handler:async(_url,options)=>options.method==='PATCH'?{status:409,ok:false,json:async()=>({})}:null});
 await click('[data-action="open"]');doc.querySelector('[data-field="review_note"]').value='需要保留的意见';await submit('.pa-edit-form');
 assert.match(doc.querySelector('[role="status"]').textContent,/版本冲突/);assert.equal(doc.querySelector('[data-field="review_note"]').value,'需要保留的意见');
 assert.equal(calls.at(-1)[1].method,'PATCH');assert.equal(doc.querySelector('[data-action="save"]').disabled,false);
});
test('stale AI approval is visible and explicit reapproval survives unchanged value',async()=>{
 const row={...record(),review_status:'approved',reviewed_at:'2026-10-18T00:00:00Z',review_matches_ai:false};
 const {doc,calls,click}=await fixture({row,handler:async(_url,options,r)=>{
  if(options.method==='PATCH')r.review_matches_ai=true;
  return null;
 }});
 assert.match(doc.querySelector('.pa-results').textContent,/原审核不适用/);
 await click('[data-action="open"]');
 assert.match(doc.querySelector('[data-review-stale]').textContent,/证据或结论已更新/);
 await click('[data-action="reapprove-ai"]');
 assert.deepEqual(JSON.parse(calls.find(c=>c[1].method==='PATCH')[1].body),{revision:3,review_status:'approved'});
 assert.equal(doc.querySelector('[data-review-stale]'),null);
 assert.equal(doc.querySelector('[data-action="reapprove-ai"]'),null);
});

test('late detail response cannot replace a newer selection',async()=>{
 let resolveOld;
 const {app,doc,click}=await fixture({handler:async(url)=>url.includes('?')?response(page([record(),{...record(),id:8}])):url.endsWith('/7/')?new Promise(resolve=>{resolveOld=resolve;}):response({...record(),id:8})});
 doc.querySelectorAll('[data-action="open"]')[0].click();await Promise.resolve();
 doc.querySelectorAll('[data-action="open"]')[1].click();await app.settled();assert.match(doc.querySelector('.pa-detail h2').textContent,/#8/);
 resolveOld(response(record()));await new Promise(resolve=>setImmediate(resolve));assert.match(doc.querySelector('.pa-detail h2').textContent,/#8/);
});
