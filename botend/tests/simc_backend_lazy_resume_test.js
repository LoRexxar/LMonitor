const assert = require('node:assert/strict');
const {test} = require('node:test');
const {JSDOM} = require('jsdom');
const {readSource} = require('./vm_harness');
const tick = () => new Promise(resolve => setImmediate(resolve));

test('backend re-entry refreshes completed build after hidden polling stops', async t => {
  const dom = new JSDOM(`<section id="simc-advanced" class="content-section active" data-simc-page="advanced"><div data-simc-panel="backend"><button id="simc-compile-submit"></button><button id="simc-compile-check"></button><input id="simc-compile-threads"><input id="simc-compile-no-pull"><div id="simc-upload-result"></div></div></section><section id="simc-history" class="content-section" data-simc-page="history"><div data-simc-panel="tasks"></div></section>`, {runScripts:'outside-only', url:'https://example.test/dashboard/'});
  t.after(() => dom.window.close());
  const w = dom.window, add = w.document.addEventListener.bind(w.document);
  w.document.addEventListener = (type,...args) => { if (type !== 'DOMContentLoaded') add(type,...args); };
  const intervals = new Map(); let sequence = 0, requests = 0;
  w.setInterval = fn => {intervals.set(++sequence,fn); return sequence;};
  w.clearInterval = id => intervals.delete(id);
  w.fetch = async url => {
    assert.equal(url,'/api/simc-backend-binary/'); requests++;
    return {ok:true,json:async () => ({success:true,data:{is_updating:requests===1,update_status:requests===1?'building':'ready'}})};
  };
  w.eval(readSource('static/dashboard/js/main.js'));
  w.switchSimcWorkbenchL1Tab('advanced','backend'); await tick();
  assert.equal(requests,1);
  assert.equal(w.document.getElementById('simc-compile-submit').disabled,true);
  assert.equal(intervals.size,1);
  w.switchSimcWorkbenchL1Tab('history','tasks');
  for (const fn of [...intervals.values()]) await fn();
  assert.equal(intervals.size,0);
  assert.equal(requests,1);
  w.switchSimcWorkbenchL1Tab('advanced','backend'); await tick();
  assert.equal(requests,2,'re-entering must read the current backend status');
  assert.equal(w.document.getElementById('simc-compile-submit').disabled,false);
  assert.equal(intervals.size,0);
});
