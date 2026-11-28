import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const src = new URL('../../../static/portal/js/home-recovery.js', import.meta.url);
function visit({href='https://example.org/?from=menu#news', healthy=false, online=true, hidden=false}={}) {
  const navigations = [], timers = new Map(), events = {};
  const document = {hidden, querySelector: () => healthy, addEventListener(k,f){events[k]=f;}};
  const navigator = {onLine:online};
  vm.runInNewContext(readFileSync(src,'utf8'), {document,navigator,URL,Date,
    location:{href,replace:u=>navigations.push(new URL(u))}, history:{replaceState(){}},
    addEventListener(k,f){events[k]=f;}, setTimeout(f,ms){timers.set(f,ms);return f;}, clearTimeout(f){timers.delete(f);}});
  return {navigations,document,events,timers,expire(){for(const [f,ms] of [...timers]) {assert.equal(ms,30000);timers.delete(f);f();}}};
}
test('stalled home retries once, preserving the original destination even without storage',()=>{
  const a=visit(); assert.equal(a.navigations.length,0); a.expire(); assert.equal(a.navigations.length,1);
  const [url]=a.navigations; assert.equal(url.origin,'https://example.org');assert.equal(url.searchParams.get('from'),'menu');assert.equal(url.hash,'#news');assert.ok(url.searchParams.has('home_retry'));
  const b=visit({href:url.href});b.expire();assert.equal(b.navigations.length,0);
});
test('healthy, hidden or offline home does not navigate',()=>{
  for(const options of [{healthy:true},{hidden:true},{online:false}]){const a=visit(options);a.expire();assert.equal(a.navigations.length,0);}
  const a=visit({hidden:true});a.document.hidden=false;a.events.visibilitychange();a.expire();assert.equal(a.navigations.length,1);
});
