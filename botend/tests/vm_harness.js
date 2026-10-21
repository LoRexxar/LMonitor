// Fail closed when a production entry point moves or becomes ambiguous.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const readSource = file => fs.readFileSync(path.resolve(__dirname, '../..', file), 'utf8');
function markerIndex(source, marker) {
    assert.ok(marker, 'marker must not be empty');
    const index = source.indexOf(marker);
    assert.ok(index >= 0, `missing source marker: ${marker}`);
    assert.equal(source.indexOf(marker, index + marker.length), -1, `ambiguous source marker: ${marker}`);
    return index;
}
function replaceMarker(source, marker, replacement) {
    const index = markerIndex(source, marker);
    return source.slice(0, index) + replacement + source.slice(index + marker.length);
}
function beforeMarker(source, marker) { return source.slice(0, markerIndex(source, marker)); }
function betweenMarkers(source, start, end) {
    const first = markerIndex(source, start), last = markerIndex(source, end);
    assert.ok(last > first, 'source markers must be ordered');
    return source.slice(first, last);
}
function skillRenderer() {
    const elements = new Map();
    function element(id) {
        if (!elements.has(id)) elements.set(id, {value:'', dataset:{}, innerHTML:'', textContent:'',
            classList:{add(){}, remove(){}}, setAttribute(){}});
        return elements.get(id);
    }
    element('simc-skill-damage-spec').value = 'warrior:fury';
    element('simc-skill-damage-hero-tree').value = '60';
    const target = {dataset:{targetCount:'1'}, getAttribute:()=> 'true'};
    const context = vm.createContext({document:{getElementById:element, querySelectorAll:()=>[target]},
        escapeHtml:value=>String(value).replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;')});
    vm.runInContext(betweenMarkers(readSource('static/dashboard/js/main.js'),
        'function renderSimcSkillIdentity(', 'function initSimcSkillDamagePanel('), context);
    return {context, element, elements, target};
}
function gearRenderer(expose, globals = {}) {
    const context = vm.createContext({document:{querySelector:()=>({dataset:{}}), getElementById:()=>({})}, ...globals});
    vm.runInContext(readSource('static/shared/js/wow-gear-sources.js'), context);
    vm.runInContext(replaceMarker(readSource('static/portal/js/gear_builder.js'), '  initialize();', expose), context);
    return context;
}
module.exports = {readSource, markerIndex, replaceMarker, beforeMarker, betweenMarkers, skillRenderer, gearRenderer};
