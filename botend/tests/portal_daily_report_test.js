const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {readSource} = require('./vm_harness');

test('daily homepage exposes all sources without mixing news/cards/regions into one count', () => {
  const card = {innerHTML:''};
  const context = vm.createContext({URL, console, document:{addEventListener(){},
    getElementById(id){return id === 'featured-news-card' ? card : null;}}});
  vm.runInContext(readSource('static/portal/js/main.js'), context);
  const sections = ['news','events','updates','nga','videos','today','cutoffs','rio'].map((key,index)=>({key,label:key,count:index+1}));
  vm.runInContext(`PORTAL_STATE.dailyReport=${JSON.stringify({title:'日报',url:'/portal/reports/daily.html',sections})};renderDailyReportCard();`, context);
  for(const section of sections)assert.ok(card.innerHTML.includes(`<span>${section.label}</span>`), section.key);
  assert.ok(card.innerHTML.includes('8 个板块'));
  assert.ok(!card.innerHTML.includes('条汇总'));
  assert.ok(card.innerHTML.includes('最新日报'));
  vm.runInContext(`PORTAL_STATE.dailyReport=${JSON.stringify({title:'日报',url:'/portal/reports/daily.html?v=123',current_url:'/portal/daily-report/',sections})};renderDailyReportCard();`, context);
  assert.ok(card.innerHTML.includes('class="portal-daily-report-open" href="/portal/daily-report/"'));
  assert.ok(card.innerHTML.includes('class="portal-daily-report-title" href="/portal/reports/daily.html?v=123"'));
  assert.ok(card.innerHTML.includes('>当日日报</a>'));
});

test('Wednesday homepage identity follows explicit backend edition, not report weekday', () => {
  const card = {innerHTML:''};
  const context = vm.createContext({URL, console, document:{addEventListener(){},
    getElementById(id){return id === 'featured-news-card' ? card : null;}}});
  vm.runInContext(readSource('static/portal/js/main.js'), context);
  const report = {title:'11月11日魔兽世界周报', report_date:'2026-11-10',
    url:'/portal/reports/tuesday.html?v=123', current_url:'/portal/daily-report/',
    edition:{kind:'wednesday_weekly',publication_date:'2026-11-11',period_start:'2026-11-04',period_end:'2026-11-10'},
    sections:[{key:'rio',label:'数据周报',count:2},{key:'news',label:'新闻',count:3}]};
  const render = value => vm.runInContext(`PORTAL_STATE.dailyReport=${JSON.stringify(value)};renderDailyReportCard();`, context);
  render(report);
  assert.ok(card.innerHTML.includes('>周三周报</div>'));
  assert.ok(card.innerHTML.includes('>阅读周报</a>'));
  assert.ok(card.innerHTML.includes('2026-11-04—2026-11-10'));
  assert.ok(card.innerHTML.includes('<span>数据周报</span>'));
  assert.ok(card.innerHTML.includes('href="/portal/daily-report/"'));
  delete report.edition;
  render(report);
  assert.ok(card.innerHTML.includes('>最新日报</div>'));
  assert.ok(card.innerHTML.includes('>当日日报</a>'));
  assert.ok(!card.innerHTML.includes('2026-11-04—'));
  report.edition = {kind:'wednesday_weekly',period_start:'<img src=x>',period_end:'<script>x</script>'};
  render(report);
  assert.ok(!card.innerHTML.includes('<img'));
  assert.ok(!card.innerHTML.includes('<script>'));
  assert.ok(card.innerHTML.includes('&lt;img src=x&gt;—&lt;script&gt;x&lt;/script&gt;'));
});
