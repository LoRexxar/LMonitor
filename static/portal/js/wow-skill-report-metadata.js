(function () {
  'use strict';
  const host = document.querySelector('[data-skill-report-metadata]');
  if (!host) return;
  const articles = [...document.querySelectorAll('.spell[id^="spell-"]')];
  if (!articles.length) return;
  // 文字首字不是技能图标，补全期间也不继续展示它。
  const branchPrefix = {wowt: 'ptr/', wowxptr: 'ptr-2/', wow_beta: 'beta/'}[host.dataset.reportBranch] || '';
  articles.forEach(article => {
    article.querySelector('.spell-icon-fallback')?.remove();
    const link = article.querySelector('.spell-title-row a');
    const id = article.id.slice('spell-'.length);
    if (link && /^\d+$/.test(id)) link.href = `https://www.wowhead.com/${branchPrefix}spell=${id}`;
  });

  function icon(item, className) {
    if (!item.icon_url) return null;
    const image = document.createElement('img');
    image.className = className;
    image.alt = '';
    image.loading = 'lazy';
    image.addEventListener('error', () => {
      if (item.icon_fallback_url && image.src !== item.icon_fallback_url) {
        image.src = item.icon_fallback_url;
      } else image.remove();
    });
    image.src = item.icon_url;
    return image;
  }

  function displayTargets(targets) {
    const grouped = new Map();
    targets.forEach(target => {
      const current = grouped.get(target.name);
      if (current) current.ids.push(target.id);
      else grouped.set(target.name, {...target, ids: [target.id]});
    });
    return [...grouped.values()];
  }

  fetch(host.dataset.skillReportMetadata)
    .then(response => { if (!response.ok) throw new Error('报告补全失败'); return response.json(); })
    .then(payload => {
      articles.forEach(article => {
        const id = article.id.slice('spell-'.length);
        const item = payload.spells?.[id];
        if (!item) return;
        const title = article.querySelector('.spell-title');
        const titleRow = article.querySelector('.spell-title-row');
        const head = article.querySelector('.spell-head');
        if (!title || !titleRow || !head) return;
        const link = titleRow.querySelector('a');
        if (link) link.href = item.url;
        const effects = item.effects || [];
        if (!effects.length) {
          if (item.name) title.textContent = item.name;
          const image = icon(item, 'spell-icon');
          if (image) { head.querySelector('.spell-icon')?.remove(); head.prepend(image); }
          return;
        }
        head.querySelector('.spell-icon')?.remove();
        const sourceName = title.textContent;
        const targets = displayTargets(effects.flatMap(effect => effect.targets));
        title.textContent = targets.length ? targets.map(target => target.name).join('、') : `${sourceName}：PvP 调整`;
        const source = document.createElement('div');
        source.className = 'spell-adjustment-source';
        source.textContent = `PvP 调整 · 来源：${sourceName} #${id}`;
        titleRow.querySelector('.spell-id')?.remove();
        if (link) { link.textContent = '查看调整记录'; source.append(' · ', link); }
        titleRow.after(source);
        article.dataset.search = `${article.dataset.search || ''} ${title.textContent.toLowerCase()} pvp`;
        let previousGroup = source;
        effects.forEach(effect => {
          const group = document.createElement('div');
          group.className = 'spell-affected-skills';
          const label = document.createElement('span');
          label.textContent = `效果 #${effect.index} · PvP ${[647, 649].includes(effect.aura) ? '百分比' : '固定值'}修正：`;
          group.append(label);
          const related = displayTargets(effect.targets);
          related.forEach(target => {
            const targetLink = document.createElement('a');
            targetLink.href = target.url;
            targetLink.target = '_blank';
            targetLink.rel = 'noopener noreferrer';
            targetLink.title = `技能 ID：${target.ids.join('、')}`;
            const image = icon(target, 'spell-affected-icon');
            if (image) targetLink.append(image);
            targetLink.append(document.createTextNode(target.name));
            group.append(targetLink);
          });
          if (!effect.targets.length) group.append(document.createTextNode('关联技能待解析'));
          previousGroup.after(group);
          previousGroup = group;
          article.querySelectorAll('.impact-evidence').forEach(evidence => {
            if (evidence.textContent.includes(`(#${effect.index})`)) {
              evidence.textContent = `PvP · ${related.map(target => target.name).join('、') || '关联技能待解析'} · 效果 #${effect.index}`;
            }
          });
        });
      });
      document.getElementById('spellFilter')?.dispatchEvent(new Event('input', {bubbles: true}));
    })
    .catch(() => {
      // 原报告仍可阅读；网络恢复后重新打开页面即可重试。
    });
})();
