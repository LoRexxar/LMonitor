const {test} = require('node:test');
const assert = require('node:assert/strict');
const {beforeMarker, betweenMarkers, replaceMarker} = require('./vm_harness');
test('VM markers reject missing, ambiguous and reversed boundaries', () => {
    assert.throws(() => beforeMarker('source', 'absent'), /missing/);
    assert.throws(() => beforeMarker('xx', 'x'), /ambiguous/);
    assert.throws(() => betweenMarkers('end start', 'start', 'end'), /ordered/);
    assert.throws(() => beforeMarker('source', ''), /empty/);
    assert.equal(betweenMarkers('a START body END z', 'START', 'END'), 'START body ');
    assert.equal(replaceMarker('a MARK z', 'MARK', 'replacement'), 'a replacement z');
});
