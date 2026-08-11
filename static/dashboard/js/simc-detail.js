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

  async function showRunInput(taskId, runId) {
    const dialog = document.getElementById('simc-input-dialog');
    const status = dialog?.querySelector('[data-run-input-status]');
    const code = dialog?.querySelector('[data-run-input-content]');
    if (!dialog || !status || !code) return;
    status.textContent = '正在生成 SimC 输入…';
    code.textContent = '';
    dialog.showModal();
    try {
      const response = await fetch(`/api/simc-workbench/tasks/${taskId}/runs/${runId}/input/`, {headers: {'Accept': 'application/json'}});
      const result = await response.json();
      if (!response.ok || !result.success) throw new Error(result.error || '执行输入加载失败');
      const payload = result.data || {};
      status.textContent = `Run #${payload.sequence} · SimC 输入`;
      code.textContent = payload['content'] || '';
    } catch (error) {
      status.textContent = error.message || '执行输入加载失败';
    }
  }

  function renderTask(row) {
    if (row.mode === 'attribute_sweep') {
      root.innerHTML = renderAttributeTask(row);
      return;
    }
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
    const taskFailed = Number(row.status) === 3;
    const failureSummary = runs.find(run => String(run.status) === 'failed' && String(run.error_summary || '').trim())?.error_summary || '';
    const failureTooltip = failureSummary ? `<span class="simc-error-tooltip"><button type="button" class="simc-error-tooltip__trigger" aria-label="查看失败详情"><span aria-hidden="true">!</span></button><span class="simc-error-tooltip__content" role="tooltip">${value(failureSummary)}</span></span>` : '';
    const nativeReportAction = nativeArtifact ? `<div class="hero-actions"><a class="primary-link" href="${esc(nativeArtifact.preview_url)}">查看完整原生报告 <span aria-hidden="true">↗</span></a><span class="muted" style="color:#dbeafe">当前页已安全展开完整数值；原生报告用于视觉图表与交叉核对</span></div>` : '';
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
    const runRows = runs.map(run => `<tr><td>#${value(run.sequence)}</td><td><span class="status-dot ${statusKey(run.status)}"></span>${runStatus(run.status)}</td><td class="right">${number(run.result_summary?.dps)}</td><td>${value(run.started_at)}</td><td>${value(run.completed_at)}</td><td class="right"><button type="button" class="run-input-button" data-run-input data-task-id="${objectId}" data-run-id="${Number(run.id)}">查看 SimC 输入</button></td></tr>`).join('');
    const talentValue = talents.string ? `<code class="talent-code">${value(talents.string)}</code>` : '报告未解析到天赋字符串';
    const talentCandidate = row.mode_summary?.talent_candidate || null;
    const talentCandidateValue = talentCandidate?.talent
      ? `<dl><div><dt>方案名称</dt><dd>${value(talentCandidate.name || row.candidate_label)}</dd></div><div><dt>完整天赋树字符串</dt><dd><code class="talent-code">${value(talentCandidate.talent)}</code></dd></div></dl>`
      : '<p class="muted">当前任务不是命名天赋候选。</p>';
    const bonusValue = setBonuses.length ? `<div class="bonus-list">${setBonuses.map(item => `<span class="bonus-tag">${value(item)}</span>`).join('')}</div>` : '报告未解析到套装效果';
    root.innerHTML = `<section class="hero"><div class="hero-status"><span class="pill">任务${statusClass(row)}</span>${failureTooltip}</div><div class="hero-primary-column"><h1>${value(row.name, `任务 #${objectId}`)}</h1><div class="hero-resource-stack" aria-label="模拟资源"><div class="hero-resource-line"><span>APL</span><b>${value(row.apl_name, '未命名')}</b></div><div class="hero-resource-line"><span>Profile</span><b>${value(row.profile_name, '未命名')}</b></div></div></div><div class="hero-meta">${characterPills}<span class="pill">更新 ${value(row.updated_at)}</span></div>${nativeReportAction}</section>
      ${hasStructuredReport ? '' : (taskFailed ? '<div class="analysis-warning"><b>模拟执行失败</b><span>失败状态旁的提示图标可查看详情；只有 SimC 实际生成 HTML Artifact 时才会提供原生报告。</span></div>' : '<div class="analysis-warning"><b>模拟已成功，结构化分析信息不完整</b><span>当前仅展示已确认的 DPS、参数、执行轮次和原生报告；缺失字段不会被猜测填充。</span></div>')}
      ${window.SimcResultReport.render(report)}
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
        ${card('执行轮次', `<div class="table-scroll"><table><thead><tr><th>轮次</th><th>状态</th><th class="right">DPS</th><th>开始</th><th>完成</th><th class="right">输入</th></tr></thead><tbody>${runRows || '<tr><td colspan="6" class="empty">暂无执行轮次</td></tr>'}</tbody></table></div><details><summary>输入说明</summary>查看输入会按当前任务冻结配置调用 SimC Composer 生成可读文本；它不是历史执行输入的复原或校验。命令、路径及原始 stderr 不在页面展示。</details>`, true)}
        ${card('Artifact / 原生报告', `<p class="muted">${taskFailed && !nativeArtifact ? '本次失败未生成原生报告。SimC 在初始化阶段终止时不会产出 HTML Artifact。' : '原生报告继续通过独立鉴权页面读取。'}</p><div class="table-scroll"><table><thead><tr><th>文件</th><th>类型</th><th class="right">大小</th><th class="right">操作</th></tr></thead><tbody>${artifactRows(artifacts) || '<tr><td colspan="4" class="empty">暂无 Artifact</td></tr>'}</tbody></table></div>`, true)}
        ${card('引用版本', `<dl><div><dt>Profile</dt><dd>${value(row.profile_name, '未命名')} · #${value(row.profile_id)} · v${value(row.profile_version_id)}</dd></div><div><dt>基础模板</dt><dd>#${value(row.template_id)} · v${value(row.template_version_id)}</dd></div><div><dt>APL</dt><dd>${value(row.apl_name, '未命名')} · #${value(row.apl_id)} · v${value(row.apl_version_id)}</dd></div><div><dt>来源任务</dt><dd>${row.source_task_id ? `<a href="/dashboard/simc/tasks/${Number(row.source_task_id)}/">#${Number(row.source_task_id)}</a>` : '-'}</dd></div></dl><details><summary>为什么显示版本号？</summary>名称来自任务创建时的冻结资源版本；版本引用用于复现，不展示配置原文或服务器路径。</details>`, true)}
      </div>`;
  }

  function renderAttributeTask(row) {
    const attribute = row.attribute_report || {};
    const searchConverged = attribute.converged === true || Number(row.status) === 2;
    const finalResult = searchConverged
      ? (attribute.final_result || attribute.recommendation || null)
      : (attribute.recommendation || null);
    const resultHeading = searchConverged ? '最终推荐结果' : '当前最佳结果';
    const resultBadge = searchConverged ? '最终推荐' : '当前最佳';
    const initialRatings = attribute.initial_ratings || {};
    const searchPath = Array.isArray(attribute.search_path) ? attribute.search_path : [];
    const candidates = Array.isArray(attribute.candidates) ? [...attribute.candidates] : [];
    const runs = Array.isArray(row.runs) ? row.runs : [];
    const stats = [
      {key: 'crit', legacyKey: 'crit_rating', label: '暴击'},
      {key: 'haste', legacyKey: 'haste_rating', label: '急速'},
      {key: 'mastery', legacyKey: 'mastery_rating', label: '精通'},
      {key: 'versatility', legacyKey: 'versatility_rating', label: '全能'},
    ];
    const rating = (ratings, stat) => {
      if (!ratings || typeof ratings !== 'object') return null;
      return ratings[stat.key] ?? ratings[stat.legacyKey] ?? null;
    };
    const signed = amount => Number.isFinite(Number(amount)) ? `${Number(amount) > 0 ? '+' : ''}${number(amount)}` : '—';
    const statCells = ratings => stats.map(stat => `<td class="right">${number(rating(ratings, stat))}</td>`).join('');
    const finalStatCards = stats.map(stat => {
      const current = rating(finalResult?.ratings, stat);
      const initial = rating(initialRatings, stat);
      const delta = Number(current) - Number(initial);
      return `<div class="attribute-change attribute-stat-delta"><span>${stat.label}</span><b>${number(current)}</b><em class="${delta > 0 ? 'positive' : delta < 0 ? 'negative' : ''}">${Number.isFinite(delta) ? signed(delta) : '—'}</em></div>`;
    }).join('');
    const configuredSteps = (Array.isArray(attribute.steps) ? attribute.steps : [100, 50, 20])
      .map(item => Number(item)).filter(Number.isFinite);
    const stepText = configuredSteps.length ? configuredSteps.join(' → ') : number(attribute.step);
    const rawStopReason = String(attribute.stop_reason || '');
    const stopReason = ({
      local_optimum_20_pairwise: '20 点精细邻域已收敛',
      local_optimum_50_pairwise: '50 点两两邻域已收敛（历史任务）',
      refining_step: '当前精度无显著提升，继续缩小步长',
      max_rounds_reached: '达到最大搜索轮次',
      cycle_detected: '检测到重复中心点，搜索停止',
      insufficient_rating_for_20_transfer: '没有足够绿字生成 20 点转移候选',
      no_valid_candidate: '没有可用候选',
      cancelled: '搜索已取消',
      awaiting_current_round: `第 ${number(attribute.current_round)} 轮执行中`,
      awaiting_marginal_gains: '搜索已收敛，正在测量边际收益',
    })[rawStopReason] || value(rawStopReason, '等待结论');
    const succeeded = runs.filter(run => run.status === 'completed').length;
    const running = runs.filter(run => run.status === 'running').length;
    const pending = runs.filter(run => run.status === 'pending').length;
    const failed = runs.filter(run => run.status === 'failed').length;
    const progress = runs.length ? Math.round((succeeded + failed) / runs.length * 100) : 0;
    const initialDps = Number(searchPath[0]?.dps);
    const finalDps = Number(finalResult?.dps);
    const finalDelta = Number.isFinite(initialDps) && Number.isFinite(finalDps) ? finalDps - initialDps : NaN;
    const finalReportUrl = String(finalResult?.report_url || '');
    const finalReportAction = finalReportUrl
      ? `<a class="primary-link attribute-final-report-link" href="${esc(finalReportUrl)}" target="_blank" rel="noopener noreferrer">查看${searchConverged ? '最终' : '当前最佳'}结果报告 <span aria-hidden="true">↗</span></a>`
      : `<span class="attribute-report-unavailable">${searchConverged ? '最终' : '当前最佳'}候选的 HTML 报告暂不可用</span>`;
    const trailRows = searchPath.map(point => `<tr><td><b>第 ${number(point.round)} 轮</b></td><td class="right"><b>${number(point.step)}</b></td>${statCells(point.ratings)}<td class="right"><b>${number(point.dps)}</b></td></tr>`).join('');
    const rankedCandidates = candidates
      .filter(item => Number.isFinite(Number(item.dps)))
      .sort((left, right) => Number(right.dps) - Number(left.dps) || Number(left.id) - Number(right.id));
    const candidateRows = rankedCandidates.map((item, index) => {
      const isFinal = searchConverged && Number(item.id) === Number(finalResult?.id);
      const isCurrentBest = !searchConverged && Number(item.id) === Number(finalResult?.id);
      return `<tr class="${isFinal || isCurrentBest ? 'rank-winner attribute-final-candidate' : ''}"><td><span class="rank-medal">${index === 0 ? '🥇' : index + 1}</span></td><td><b>${value(item.label, `Run #${item.id}`)}</b>${isFinal || isCurrentBest ? `<small class="attribute-final-badge">${resultBadge}</small>` : ''}</td><td class="right">${number(item.round)}</td><td class="right">${number(item.step)}</td>${statCells(item.ratings)}<td class="right"><b>${number(item.dps)}</b></td></tr>`;
    }).join('');
    const currentRound = Number(attribute.current_round);
    const currentRoundRuns = runs.filter(run => Number(run.round_number) === currentRound);
    const runStatusLabels = {pending: '等待', running: '执行中', completed: '完成', failed: '失败'};
    const currentRoundRows = currentRoundRuns.map(run => {
      const summary = run.candidate_summary || {};
      const ratings = summary.attribute_ratings || {};
      const dps = run.result_summary?.dps;
      const runState = String(run.status || 'pending');
      return `<tr><td><b>${value(run.candidate_label, `Run #${run.id}`)}</b></td><td><span class="pill ${statusKey(runState)}">${value(runStatusLabels[runState], runState)}</span></td>${statCells(ratings)}<td class="right">${Number.isFinite(Number(dps)) ? `<b>${number(dps)}</b>` : '—'}</td></tr>`;
    }).join('');
    const initialStats = stats.map(stat => `<div><dt>${stat.label}</dt><dd>${number(rating(initialRatings, stat))}</dd></div>`).join('');
    const marginalGains = Array.isArray(attribute.marginal_gains) ? [...attribute.marginal_gains] : [];
    const marginalOrder = new Map(stats.map((stat, index) => [stat.key, index]));
    marginalGains.sort((left, right) =>
      (marginalOrder.get(left.stat) ?? 99) - (marginalOrder.get(right.stat) ?? 99)
      || Number(left.amount) - Number(right.amount)
    );
    const marginalPercent = amount => Number.isFinite(Number(amount))
      ? `${Number(amount) > 0 ? '+' : ''}${Number(amount).toFixed(3)}%`
      : '—';
    const marginalRows = marginalGains.map((item, index) => {
      const stat = stats.find(candidate => candidate.key === item.stat);
      const previous = marginalGains[index - 1];
      const groupStart = !previous || previous.stat !== item.stat;
      const gain = Number(item.dps_gain);
      return `<tr class="${groupStart ? 'marginal-group-start' : ''}"><td>${groupStart ? `<b>${value(stat?.label, item.stat)}</b>` : ''}</td><td class="right"><b>+${number(item.amount)}</b></td><td class="right">${number(item.dps)}</td><td class="right delta ${gain > 0 ? 'positive' : gain < 0 ? 'negative' : ''}">${signed(item.dps_gain)}</td><td class="right delta ${gain > 0 ? 'positive' : gain < 0 ? 'negative' : ''}">${marginalPercent(item.gain_percent)}</td></tr>`;
    }).join('');
    const marginalEmpty = !searchConverged
      ? '搜索尚未收敛，完成当前邻域后才会开始边际收益测量。'
      : attribute.marginal_gain_status === 'pending'
        ? '最优解已固定，正在完成 12 个边际收益 Run。'
        : '此任务没有边际收益结果。';
    const marginalBaselineDps = attribute.marginal_gain_baseline_dps ?? finalResult?.dps;
    const marginalDescription = searchConverged
      ? `固定最优四属性与其他全部模拟条件，仅额外增加一个属性；提升值和百分比均相对固定最优点 ${number(marginalBaselineDps)} DPS。`
      : '当前搜索仍在执行；收敛后才会固定最优点并追加 12 个边际收益 Run。';

    return `<section class="hero attribute-hero"><div class="report-kicker">ATTRIBUTE OPTIMIZATION</div><div class="hero-status"><span class="pill">任务${statusClass(row)}</span><span class="pill">${number(progress)}% 完成</span><span class="pill">${number(runs.length)} 个候选 Run</span></div><div class="hero-primary-column"><h1>${value(row.name, `任务 #${objectId}`)}</h1><div class="hero-resource-stack" aria-label="模拟资源"><div class="hero-resource-line"><span>APL</span><b>${value(row.apl_name, '未命名')}</b></div><div class="hero-resource-line"><span>Profile</span><b>${value(row.profile_name, '未命名')}</b></div></div></div><div class="hero-meta"><span class="pill">搜索精度 ${value(stepText)} · 当前 ${number(attribute.step)}</span><span class="pill">${stopReason}</span><span class="pill">更新 ${value(row.updated_at)}</span></div></section>
      <div class="grid attribute-task-grid">
        <section class="card wide attribute-report attribute-final-result"><div class="report-kicker">${searchConverged ? 'FINAL RESULT' : 'LIVE BEST'}</div><div class="attribute-final-heading"><div><h2>${resultHeading}</h2><p>${searchConverged ? `以任务持久化结论为准，并精确绑定产生该结论的 Run #${value(finalResult?.id)}。` : `第 ${number(attribute.current_round)} 轮尚未结束；这里只展示当前已完成 Run 中的最佳测量值，后续仍可能变化。`}</p></div><div class="attribute-final-dps"><span>${searchConverged ? '推荐' : '当前最佳'} DPS</span><b>${number(finalResult?.dps)}</b><em class="${finalDelta > 0 ? 'positive' : finalDelta < 0 ? 'negative' : ''}">${Number.isFinite(finalDelta) ? `${signed(finalDelta)} vs 初始轮` : '等待完整结果'}</em></div></div><div class="attribute-grid">${finalResult ? finalStatCards : '<p class="muted">当前尚无已完成候选。</p>'}</div><div class="attribute-final-footer"><span><b>${stopReason}</b> · 完成 ${number(attribute.rounds_completed)} 轮搜索</span>${finalReportAction}</div></section>
        ${card('当前轮候选', `<p class="muted">第 ${number(attribute.current_round)} 轮的全部 Run，包括已完成、执行中和等待中的候选；尚未完成的候选不会提前参与排名。</p><div class="table-scroll"><table class="attribute-current-round-table"><thead><tr><th>候选</th><th>状态</th>${stats.map(stat => `<th class="right">${stat.label}</th>`).join('')}<th class="right">DPS</th></tr></thead><tbody>${currentRoundRows || '<tr><td colspan="7" class="empty">当前轮尚未创建候选 Run</td></tr>'}</tbody></table></div>`, true)}
        ${card('收敛后边际收益', `<p class="muted">${marginalDescription}</p><div class="table-scroll"><table class="marginal-gain-table"><thead><tr><th>属性</th><th class="right">额外属性</th><th class="right">绝对 DPS</th><th class="right">DPS 提升</th><th class="right">提升百分比</th></tr></thead><tbody>${marginalRows || `<tr><td colspan="5" class="empty">${marginalEmpty}</td></tr>`}</tbody></table></div>`, true)}
        ${card('搜索轨迹', `<p class="muted">每一行是该轮搜索的中心点和权威步长；系统在当前精度无显著提升时按 ${value(stepText)} 逐步缩小步长。</p><div class="table-scroll"><table class="attribute-path-table"><thead><tr><th>轮次</th><th class="right">步长</th>${stats.map(stat => `<th class="right">${stat.label}</th>`).join('')}<th class="right">中心 DPS</th></tr></thead><tbody>${trailRows || '<tr><td colspan="7" class="empty">首轮中心点尚未完成</td></tr>'}</tbody></table></div>`, true)}
        ${card('候选测量排名', `<p class="muted">展示全部已完成候选的真实测量值；${searchConverged ? '最终推荐由任务持久化结论标记' : '当前最佳仅代表已完成测量，搜索收敛前不是最终结论'}，不以页面临时重算替代。</p><div class="table-scroll"><table class="ranking-table attribute-ranking-table"><thead><tr><th>排名</th><th>候选</th><th class="right">轮次</th><th class="right">步长</th>${stats.map(stat => `<th class="right">${stat.label}</th>`).join('')}<th class="right">DPS</th></tr></thead><tbody>${candidateRows || '<tr><td colspan="9" class="empty">暂无已完成候选</td></tr>'}</tbody></table></div>`, true)}
        ${card('寻优口径', `<dl>${initialStats}<div><dt>总绿字</dt><dd>${number(attribute.total_rating)}</dd></div><div><dt>搜索精度</dt><dd>${value(stepText)}（当前 ${number(attribute.step)}）</dd></div><div><dt>完成轮次</dt><dd>${number(attribute.rounds_completed)}</dd></div><div><dt>任务进度</dt><dd>成功 ${number(succeeded)} · 运行 ${number(running)} · 等待 ${number(pending)} · 失败 ${number(failed)}</dd></div></dl><details><summary>结论口径</summary>只展示属性寻优搜索路径、候选测量值与最终推荐；技能、Buff、施放序列等单次 SimC 原始分析不参与本页结论。</details>`, true)}
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
    const chartItems = [baseline, ...candidates]
      .filter(item => item && item.is_complete === true && Number.isFinite(Number(item.dps)))
      .sort((leftItem, rightItem) => Number(leftItem.dps) - Number(rightItem.dps));
    let comparisonChartPanel = '';
    if (!isAttribute) {
      const minimumDps = chartItems.length ? Number(chartItems[0].dps) : NaN;
      const maximumDps = chartItems.length ? Number(chartItems[chartItems.length - 1].dps) : NaN;
      const bars = chartItems.map((item, index) => {
        const dps = Number(item.dps);
        const ratio = minimumDps > 0 ? dps / minimumDps * 100 : 0;
        const width = maximumDps > 0 ? dps / maximumDps * 100 : 0;
        const icon = item.candidate_icon_url ? `<img src="${esc(item.candidate_icon_url)}" alt="" loading="lazy">` : '<span class="candidate-icon-placeholder">?</span>';
        return `<div class="comparison-relative-bar-chart" data-candidate-icon-url="${esc(item.candidate_icon_url || '')}"><div class="comparison-relative-bar-label"><span class="candidate-icon">${icon}</span><b>${value(item.label || item.name || `方案 ${index + 1}`)}</b><small>${number(dps)} DPS · ${ratio.toFixed(1)}%</small></div><div class="comparison-relative-track"><div class="comparison-relative-fill ${item.is_base === true ? 'is-baseline' : ''}" style="width:${Math.min(100, Math.max(0, width))}%"></div></div></div>`;
      }).join('');
      comparisonChartPanel = card('最低 DPS 基准 · 相对比例', `<section class="comparison-simulation-context"><b>冻结模拟上下文</b><span>${baselineFacts}</span></section><div class="comparison-relative-bars">${bars || '<div class="empty">暂无完整 DPS，无法绘制比例图</div>'}</div><p class="muted">最低完成候选固定为 100%，其他候选按 DPS / 最低 DPS 计算。绝对 DPS 和相对基线变化见下方表格。</p>`, true);
    }
    const recommendation = attribute?.recommendation || null;
    const initial = attribute?.initial_ratings || {};
    const statLabels = {crit:'暴击',crit_rating:'暴击',haste:'急速',haste_rating:'急速',mastery:'精通',mastery_rating:'精通',versatility:'全能',versatility_rating:'全能'};
    const attributeChanges = recommendation ? Object.entries(recommendation.ratings || {}).map(([key, rating]) => { const delta = Number(rating) - Number(initial[key] || 0); return `<div class="attribute-change attribute-stat-delta"><span>${value(statLabels[key] || key)}</span><b>${number(rating)}</b><em class="${delta > 0 ? 'positive' : delta < 0 ? 'negative' : ''}">${signed(delta)}</em></div>`; }).join('') : '';
    const searchTrail = Array.isArray(attribute?.search_path) ? attribute.search_path : [];
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

  document.addEventListener('click', event => {
    const inputButton = event.target.closest('[data-run-input]');
    if (inputButton) showRunInput(Number(inputButton.dataset.taskId), Number(inputButton.dataset.runId));
    if (event.target.closest('[data-run-input-close]')) document.getElementById('simc-input-dialog')?.close();
  });
})();
