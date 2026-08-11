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

  const reportTextTranslations = Object.freeze({
    'Results, Spec and Gear': '结果、专精与装备',
    'Procs, Uptimes & Benefits': '触发、覆盖率与收益',
    'Parsed Player Effects': '已解析的玩家效果',
    'Cooldown Waste': '冷却浪费',
    'Resource Gains': '资源获取',
    'Resource Changes': '资源变化',
    'Resource Usage': '资源消耗',
    'Statistics & Data Analysis': '模拟统计与数据分析',
    'Action Priority List': '动作优先级列表（APL）',
    'Sample Sequence Table': '技能施放样本序列',
    'Scale Factors': '属性权重（Scale Factors）',
    'DPS': 'DPS',
    'DPS(e)': '期望 DPS（DPS(e)）',
    'DPS Error': 'DPS 误差',
    'DPS Range': 'DPS 波动范围',
    'DPR': '每点资源伤害（DPR）',
    'Resource': '资源',
    'Resources': '资源',
    'Out': '消耗',
    'In': '获取',
    'Waiting': '等待占比',
    'Active': '有效行动占比',
    'Set Bonus': '套装效果',
    'Proc': '触发（Proc）',
    'Procs': '触发（Proc）',
    'Uptime': '覆盖率（Uptime）',
    'Benefit': '有效收益（Benefit）',
    'Count': '次数',
    'Min': '最小值',
    'Max': '最大值',
    'Mean': '平均值',
    'Median': '中位数',
    'Average': '平均值',
    'Interval': '触发间隔',
    'Avg %': '平均占比',
    'Avg Dur': '平均持续时间',
    'Duration': '持续时间',
    'Cooldown': '冷却时间',
    'Overflow': '溢出',
    'Expiry': '到期',
    'Passive Effects': '被动效果',
    'Passive Modified Spell': '受被动效果影响的技能',
    'Dynamic Effects': '动态效果',
    'Spell': '技能',
    'Action': '技能 / 动作',
    'Action List': '动作列表',
    'Ability': '技能',
    'Name': '名称',
    'Type': '类型',
    'ID': 'ID',
    'Value': '数值',
    'Source': '来源',
    'Effect Type': '效果类型',
    'Modified By': '受以下效果修改',
    'Target': '目标',
    'Targets': '目标数',
    'Execute': '执行次数',
    'Execute Time': '执行时间',
    'Cast Time': '施法时间',
    'Crit': '暴击',
    'Critical Strike': '暴击',
    'Hit': '命中',
    'Miss': '未命中',
    'Tick': '周期次数（Tick）',
    'Ticks': '周期次数（Ticks）',
    'Stats': '角色属性',
    'Stat': '属性',
    'Base': '基础值',
    'Gear Amount': '装备数值（Gear Amount）',
    'Unbuffed': '无增益',
    'Raid-Buffed': '团队增益后',
    'Rating': '评分',
    'Strength': '力量',
    'Agility': '敏捷',
    'Stamina': '耐力',
    'Intellect': '智力',
    'Spirit': '精神',
    'Health': '生命值',
    'Mana': '法力值',
    'Rage': '怒气',
    'Energy': '能量',
    'Focus': '集中值',
    'Runic Power': '符文能量',
    'Astral Power': '星界能量',
    'Maelstrom': '漩涡值',
    'Insanity': '狂乱值',
    'Fury': '恶魔之怒',
    'Pain': '痛苦值',
    'Armor': '护甲',
    'Attack Power': '攻击强度',
    'Spell Power': '法术强度',
    'Haste': '急速',
    'Mastery': '精通',
    'Versatility': '全能',
    'Leech': '吸血',
    'Avoidance': '闪避',
    'Speed': '速度',
    'Gear': '装备',
    'Slot': '装备槽位',
    'Item': '物品',
    'Item Level': '物品等级',
    'Enchant': '附魔',
    'Gems': '宝石',
    'Bonus ID': 'Bonus ID',
    'Head': '头部',
    'Neck': '颈部',
    'Shoulders': '肩部',
    'Back': '背部',
    'Chest': '胸部',
    'Wrist': '手腕',
    'Hands': '手部',
    'Waist': '腰部',
    'Legs': '腿部',
    'Feet': '脚部',
    'Finger 1': '手指 1',
    'Finger 2': '手指 2',
    'Trinket 1': '饰品 1',
    'Trinket 2': '饰品 2',
    'Main Hand': '主手',
    'Off Hand': '副手',
    'Talents': '天赋',
    'Talent': '天赋',
    'Class Spell': '职业技能',
    'Profile': 'Profile / 可复现配置',
    'Simulation Length': '模拟战斗时长',
    'Fight Length': '战斗时长',
    'Waiting Time': '等待时间',
    'Iterations': '迭代次数',
    'Standard Deviation': '标准差',
    'Std Dev': '标准差',
    'Percentile': '分位数',
  });
  const reportTextTranslationsFolded = new Map(
    Object.entries(reportTextTranslations).map(([source, translated]) => [source.toLocaleLowerCase(), translated]),
  );

  const esc = value => String(value == null ? '' : value).replace(
    /[&<>"']/g,
    char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]),
  );

  function boundedInteger(value, fallback, maximum) {
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) ? Math.max(1, Math.min(maximum, parsed)) : fallback;
  }

  function localizedNameMap(report) {
    const names = new Map();
    const add = (source, translated) => {
      const original = String(source || '').trim();
      const localized = String(translated || '').trim();
      if (original && localized && original !== localized) names.set(original, localized);
    };
    const collect = (rows, sourceKey = 'name_en', translatedKey = 'name') => {
      (Array.isArray(rows) ? rows : []).forEach(row => add(row?.[sourceKey], row?.[translatedKey]));
    };
    collect(report?.abilities);
    collect(report?.top_abilities);
    collect(report?.buffs?.dynamic);
    collect(report?.buffs?.constant);
    collect(report?.sample_sequence, 'action_en', 'action');
    return names;
  }

  function localizeReportText(value, localizedNames) {
    const text = String(value == null ? '' : value).trim();
    if (!text) return '';
    const localizedName = localizedNames?.get(text);
    if (localizedName) return localizedName;
    const exact = reportTextTranslationsFolded.get(text.toLocaleLowerCase());
    if (exact) return exact;
    const percentile = text.match(/^(\d+)(?:st|nd|rd|th) Percentile$/i);
    if (percentile) return `${percentile[1]}% 分位数`;
    return text;
  }

  function renderCell(cell, localizedNames) {
    const tag = cell?.header === true ? 'th' : 'td';
    const colspan = boundedInteger(cell?.colspan, 1, 24);
    const rowspan = boundedInteger(cell?.rowspan, 1, 500);
    const sourceText = String(cell?.text == null ? '' : cell.text);
    const text = localizeReportText(sourceText, localizedNames);
    const numeric = /^[-+−]?[\d,.]+(?:\s*\/\s*[-+−]?[\d,.]+)?(?:%|ms|s|m|h)?$/i.test(text.trim());
    const longToken = text.length > 60 && !/\s/.test(text);
    const classes = [numeric ? 'simc-report-cell-number' : '', longToken ? 'simc-report-cell-token' : ''].filter(Boolean).join(' ');
    const sourceTitle = text !== sourceText.trim() ? ` title="原文：${esc(sourceText)}"` : '';
    const content = longToken
      ? `<code title="${esc(text)}">${esc(text)}</code><button type="button" data-simc-report-copy="${esc(sourceText)}">复制</button>`
      : esc(text);
    return `<${tag}${classes ? ` class="${classes}"` : ''}${sourceTitle} colspan="${colspan}" rowspan="${rowspan}">${content}</${tag}>`;
  }

  function renderTable(table, index, localizedNames) {
    const rows = Array.isArray(table?.rows) ? table.rows : [];
    if (!rows.length) return '';
    const sourceLabel = String(table?.label || '').trim();
    const label = localizeReportText(sourceLabel, localizedNames);
    const labelTitle = label !== sourceLabel ? ` title="原文：${esc(sourceLabel)}"` : '';
    const hasDataRow = rows.some(row => (Array.isArray(row) ? row : []).some(cell => cell?.header !== true));
    return `<section class="simc-report-table-block">
      <div class="simc-report-table-heading"><h4${labelTitle}>${esc(label || `数据表 ${index + 1}`)}</h4><span>${rows.length} 行</span></div>
      <div class="simc-report-table-scroll"><table><tbody>${rows.map(row => `<tr>${(Array.isArray(row) ? row : []).map(cell => renderCell(cell, localizedNames)).join('')}</tr>`).join('')}</tbody></table></div>
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

  function tableValueMap(report, sectionKey, tableLabel) {
    const section = (Array.isArray(report?.sections) ? report.sections : []).find(item => item?.key === sectionKey);
    const table = (Array.isArray(section?.tables) ? section.tables : []).find(item => (
      String(item?.label || '').trim().toLocaleLowerCase() === tableLabel.toLocaleLowerCase()
    ));
    const rows = Array.isArray(table?.rows) ? table.rows : [];
    const headerIndex = rows.findIndex(row => (Array.isArray(row) ? row : []).some(cell => cell?.header === true));
    if (headerIndex < 0) return new Map();
    const headers = Array.isArray(rows[headerIndex]) ? rows[headerIndex] : [];
    const valueRow = rows.slice(headerIndex + 1).find(row => (Array.isArray(row) ? row : []).some(cell => cell?.header !== true));
    if (!valueRow) return new Map();
    return new Map(headers.map((cell, index) => [
      String(cell?.text || '').trim(),
      String(valueRow[index]?.text || '').trim(),
    ]));
  }

  function extractPrimaryStats(report) {
    const preferred = ['Crit', 'Haste', 'Mastery', 'Versatility'];
    const section = (Array.isArray(report?.sections) ? report.sections : []).find(item => item?.key === 'stats');
    for (const table of (Array.isArray(section?.tables) ? section.tables : [])) {
      const rows = Array.isArray(table?.rows) ? table.rows : [];
      const headerIndex = rows.findIndex(row => (Array.isArray(row) ? row : []).some(cell => (
        String(cell?.text || '').trim() === 'Raid-Buffed'
      )));
      if (headerIndex < 0) continue;
      const headers = rows[headerIndex] || [];
      const valueIndex = headers.findIndex(cell => String(cell?.text || '').trim() === 'Raid-Buffed');
      if (valueIndex < 0) continue;
      const values = new Map(rows.slice(headerIndex + 1).map(row => [
        String(row?.[0]?.text || '').trim(),
        String(row?.[valueIndex]?.text || '').trim(),
      ]));
      const stats = preferred.map(name => ({name, value: values.get(name) || ''})).filter(item => item.value);
      if (stats.length) return stats;
    }
    const fallback = report?.gear_ratings || {};
    return [
      {name: 'Crit', value: fallback.crit},
      {name: 'Haste', value: fallback.haste},
      {name: 'Mastery', value: fallback.mastery},
      {name: 'Versatility', value: fallback.versatility},
    ].filter(item => item.value !== undefined && item.value !== null && item.value !== '');
  }

  function displayValue(value, fallback = '—') {
    if (value === undefined || value === null || value === '') return fallback;
    return String(value);
  }

  function fallbackDps(value) {
    if (value === undefined || value === null || value === '') return '—';
    const parsed = Number(value);
    return Number.isFinite(parsed)
      ? parsed.toLocaleString(undefined, {maximumFractionDigits: 1})
      : displayValue(value);
  }

  function renderResultSummary(report) {
    const character = report?.character || {};
    const simulation = report?.simulation || {};
    const results = tableValueMap(report, 'results', 'DPS');
    const dps = results.get('DPS') || fallbackDps(report?.dps);
    const metrics = [
      {label: '最终 DPS', value: dps, primary: true},
      {label: 'DPS 误差', value: results.get('DPS Error')},
      {label: 'DPS 波动范围', value: results.get('DPS Range')},
      {label: '每点资源伤害（DPR）', value: results.get('DPR')},
    ];
    const role = [character.class, character.spec].filter(Boolean).join(' / ');
    const raceLevel = [character.race, character.level ? `等级 ${character.level}` : ''].filter(Boolean).join(' / ');
    const facts = [
      ['角色', character.name],
      ['职业 / 专精', role],
      ['种族 / 等级', raceLevel],
      ['迭代次数', simulation.iterations],
      ['战斗时长', simulation.fight_length],
      ['战斗模型', simulation.fight_style],
      ['报告时间', simulation.timestamp],
    ].filter(([, value]) => value !== undefined && value !== null && value !== '');
    const stats = extractPrimaryStats(report);
    return `<section class="simc-report-result-summary" aria-labelledby="simc-report-result-title">
      <div class="simc-report-result-heading"><span>模拟结论</span><div><h2 id="simc-report-result-title">${esc(character.name ? `${character.name} · 模拟结果` : '模拟结果')}</h2><p>直接读取当前 Run 的 SimC Results 与 Stats 表；未解析到的数据不推算、不补值。</p></div></div>
      <div class="simc-report-result-metrics">${metrics.map(metric => `<article class="simc-report-result-metric ${metric.primary ? 'is-primary' : ''}"><span>${esc(metric.label)}</span><strong>${esc(displayValue(metric.value))}</strong></article>`).join('')}</div>
      ${stats.length ? `<div class="simc-report-result-stats"><b>角色属性</b><div>${stats.map(stat => `<span><small>${esc(localizeReportText(stat.name))}</small><strong>${esc(displayValue(stat.value))}</strong></span>`).join('')}</div></div>` : ''}
      ${facts.length ? `<dl class="simc-report-result-facts">${facts.map(([label, value]) => `<div><dt>${esc(label)}</dt><dd>${esc(displayValue(value))}</dd></div>`).join('')}</dl>` : ''}
    </section>`;
  }

  function renderSection(section, index, localizedNames) {
    const key = String(section?.key || `section-${index}`);
    const meta = sectionMeta[key] || {
      title: String(section?.title || key),
      description: '从 SimC 报告安全提取的纯文本数据。',
      open: false,
    };
    const {tables, textBlocks, rows} = sectionFacts(section);
    const sectionId = `simc-report-${key.replace(/[^a-z0-9_-]+/gi, '-')}`;
    const sourceTitle = String(section?.title || '').trim();
    const localizedSourceTitle = localizeReportText(sourceTitle, localizedNames);
    const sourceTitleDisplay = sourceTitle && localizedSourceTitle !== sourceTitle
      ? `${localizedSourceTitle}（${sourceTitle}）`
      : (localizedSourceTitle || key);
    return `<details id="${esc(sectionId)}" class="simc-report-section" data-simc-report-key="${esc(key)}" ${meta.open ? 'open' : ''}>
      <summary>
        <span class="simc-report-section-copy"><b>${esc(meta.title)}</b><small>${esc(sourceTitleDisplay)} · ${esc(meta.description)}</small></span>
        <span class="simc-report-section-count">${tables.length ? `${tables.length} 表 · ${rows} 行` : `${textBlocks.length} 个文本块`}</span>
      </summary>
      <div class="simc-report-section-body">
        ${tables.map((table, tableIndex) => renderTable(table, tableIndex, localizedNames)).join('')}
        ${textBlocks.map(renderTextBlock).join('')}
      </div>
    </details>`;
  }

  function render(report) {
    const sections = Array.isArray(report?.sections) ? report.sections.filter(section => {
      const facts = sectionFacts(section);
      return facts.tables.length || facts.textBlocks.length;
    }) : [];
    const localizedNames = localizedNameMap(report);
    const nav = sections.map((section, index) => {
      const key = String(section?.key || `section-${index}`);
      const meta = sectionMeta[key] || {title: localizeReportText(section?.title || key, localizedNames)};
      const sectionId = `simc-report-${key.replace(/[^a-z0-9_-]+/gi, '-')}`;
      return `<a href="#${esc(sectionId)}" data-simc-report-target="${esc(sectionId)}">${esc(meta.title)}</a>`;
    }).join('');
    return `<section class="simc-report-shell">
      ${renderResultSummary(report)}
      <header class="simc-report-header"><div><span class="simc-report-kicker">完整 SimC 数据投影</span><h2>详细数据表</h2><p>下方保留原报告顶层数据表和 Profile 文本；标题、表头与通用行名提供中文显示，悬停可查看对应英文原文，未知专有名词保持原样。</p></div><div class="simc-report-header-actions"><span class="simc-report-coverage">${sections.length} 个版块</span><button type="button" data-simc-report-toggle="expand">全部展开</button><button type="button" data-simc-report-toggle="collapse">全部折叠</button></div></header>
      ${nav ? `<nav class="simc-report-nav" aria-label="完整模拟报告版块">${nav}</nav>` : ''}
      <div class="simc-report-sections">${sections.length ? sections.map((section, index) => renderSection(section, index, localizedNames)).join('') : '<section class="simc-report-empty">此 Artifact 尚未生成完整报告投影。</section>'}</div>
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
