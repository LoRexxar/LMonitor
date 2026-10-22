const {beforeMarker} = require('./vm_harness');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../static/dashboard/js/simc-benchmark-dashboard.js'), 'utf8');
const context = vm.createContext({});
const startup = "if(typeof module==='object'&&module.exports)";
vm.runInContext(beforeMarker(source, startup) + 'globalThis.check=configErrors;globalThis.metrics=metricsFor;globalThis.collect=collectCandidateCard;})();', context);
const candidate = params => ({params, is_enabled: true, spec_keys: []});
const payload = params => ({name:'equipment', interval_seconds:60, benchmark_type:'standard',
  specs:[{spec_key:'warrior_arms', apl_id:1, template_id:1, backend_id:1, is_enabled:true,
    profiles:[{talent_string_id:1, is_enabled:true}]}],
  scenarios:[{key:'st', name:'ST', is_enabled:true, simulation_params:{}}],
  candidates:[candidate(params)]});
const pair = 'main_hand=,id=268213,ilevel=334\ntrinket2=,id=270173,ilevel=334';
assert.equal(context.check(payload(pair)).length, 0);
assert.equal(context.metrics(payload(pair)).maxRuns, 3);
assert.equal(context.metrics(payload('trinket2=,id=270173,ilevel=334')).maxRuns, 2);
assert.ok(context.check(payload(pair + '\ntrinket2=,id=270175,ilevel=334')).some(e=>e.includes('部位无效或重复')));
assert.ok(context.check(payload('main_hand=,id=0,ilevel=334\ntrinket2=,id=270173,ilevel=334')).some(e=>e.includes('正整数')));
assert.ok(context.check(payload('=,id=268213,ilevel=334')).length);
const reference={owner_item_id:239649,contract_key:'a'.repeat(64)};
const conditionalParams={gear_swaps:[{slot:'neck',raw_value:'id=240950,ilevel=321'},{slot:'waist',raw_value:'id=239649,ilevel=321'}],conditional_comparison:reference};
const originalText='neck=id=240950,ilevel=321\nwaist=id=239649,ilevel=321';
let editedText=originalText;
const card={_candidateMeta:{params:conditionalParams,key:'conditional',originalEquipmentText:originalText},
  querySelector(selector){return selector.includes('candidate_input_mode')?{value:'equipment'}:{value:editedText};}};
const normalize=value=>JSON.parse(JSON.stringify(value));
assert.equal(context.collect(card,0)[0].key,'conditional');
assert.deepEqual(normalize(context.collect(card,0)[0].params),conditionalParams);
editedText=originalText.replaceAll('321','334');
assert.deepEqual(normalize(context.collect(card,0)[0].params.conditional_comparison),reference);
assert.ok(context.collect(card,0)[0].params.gear_swaps.every(row=>row.raw_value.includes('334')));
console.log('PASS equipment UI configuration and conditional-reference unchanged/edited roundtrip');

// Ordinary numeric editor: both persisted levels retain their own identity,
// not the coalesced card's first key or a newly generated candidate key.
const ordinaryParams={candidate_type:'gear_swap',is_base:false,
  gear_swap:{slot:'trinket1',item_id:270173,raw_value:',id=270173,ilevel=321,bonus_id=123',source:'manual'},
  simc_options:['midnight.crucible_of_erratic_energies_predation=1']};
const numericFields={candidate_input_mode:'quick',item_id:'270173',item_level:'321, 334'};
const numericCard={_candidateMeta:{key:'saved-321',keysByLevel:{321:'saved-321',334:'saved-334'},
  originalItemId:270173,originalItemLevel:321,slot:'trinket1',params:ordinaryParams},
  querySelector(selector){const name=selector.match(/^\[name="([^"]+)"\]$/)?.[1];
    assert.ok(name in numericFields,`unexpected field ${selector}`);return {value:numericFields[name]};}};
assert.deepEqual(normalize(context.collect(numericCard,0)).map(({key,params})=>({key,params})),[
  {key:'saved-321',params:ordinaryParams},
  {key:'saved-334',params:{...ordinaryParams,gear_swap:{...ordinaryParams.gear_swap,
    raw_value:',id=270173,ilevel=334,bonus_id=123'}}},
]);
console.log('PASS ordinary numeric equipment: exact per-level stable keys and hidden params');
