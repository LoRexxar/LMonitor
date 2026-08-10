(() => {
  'use strict';

  const sectionMeta = {
    results: {
      title: '结果、精度与资源',
      description: 'DPS、误差、资源效率、角色摘要与套装激活状态。',
      open: true,
    },
    procs: {
      title: 'Proc 与覆盖率',
      description: '触发次数、触发间隔、持续时间及有效覆盖。',
      open: true,
    },
    effects: {
      title: '玩家效果解析',
      description: 'SimC 实际解析到的被动、附魔、饰品与动态效果参数。',
      open: false,
    },
    cooldown_waste: {
      title: '冷却浪费',
      description: '按执行统计可用冷却未被使用的时间分布。',
      open: false,
    },
    resources: {
      title: '资源获取、变化与消耗',
      description: '资源来源、净变化、溢出及各技能的实际消耗。',
      open: true,
    },
    statistics: {
      title: '模拟统计与结果分布',
      description: 'DPS、战斗时长、等待时间等指标的均值、方差、分位数与分布。',
      open: false,
    },
    action_priority: {
      title: 'APL 执行统计',
      description: '动作优先级列表、调用次数、命中目标与执行间隔。',
      open: false,
    },
    stats: {
      title: '最终角色属性',
      description: 'SimC 计算后的基础值、装备贡献、增益后数值与战斗评分。',
      open: true,
    },
    gear: {
      title: '逐槽装备',
      description: '每个装备槽位的物品、装等、属性、附魔、宝石和 Bonus ID。',
      open: true,
    },
    talents: {
      title: '天赋树',
      description: '完整天赋选择、等级、Spell ID 与被动效果。',
      open: true,
    },
    profile: {
      title: 'Profile / 可复现配置',
      description: '报告内嵌的角色 Profile 与 APL 纯文本投影，可用于审计配置。',
      open: false,
    },
    scale_factors: {
      title: '属性权重',
      description: '报告包含的 Scale Factors、标准化权重与误差。',
      open: true,
    },
  };

  const esc = value => String(value == null ? '' : value).replace(
    /[&<>"']/g,
    char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]),
  );

  function boundedInteger(value, fallback, maximum) {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) ? Math.max(1, Math.min(maximum, parsed)) : fallback;
  }

  function renderCell(cell) {
    const tag = cell?.header === true ? 'th' : 'td';
    const colspan = boundedInteger(cell?.colspan, 1, 24);
    const rowspan = boundedInteger(cell?.rowspan, 1, 500);
    const text = String(cell?.text == null ? '' : cell.text);
    const numeric = /^[-+−]?[\d,.]+(?:\s*\/\s*[-+−]?[\d,.]+)?(?:%|ms|s|m|h)?$/i.test(text.trim());
    const longToken = text.length > 60 && !/\s/.test(text);
    const classes = [numeric ? 'simc-report-cell-number' : '', longToken ? 'simc-report-cell-token' : ''].filter(Boolean).join(' ');
    const content = longToken
      ? `<code title="${esc(text)}">${esc(text)}</code><button type="button" data-simc-report-copy="${esc(text)}">复制</button>`
      : esc(text);
    return `<${tag}${classes ? ` class="${classes}"` : ''} colspan="${colspan}" rowspan="${rowspan}">${content}</${tag}>`;
  }

  function renderTable(table, index) {
    const rows = Array.isArray(table?.rows) ? table.rows : [];
    if (!rows.length) return '';
    const label = String(table?.label || '').trim();
    const hasDataRow = rows.some(row => (Array.isArray(row) ? row : []).some(cell => cell?.header !== true));
    return `<section class="simc-report-table-block">
      <div class="simc-report-table-heading"><h4>${esc(label || `数据表 ${index + 1}`)}</h4><span>${rows.length} 行</span></div>
      <div class="simc-report-table-scroll"><table><tbody>${rows.map(row => `<tr>${(Array.isArray(row) ? row : []).map(renderCell).join('')}</tr>`).join('')}</tbody></table></div>
      ${hasDataRow ? '' : '<p class="simc-report-table-empty">本报告未包含该表的可解析数据行。</p>'}
    </section>`;
  }

  function renderTextBlock(text, index) {
    return `<section class="simc-report-text-block"><div class="simc-report-table-heading"><h4>${index === 0 ? '完整 Profile 文本' : `文本块 ${index + 1}`}</h4><span>${String(text || '').length.toLocaleString()} 字符</span></div><pre><code>${esc(text)}</code></pre></section>`;
  }

  function sectionFacts(section) {
    const tables = Array.isArray(section?.tables) ? section.tables : [];
    const textBlocks = Array.isArray(section?.text_blocks) ? section.text_blocks : [];
    const rows = tables.reduce((total, table) => total + (Array.isArray(table?.rows) ? table.rows.length : 0), 0);
    return {tables, textBlocks, rows};
  }

  function renderSection(section, index) {
    const key = String(section?.key || `section-${index}`);
    const meta = sectionMeta[key] || {
      title: String(section?.title || key),
      description: '从 SimC 报告安全提取的纯文本数据。',
      open: false,
    };
    const {tables, textBlocks, rows} = sectionFacts(section);
    const sectionId = `simc-report-${key.replace(/[^a-z0-9_-]+/gi, '-')}`;
    const sourceTitle = String(section?.title || '').trim();
    return `<details id="${esc(sectionId)}" class="simc-report-section" data-simc-report-key="${esc(key)}" ${meta.open ? 'open' : ''}>
      <summary>
        <span class="simc-report-section-copy"><b>${esc(meta.title)}</b><small>${esc(sourceTitle || key)} · ${esc(meta.description)}</small></span>
        <span class="simc-report-section-count">${tables.length ? `${tables.length} 表 · ${rows} 行` : `${textBlocks.length} 个文本块`}</span>
      </summary>
      <div class="simc-report-section-body">
        ${tables.map(renderTable).join('')}
        ${textBlocks.map(renderTextBlock).join('')}
      </div>
    </details>`;
  }

  function render(report) {
    const sections = Array.isArray(report?.sections) ? report.sections.filter(section => {
      const facts = sectionFacts(section);
      return facts.tables.length || facts.textBlocks.length;
    }) : [];
    if (!sections.length) return '<section class="simc-report-empty">此 Artifact 尚未生成完整报告投影。</section>';
    const nav = sections.map((section, index) => {
      const key = String(section?.key || `section-${index}`);
      const meta = sectionMeta[key] || {title: section?.title || key};
      const sectionId = `simc-report-${key.replace(/[^a-z0-9_-]+/gi, '-')}`;
      return `<a href="#${esc(sectionId)}" data-simc-report-target="${esc(sectionId)}">${esc(meta.title)}</a>`;
    }).join('');
    return `<section class="simc-report-shell">
      <header class="simc-report-header"><div><span class="simc-report-kicker">SIMULATIONCRAFT COMPLETE READ MODEL</span><h2>完整模拟报告</h2><p>保留原报告顶层数据表和 Profile 文本；仅移除脚本、链接、嵌套 tooltip 与隐藏详情行。</p></div><div class="simc-report-header-actions"><span class="simc-report-coverage">${sections.length} 个版块</span><button type="button" data-simc-report-toggle="expand">全部展开</button><button type="button" data-simc-report-toggle="collapse">全部折叠</button></div></header>
      <nav class="simc-report-nav" aria-label="完整模拟报告版块">${nav}</nav>
      <div class="simc-report-sections">${sections.map(renderSection).join('')}</div>
    </section>`;
  }

  document.addEventListener('click', event => {
    const toggle = event.target.closest('[data-simc-report-toggle]');
    if (toggle) {
      const shell = toggle.closest('.simc-report-shell');
      const shouldOpen = toggle.dataset.simcReportToggle === 'expand';
      shell?.querySelectorAll('.simc-report-section').forEach(section => { section.open = shouldOpen; });
      return;
    }

    const copy = event.target.closest('[data-simc-report-copy]');
    if (copy) {
      navigator.clipboard?.writeText(copy.dataset.simcReportCopy || '').then(() => {
        const previous = copy.textContent;
        copy.textContent = '已复制';
        window.setTimeout(() => { copy.textContent = previous; }, 1200);
      });
      return;
    }

    const link = event.target.closest('[data-simc-report-target]');
    if (!link) return;
    const target = document.getElementById(link.dataset.simcReportTarget || '');
    if (!target) return;
    event.preventDefault();
    target.open = true;
    target.scrollIntoView({behavior: 'smooth', block: 'start'});
  });

  window.SimcResultReport = Object.freeze({render});
})();
