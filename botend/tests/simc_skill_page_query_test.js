const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawnSync, spawn} = require('node:child_process');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const worker = path.join(root, 'scripts/simc_skill_page_query.cjs');
const queryModule = path.join(root, 'static/dashboard/js/simc_skill_query.js');
const base = {hero_tree_id:'60',target_count:'1',sort:'final',direction:'desc',q:'',exclude:[],limit:7,byte_budget:100000};
const actions = Array.from({length:113}, (_,i)=>({spell_id:i+1,display_name:['技能2','技能10','阿尔法','beta','BETA'][i%5],name:`spell_${i}`,
 player_skill:i%17!==0, affected_target_counts:i%3 ? [1,2] : [1], hero_subtree_ids:i%11===0?[61]:[],
 variant:{hero_subtree_id:i%7===0?61:i%3?0:null,talent_id:i%4?10:null,talent_name_zh:'天赋2',
 runtime_condition:i%8?'':'血量低于35%',runtime_conditions:i%2?[{token:'buff.test',scope:i%3?'self':'target',spell_id:42,name_zh:'激怒',stacks:i%3+1,stack_values:i%5?[1,2]:[]}]:[]},
 product:{final_normalized_damage:i%13===0?'123':i%23===0?false:i%9?i%6:null,final_normalized_damage_by_target:{'2':i%13}}}));
function run(query, fixture=actions, extra={}) {
 const input=[{type:'query',query},...fixture.map((action,ordinal)=>({type:'action',ordinal,action}))].map(x=>JSON.stringify(x)).join('\n')+'\n';
 const out=spawnSync(process.execPath,[worker],{input,encoding:'utf8',maxBuffer:2000000,...extra});
 assert.equal(out.status,0,out.stderr); return JSON.parse(out.stdout);
}
test('real Node worker matches frozen full original oracle, all global pages and complete facets',()=>{
 const shared=require(queryModule), oracle=require('./fixtures/simc_skill_query_original.cjs');
 for(const target_count of ['1','2']) for(const hero_tree_id of ['60','61']) for(const sort of ['name','final']) for(const direction of ['asc','desc']) for(const q of ['','技能','激怒','35%','2']) {
  const query={...base,target_count,hero_tree_id,sort,direction,q};
  const expected=oracle(actions,query); let after=null, found=[], pages=0;
  do {const result=run({...query,after}); found.push(...result.actions); assert.deepEqual(result.facets,expected.facets);
   assert.equal(result.counts.stored_actions,actions.length);assert.equal(result.counts.matched,expected.actions.length);
   assert.equal(result.source_ordinals.length,result.actions.length);after=result.page.has_more?result.page.after:null;assert.ok(++pages<30);
  } while(after);
  assert.deepEqual(found,expected.actions,JSON.stringify(query));
 }
 const facets=run(base).facets;
 for(const excluded of [...facets.talents,...facets.buffs]) {
  const q={...base,exclude:[excluded.key],limit:100}; assert.deepEqual(run(q).actions,oracle(actions,q).actions);
 }
 for(const target_count of ['5','10','20']) {
  const query={...base,target_count,limit:100};
  const fixture=actions.map(action=>({...action,affected_target_counts:[Number(target_count)]}));
  assert.deepEqual(run(query,fixture).actions,oracle(fixture,query).actions);
 }
 assert.equal(shared.PRESENTATION_VERSION,'actor_page_v1');
});
test('byte budget gives continuation, and oversized row fails explicitly without losing it',()=>{
 const rows=actions.slice(1,8).map((x,i)=>({...x,player_skill:true,hero_subtree_ids:[],affected_target_counts:[1],variant:{},payload:'中'.repeat(700),product:{final_normalized_damage:100-i}}));
 const first=run({...base,byte_budget:6500},rows);assert.ok(first.actions.length<7);assert.ok(first.page.has_more);
 const second=run({...base,byte_budget:6500,after:first.page.after},rows);assert.notEqual(first.source_ordinals[0],second.source_ordinals[0]);
 const input=[{type:'query',query:{...base,byte_budget:2000}},{type:'action',ordinal:0,action:rows[0]}].map(JSON.stringify).join('\n');
 const failed=spawnSync(process.execPath,[worker],{input,encoding:'utf8'}); assert.equal(failed.status,0,failed.stderr);assert.deepEqual(JSON.parse(failed.stdout).error,{code:'row_exceeds_page_budget',source_ordinal:0,detail_view:'actor_fragment'});
});
test('stream/backpressure keeps bounded top K under a 64MiB V8 heap, large late winners',async t=>{
 const child=spawn(process.execPath,['--max-old-space-size=64',worker],{stdio:['pipe','pipe','pipe']});
 let maxRssKiB=0;
 const sampler=setInterval(()=>{try{const text=require('node:fs').readFileSync(`/proc/${child.pid}/status`,'utf8');const match=/VmRSS:\s+(\d+)/.exec(text);if(match)maxRssKiB=Math.max(maxRssKiB,Number(match[1]));}catch(_error){}},10);
 t.after(()=>clearInterval(sampler));
 let stdout='',stderr='',drains=0;child.stdout.on('data',x=>stdout+=x);child.stderr.on('data',x=>stderr+=x);
 const done=new Promise(resolve=>child.on('close',resolve));
 async function send(frame){if(!child.stdin.write(JSON.stringify(frame)+'\n')){drains++;await new Promise(resolve=>child.stdin.once('drain',resolve));}}
 await send({type:'query',query:base});
 for(let i=0;i<25000;i++)await send({type:'action',ordinal:i,action:{display_name:'技能'+i,blob:'x'.repeat(8000),product:{final_normalized_damage:i},variant:{}}});
 child.stdin.end();assert.equal(await done,0,stderr);const result=JSON.parse(stdout);
 assert.equal(result.counts.matched,25000);assert.deepEqual(result.source_ordinals,[24999,24998,24997,24996,24995,24994,24993]);assert.ok(drains>0);assert.ok(Buffer.byteLength(stdout)<=base.byte_budget);assert.ok(maxRssKiB<160*1024,`RSS ${maxRssKiB}KiB`);t.diagnostic(`25,000 rows streamed; observed worker peak RSS ${maxRssKiB}KiB; backpressure drains ${drains}`);
});
