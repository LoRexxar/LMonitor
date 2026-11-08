const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {readSource, beforeMarker, replaceMarker} = require('./vm_harness');

// Real production renderers in Node VM with a minimal DOM stub, not a browser.
function dom() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      innerHTML: '', textContent: '', setAttribute() {}, querySelectorAll: () => [],
    });
    return elements.get(id);
  };
  return {element, document: {getElementById: element, addEventListener() {}}};
}

const articleInline = readSource('templates/portal/article.html').match(/<script>\s*([\s\S]*?)<\/script>/)?.[1];
assert.ok(articleInline, 'article detail inline renderer must exist');
const articleScript = replaceMarker(replaceMarker(articleInline,
  '{{ article_id|default:"null" }}', '42'), '\n        loadArticle();', '');

const renderers = {
  legacy: async item => {
    const {element, document} = dom();
    const context = vm.createContext({document, console, URL, Date});
    vm.runInContext(readSource('static/portal/js/main.js'), context);
    context.renderSimpleList('blueposts-list', [item]);
    return element('blueposts-list').innerHTML;
  },
  home: async item => {
    const {element, document} = dom();
    const context = vm.createContext({document, console, URL, Date, item});
    vm.runInContext(readSource('static/portal/js/main.js'), context);
    vm.runInContext(`PORTAL_STATE.homeNews.status.blueposts = 'ready';
      PORTAL_STATE.dataBySection.blueposts = [item]; renderHomeNews('blueposts');`, context);
    return element('blueposts-list').innerHTML;
  },
  news: async item => {
    const {element, document} = dom();
    const context = vm.createContext({document, console, URL, URLSearchParams});
    vm.runInContext(beforeMarker(readSource('static/portal/js/news_index.js'),
      "if (new URLSearchParams(window.location.search).get('tab') === 'hotfix')"), context);
    context.renderNewsList([item]);
    return element('news-list').innerHTML;
  },
  article: async item => {
    const {element, document} = dom();
    const requests = [];
    const context = vm.createContext({document, console, fetch: async url => {
      requests.push(url);
      return {ok: true, json: async () => ({status: 'success', data: item})};
    }});
    vm.runInContext(articleScript, context);
    await context.loadArticle();
    assert.deepEqual(requests, ['/portal/api/article/42/']);
    assert.equal(element('page-title').textContent, `${item.title_cn || item.title} - 魔兽世界每日节奏`);
    assert.match(element('article-container').innerHTML, /<p>完整原文正文<\/p>/);
    return element('article-container').innerHTML;
  },
};

function article(overrides = {}) {
  return {id: 42, source: 'blizzard_tracker', title: 'Original PTR title', title_cn: '原中文标题',
    url: 'https://example.invalid/forums/thread/42', article_url: '/portal/article/42/',
    author: '原作者', publish_time: '2026-11-08 08:09:10', category: '原分类',
    content_blocks: [{type: 'paragraph', text: '完整原文正文'}], ...overrides};
}

for (const [surface, render] of Object.entries(renderers)) {
  test(`${surface}: shows each backend label in metadata without changing title/source/links/body`, async () => {
    // These are API sample pairs, not a frontend classification rule.
    for (const [game_version, game_version_label] of [
      ['retail', '正式服'], ['forever', '无限服'], ['test', '测试服'],
    ]) {
      const plain = article();
      const row = {...plain, game_version, game_version_label,
        forum_category: {id: 123, name: '真实来源板块', url: 'https://example.invalid/forums/category'}};
      const original = JSON.stringify(row);
      const html = await render(row);
      const badge = html.match(/<span\b[^>]*>\[(正式服|无限服|测试服)\]<\/span>/g) || [];
      assert.equal(badge.length, 1, 'one metadata badge, not a title prefix');
      assert.ok(badge[0].includes(`[${game_version_label}]`));
      assert.ok(badge[0].includes('bluepost-channel-badge'));
      assert.ok(badge[0].includes(`data-game-version="${game_version}"`));
      assert.ok(badge[0].includes('title="真实来源板块"'));
      assert.equal(html.replace(badge[0], ''), await render(plain), 'all original presentation remains intact');
      assert.equal(JSON.stringify(row), original, 'renderer must not rewrite payload');
    }
  });

  test(`${surface}: absent/empty labels and version codes alone never infer a badge`, async () => {
    const baseline = await render(article());
    for (const fields of [{game_version: 'test'}, {game_version_label: ''},
      {game_version_label: null}, {game_version_label: undefined},
      {game_version: 'forever', forum_category: {name: 'PTR 正式服 无限服'}}]) {
      assert.equal(await render(article(fields)), baseline);
    }
  });

  test(`${surface}: unrelated sources keep their existing titles and cannot inject a channel label`, async () => {
    for (const source of ['wowhead', 'exwind', 'blizzard_cn', 'nga', 'unknown']) {
      const plain = article({source, title_cn: '[无限] 既有标题'});
      assert.equal(await render({...plain, game_version: 'retail', game_version_label: '正式服',
        forum_category: {name: '错误渠道'}}), await render(plain));
    }
  });

  test(`${surface}: backend label is escaped verbatim, including labels outside the sample set`, async () => {
    const html = await render(article({game_version: '', game_version_label: '权威<&"标签>',
      forum_category: {name: '板块"><img src=x onerror=alert(1)>&'}}));
    assert.ok(html.includes('[权威&lt;&amp;&quot;标签&gt;]'));
    assert.ok(html.includes('title="板块&quot;&gt;&lt;img src=x onerror=alert(1)&gt;&amp;"'));
    assert.doesNotMatch(html, /<img|title="板块">/);
  });

  test(`${surface}: optional category does not create a fake tooltip`, async () => {
    for (const forum_category of [undefined, null, {}, {name: ''}]) {
      const html = await render(article({game_version_label: '测试服', forum_category}));
      const badge = html.match(/<span\b[^>]*>\[测试服\]<\/span>/)?.[0];
      assert.ok(badge, 'backend label still appears without category');
      assert.doesNotMatch(badge, /\btitle=/);
    }
  });
}


test('today strip and search use backend channel text without rewriting titles or inferring from names', () => {
  const {document} = dom();
  const context = vm.createContext({document, console, URL, Date});
  vm.runInContext(readSource('static/portal/js/main.js'), context);
  const item = article({game_version_label: '无限服', forum_category: {name: '真实板块'}});
  assert.equal(context.getItemTitle(item), '[无限服] ' + item.title);
  assert.equal(context.matchItem(item, '无限服'), true);
  assert.equal(context.matchItem(item, '真实板块'), true);
  assert.equal(context.getItemTitle(article({game_version: 'test'})), article().title);
  assert.equal(context.getItemTitle({...item, source: 'wowhead'}), item.title);
  assert.equal(context.matchItem({...item, source: 'wowhead'}, '无限服'), false);
});


test('today strip has a separate color badge, keeps title text, and does not color unrelated sources', () => {
  const {document} = dom();
  const context = vm.createContext({document, console, URL, Date});
  vm.runInContext(readSource('static/portal/js/main.js'), context);
  const item = article({game_version: 'forever', game_version_label: '无限服'});
  const html = context.makeTodayChip('蓝帖速递', item.title, 'section-news', '', '', item);
  assert.match(html, /class="bluepost-channel-badge" data-game-version="forever"/);
  assert.equal((html.match(/\[无限服\]/g) || []).length, 1);
  assert.ok(html.includes(item.title));
  assert.equal(context.renderBluepostChannelBadge({...item, source: 'wowhead'}), '');
});

for (const [surface, render] of Object.entries(renderers)) {
  test(`${surface}: version style key cannot inject attributes`, async () => {
    const html = await render(article({game_version: 'retail" onmouseover="bad()', game_version_label: '测试服'}));
    assert.ok(html.includes('data-game-version="retail&quot; onmouseover=&quot;bad()"'));
    assert.doesNotMatch(html, / onmouseover="/);
  });
}


test('all four pages load the single shared palette, with distinct accessible light/dark colors', () => {
  for (const path of ['templates/portal/index.html', 'templates/portal/news.html',
    'templates/portal/article.html', 'templates/dashboard/index.html']) {
    assert.ok(readSource(path).includes("shared/css/bluepost-channel.css"));
  }
  const css = readSource('static/shared/css/bluepost-channel.css');
  const colors = {};
  const luminance = hex => {
    const rgb = hex.slice(1).match(/../g).map(n => parseInt(n,16)/255)
      .map(n => n <= 0.04045 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4);
    return rgb[0]*0.2126 + rgb[1]*0.7152 + rgb[2]*0.0722;
  };
  for (const match of css.matchAll(/\.bluepost-channel-badge\[data-game-version="(retail|forever|test)"\]\s*\{([^}]+)\}/g)) {
    const fg = match[2].match(/^\s*color:\s*(#[a-f0-9]+);/m)[1];
    const bg = match[2].match(/background-color:\s*(#[a-f0-9]+);/)[1];
    const a = luminance(fg), b = luminance(bg);
    assert.ok((Math.max(a,b)+0.05)/(Math.min(a,b)+0.05) >= 4.5);
    (colors[match[1]] ||= []).push([fg,bg]);
  }
  assert.deepEqual(Object.keys(colors).sort(), ['forever','retail','test']);
  for (const palette of Object.values(colors)) assert.equal(palette.length, 2);
  for (let theme=0; theme<2; theme++) {
    assert.equal(new Set(Object.values(colors).map(p => p[theme][0])).size, 3);
    assert.equal(new Set(Object.values(colors).map(p => p[theme][1])).size, 3);
  }
});
