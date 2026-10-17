// Execute the real detail loader against a loopback HTTP server.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const http = require('node:http');
(async () => {
  let mode = 'building', generation = 'one', requests = 0;
  const row = {spec_key:'mage_fire',talent_key:'profile:talent',scenario_key:'single',profile_detail:{identity:{name:'frozen'}}};
  const server = http.createServer((req,res) => {
    requests++;
    const rows = mode === 'building' ? [] : [{...row,spec_key:mode === 'wrong' ? 'warrior_fury' : row.spec_key}];
    res.setHeader('Content-Type','application/json');
    res.end(JSON.stringify({results:{coordinates:rows,snapshot:{state:mode === 'building' ? 'building' : 'ready',generation}}}));
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try {
    const source = fs.readFileSync(path.join(__dirname,'../../static/portal/js/simc-benchmarks.js'),'utf8');
    const startup='  document.addEventListener("DOMContentLoaded", loadBenchmarks);';
    const context=vm.createContext({Intl,URL,URLSearchParams,fetch});
    vm.runInContext(source.slice(0,source.indexOf(startup))+'globalThis.load=loadCoordinateDetail; globalThis.read=requestJson;})();',context);
    const url=`http://127.0.0.1:${server.address().port}/panel`;
    const coordinate={...row,_snapshotGeneration:'one'};
    await assert.rejects(context.load(url,coordinate),error=>error.snapshotPending===true);
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
    console.log('PASS: pending recovery, generation-keyed cache, publication race, exact coordinates');
  } finally { await new Promise(resolve=>server.close(resolve)); }
})().catch(error=>{console.error(error);process.exitCode=1;});
