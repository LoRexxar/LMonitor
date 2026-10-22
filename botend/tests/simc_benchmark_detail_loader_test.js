const {beforeMarker} = require('./vm_harness');
// Execute the real detail loader against a loopback HTTP server.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const http = require('node:http');
// Rerun now enters the workflow; it must not submit an implicit frozen-copy POST.
{
  const source = fs.readFileSync(path.join(__dirname, '../../static/dashboard/js/simc-workbench.js'), 'utf8');
  const start = '    function renderTaskRerunForm(taskId) {';
  const end = '    function specLabel(';
  assert.equal(source.split(start).length, 2);
  assert.equal(source.split(end).length, 2);
  assert.ok(source.indexOf(end) > source.indexOf(start));
  let navigated;
  const context = vm.createContext({URL, idOf: value => Number.parseInt(value, 10) || 0,
    window: {location: {origin: 'https://example.test', assign: value => { navigated = value; }}}});
  vm.runInContext(source.slice(source.indexOf(start), source.indexOf(end)), context);
  context.renderTaskRerunForm(42);
  const target = new URL(navigated);
  assert.equal(target.origin, 'https://example.test');
  assert.equal(target.pathname, '/dashboard/');
  assert.deepEqual([...target.searchParams], [['section', 'simc-workflow'], ['simc_rerun_task', '42']]);
  console.log('PASS: real rerun entry navigates to workflow without submitting overrides');
}
(async () => {
  let mode = 'building', generation = 'one', requests = 0;
  let row = {spec_key:'mage_fire',talent_key:'profile:talent',scenario_key:'single',profile_detail:{identity:{name:'frozen'}}};
  const received = [];
  const server = http.createServer((req,res) => {
    requests++;
    const target = new URL(req.url, 'http://127.0.0.1');
    received.push({pathname:target.pathname,query:[...target.searchParams].sort()});
    const rows = mode === 'building' ? [] : [{...row,spec_key:mode === 'wrong' ? 'warrior_fury' : row.spec_key}];
    res.setHeader('Content-Type','application/json');
    res.end(JSON.stringify({results:{coordinates:rows,snapshot:{state:mode === 'building' ? 'building' : 'ready',generation}}}));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    const source = fs.readFileSync(path.join(__dirname,'../../static/portal/js/simc-benchmarks.js'),'utf8');
    const startup='  document.addEventListener("DOMContentLoaded", loadBenchmarks);';
    const context=vm.createContext({Intl,URL,URLSearchParams,fetch});
    vm.runInContext(beforeMarker(source, startup)+'globalThis.load=loadCoordinateDetail; globalThis.read=requestJson;})();',context);
    const url=`http://127.0.0.1:${server.address().port}/panel`;
    const coordinate={...row,_snapshotGeneration:'one'};
    const expectedRequest = coordinate => ({pathname:'/panel',query:[
      ['detail','1'],['spec',coordinate.spec_key],['talent',coordinate.talent_key],
      ['scenario',coordinate.scenario_key],
    ].sort()});
    await assert.rejects(context.load(url,coordinate),error=>error.snapshotPending===true);
    assert.deepEqual(received, [expectedRequest(coordinate)]);
    mode='ready';
    const first=await context.load(url,coordinate);
    assert.equal(first._snapshotGeneration,'one');
    assert.equal((await context.load(url,coordinate)).profile_detail.identity.name,'frozen');
    assert.equal(requests,2);
    generation='two';
    assert.equal((await context.load(url,{...coordinate,_snapshotGeneration:'two'}))._snapshotGeneration,'two');
    assert.equal(requests,3);
    await assert.rejects(context.load(url,{...coordinate,_snapshotGeneration:'three'}),error=>error.snapshotChanged===true);
    mode='wrong';generation='four';
    await assert.rejects(context.load(url,{...coordinate,_snapshotGeneration:'four'}),/unavailable/);
    assert.equal(requests,5);
    assert.deepEqual(received, Array.from({length:5},()=>expectedRequest(coordinate)));
    // Change every coordinate component; the server records the actual wire
    // query rather than deriving its canned response from that query.
    mode='ready';generation='two';
    row={...row,spec_key:'warrior_arms',talent_key:'other:talent+2',scenario_key:'multi'};
    const secondCoordinate={...row,_snapshotGeneration:'two'};
    const second=await context.load(url,secondCoordinate);
    assert.equal(second.spec_key,secondCoordinate.spec_key);
    assert.equal(second.talent_key,secondCoordinate.talent_key);
    assert.equal(second.scenario_key,secondCoordinate.scenario_key);
    assert.equal(requests,6);
    assert.deepEqual(received[5],expectedRequest(secondCoordinate));
    await context.load(url,secondCoordinate);
    assert.equal((await context.load(url,{...coordinate,_snapshotGeneration:'two'})).spec_key,'mage_fire');
    assert.equal(requests,6); // Both coordinate caches remain independently reusable.
    assert.equal(received.length,6);
    row={...row,scenario_key:'cleave'};
    const thirdCoordinate={...row,_snapshotGeneration:'two'};
    assert.equal((await context.load(url,thirdCoordinate)).scenario_key,'cleave');
    assert.equal(requests,7);
    assert.deepEqual(received, [
      ...Array.from({length:5},()=>expectedRequest(coordinate)),
      expectedRequest(secondCoordinate),expectedRequest(thirdCoordinate),
    ]);
    console.log('PASS: pending recovery, generation-keyed cache, publication race, exact coordinates');
  } finally { await new Promise(resolve=>server.close(resolve)); }
})().catch(error=>{console.error(error);process.exitCode=1;});
