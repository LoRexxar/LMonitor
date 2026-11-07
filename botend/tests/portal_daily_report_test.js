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
