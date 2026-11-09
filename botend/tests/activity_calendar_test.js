const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const source = fs.readFileSync(path.resolve(__dirname, '../../static/portal/js/main.js'), 'utf8');
// Frozen real API sample; no source-of-truth data file or external request.
const real = {title:'时空漫游地下城活动', url:'https://wago.tools/db2/Holidays?build=12.1.7.70740&locale=zhCN&filter%5BID%5D=exact%3A1065', source:'db2_holidays', tag:'日历活动', status:'已结束', start_at:'2026-10-29 08:00:00', end_at:'2026-11-05 07:00:00', summary:'在此活动期间，10级及以上玩家可以加入特殊的随机时空漫游地下城队列，将地下城与奖励调整到与玩家相同的等级水平，重温“德拉诺之王”内容更新的地下城。'};
function setup(items = [real]) {
  let focus;
  const controls = [];
  const el = {html:'', set innerHTML(v) {
    this.html = v; controls.length = 0;
    for (const m of v.matchAll(/<button\b([^>]*)>/g)) {
      const attrs = Object.fromEntries([...m[1].matchAll(/([\w-]+)="([^"]*)"/g)].map(a => [a[1], a[2]]));
      controls.push({dataset:{calDay:attrs['data-cal-day'], calAction:attrs['data-cal-action']}, disabled:/\sdisabled(?:\s|$)/.test(m[1]), attrs,
        focus(opts) { focus = {attrs, opts}; }});
    }
  }, get innerHTML() { return this.html; }, querySelectorAll() { return controls; }, querySelector(selector) {
    const m = selector.match(/\[([^=]+)="([^"]*)"\]/); return controls.find(b => m && b.attrs[m[1]] === m[2]) || null;
  }};
  const context = vm.createContext({console, URL, Intl,
    Date:class extends Date { constructor(...args) { super(...(args.length ? args : ['2026-11-09T04:00:00Z'])); } },
    fetch() { throw Error('calendar must not fetch'); },
    document:{addEventListener(){}, getElementById(id) { return id === 'events-list' ? el : null; }}});
  vm.runInContext(source, context);
  context.renderEvents(items);
  return {context, el, controls, state:() => vm.runInContext('PORTAL_ACTIVITY_STATE', context), focus:() => focus,
    query(q) { context.q = q; vm.runInContext('PORTAL_STATE.query = q', context); context.renderEvents(items); },
    click(attr, value) { const b = controls.find(c => c.attrs[attr] === value); assert.ok(b, value); assert.equal(b.disabled,false); b.onclick(); },
    key(day, key) { const b = controls.find(c => c.attrs['data-cal-day'] === day); assert.ok(b); b.onkeydown({key, preventDefault(){}}); }};
}
test('default today, real cross-month occurrence and original seconds/description, safe gate', () => {
  const s = setup(); assert.equal(s.state().selectedDay,'2026-11-09');
  s.click('data-cal-day','2026-11-01');
  assert.match(s.el.innerHTML,/时空漫游地下城活动/); assert.match(s.el.innerHTML,/2026-10-29 08:00:00/); assert.match(s.el.innerHTML,/2026-11-05 07:00:00/);
  assert.match(s.el.innerHTML,/活动说明/); assert.match(s.el.innerHTML,/重温“德拉诺之王”/); assert.doesNotMatch(s.el.innerHTML,/href="https:\/\/wago/);
  assert.match(s.el.innerHTML,/aria-current="date"/); assert.match(s.el.innerHTML,/aria-pressed="true"/);
});
test('month navigation, today, adjacent month selection, focus and boundary', () => {
  const s = setup(); s.click('data-cal-action','prev'); assert.equal(s.state().viewMonth,'2026-10');
  assert.equal(s.controls.find(c=>c.dataset.calAction==='prev').disabled,true);
  assert.equal(s.focus().opts.preventScroll,true);
  s.click('data-cal-day','2026-11-01'); assert.equal(s.state().viewMonth,'2026-11');
  s.click('data-cal-action','today'); assert.equal(s.state().selectedDay,'2026-11-09');
  assert.equal(s.controls.find(c=>c.dataset.calAction==='next').disabled,true);
});
test('keyboard day/week and Home/End navigation', () => {
  const s=setup(); s.key('2026-11-09','ArrowRight'); assert.equal(s.state().selectedDay,'2026-11-10');
  s.key('2026-11-10','ArrowDown'); assert.equal(s.state().selectedDay,'2026-11-17');
  s.key('2026-11-17','Home'); assert.equal(s.state().selectedDay,'2026-11-16');
  s.key('2026-11-16','End'); assert.equal(s.state().selectedDay,'2026-11-22');
  assert.equal(s.focus().attrs['data-cal-day'],'2026-11-22');
});
test('Monday grid has exactly 4, 5 or 6 complete weeks', () => {
  const s=setup(); for(const [month, count] of [['2021-02',28],['2026-11',42],['2026-04',35]]) {
    const days=s.context.activityCalendarGrid(month); assert.equal(days.length,count);
    assert.equal(new Date(days[0]+'T00:00:00Z').getUTCDay(),1);
    assert.equal(new Date(days.at(-1)+'T00:00:00Z').getUTCDay(),0);
  }
});
test('all concurrent records and repeated titles remain discoverable, API status not recomputed', () => {
  const items=Array.from({length:14},(_,i)=>({...real,title:'同名活动',start_at:`2026-11-09 08:00:${String(i).padStart(2,'0')}`,end_at:'',status:i===0?'未知 <状态>':'已结束'}));
  items.push({...real,title:'同名活动',start_at:'2026-12-01 09:00:00',end_at:''});
  const s=setup(items); assert.match(s.el.innerHTML,/14 项/); assert.equal((s.el.innerHTML.match(/class="portal-monthly-event"/g)||[]).length,28);
  assert.match(s.el.innerHTML,/未知 &lt;状态&gt;/); assert.match(s.el.innerHTML,/08:00:13/);
  s.click('data-cal-action','next'); assert.match(s.el.innerHTML,/本月全部活动（1）/); assert.match(s.el.innerHTML,/2026-12-01 09:00:00/);
});
test('search changes selection, unmatched state and clear restore today and available months', () => {
  const s=setup([real,{...real,title:'未来独特',start_at:'2026-12-12 12:34:56',end_at:''}]);
  s.query('未来独特'); assert.equal(s.state().selectedDay,'2026-12-12'); assert.match(s.el.innerHTML,/未来独特/);
  s.query('not-found'); assert.match(s.el.innerHTML,/无匹配活动/);
  s.query(''); assert.equal(s.state().selectedDay,'2026-11-09'); s.click('data-cal-action','next'); assert.equal(s.state().viewMonth,'2026-12');
});
test('empty data/day, invalid times, missing/reversed ends are honest and single-day', () => {
  assert.match(setup([]).el.innerHTML,/暂无活动数据/);
  const s=setup([{...real,start_at:'2026-11-09 08:00:00',end_at:'2026-11-08 23:00:00'}, {...real,start_at:'2026-11-10 08:00:00',end_at:''}]);
  s.click('data-cal-day','2026-11-08'); assert.match(s.el.innerHTML,/已收录活动中，当天暂无安排/);
  assert.equal(s.context.activityCalendarTime('2026-02-30 08:00:00'),null);
  assert.equal(s.context.activityCalendarTime('2026-11-09 25:00:00'),null);
  assert.equal(s.context.activityCalendarTime('2026-02-30T08:00:00Z'),null);
});
test('titles and descriptions escaped; details outside safe link; all link gates preserved', () => {
  const s=setup([{...real,title:'<img onerror="alert(1)">',summary:'<script>bad</script>\nFull description',url:'https://wowhead.com/event=1',start_at:'2026-11-09 08:00:00'}]);
  assert.doesNotMatch(s.el.innerHTML,/<img|<script>/); assert.match(s.el.innerHTML,/&lt;script&gt;/); assert.match(s.el.innerHTML,/<details class="portal-monthly-description">/); for (const link of s.el.innerHTML.matchAll(/<a\b[^>]*>[\s\S]*?<\/a>/g)) assert.doesNotMatch(link[0], /<details|<summary|<button/);
  for(const url of ['https://wago.tools/db2/Holidays','javascript:alert(1)','https://wowhead.com.evil.test/','//evil.test/']) assert.equal(s.context.getPortalEventHref({url}), '');
  assert.equal(s.context.getPortalEventHref({url:'/portal/event/1'}),'/portal/event/1');
});
test('civil dates, DST and aware offsets are host-timezone independent', () => {
  const snippet = `const vm=require('node:vm'),fs=require('node:fs');const c=vm.createContext({URL,document:{addEventListener(){},getElementById(){return null}}});vm.runInContext(fs.readFileSync(${JSON.stringify(path.resolve(__dirname,'../../static/portal/js/main.js'))},'utf8'),c);console.log(JSON.stringify(['2026-03-08 02:30:00','2026-11-01 01:30:00','2026-11-08T23:30:12-08:00','2026-11-09T00:30:12+09:00'].map(x=>c.activityCalendarTime(x))))`;
  const results=['UTC','America/Los_Angeles','Asia/Shanghai'].map(TZ=>execFileSync(process.execPath,['-e',snippet],{env:{...process.env,TZ},encoding:'utf8'}));
  assert.equal(new Set(results).size,1); const parsed=JSON.parse(results[0]); assert.equal(parsed[2].day,'2026-11-09'); assert.equal(parsed[3].day,'2026-11-08');
});

test('event links reject browser-stripped controls before relative URL approval', () => {
  const s=setup();
  for(const c of ['\t','\n','\r','\u0000','\u007f']) assert.equal(s.context.getPortalEventHref({url:'/'+c+'/evil.test/'}),'');
  assert.equal(s.context.getPortalEventHref({url:'/portal/event/1?x=2'}),'/portal/event/1?x=2');
});
test('long source intervals render within a fixed execution budget and navigate lazily', () => {
  const s=setup();s.context.longItems=[{...real,end_at:'9999-12-31 23:59:59'}];
  assert.doesNotThrow(()=>vm.runInContext('renderEvents(longItems)',s.context,{timeout:500}));
  assert.match(s.el.innerHTML,/9999-12-31 23:59:59/);
  s.click('data-cal-action','next');assert.equal(s.state().viewMonth,'2026-12');
  s.click('data-cal-action','prev');assert.equal(s.state().viewMonth,'2026-11');
});
