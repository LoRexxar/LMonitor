(() => {
  const lootForm = document.querySelector('.journal-loot-filters');
  const filterData = JSON.parse(document.getElementById('journal-loot-filter-data')?.textContent || 'null');
  const snapshot = JSON.parse(document.getElementById('journal-loot-snapshot')?.textContent || 'null');
  const catalogForm = document.querySelector('[data-catalog-filter]');
  const catalogData = JSON.parse(document.getElementById('journal-catalog-filter-data')?.textContent || 'null');
  const skillData = JSON.parse(document.getElementById('journal-skill-filter-data')?.textContent || 'null');
  document.querySelectorAll('form[data-auto-filter]').forEach(form => {
    if (form === lootForm && filterData && snapshot?.state === 'ready') return;
    if (form === catalogForm && catalogData) return;
    form.querySelectorAll('select').forEach(select => select.addEventListener('change', () => {
      if (select.name === 'role' && skillData) return;
      if (select.name === 'class' && form.elements.spec) form.elements.spec.value = '';
      form.requestSubmit();
    }));
    form.querySelectorAll('input[type="search"]').forEach(input => {
      input.addEventListener('search', () => form.requestSubmit());
    });
    form.addEventListener('submit', () => {
      if (document.body.dataset.journalInstance) {
        try {
          sessionStorage.setItem('journal-filter-scroll', JSON.stringify({path: location.pathname, y: scrollY}));
        } catch (_) { /* 存储限制不影响表单提交。 */ }
      }
    });
  });
  try {
    const saved = JSON.parse(sessionStorage.getItem('journal-filter-scroll') || 'null');
    sessionStorage.removeItem('journal-filter-scroll');
    if (saved?.path === location.pathname) requestAnimationFrame(() => scrollTo(0, saved.y));
  } catch (_) { /* 浏览器禁用存储时仍可正常筛选。 */ }

  if (catalogForm && catalogData) {
    const cards = new Map(Array.from(document.querySelectorAll('[data-journal-id]')).map(card => [Number(card.dataset.journalId), card]));
    const apply = () => {
      const filters = Object.fromEntries(['q', 'tier', 'kind'].map(key => [key, catalogForm.elements[key].value]));
      let count = 0;
      catalogData.rows.forEach(row => {
        const visible = JournalCatalogMatches(row, filters);
        cards.get(row.id).hidden = !visible;
        if (visible) count++;
      });
      const label = document.querySelector('[data-catalog-count]');
      if (label) label.textContent = count;
      document.querySelector('[data-catalog-empty]').hidden = count > 0;
      const url = new URL(location.href);
      ['q', 'tier', 'kind'].forEach(key => {
        // 空资料片必须显式保留，刷新时才不会重新选回默认赛季。
        if (key === 'tier' || filters[key]) url.searchParams.set(key, filters[key]);
        else url.searchParams.delete(key);
      });
      history.replaceState(null, '', url);
    };
    catalogForm.addEventListener('submit', event => { event.preventDefault(); apply(); });
    catalogForm.addEventListener('change', apply);
    catalogForm.elements.q.addEventListener('input', apply);
    apply();
  }

  if (skillData) {
    const form = document.querySelector('.journal-battle-filters');
    const escape = value => String(value || '').replaceAll('&', '&amp;').replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
    const lines = value => escape(value).replace(/\r\n|\r|\n/g, '<br>');
    const renderSkill = row => `<details class="journal-skill" ${row.children.length ? 'open' : ''} id="skill-${Number(row.id)}">
      <summary><span>${escape(row.title || '技能说明')}</span><span class="journal-skill-tags">${[...row.role_names, ...(row.tags || [])].map(tag => `<em>${escape(tag)}</em>`).join('')}</span></summary>
      <div class="journal-skill-body">${row.text ? `<p>${lines(row.text)}</p>` : ''}
      ${row.spell_id ? `<a data-tooltip-kind="spell" data-tooltip-id="${Number(row.spell_id)}" class="journal-spell-link" href="https://www.wowhead.com/cn/spell=${Number(row.spell_id)}" target="_blank" rel="noopener">查看技能详情 ↗</a>` : ''}
      ${row.children.map(renderSkill).join('')}</div></details>`;
    const apply = () => {
      const role = form.elements.role.value;
      const boss = JournalSkillSelection(skillData, role);
      document.querySelector('[data-journal-roles]').innerHTML = boss.roles.length ? boss.roles.map(row =>
        `<section class="journal-role-card"><h3>${escape(row.title)}</h3><p>${lines(row.text)}</p></section>`).join('') :
        '<p class="journal-muted">游戏手册未提供当前难度／职责的专属提示，请结合下方技能说明安排应对。</p>';
      document.querySelector('[data-journal-skills]').innerHTML = boss.skills.map(renderSkill).join('') ||
        '<div class="journal-empty"><p>游戏手册暂无此首领在当前筛选条件下的技能说明。</p></div>';
      document.querySelector('[data-journal-dynamic]').hidden = !boss.has_dynamic;
      document.getElementById('journal-expand').textContent = '展开全部';
      const url = new URL(location.href);
      if (role) url.searchParams.set('role', role); else url.searchParams.delete('role');
      history.replaceState(null, '', url);
      document.querySelectorAll('input[name="role"]').forEach(input => { input.value = role; });
      document.querySelectorAll('.journal-sidebar a[href*="?"]').forEach(link => {
        const target = new URL(link.href);
        if (role) target.searchParams.set('role', role); else target.searchParams.delete('role');
        link.href = target.pathname + target.search;
      });
    };
    form.elements.role.addEventListener('change', apply);
    form.addEventListener('submit', event => {
      if (form.elements.difficulty.value === document.body.dataset.journalDifficulty) {
        event.preventDefault(); apply();
      }
    });
    apply();
  }

  const itemRequests = new Map();
  const emphasizeValues = parent => {
    parent.querySelectorAll('p').forEach(paragraph => {
      const pieces = paragraph.textContent.split(/(\d[\d,.]*%?)/g);
      paragraph.replaceChildren(...pieces.map((text, index) => {
        if (index % 2 === 0) return document.createTextNode(text);
        const strong = document.createElement('strong');
        strong.textContent = text;
        return strong;
      }));
    });
  };
  document.querySelectorAll('[data-item-effects]').forEach(emphasizeValues);
  const fetchDetails = (kind, id) => {
    const key = `${kind}:${id}`;
    if (!itemRequests.has(key)) {
      const {journalInstance: instance, journalBoss: boss, journalDifficulty: difficulty} = document.body.dataset;
      itemRequests.set(key, fetch(`/portal/api/adventure-journal/${instance}/tooltip/${kind}/${id}/?${kind === 'spell' ? `boss=${boss}&` : ''}difficulty=${difficulty}`)
        .then(async response => {
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || '来源暂不可用');
          return data;
        }).catch(error => { itemRequests.delete(key); throw error; }));
    }
    return itemRequests.get(key);
  };
  let lootLoading = false;
  const loadLoot = () => {
    if (lootLoading) return;
    const queue = Array.from(document.querySelectorAll('[data-loot-id][data-details-loaded="false"]')).filter(row => !row.hidden);
    if (!queue.length) return;
    lootLoading = true;
    queue.forEach(row => { row.dataset.detailsLoaded = 'loading'; });
    const worker = async () => {
      while (queue.length) {
        const row = queue.shift();
        const stats = row.querySelector('[data-item-stats]');
        const effects = row.querySelector('[data-item-effects]');
        try {
          const data = await fetchDetails('item', row.dataset.lootId);
          row.querySelector('.journal-loot-link').dataset.wowItemTooltip = [...(data.lines || []), data.note || ''].join('\n');
          if (data.icon && !row.querySelector('.journal-loot-symbol img')) {
            const icon = document.createElement('img');
            icon.src = data.icon;
            icon.alt = '';
            icon.addEventListener('error', () => { icon.hidden = true; });
            row.querySelector('.journal-loot-symbol').append(icon);
          }
          if (!data.complete) {
            if (data.status === 'not_equipment') {
              stats.textContent = '非装备掉落';
              effects.textContent = '无装备属性或特效，可点击名称查看基础资料。';
            } else if (data.status === 'basic') {
              stats.textContent = '基础资料已同步';
              effects.textContent = '无可展示的装备数值，可点击名称查看基础资料。';
            } else {
              throw new Error('补充资料暂不可用');
            }
            row.dataset.detailsLoaded = data.status;
            continue;
          }
          stats.replaceChildren();
          effects.replaceChildren();
          const append = (parent, tag, text) => {
            const element = document.createElement(tag);
            element.textContent = text;
            parent.append(element);
          };
          if (data.item_level) append(stats, 'strong', `装等 ${data.item_level}`);
          (data.stats?.length ? data.stats : ['无基础属性']).forEach(text => append(stats, 'span', text));
          (data.effects || []).forEach(text => append(effects, 'p', text));
          emphasizeValues(effects);
          row.dataset.detailsLoaded = 'true';
        } catch (_) {
          stats.textContent = '属性暂不可用';
          effects.textContent = '特效资料暂不可用，可点击装备名称重试。';
          row.querySelector('.journal-loot-link').dataset.wowItemTooltip = '属性详情暂时不可用，可点击装备名称重试。';
        }
      }
    };
    Promise.all(Array.from({length: 4}, worker)).finally(() => { lootLoading = false; loadLoot(); });
  };
  if (lootForm && filterData && snapshot?.state === 'ready') {
    const rows = Array.from(document.querySelectorAll('[data-loot-id]'));
    const keys = ['slot', 'class', 'spec', 'item_type', 'loot_boss', 'loot_q'];
    const apply = () => {
      const filters = Object.fromEntries(keys.map(key => [key, lootForm.elements[key]?.value || '']));
      let count = 0;
      filterData.rows.forEach((item, index) => {
        const visible = JournalLootFilter(item, filters);
        rows[index].hidden = !visible;
        if (visible) count++;
      });
      document.querySelector('[data-loot-count]').textContent = `${count} 件匹配物品 · ${filters.spec ? '已按拾取专精筛选。' : '选择职业和拾取专精，查看对应掉落。'}`;
      document.querySelector('[data-loot-empty]').hidden = count > 0;
      const url = new URL(location.href);
      keys.forEach(key => { if (filters[key]) url.searchParams.set(key, filters[key]); else url.searchParams.delete(key); });
      history.replaceState(null, '', url);
      // 切换难度或战斗指南时保留浏览器当前选择。
      document.querySelectorAll('.journal-battle-filters input').forEach(input => {
        if (keys.includes(input.name)) input.value = filters[input.name];
      });
      document.querySelectorAll('.journal-sidebar a[href*="?"]').forEach(link => {
        const target = new URL(link.href);
        keys.forEach(key => { if (filters[key]) target.searchParams.set(key, filters[key]); else target.searchParams.delete(key); });
        link.href = target.pathname + target.search;
      });
      loadLoot();
    };
    lootForm.addEventListener('submit', event => { event.preventDefault(); apply(); });
    lootForm.addEventListener('change', event => {
      if (event.target.name === 'class') {
        const options = filterData.specs[event.target.value] || [];
        const select = lootForm.elements.spec;
        select.replaceChildren(new Option(event.target.value ? '全部专精' : '请先选择职业', ''), ...options.map(spec => new Option(spec.name, spec.id)));
        select.disabled = !event.target.value;
      }
      apply();
    });
    lootForm.elements.loot_q.addEventListener('input', apply);
    apply();
  } else if (snapshot?.state === 'building') {
    // 仅轻量轮询准备状态；数据就绪后刷新一次以加载完整展示。
    let attempts = 0;
    const waitForLoot = async () => {
      try {
        const response = await fetch(`/portal/api/adventure-journal/${document.body.dataset.journalInstance}/?difficulty=${document.body.dataset.journalDifficulty}&snapshot_status=1`);
        if (!response.ok) throw new Error('读取失败');
        const data = await response.json();
        if (data.snapshot?.state === 'ready') { location.reload(); return; }
      } catch (_) { /* 短暂失败仍可重试。 */ }
      if (++attempts < 20) setTimeout(waitForLoot, 3000);
      else document.querySelector('[data-loot-empty]').textContent = '掉落列表仍在准备，请稍后刷新重试。';
    };
    setTimeout(waitForLoot, 3000);
  } else if (document.querySelector('.journal-loot-table')) loadLoot();
  document.getElementById('journal-expand')?.addEventListener('click', event => {
    const expanded = event.currentTarget.textContent === '收起全部';
    document.querySelectorAll('.journal-skill').forEach(detail => { detail.open = !expanded; });
    event.currentTarget.textContent = expanded ? '展开全部' : '收起全部';
  });
  const dialog = document.getElementById('journal-tooltip-dialog');
  const content = document.getElementById('journal-tooltip-content');
  let pending;
  document.getElementById('journal-tooltip-close')?.addEventListener('click', () => dialog.close());
  dialog?.addEventListener('close', () => pending?.abort());
  document.addEventListener('click', async event => {
    const link = event.target.closest?.('[data-tooltip-id]');
    if (!link) return;
    if (event.ctrlKey || event.metaKey || event.shiftKey || !dialog?.showModal) return;
    event.preventDefault();
    pending?.abort();
    const controller = new AbortController();
    pending = controller;
    content.textContent = '正在获取详情…';
    dialog.showModal();
    const source = link.dataset.tooltipKind === 'item' ? null : document.createElement('a');
    if (source) {
      source.href = link.href;
      source.target = '_blank';
      source.rel = 'noopener';
      source.textContent = '在 Wowhead 查看详情 ↗';
    }
    try {
      const data = await fetchDetails(link.dataset.tooltipKind, link.dataset.tooltipId);
      if (pending !== controller || controller.signal.aborted) return;
      if (source) {
        source.href = data.url || link.href;
        source.textContent = `在 ${data.source || 'Wowhead'} 查看来源 ↗`;
      } else {
        link.dataset.wowItemTooltip = [...(data.lines || []), data.note || ''].join('\n');
      }
      content.replaceChildren();
      const title = document.createElement('h3');
      title.textContent = data.name;
      content.append(title);
      data.lines.forEach(line => {
        const paragraph = document.createElement('p');
        paragraph.textContent = line;
        content.append(paragraph);
      });
      const note = document.createElement('p');
      note.className = 'journal-note';
      note.textContent = data.note;
      content.append(note);
      if (source) content.append(source);
    } catch (error) {
      if (error.name === 'AbortError' || pending !== controller) return;
      content.textContent = '来源详情暂时不可用，请稍后重试。';
      if (source) content.append(document.createElement('br'), source);
    }
  });
})();
