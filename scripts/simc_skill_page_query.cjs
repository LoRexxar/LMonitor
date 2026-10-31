#!/usr/bin/node
'use strict';
// NDJSON streaming adapter. No database/filesystem access and no actor accumulation.
const Q = require('../static/dashboard/js/simc_skill_query.js');
const MAX_FRAME_BYTES = 8 * 1024 * 1024;
const MAX_FACET_BYTES = 1024 * 1024;
let query, facets, heap = [], stored = 0, scope = 0, matched = 0, lastOrdinal = -1;
let facetBytes = 0;
class BoundedFacetMap extends Map {
 set(key, value) {
  const bytes = Buffer.byteLength(JSON.stringify([key, value]));
  if (facetBytes + bytes > MAX_FACET_BYTES) fail('facet_budget_exceeded');
  facetBytes += bytes;
  return super.set(key, value);
 }
}
function fail(message) { throw new Error(message); }
function compare(a,b) { return Q.compareRows(a,b,query); }
function swap(i,j) { [heap[i],heap[j]]=[heap[j],heap[i]]; }
function retain(row) {
 const k=query.limit+1;
 if(heap.length<k) {
  heap.push(row);let i=heap.length-1;
  while(i>0){const p=(i-1)>>1;if(compare(heap[p],heap[i])>=0)break;swap(p,i);i=p;}
 } else if(compare(row,heap[0])<0) {
  heap[0]=row;let i=0;
  while(true){let j=i*2+1;if(j>=heap.length)break;if(j+1<heap.length&&compare(heap[j+1],heap[j])>0)j++;if(compare(heap[i],heap[j])>=0)break;swap(i,j);i=j;}
 }
}
function consume(frame) {
 if(!query) {
  if(frame.type!=='query'||!frame.query)fail('expected_query_frame');
  const input=frame.query;
  query={...input,hero_tree_id:String(input.hero_tree_id),target_count:String(input.target_count || '1'),sort:input.sort || 'final',direction:input.direction || 'desc',q:String(input.q || ''),exclude:new Set(input.exclude || []),limit:Number(input.limit || 50),byte_budget:Number(input.byte_budget || 262144)};
  if(!['final','name'].includes(query.sort)||!['asc','desc'].includes(query.direction)||!Number.isInteger(query.limit)||query.limit<1||query.limit>100||!Number.isInteger(query.byte_budget)||query.byte_budget<1024||query.byte_budget>1048576)fail('invalid_query');
  if(query.after && (typeof query.after.name!=='string'||!Number.isSafeInteger(query.after.ordinal)||(query.after.final!==null && (typeof query.after.final!=='number'||!Number.isFinite(query.after.final)))))fail('invalid_after');
  facets=Object.fromEntries(Object.keys(Q.createFacets()).map(key=>[key,new BoundedFacetMap()]));return;
 }
 if(frame.type!=='action'||!Number.isSafeInteger(frame.ordinal)||frame.ordinal<=lastOrdinal||!frame.action||typeof frame.action!=='object'||Array.isArray(frame.action))fail('invalid_action_frame');
 lastOrdinal=frame.ordinal;stored++;
 const candidate=Q.scanCandidate(frame.action,query,facets);
 if(!candidate)return;scope++;
 // Compact condition facts are the only unbounded-in-count structure; reject
 // an unavailable facet view explicitly rather than truncate or exhaust memory.
 const row=Q.matchCandidate(candidate,query,frame.ordinal);if(!row)return;matched++;
 if(query.after && compare(row,Q.cursorRow(query.after))<=0)return;
 const bytes=Buffer.byteLength(JSON.stringify(frame.action));
 // Keep order identity but not a huge full action in the top-K heap.
 if(bytes>query.byte_budget){row.action={display_name:String(frame.action.display_name || frame.action.name || '')};row.oversized=true;delete row.product;}
 retain(row);
}
function finish() {
 if(!query)fail('missing_query');
 const rows=heap.sort(compare), serializedFacets=Q.serializeFacets(facets);
 const result={actions:[],source_ordinals:[],counts:{stored_actions:stored,scope_candidates:scope,matched},facets:serializedFacets,page:{returned:0,has_more:rows.length>0,after:query.after || null}};
 if(Buffer.byteLength(JSON.stringify(result))+1>query.byte_budget)fail('facets_exceed_page_budget');
 for(const row of rows.slice(0,query.limit)) {
  if(row.oversized){if(!result.actions.length)fail(`row_exceeds_page_budget source_ordinal=${row.sourceIndex}`);break;}
  const previous=result.page.after;
  result.actions.push(row.action);result.source_ordinals.push(row.sourceIndex);result.page.returned=result.actions.length;result.page.after=Q.cursorFor(row);result.page.has_more=rows.length>result.actions.length;
  if(Buffer.byteLength(JSON.stringify(result))+1>query.byte_budget){result.actions.pop();result.source_ordinals.pop();result.page.returned=result.actions.length;result.page.after=previous;result.page.has_more=true;if(!result.actions.length)fail(`row_exceeds_page_budget source_ordinal=${row.sourceIndex}`);break;}
 }
 process.stdout.write(JSON.stringify(result)+'\n');
}
(async()=>{
 // Byte buffers preserve arbitrary UTF-8 splits. Async iteration honors stdin
 // backpressure and holds at most one frame plus the stream high-water mark.
 let pending=Buffer.alloc(0);
 for await(const chunk of process.stdin) {
  let start=0;
  for(let i=0;i<chunk.length;i++)if(chunk[i]===10){const part=chunk.subarray(start,i);if(pending.length+part.length>MAX_FRAME_BYTES)fail('action_frame_exceeds_bounded_availability');const line=pending.length?Buffer.concat([pending,part]):part;pending=Buffer.alloc(0);if(line.length)consume(JSON.parse(line.toString('utf8')));start=i+1;}
  const rest=chunk.subarray(start);if(pending.length+rest.length>MAX_FRAME_BYTES)fail('action_frame_exceeds_bounded_availability');if(rest.length)pending=pending.length?Buffer.concat([pending,rest]):Buffer.from(rest);
 }
 if(pending.length)consume(JSON.parse(pending.toString('utf8')));finish();
})().catch(error=>{
 const message=String(error.message || 'query_failed').slice(0,512);
 if (/^(?:row_exceeds_page_budget|facets_exceed_page_budget)/.test(message)) {
  const ordinal=/source_ordinal=(\d+)/.exec(message);
  process.stdout.write(JSON.stringify({error:{code:message.split(' ')[0],source_ordinal:ordinal?Number(ordinal[1]):null,detail_view:'actor_fragment'}})+'\n');
 } else {process.stderr.write(message+'\n');process.exitCode=1;}
});
