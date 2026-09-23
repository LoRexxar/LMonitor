(function () {
  'use strict';
  const host = document.querySelector('[data-skill-report-metadata]');
  if (!host) return;
  const articles = [...document.querySelectorAll('.spell[id^="spell-"]')];
  if (!articles.length) return;
  const summary = document.querySelector('.skill-report .summary');
  const legacyMetric = [...(summary?.querySelectorAll('.metric') || [])].find(metric =>
    metric.querySelector('span')?.textContent.trim() === '变更技能');
  if (legacyMetric) {
    legacyMetric.querySelector('span').textContent = '改动来源';
    const metric = document.createElement('div');
    metric.className = 'metric affected-metric';
    const label = document.createElement('span');
    label.textContent = '受影响技能';
    const value = document.createElement('strong');
    value.textContent = '—';
    metric.append(label, value);
    legacyMetric.after(metric);
    document.querySelectorAll('.toc-item > .subtle, .class-head > .subtle, .spec-section h3 > .subtle').forEach(node => {
      node.textContent = node.textContent.replace(/(\d+)\s*技能/g, '$1 条改动来源');
    });
  }
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
      const affectedIds = new Set();
      let complete = true;
      articles.forEach(article => {
        const id = article.id.slice('spell-'.length);
        const item = payload.spells?.[id];
        if (!item) { complete = false; return; }
        const title = article.querySelector('.spell-title');
        const titleRow = article.querySelector('.spell-title-row');
        const head = article.querySelector('.spell-head');
        if (!title || !titleRow || !head) { complete = false; return; }
        const link = titleRow.querySelector('a');
        if (link) link.href = item.url;
        const effects = item.effects || [];
        const expectedIndices = new Set();
        article.querySelectorAll('.impact-evidence, .line').forEach(element => {
          for (const match of element.textContent.matchAll(/\(#(\d+)\)/g)) expectedIndices.add(Number(match[1]));
        });
        const returnedIndices = new Set(effects.map(effect => Number(effect.index)));
        const allExpectedReturned = [...expectedIndices].every(index => returnedIndices.has(index));
        if (!allExpectedReturned) complete = false;
        if (!effects.length) {
          if (!expectedIndices.size) affectedIds.add(id);
          if (item.name) title.textContent = item.name;
          const image = icon(item, 'spell-icon');
          if (image) { head.querySelector('.spell-icon')?.remove(); head.prepend(image); }
          return;
        }
        head.querySelector('.spell-icon')?.remove();
        const sourceName = title.textContent;
        const targets = displayTargets(effects.flatMap(effect => effect.targets));
        title.textContent = targets.length ? targets.map(target => target.name).join('、') : `${sourceName}：关联技能待解析`;
        const source = document.createElement('div');
        source.className = 'spell-adjustment-source';
        source.textContent = `改动来源：${sourceName} #${id}`;
        titleRow.querySelector('.spell-id')?.remove();
        if (link) { link.textContent = '查看调整记录'; source.append(' · ', link); }
        titleRow.after(source);
        article.dataset.search = `${article.dataset.search || ''} ${title.textContent.toLowerCase()} pvp`;
        let previousGroup = source;
        let copiedFacts = 0;
        effects.forEach(effect => {
          const group = document.createElement('div');
          group.className = 'spell-affected-skills';
          const label = document.createElement('span');
          label.textContent = `效果 #${effect.index} · 受影响技能：`;
          group.append(label);
          const related = displayTargets(effect.targets);
          if (!effect.targets.length || effect.truncated) complete = false;
          effect.targets.forEach(target => affectedIds.add(String(target.id)));
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
          const factRows = Array.from(article.querySelectorAll('.impact-block .impact-row[data-effect-index]')).filter(row =>
            row.dataset.effectIndex === String(effect.index));
          const detailRows = Array.from(article.querySelectorAll('.tech-details .line')).filter(row =>
            row.textContent.includes(`(#${effect.index})`));
          const evidenceRows = factRows.length ? factRows : detailRows;
          if (evidenceRows.length) {
            const facts = document.createElement('div');
            facts.className = 'spell-effect-change';
            evidenceRows.forEach(row => facts.append(row.cloneNode(true)));
            group.append(facts);
            copiedFacts++;
          } else {
            const unavailable = document.createElement('div');
            unavailable.className = 'spell-effect-change';
            unavailable.textContent = '该历史报告未保存本效果的逐项字段值';
            group.append(unavailable);
          }
          previousGroup.after(group);
          previousGroup = group;
        });
        if (allExpectedReturned && copiedFacts === effects.length && effects.every(effect => effect.targets.length && !effect.truncated)) {
          const sourceFacts = article.querySelector(':scope > .impact-block');
          if (sourceFacts) {
            const details = document.createElement('details');
            details.className = 'source-facts';
            const summary = document.createElement('summary');
            summary.textContent = '查看来源记录的完整字段变化';
            sourceFacts.before(details);
            details.append(summary, sourceFacts);
          }
        }
      });
      const count = document.querySelector('.affected-metric strong');
      if (count) count.textContent = complete ? String(affectedIds.size) : (affectedIds.size ? `已解析 ${affectedIds.size}+` : '待解析');
      document.getElementById('spellFilter')?.dispatchEvent(new Event('input', {bubbles: true}));
    })
    .catch(() => {
      // 原报告仍可阅读；网络恢复后重新打开页面即可重试。
    });
})();
