(() => {
  document.querySelectorAll('form[data-auto-filter]').forEach(form => {
    form.querySelectorAll('select').forEach(select => select.addEventListener('change', () => form.requestSubmit()));
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
  let lootStarted = false;
  const loadLoot = () => {
    if (lootStarted) return;
    lootStarted = true;
    const queue = Array.from(document.querySelectorAll('[data-loot-id][data-details-loaded="false"]'));
    const worker = async () => {
      while (queue.length) {
        const row = queue.shift();
        const stats = row.querySelector('[data-item-stats]');
        const effects = row.querySelector('[data-item-effects]');
        try {
          const data = await fetchDetails('item', row.dataset.lootId);
          if (!data.complete) throw new Error('补充资料暂不可用');
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
          if (data.icon && !row.querySelector('.journal-loot-symbol img')) {
            const icon = document.createElement('img');
            icon.src = data.icon;
            icon.alt = '';
            icon.addEventListener('error', () => { icon.hidden = true; });
            row.querySelector('.journal-loot-symbol').append(icon);
          }
          row.dataset.detailsLoaded = 'true';
        } catch (_) {
          stats.textContent = '属性暂不可用';
          effects.textContent = '特效资料暂不可用，可点击装备名称查看来源。';
        }
      }
    };
    for (let i = 0; i < 4; i++) worker();
  };
  if (document.querySelector('.journal-loot-table')) loadLoot();
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
  document.querySelectorAll('[data-tooltip-id]').forEach(link => link.addEventListener('click', async event => {
    if (event.ctrlKey || event.metaKey || event.shiftKey || !dialog?.showModal) return;
    event.preventDefault();
    pending?.abort();
    const controller = new AbortController();
    pending = controller;
    content.textContent = '正在获取详情…';
    dialog.showModal();
    const source = document.createElement('a');
    source.href = link.href;
    source.target = '_blank';
    source.rel = 'noopener';
    source.textContent = '在 Wowhead 查看详情 ↗';
    try {
      const data = await fetchDetails(link.dataset.tooltipKind, link.dataset.tooltipId);
      if (pending !== controller || controller.signal.aborted) return;
      source.href = data.url || link.href;
      source.textContent = `在 ${data.source || 'Wowhead'} 查看来源 ↗`;
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
      content.append(note, source);
    } catch (error) {
      if (error.name === 'AbortError' || pending !== controller) return;
      content.textContent = '来源详情暂时不可用，请稍后重试。';
      content.append(document.createElement('br'), source);
    }
  }));
})();
