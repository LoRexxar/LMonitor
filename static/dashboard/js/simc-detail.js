(() => {
  'use strict';
  const root = document.getElementById('simc-detail-root');
  if (!root) return;
  const kind = root.dataset.simcDetailKind === 'batches' ? 'batches' : 'tasks';
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
    const runRows = runs.map(run => `<tr><td>#${value(run.sequence)}</td><td><span class="status-dot ${statusKey(run.status)}"></span>${runStatus(run.status)}</td><td class="right">${number(run.result_summary?.dps)}</td><td>${value(run.started_at)}</td><td>${value(run.completed_at)}</td></tr>`).join('');
    const talentValue = talents.string ? `<code class="talent-code">${value(talents.string)}</code>` : '报告未解析到天赋字符串';
    const bonusValue = setBonuses.length ? `<div class="bonus-list">${setBonuses.map(item => `<span class="bonus-tag">${value(item)}</span>`).join('')}</div>` : '报告未解析到套装效果';
    root.innerHTML = `<section class="hero"><span class="pill">任务${statusClass(row)}</span><h1>${value(row.name, `任务 #${objectId}`)}</h1><div class="hero-meta">${characterPills}<span class="pill">更新 ${value(row.updated_at)}</span></div>${nativeReportAction}</section>
      ${hasStructuredReport ? '' : '<div class="analysis-warning"><b>模拟已成功，结构化分析信息不完整</b><span>当前仅展示已确认的 DPS、参数、执行轮次和原生报告；缺失字段不会被猜测填充。</span></div>'}
      <div class="grid">
        ${card('结果概览', `<div class="metrics"><div class="metric"><span>DPS</span><b>${number(report.dps ?? row.result_summary?.dps)}</b></div><div class="metric"><span>迭代次数</span><b>${number(simulation.iterations ?? params.iterations)}</b></div><div class="metric"><span>战斗时长</span><b>${value(simulation.fight_length ?? params.max_time)} 秒</b></div><div class="metric"><span>目标数</span><b>${value(params.desired_targets ?? params.target_count)}</b></div></div>`, true)}
        ${card('角色', `<dl><div><dt>名称</dt><dd>${value(character.name)}</dd></div><div><dt>职业 / 专精</dt><dd>${value(character.class)} / ${value(character.spec)}</dd></div><div><dt>种族</dt><dd>${value(character.race)}</dd></div><div><dt>等级</dt><dd>${value(character.level)}</dd></div></dl>`)}
        ${card('模拟参数', `<dl><div><dt>战斗模型</dt><dd>${value(simulation.fight_style ?? params.fight_style)}</dd></div><div><dt>最长时间</dt><dd>${value(params.max_time)} 秒</dd></div><div><dt>迭代次数</dt><dd>${value(simulation.iterations ?? params.iterations)}</dd></div><div><dt>目标数量</dt><dd>${value(params.desired_targets ?? params.target_count)}</dd></div><div><dt>报告时间</dt><dd>${value(simulation.timestamp)}</dd></div></dl>`)}
        ${card('天赋与套装', `<dl><div><dt>天赋字符串</dt><dd>${talentValue}</dd></div><div><dt>套装效果</dt><dd>${bonusValue}</dd></div></dl>`, true)}
        ${card('技能伤害与触发明细', `<p class="muted">保留报告中的全部伤害技能；施放、间隔、暴击、覆盖、Tick 和刷新次数均按原始 SimC 数值展示。</p><div class="table-scroll"><table class="dense-table"><thead><tr><th>技能</th><th class="right">DPS</th><th>伤害占比</th><th class="right">施放</th><th class="right">间隔</th><th class="right">暴击</th><th class="right">覆盖</th><th class="right">Ticks</th><th class="right">刷新</th></tr></thead><tbody>${abilityRows || '<tr><td colspan="9" class="empty">暂无已解析技能</td></tr>'}</tbody></table></div>`, true)}
        ${card('动态 Buff / Proc', `<p class="muted">展示全部动态 Buff 的启动、刷新、总触发、触发间隔、持续时间、覆盖率、收益覆盖和各层数覆盖。</p><div class="table-scroll"><table class="dense-table"><thead><tr><th>Buff</th><th class="right">启动</th><th class="right">刷新</th><th class="right">总触发</th><th class="right">触发间隔</th><th class="right">持续</th><th class="right">覆盖率</th><th class="right">收益覆盖</th><th class="right">溢出</th><th class="right">到期</th><th class="right">触发率</th><th>属性效果</th></tr></thead><tbody>${dynamicBuffRows || '<tr><td colspan="12" class="empty">暂无动态 Buff</td></tr>'}</tbody></table></div>`, true)}
        ${card('常驻 Buff', `<div class="table-scroll"><table class="dense-table"><thead><tr><th>Buff</th><th class="right">最大层数</th><th class="right">基础持续</th><th class="right">基础冷却</th><th>属性效果</th></tr></thead><tbody>${constantBuffRows || '<tr><td colspan="5" class="empty">暂无常驻 Buff</td></tr>'}</tbody></table></div>`, true)}
        ${card('执行轮次', `<div class="table-scroll"><table><thead><tr><th>轮次</th><th>状态</th><th class="right">DPS</th><th>开始</th><th>完成</th></tr></thead><tbody>${runRows || '<tr><td colspan="5" class="empty">暂无执行轮次</td></tr>'}</tbody></table></div><details><summary>技术追溯说明</summary>仅展示轮次时间与状态；命令、路径、哈希及原始错误均不在页面展示。</details>`, true)}
        ${card('Artifact / 原生报告', `<p class="muted">原生报告继续通过独立鉴权页面读取。</p><div class="table-scroll"><table><thead><tr><th>文件</th><th>类型</th><th class="right">大小</th><th class="right">操作</th></tr></thead><tbody>${artifactRows(artifacts) || '<tr><td colspan="4" class="empty">暂无 Artifact</td></tr>'}</tbody></table></div>`, true)}
        ${card('引用版本', `<dl><div><dt>Profile</dt><dd>#${value(row.profile_id)} · v${value(row.profile_version_id)}</dd></div><div><dt>基础模板</dt><dd>#${value(row.template_id)} · v${value(row.template_version_id)}</dd></div><div><dt>APL</dt><dd>#${value(row.apl_id)} · v${value(row.apl_version_id)}</dd></div><div><dt>来源任务</dt><dd>${row.source_task_id ? `<a href="/dashboard/simc/tasks/${Number(row.source_task_id)}/">#${Number(row.source_task_id)}</a>` : '-'}</dd></div></dl><details><summary>为什么显示版本号？</summary>版本引用用于复现，不展示配置原文或服务器路径。</details>`, true)}
      </div>`;
  }

  function renderBatch(row) {
    const members = Array.isArray(row.tasks) ? row.tasks : [];
    const ranking = Array.isArray(row.ranking) ? [...row.ranking] : [];
    const attribute = row.attribute_report || null;
    const isAttribute = row.batch_type === 'attribute_sweep' && attribute;
    const baseline = ranking.find(item => item.is_base === true) || null;
    const candidates = ranking.filter(item => item.is_base !== true).sort((a, b) => (a.rank || 9999) - (b.rank || 9999));
    const baselineDps = baseline?.is_complete === true ? Number(baseline.dps) : NaN;
    const signed = amount => Number.isFinite(Number(amount)) ? `${Number(amount) > 0 ? '+' : ''}${number(amount)}` : '—';
    const memberRows = members.map(member => `<tr><td><a href="/dashboard/simc/tasks/${member.id}/">${value(member.name, `任务 #${member.id}`)}</a></td><td>${value(member.status_label || member.status)}</td><td>${value(member.updated_at)}</td></tr>`).join('');
    const baselinePanel = baseline ? `<section class="comparison-baseline"><div><span>当前 Profile 基线</span><a href="/dashboard/simc/tasks/${baseline.id}/">${value(baseline.label || baseline.name)}</a></div><b>${baseline.is_complete === true ? number(baseline.dps) : '结果不完整'}</b></section>` : '<section class="comparison-baseline muted">本批次未包含 Profile 基线</section>';
    const rankRows = candidates.map(item => {
      const complete = item.is_complete === true && Number.isFinite(Number(item.dps));
      const delta = complete && Number.isFinite(baselineDps) ? Number(item.dps) - baselineDps : NaN;
      const deltaPercent = Number.isFinite(delta) && baselineDps !== 0 ? delta / baselineDps * 100 : NaN;
      const deltaText = Number.isFinite(deltaPercent) ? `${signed(delta)} (${deltaPercent > 0 ? '+' : ''}${deltaPercent.toFixed(2)}%)` : '—';
      return `<tr class="${item.rank === 1 ? 'rank-winner comparison-winner' : ''} ${complete ? '' : 'rank-incomplete'}"><td><span class="rank-medal">${complete ? (item.rank === 1 ? '🥇' : value(item.rank)) : '—'}</span></td><td><a href="/dashboard/simc/tasks/${item.id}/">${value(item.label || item.name)}</a>${complete ? '' : '<small class="incomplete-label">结果不完整，不参与排名</small>'}</td><td class="right"><b>${complete ? number(item.dps) : '—'}</b></td><td class="right delta comparison-delta ${Number(delta) > 0 ? 'positive' : Number(delta) < 0 ? 'negative' : ''}">${deltaText}</td></tr>`;
    }).join('');
    const recommendation = attribute?.recommendation || null;
    const initial = attribute?.initial_ratings || {};
    const statLabels = {crit_rating:'暴击',haste_rating:'急速',mastery_rating:'精通',versatility_rating:'全能'};
    const attributeChanges = recommendation ? Object.entries(recommendation.ratings || {}).map(([key, rating]) => { const delta = Number(rating) - Number(initial[key] || 0); return `<div class="attribute-change attribute-stat-delta"><span>${value(statLabels[key] || key)}</span><b>${number(rating)}</b><em class="${delta > 0 ? 'positive' : delta < 0 ? 'negative' : ''}">${signed(delta)}</em></div>`; }).join('') : '';
    const searchTrail = Array.isArray(attribute?.history) ? attribute.history : [];
    const trailRows = searchTrail.slice(-8).map((step, index) => `<span>第 ${index + 1} 步 · ${number(step.dps)}</span>`).join('');
    const attributePanel = isAttribute ? `<section class="card wide attribute-report attribute-landscape"><div class="report-kicker">ATTRIBUTE OPTIMIZATION</div><h2>属性寻优结论</h2><div class="report-summary"><div><span>推荐 DPS</span><b>${number(recommendation?.dps)}</b></div><div><span>搜索轮次</span><b>${number(attribute.rounds_completed)} / ${number(attribute.current_round)}</b></div><div><span>步进粒度</span><b>${number(attribute.step)}</b></div><div><span>结论</span><b>${attribute.local_optimum ? '局部最优' : '继续搜索'}</b></div></div><h3>推荐属性</h3><div class="attribute-grid">${attributeChanges || '<p class="muted">等待候选完成后生成属性变化。</p>'}</div><h3>搜索轨迹</h3><div class="search-trail">${trailRows || '<span class="muted">暂无轨迹数据</span>'}</div></section>` : '';
    root.innerHTML = `<section class="hero ${isAttribute ? 'attribute-hero' : 'comparison-hero'}"><span class="pill">${statusClass(row)}</span><div class="report-kicker">${isAttribute ? '属性寻优报告' : '候选对比报告'}</div><h1>${value(row.name, `批次 #${objectId}`)}</h1><div class="hero-meta"><span class="pill">${number(row.percent)}% 完成</span><span class="pill">${number(row.total)} 个成员</span><span class="pill">更新 ${value(row.updated_at)}</span></div></section><div class="grid">
      ${card('批次进度', `<div class="metrics"><div class="metric"><span>成功</span><b>${number(row.succeeded)}</b></div><div class="metric"><span>运行</span><b>${number(row.running)}</b></div><div class="metric"><span>等待</span><b>${number(row.pending)}</b></div><div class="metric"><span>失败</span><b>${number(row.failed)}</b></div></div>`, true)}
      ${attributePanel}
      ${isAttribute ? '' : baselinePanel}
      ${card(isAttribute ? '候选测量排名' : '候选 DPS 排名与基线差值', `<div class="table-scroll"><table class="ranking-table"><thead><tr><th>排名</th><th>候选角色 / 方案</th><th class="right">DPS</th><th class="right">相对基线（数值 / 百分比）</th></tr></thead><tbody>${rankRows || '<tr><td colspan="4" class="empty">暂无可排名结果</td></tr>'}</tbody></table></div>`, true)}
      ${card('批次成员', `<div class="table-scroll"><table><thead><tr><th>任务</th><th>状态</th><th>更新时间</th></tr></thead><tbody>${memberRows || '<tr><td colspan="3" class="empty">暂无成员</td></tr>'}</tbody></table></div>`, true)}
      ${card('Artifact / 原生报告', `<p class="muted">产物和原生报告均保持鉴权访问。</p><div class="table-scroll"><table><tbody>${artifactRows(row.artifacts) || '<tr><td class="empty">暂无 Artifact</td></tr>'}</tbody></table></div>`, true)}
    </div>`;
  }

  fetch(`/api/simc-workbench/${kind}/${objectId}/`, {headers: {'Accept': 'application/json'}})
    .then(async response => { const payload = await response.json(); if (!response.ok || !payload.success) throw new Error(payload.error || '详情加载失败'); return payload.data || {}; })
    .then(kind === 'tasks' ? renderTask : renderBatch)
    .catch(() => { root.innerHTML = '<div class="error"><b>详情暂时无法加载</b><p>请返回工作台稍后重试。为避免泄露内部信息，此处不展示原始错误。</p></div>'; });
})();
