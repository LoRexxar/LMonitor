(() => {
  'use strict';
  const root = document.getElementById('simc-detail-root');
  if (!root) return;
  const kind = 'tasks';
  const objectId = Number.parseInt(root.dataset.simcDetailId || '', 10);
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = value => Number.isFinite(Number(value)) ? Math.round(Number(value)).toLocaleString() : '-';
  const value = (item, fallback = '-') => item == null || item === '' ? fallback : esc(item);
  const card = (title, body, wide = false) => `<section class="card${wide ? ' wide' : ''}"><h2>${title}</h2>${body}</section>`;
  const statusClass = row => [0, 1, 4].includes(Number(row.status)) ? '运行中' : value(row.status_label || row.status);
  const statusKey = status => ['completed', 'running', 'failed'].includes(String(status)) ? String(status) : '';
  const percentNumber = input => { const parsed = Number.parseFloat(String(input == null ? '' : input).replace('%', '')); return Number.isFinite(parsed) ? Math.max(0, Math.min(100, parsed)) : 0; };
  const humanSize = bytes => { const size = Number(bytes); if (!Number.isFinite(size)) return '-'; if (size < 1024) return `${size} B`; if (size < 1048576) return `${(size / 1024).toFixed(1)} KB`; return `${(size / 1048576).toFixed(2)} MB`; };
  const runStatus = status => ({completed: '已完成', running: '运行中', pending: '等待中', failed: '失败'}[String(status)] || value(status));
  const artifactType = type => ({html_report: 'HTML 原生报告'}[String(type)] || value(type));
  const artifactRows = rows => (Array.isArray(rows) ? rows : []).map(item => `<tr><td title="${value(item.file_name)}">${value(item.file_name || item.artifact_type)}</td><td>${artifactType(item.artifact_type)}</td><td class="right">${humanSize(item.file_size)}</td><td class="right">${item.can_preview === true ? `<a href="${esc(item.preview_url)}">查看原生报告</a>` : '不可预览'}</td></tr>`).join('');

  function renderTask(row) {
    const report = row.report_summary || {};
    const character = report.character || {};
    const simulation = report.simulation || {};
    const params = row.simulation_params || {};
    const abilities = Array.isArray(report.abilities) && report.abilities.length ? report.abilities : (Array.isArray(report.top_abilities) ? report.top_abilities : []);
    const buffs = report.buffs || {};
    const dynamicBuffs = Array.isArray(buffs.dynamic) ? buffs.dynamic : [];
    const constantBuffs = Array.isArray(buffs.constant) ? buffs.constant : [];
    const sampleSequence = Array.isArray(report.sample_sequence) ? report.sample_sequence : [];
    const talents = report.talents || {};
    const setBonuses = Array.isArray(talents.set_bonuses) ? talents.set_bonuses : [];
    const runs = Array.isArray(row.runs) ? row.runs : [];
    const artifacts = Array.isArray(row.artifacts) ? row.artifacts : [];
    const hasStructuredReport = Boolean(character.name || character.class || character.spec || abilities.length || dynamicBuffs.length || constantBuffs.length || talents.string || setBonuses.length);
    const nativeArtifact = artifacts.find(item => item.can_preview === true && item.preview_url);
    const nativeReportAction = nativeArtifact ? `<div class="hero-actions"><a class="primary-link" href="${esc(nativeArtifact.preview_url)}">查看完整原生报告 <span aria-hidden="true">↗</span></a><span class="muted" style="color:#dbeafe">技能明细、Buff、Proc 与图表均在原生报告中</span></div>` : '';
    const characterPills = hasStructuredReport ? `<span class="pill">角色 ${value(character.name, '未命名')}</span><span class="pill">${value(character.class, '职业未知')} · ${value(character.spec, '专精未知')}</span>` : '<span class="pill warning">结构化分析待完善</span>';
    const abilityRows = abilities.map(item => {
      const share = percentNumber(item.dps_percent);
      const details = item.details || {};
      return `<tr><td class="ability-name">${value(item.name)}${item.spell_id ? `<small>#${value(item.spell_id)}</small>` : ''}</td><td class="right">${number(item.dps)}</td><td class="ability-share"><b>${value(item.dps_percent)}</b><div class="share-track" aria-hidden="true"><div class="share-fill" style="width:${share}%"></div></div></td><td class="right">${value(item.execute || details.executes)}</td><td class="right">${value(item.interval)}</td><td class="right">${value(item.crit_percent)}</td><td class="right">${value(item.uptime_percent)}</td><td class="right">${value(details.ticks)}</td><td class="right">${value(details.refreshes)}</td></tr>`;
    }).join('');
    const dynamicBuffRows = dynamicBuffs.map(item => {
      const details = item.details || {};
      const stacks = Array.isArray(item.stack_uptimes) ? item.stack_uptimes : [];
      return `<tr><td class="ability-name">${value(item.name)}${item.spell_id ? `<small>#${value(item.spell_id)}</small>` : ''}${stacks.length ? `<div class="stack-list">${stacks.map(stack => `<span>${value(stack.stack)} ${value(stack.uptime)}</span>`).join('')}</div>` : ''}</td><td class="right">${value(item.trigger_count_start)}</td><td class="right">${value(item.trigger_count_refresh)}</td><td class="right"><b>${value(item.trigger_count_total)}</b></td><td class="right">${value(item.interval_trigger)}</td><td class="right">${value(item.duration)}</td><td class="right">${value(item.uptime)}</td><td class="right">${value(item.benefit)}</td><td class="right">${value(item.overflow)}</td><td class="right">${value(item.expiry)}</td><td class="right">${value(details.trigger_pct)}</td><td>${details.stat ? `${value(details.stat)} ${value(details.amount, '')}` : '-'}</td></tr>`;
    }).join('');
    const constantBuffRows = constantBuffs.map(item => { const details = item.details || {}; return `<tr><td class="ability-name">${value(item.name)}${item.spell_id ? `<small>#${value(item.spell_id)}</small>` : ''}</td><td class="right">${value(details.max_stacks)}</td><td class="right">${value(details.base_duration)}</td><td class="right">${value(details.base_cooldown)}</td><td>${details.stat ? `${value(details.stat)} ${value(details.amount, '')}` : '-'}</td></tr>`; }).join('');
    const sequenceRows = sampleSequence.map(item => `<tr><td class="right">${value(item.time)}</td><td class="right">${value(item.marker)}</td><td class="ability-name">${value(item.action)}${item.action_list ? `<small>${value(item.action_list)}</small>` : ''}</td><td>${value(item.target)}</td><td>${value(item.resources)}</td><td class="sequence-buffs">${value(item.buffs)}</td></tr>`).join('');
    const runRows = runs.map(run => `<tr><td>#${value(run.sequence)}</td><td><span class="status-dot ${statusKey(run.status)}"></span>${runStatus(run.status)}</td><td class="right">${number(run.result_summary?.dps)}</td><td>${value(run.started_at)}</td><td>${value(run.completed_at)}</td></tr>`).join('');
    const talentValue = talents.string ? `<code class="talent-code">${value(talents.string)}</code>` : '报告未解析到天赋字符串';
    const talentCandidate = row.mode_summary?.talent_candidate || null;
    const talentCandidateValue = talentCandidate?.talent
      ? `<dl><div><dt>方案名称</dt><dd>${value(talentCandidate.name || row.candidate_label)}</dd></div><div><dt>完整天赋树字符串</dt><dd><code class="talent-code">${value(talentCandidate.talent)}</code></dd></div></dl>`
      : '<p class="muted">当前任务不是命名天赋候选。</p>';
    const bonusValue = setBonuses.length ? `<div class="bonus-list">${setBonuses.map(item => `<span class="bonus-tag">${value(item)}</span>`).join('')}</div>` : '报告未解析到套装效果';
    root.innerHTML = `<section class="hero"><span class="pill">任务${statusClass(row)}</span><h1>${value(row.name, `任务 #${objectId}`)}</h1><div class="hero-meta">${characterPills}<span class="pill">更新 ${value(row.updated_at)}</span></div>${nativeReportAction}</section>
      ${hasStructuredReport ? '' : '<div class="analysis-warning"><b>模拟已成功，结构化分析信息不完整</b><span>当前仅展示已确认的 DPS、参数、执行轮次和原生报告；缺失字段不会被猜测填充。</span></div>'}
      ${renderTaskComparison(row)}
      <div class="grid">
        ${card('结果概览', `<div class="metrics"><div class="metric"><span>DPS</span><b>${number(report.dps ?? row.result_summary?.dps)}</b></div><div class="metric"><span>迭代次数</span><b>${number(simulation.iterations ?? params.iterations)}</b></div><div class="metric"><span>战斗时长</span><b>${value(simulation.fight_length ?? params.max_time)} 秒</b></div><div class="metric"><span>目标数</span><b>${value(params.desired_targets ?? params.target_count)}</b></div></div>`, true)}
        ${card('角色', `<dl><div><dt>名称</dt><dd>${value(character.name)}</dd></div><div><dt>职业 / 专精</dt><dd>${value(character.class)} / ${value(character.spec)}</dd></div><div><dt>种族</dt><dd>${value(character.race)}</dd></div><div><dt>等级</dt><dd>${value(character.level)}</dd></div></dl>`)}
        ${card('模拟参数', `<dl><div><dt>战斗模型</dt><dd>${value(simulation.fight_style ?? params.fight_style)}</dd></div><div><dt>最长时间</dt><dd>${value(params.max_time)} 秒</dd></div><div><dt>迭代次数</dt><dd>${value(simulation.iterations ?? params.iterations)}</dd></div><div><dt>目标数量</dt><dd>${value(params.desired_targets ?? params.target_count)}</dd></div><div><dt>报告时间</dt><dd>${value(simulation.timestamp)}</dd></div></dl>`)}
        ${card('候选方案', talentCandidateValue, true)}
        ${card('天赋与套装', `<dl><div><dt>天赋字符串</dt><dd>${talentValue}</dd></div><div><dt>套装效果</dt><dd>${bonusValue}</dd></div></dl>`, true)}
        ${card('技能伤害与触发明细', `<p class="muted">保留报告中的全部伤害技能；施放、间隔、暴击、覆盖、Tick 和刷新次数均按原始 SimC 数值展示。</p><div class="table-scroll"><table class="dense-table"><thead><tr><th>技能</th><th class="right">DPS</th><th>伤害占比</th><th class="right">施放</th><th class="right">间隔</th><th class="right">暴击</th><th class="right">覆盖</th><th class="right">Ticks</th><th class="right">刷新</th></tr></thead><tbody>${abilityRows || '<tr><td colspan="9" class="empty">暂无已解析技能</td></tr>'}</tbody></table></div>`, true)}
        ${card('技能施放序列', `<p class="muted">来自 SimC Sample Sequence Table，按一次代表性战斗逐步展示时间、动作列表、目标、资源和当时激活的 Buff。</p><div class="table-scroll sequence-scroll"><table class="dense-table sequence-table"><thead><tr><th class="right">时间</th><th class="right">序号</th><th>技能 / 动作列表</th><th>目标</th><th>资源</th><th>激活 Buff</th></tr></thead><tbody>${sequenceRows || '<tr><td colspan="6" class="empty">报告中未包含技能施放序列</td></tr>'}</tbody></table></div>`, true)}
        ${card('动态 Buff / Proc', `<p class="muted">展示全部动态 Buff 的启动、刷新、总触发、触发间隔、持续时间、覆盖率、收益覆盖和各层数覆盖。</p><div class="table-scroll"><table class="dense-table"><thead><tr><th>Buff</th><th class="right">启动</th><th class="right">刷新</th><th class="right">总触发</th><th class="right">触发间隔</th><th class="right">持续</th><th class="right">覆盖率</th><th class="right">收益覆盖</th><th class="right">溢出</th><th class="right">到期</th><th class="right">触发率</th><th>属性效果</th></tr></thead><tbody>${dynamicBuffRows || '<tr><td colspan="12" class="empty">暂无动态 Buff</td></tr>'}</tbody></table></div>`, true)}
        ${card('常驻 Buff', `<div class="table-scroll"><table class="dense-table"><thead><tr><th>Buff</th><th class="right">最大层数</th><th class="right">基础持续</th><th class="right">基础冷却</th><th>属性效果</th></tr></thead><tbody>${constantBuffRows || '<tr><td colspan="5" class="empty">暂无常驻 Buff</td></tr>'}</tbody></table></div>`, true)}
        ${card('执行轮次', `<div class="table-scroll"><table><thead><tr><th>轮次</th><th>状态</th><th class="right">DPS</th><th>开始</th><th>完成</th></tr></thead><tbody>${runRows || '<tr><td colspan="5" class="empty">暂无执行轮次</td></tr>'}</tbody></table></div><details><summary>技术追溯说明</summary>仅展示轮次时间与状态；命令、路径、哈希及原始错误均不在页面展示。</details>`, true)}
        ${card('Artifact / 原生报告', `<p class="muted">原生报告继续通过独立鉴权页面读取。</p><div class="table-scroll"><table><thead><tr><th>文件</th><th>类型</th><th class="right">大小</th><th class="right">操作</th></tr></thead><tbody>${artifactRows(artifacts) || '<tr><td colspan="4" class="empty">暂无 Artifact</td></tr>'}</tbody></table></div>`, true)}
        ${card('引用版本', `<dl><div><dt>Profile</dt><dd>#${value(row.profile_id)} · v${value(row.profile_version_id)}</dd></div><div><dt>基础模板</dt><dd>#${value(row.template_id)} · v${value(row.template_version_id)}</dd></div><div><dt>APL</dt><dd>#${value(row.apl_id)} · v${value(row.apl_version_id)}</dd></div><div><dt>来源任务</dt><dd>${row.source_task_id ? `<a href="/dashboard/simc/tasks/${Number(row.source_task_id)}/">#${Number(row.source_task_id)}</a>` : '-'}</dd></div></dl><details><summary>为什么显示版本号？</summary>版本引用用于复现，不展示配置原文或服务器路径。</details>`, true)}
      </div>`;
  }

  function renderTaskComparison(row) {
    if (row.mode !== 'comparison' && row.mode !== 'attribute_sweep') return '';
    const runs = Array.isArray(row.runs) ? row.runs : [];
    const ranking = Array.isArray(row.ranking) ? [...row.ranking] : [];
    const attribute = row.attribute_report || null;
    const isAttribute = row.mode === 'attribute_sweep' && attribute;
    const baseline = ranking.find(item => item.is_base === true) || null;
    const candidates = ranking.filter(item => item.is_base !== true).sort((a, b) => (a.rank || 9999) - (b.rank || 9999));
    const baselineDps = baseline?.is_complete === true ? Number(baseline.dps) : NaN;
    const signed = amount => Number.isFinite(Number(amount)) ? `${Number(amount) > 0 ? '+' : ''}${number(amount)}` : '—';
    const slotLabels = {head:'头部',neck:'颈部',shoulder:'肩部',back:'披风',chest:'胸部',wrist:'手腕',hands:'手部',waist:'腰部',legs:'腿部',feet:'脚部',finger1:'戒指 1',finger2:'戒指 2',trinket1:'饰品 1',trinket2:'饰品 2',main_hand:'主手',off_hand:'副手'};
    const itemText = item => {
      if (!item) return '基准未解析到该字段';
      const modifiers = item.modifiers || {};
      const details = [
        modifiers.enchant_id ? `附魔 #${value(modifiers.enchant_id)}` : '',
        Array.isArray(modifiers.gem_id) && modifiers.gem_id.length ? `宝石 #${modifiers.gem_id.map(value).join('/#')}` : '',
        Array.isArray(modifiers.bonus_id) && modifiers.bonus_id.length ? `Bonus #${modifiers.bonus_id.map(value).join('/')}` : '',
        Array.isArray(modifiers.crafted_stats) && modifiers.crafted_stats.length ? `制作属性 #${modifiers.crafted_stats.map(value).join('/')}` : '',
      ].filter(Boolean);
      return `${value(item.name, '未命名物品')} · #${value(item.item_id)}${item.item_level ? ` · ${value(item.item_level)} 装等` : ''}${details.length ? ` · ${details.join(' · ')}` : ''}`;
    };
    const talentText = item => item?.value ? `${value(item.name)} · ${value(item.value)}` : value(item?.name, '未提供天赋信息');
    const changeDetail = item => {
      const change = item.change || null;
      if (!change) return '<div class="change-block unchanged-block"><b>基准本身</b><span>没有应用候选覆盖</span></div>';
      const isGear = change.kind === 'gear';
      const before = isGear ? itemText(change.before) : talentText(change.before);
      const after = isGear ? itemText(change.after) : talentText(change.after);
      const field = isGear ? (slotLabels[change.field] || change.field || '装备') : '天赋方案';
      if (change.is_equivalent === true) {
        return `<div class="change-block unchanged-block"><b>${value(field)} · 无实际字段变化</b><div><span class="change-before">冻结基准：${before}</span><span class="change-arrow">＝</span><span class="change-after">候选等价配置：${after}</span></div></div>`;
      }
      return `<div class="change-block"><b>${value(field)}</b><div><span class="change-before">基准：${before}</span><span class="change-arrow">→</span><span class="change-after">候选：${after}</span></div></div>`;
    };
    const unchangedDetail = item => `<div class="unchanged-list">${(Array.isArray(item.unchanged) ? item.unchanged : []).map(label => `<span>${value(label)}</span>`).join('') || '<span>未提供固定项摘要</span>'}</div>`;
    const baselineInfo = row.comparison_baseline || {};
    const baselineCharacter = baselineInfo.character || {};
    const baselineStats = baselineInfo.stats || {};
    const baselineEquipment = Array.isArray(baselineInfo.equipment) ? baselineInfo.equipment : [];
    const simulationParams = baselineInfo.simulation_params || {};
    const parameterText = Object.entries(simulationParams).map(([key, item]) => `${value(key)}=${value(item)}`).join(' · ') || '默认模拟参数';
    const baselineStatLabels = {strength:'力量',agility:'敏捷',intellect:'智力',crit:'暴击',haste:'急速',mastery:'精通',versatility:'全能'};
    const baselineStatsHtml = Object.entries(baselineStats).map(([key, item]) => `<span><b>${value(baselineStatLabels[key] || key)}</b>${number(item)}</span>`).join('') || '<span class="muted">未冻结属性评分</span>';
    const baselineEquipmentHtml = baselineEquipment.map(item => `<div class="baseline-equipment-item"><span>${value(slotLabels[item.slot] || item.slot, '装备')}</span><b>${itemText(item)}</b></div>`).join('') || '<p class="muted">未解析到冻结装备</p>';
    const baselineFacts = [
      ['玩家 Profile', `${value(baselineInfo.profile?.name, '未命名')} · ${value(baselineInfo.profile?.spec, '未知专精')}`],
      ['角色', `${value(baselineCharacter.name, '未命名')} · ${value(baselineCharacter.class, '未知职业')} / ${value(baselineCharacter.spec, '未知专精')} · ${value(baselineCharacter.race, '未知种族')} · ${value(baselineCharacter.level, '?')} 级`],
      ['基础模板', value(baselineInfo.template?.name, '未指定')],
      ['APL', value(baselineInfo.apl?.name, '未指定')],
      ['执行后端', `${value(baselineInfo.backend?.name, '未指定')}${baselineInfo.backend?.version ? ` · ${value(baselineInfo.backend.version)}` : ''}`],
      ['模拟参数', parameterText],
    ].map(([label, item]) => `<div><span>${label}</span><b>${item}</b></div>`).join('');
    const runRows = runs.map(run => `<tr><td>${value(run.candidate_label, `Run #${run.sequence}`)}</td><td>${runStatus(run.status)}</td><td class="right">${number(run.result_summary?.dps)}</td><td>${value(run.completed_at)}</td></tr>`).join('');
    const baselinePanel = baseline ? `<section class="comparison-baseline"><div class="baseline-heading"><div><span>对比基准</span><b>${value(baseline.label || baseline.name)}</b></div><strong>${baseline.is_complete === true ? number(baseline.dps) : '结果不完整'} <small>DPS</small></strong></div><div class="baseline-facts">${baselineFacts}</div><div class="baseline-content"><div><h3>基础属性</h3><div class="baseline-stats">${baselineStatsHtml}</div></div><div><h3>基准天赋</h3><code class="baseline-talent">${value(baselineInfo.talent?.value, '未冻结天赋')}</code></div><div class="baseline-equipment"><h3>基准装备</h3>${baselineEquipmentHtml}</div></div><p>所有候选都从这份冻结基准开始；下方“实际变化”逐项写明每个方案相对基准改了什么，其余项目保持不变。</p></section>` : '<section class="comparison-baseline muted">此任务未包含 Profile 基线，无法计算可靠差异。</section>';
    const rankRows = candidates.map(item => {
      const complete = item.is_complete === true && Number.isFinite(Number(item.dps));
      const delta = complete && Number.isFinite(baselineDps) ? Number(item.dps) - baselineDps : NaN;
      const deltaPercent = Number.isFinite(delta) && baselineDps !== 0 ? delta / baselineDps * 100 : NaN;
      const deltaText = Number.isFinite(deltaPercent) ? `${signed(delta)} (${deltaPercent > 0 ? '+' : ''}${deltaPercent.toFixed(2)}%)` : '—';
      return `<tr class="${item.rank === 1 ? 'rank-winner comparison-winner' : ''} ${complete ? '' : 'rank-incomplete'}"><td><span class="rank-medal">${complete ? (item.rank === 1 ? '🥇' : value(item.rank)) : '—'}</span></td><td><b>${value(item.label || item.name)}</b>${complete ? '' : '<small class="incomplete-label">结果不完整，不参与排名</small>'}</td><td>${changeDetail(item)}</td><td>${unchangedDetail(item)}</td><td class="right"><b>${complete ? number(item.dps) : '—'}</b></td><td class="right delta comparison-delta ${Number(delta) > 0 ? 'positive' : Number(delta) < 0 ? 'negative' : ''}">${deltaText}</td></tr>`;
    }).join('');
    const chartItems = [baseline, ...candidates].filter(item => item && item.is_complete === true && Number.isFinite(Number(item.dps)));
    let comparisonChartPanel = '';
    if (!isAttribute) {
      if (chartItems.length) {
        const chartWidth = 900, chartHeight = 280, left = 72, right = 30, top = 28, bottom = 64;
        const values = chartItems.map(item => Number(item.dps));
        let minDps = Math.min(...values), maxDps = Math.max(...values);
        const padding = minDps === maxDps ? Math.max(1, maxDps * .01) : (maxDps - minDps) * .18;
        minDps -= padding;
        maxDps += padding;
        const xAt = index => left + (chartItems.length === 1 ? (chartWidth - left - right) / 2 : index * (chartWidth - left - right) / (chartItems.length - 1));
        const yAt = dps => top + (maxDps - dps) / (maxDps - minDps) * (chartHeight - top - bottom);
        const points = chartItems.map((item, index) => `${xAt(index).toFixed(1)},${yAt(Number(item.dps)).toFixed(1)}`).join(' ');
        const grid = [0, .5, 1].map(ratio => {
          const y = top + ratio * (chartHeight - top - bottom);
          const dps = maxDps - ratio * (maxDps - minDps);
          return `<g><line x1="${left}" y1="${y}" x2="${chartWidth - right}" y2="${y}" class="comparison-chart-grid"/><text x="${left - 10}" y="${y + 4}" text-anchor="end">${number(dps)}</text></g>`;
        }).join('');
        const pointNodes = chartItems.map((item, index) => {
          const x = xAt(index), y = yAt(Number(item.dps));
          const label = String(item.label || item.name || `方案 ${index + 1}`);
          const shortLabel = label.length > 14 ? `${label.slice(0, 14)}…` : label;
          const delta = Number.isFinite(baselineDps) ? Number(item.dps) - baselineDps : NaN;
          return `<g class="comparison-chart-point ${item.is_base === true ? 'is-baseline' : ''}"><circle cx="${x}" cy="${y}" r="6"><title>${value(label)}：${number(item.dps)} DPS${item.is_base === true ? '（基准）' : `，相对基准 ${signed(delta)}`}</title></circle><text x="${x}" y="${y - 13}" text-anchor="middle" class="comparison-chart-value">${number(item.dps)}</text><text x="${x}" y="${chartHeight - 25}" text-anchor="middle" class="comparison-chart-label">${value(shortLabel)}</text></g>`;
        }).join('');
        const chartSvg = `<div class="comparison-chart-scroll"><svg class="comparison-line-chart" viewBox="0 0 ${chartWidth} ${chartHeight}" role="img" aria-label="各候选方案 DPS 趋势折线图">${grid}<polyline points="${points}" class="comparison-chart-line"/>${pointNodes}</svg></div>`;
        comparisonChartPanel = card('DPS 趋势', `${chartSvg}<p class="muted">折线按基准与候选排名顺序连接，用于快速观察 DPS 高低；精确差异以紧随其后的候选差异表为准。</p>`, true);
      } else {
        comparisonChartPanel = card('DPS 趋势', '<div class="empty">暂无完整 DPS，无法绘制折线图</div>', true);
      }
    }
    const recommendation = attribute?.recommendation || null;
    const initial = attribute?.initial_ratings || {};
    const statLabels = {crit_rating:'暴击',haste_rating:'急速',mastery_rating:'精通',versatility_rating:'全能'};
    const attributeChanges = recommendation ? Object.entries(recommendation.ratings || {}).map(([key, rating]) => { const delta = Number(rating) - Number(initial[key] || 0); return `<div class="attribute-change attribute-stat-delta"><span>${value(statLabels[key] || key)}</span><b>${number(rating)}</b><em class="${delta > 0 ? 'positive' : delta < 0 ? 'negative' : ''}">${signed(delta)}</em></div>`; }).join('') : '';
    const searchTrail = Array.isArray(attribute?.history) ? attribute.history : [];
    const trailRows = searchTrail.slice(-8).map((step, index) => `<span>第 ${index + 1} 步 · ${number(step.dps)}</span>`).join('');
    const attributePanel = isAttribute ? `<section class="card wide attribute-report attribute-landscape"><div class="report-kicker">ATTRIBUTE OPTIMIZATION</div><h2>属性寻优结论</h2><div class="report-summary"><div><span>推荐 DPS</span><b>${number(recommendation?.dps)}</b></div><div><span>搜索轮次</span><b>${number(attribute.rounds_completed)} / ${number(attribute.current_round)}</b></div><div><span>步进粒度</span><b>${number(attribute.step)}</b></div><div><span>结论</span><b>${attribute.local_optimum ? '局部最优' : '继续搜索'}</b></div></div><h3>推荐属性</h3><div class="attribute-grid">${attributeChanges || '<p class="muted">等待候选完成后生成属性变化。</p>'}</div><h3>搜索轨迹</h3><div class="search-trail">${trailRows || '<span class="muted">暂无轨迹数据</span>'}</div></section>` : '';
    const succeeded = runs.filter(run => run.status === 'completed').length;
    const running = runs.filter(run => run.status === 'running').length;
    const pending = runs.filter(run => run.status === 'pending').length;
    const failed = runs.filter(run => run.status === 'failed').length;
    const progress = runs.length ? Math.round((succeeded + failed) / runs.length * 100) : 0;
    return `<section class="hero ${isAttribute ? 'attribute-hero' : 'comparison-hero'}"><div class="report-kicker">${isAttribute ? '属性寻优报告' : '候选对比报告'}</div><div class="hero-meta"><span class="pill">${number(progress)}% 完成</span><span class="pill">${number(runs.length)} 个 Run</span></div></section><div class="grid">
      ${attributePanel}
      ${isAttribute ? '' : comparisonChartPanel}
      ${card(isAttribute ? '候选测量排名' : '候选差异与 DPS 排名', `<div class="table-scroll"><table class="ranking-table comparison-diff-table"><thead><tr><th>排名</th><th>候选方案</th><th>实际变化</th><th>保持不变</th><th class="right">DPS</th><th class="right">相对基线（数值 / 百分比）</th></tr></thead><tbody>${rankRows || '<tr><td colspan="6" class="empty">暂无可排名结果</td></tr>'}</tbody></table></div>`, true)}
      ${isAttribute ? '' : baselinePanel}
      ${card('任务进度', `<div class="metrics"><div class="metric"><span>成功</span><b>${number(succeeded)}</b></div><div class="metric"><span>运行</span><b>${number(running)}</b></div><div class="metric"><span>等待</span><b>${number(pending)}</b></div><div class="metric"><span>失败</span><b>${number(failed)}</b></div></div>`, true)}
      ${card('候选 Runs', `<div class="table-scroll"><table><thead><tr><th>候选</th><th>状态</th><th class="right">DPS</th><th>完成时间</th></tr></thead><tbody>${runRows || '<tr><td colspan="4" class="empty">暂无 Run</td></tr>'}</tbody></table></div>`, true)}
    </div>`;
  }

  fetch(`/api/simc-workbench/${kind}/${objectId}/`, {headers: {'Accept': 'application/json'}})
    .then(async response => { const payload = await response.json(); if (!response.ok || !payload.success) throw new Error(payload.error || '详情加载失败'); return payload.data || {}; })
    .then(renderTask)
    .catch(() => { root.innerHTML = '<div class="error"><b>详情暂时无法加载</b><p>请返回工作台稍后重试。为避免泄露内部信息，此处不展示原始错误。</p></div>'; });
})();
