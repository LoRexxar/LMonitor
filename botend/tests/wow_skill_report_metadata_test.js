const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const root = path.resolve(__dirname, '../..');
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/hotfix_relation_display.json'), 'utf8'));
const script = fs.readFileSync(path.join(root, 'static/portal/js/wow-skill-report-metadata.js'), 'utf8');

test('relation rendering preserves frozen changes without repeated empty placeholders', async t => {
  for (const mode of ['unresolved', 'mixed', 'resolved']) {
    await t.test(mode, async t => {
      const payload = structuredClone(fixture.metadata);
      // Synthetic positive/partial variants of the same frozen source coordinates.
      if (mode !== 'unresolved') payload.spells['428076'].effects.forEach((effect, i) => {
        if (mode === 'resolved' || i === 0) {
          effect.targets = [{id: 999001, name: 'Test A & B < C', url: 'https://example.test/spell/999001', icon_url: ''}];
          effect.resolved = true;
        }
      });
      const dom = new JSDOM(`<main class="skill-report" data-report-branch="wow" data-skill-report-metadata="/metadata/">${fixture.html}</main><input id="spellFilter">`, {url: 'https://example.test/', runScripts: 'outside-only'});
      t.after(() => dom.window.close());
      const w = dom.window, doc = w.document;
      await new Promise(resolve => w.addEventListener('load', resolve, {once: true}));
      const originalRows = [...doc.querySelectorAll('.impact-row')].map(row => row.outerHTML);
      const originalTitle = doc.querySelector('.spell-title').textContent;
      const requests = [];
      w.fetch = async url => { requests.push(url); return {ok: true, json: async () => payload}; };
      const ready = new Promise(resolve => doc.getElementById('spellFilter').addEventListener('input', resolve, {once: true}));
      w.eval(script);
      await Promise.race([ready, new Promise((_, reject) => {const timer=setTimeout(() => reject(new Error('metadata did not render')), 1500); timer.unref();})]);
      assert.deepEqual(requests, ['/metadata/']);
      assert.equal(doc.body.textContent.includes('字段事实见本卡片下方'), false);
      assert.equal(doc.body.textContent.includes('关联技能待解析'), false);
      const notices = doc.querySelectorAll('.spell-relation-status');
      const groups = doc.querySelectorAll('.spell-affected-skills');
      if (mode === 'unresolved') {
        assert.equal(doc.querySelector('.spell-title').textContent, originalTitle);
        assert.equal(groups.length, 0);
        assert.equal(notices.length, 1);
        assert.match(notices[0].textContent, /尚未核实/);
        assert.equal(doc.querySelector('.spell-title-row a').href, 'https://www.wowhead.com/spell=428076');
      } else {
        assert.equal(groups.length, mode === 'resolved' ? 4 : 1);
        assert.equal(notices.length, mode === 'resolved' ? 0 : 1);
        assert.equal(doc.querySelector('.spell-title').textContent, 'Test A & B < C');
        assert.equal(doc.querySelector('.spell-title c'), null);
      }
      const sourceBlock = doc.querySelector('.impact-block');
      assert.deepEqual([...sourceBlock.querySelectorAll('.impact-row')].map(row => row.outerHTML), originalRows);
      assert.equal(Boolean(sourceBlock.closest('details.source-facts')), mode === 'resolved');
      if (mode !== 'resolved') {
        assert.equal(doc.querySelectorAll('.impact-row').length, 4);
        assert.equal(sourceBlock.closest('details'), null);
      } else {
        for (const group of groups) {
          const rows = group.querySelectorAll('.impact-row');
          assert.equal(rows.length, 1);
          assert.ok(group.textContent.includes(rows[0].dataset.sourceBuild));
          assert.ok(group.textContent.includes(rows[0].dataset.sourcePush));
        }
      }
    });
  }
});
