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
  const humanSize = bytes => { const size = Number(bytes); if (!Number.isFinite(size)) return '-'; if (size < 1024) return `${size} B`; if (size < 1048576) return `${(size / 1024).toFixed(1)} KB`; return `${(size / 1048576).toFixed(2)} MB`; };
  const runStatus = status => ({completed: '已完成', running: '运行中', pending: '等待中', failed: '失败'}[String(status)] || value(status));
  const artifactType = type => ({html_report: 'HTML 原生报告'}[String(type)] || value(type));
  const artifactRows = rows => (Array.isArray(rows) ? rows : []).map(item => `<tr><td title="${value(item.file_name)}">${value(item.file_name || item.artifact_type)}</td><td>${artifactType(item.artifact_type)}</td><td class="right">${humanSize(item.file_size)}</td><td class="right">${item.can_preview === true ? `<a href="${esc(item.preview_url)}">查看原生报告</a>` : '不可预览'}</td></tr>`).join('');

  function renderTask(row) {
    const report = row.report_summary || {};
    const character = report.character || {};
    const simulation = report.simulation || {};
    const params = row.simulation_params || {};
    const abilities = Array.isArray(report.top_abilities) ? report.top_abilities : [];
    const talents = report.talents || {};
    const setBonuses = Array.isArray(talents.set_bonuses) ? talents.set_bonuses : [];
    const runs = Array.isArray(row.runs) ? row.runs : [];
    const artifacts = Array.isArray(row.artifacts) ? row.artifacts : [];
    const hasStructuredReport = Boolean(character.name || character.class || character.spec || abilities.length || talents.string || setBonuses.length);
    const characterPills = hasStructuredReport ? `<span class="pill">角色 ${value(character.name, '未命名')}</span><span class="pill">${value(character.class, '职业未知')} · ${value(character.spec, '专精未知')}</span>` : '<span class="pill warning">结构化分析待完善</span>';
    const abilityRows = abilities.map(item => `<tr><td>${value(item.name)}</td><td class="right">${number(item.dps)}</td><td class="right">${value(item.dps_percent)}</td></tr>`).join('');
    const runRows = runs.map(run => `<tr><td>#${value(run.sequence)}</td><td>${runStatus(run.status)}</td><td class="right">${number(run.result_summary?.dps)}</td><td>${value(run.started_at)}</td><td>${value(run.completed_at)}</td></tr>`).join('');
    root.innerHTML = `<section class="hero"><span class="pill">任务${statusClass(row)}</span><h1>${value(row.name, `任务 #${objectId}`)}</h1><div class="hero-meta">${characterPills}<span class="pill">更新 ${value(row.updated_at)}</span></div></section>
      ${hasStructuredReport ? '' : '<div class="analysis-warning"><b>模拟已成功，结构化分析信息不完整</b><span>当前仅展示已确认的 DPS、参数、执行轮次和原生报告；缺失字段不会被猜测填充。</span></div>'}
      <div class="grid">
        ${card('结果概览', `<div class="metrics"><div class="metric"><span>DPS</span><b>${number(report.dps ?? row.result_summary?.dps)}</b></div><div class="metric"><span>迭代次数</span><b>${number(simulation.iterations ?? params.iterations)}</b></div><div class="metric"><span>战斗时长</span><b>${value(simulation.fight_length ?? params.max_time)} 秒</b></div><div class="metric"><span>目标数</span><b>${value(params.desired_targets ?? params.target_count)}</b></div></div>`, true)}
        ${card('角色', `<dl><div><dt>名称</dt><dd>${value(character.name)}</dd></div><div><dt>职业 / 专精</dt><dd>${value(character.class)} / ${value(character.spec)}</dd></div><div><dt>种族</dt><dd>${value(character.race)}</dd></div><div><dt>等级</dt><dd>${value(character.level)}</dd></div></dl>`)}
        ${card('模拟参数', `<dl><div><dt>战斗模型</dt><dd>${value(simulation.fight_style ?? params.fight_style)}</dd></div><div><dt>最长时间</dt><dd>${value(params.max_time)} 秒</dd></div><div><dt>迭代次数</dt><dd>${value(simulation.iterations ?? params.iterations)}</dd></div><div><dt>目标数量</dt><dd>${value(params.desired_targets ?? params.target_count)}</dd></div><div><dt>报告时间</dt><dd>${value(simulation.timestamp)}</dd></div></dl>`)}
        ${card('天赋与套装', `<dl><div><dt>天赋字符串</dt><dd>${value(talents.string, '报告未解析到天赋字符串')}</dd></div><div><dt>套装效果</dt><dd>${setBonuses.length ? setBonuses.map(item => `<span>${value(item)}</span>`).join('<br>') : '报告未解析到套装效果'}</dd></div></dl>`, true)}
        ${card('主要技能', `<div class="table-scroll"><table><thead><tr><th>技能</th><th class="right">DPS</th><th class="right">占比</th></tr></thead><tbody>${abilityRows || '<tr><td colspan="3" class="empty">暂无已解析技能</td></tr>'}</tbody></table></div>`, true)}
        ${card('执行轮次', `<div class="table-scroll"><table><thead><tr><th>轮次</th><th>状态</th><th class="right">DPS</th><th>开始</th><th>完成</th></tr></thead><tbody>${runRows || '<tr><td colspan="5" class="empty">暂无执行轮次</td></tr>'}</tbody></table></div><details><summary>技术追溯说明</summary>仅展示轮次时间与状态；命令、路径、哈希及原始错误均不在页面展示。</details>`, true)}
        ${card('Artifact / 原生报告', `<p class="muted">原生报告继续通过独立鉴权页面读取。</p><div class="table-scroll"><table><thead><tr><th>文件</th><th>类型</th><th class="right">大小</th><th class="right">操作</th></tr></thead><tbody>${artifactRows(artifacts) || '<tr><td colspan="4" class="empty">暂无 Artifact</td></tr>'}</tbody></table></div>`, true)}
        ${card('引用版本', `<dl><div><dt>Profile</dt><dd>#${value(row.profile_id)} · v${value(row.profile_version_id)}</dd></div><div><dt>基础模板</dt><dd>#${value(row.template_id)} · v${value(row.template_version_id)}</dd></div><div><dt>APL</dt><dd>#${value(row.apl_id)} · v${value(row.apl_version_id)}</dd></div><div><dt>来源任务</dt><dd>${row.source_task_id ? `<a href="/dashboard/simc/tasks/${Number(row.source_task_id)}/">#${Number(row.source_task_id)}</a>` : '-'}</dd></div></dl><details><summary>为什么显示版本号？</summary>版本引用用于复现，不展示配置原文或服务器路径。</details>`, true)}
      </div>`;
  }

  function renderBatch(row) {
    const members = Array.isArray(row.tasks) ? row.tasks : [];
    const ranking = Array.isArray(row.ranking) ? [...row.ranking].sort((a, b) => (a.rank || 9999) - (b.rank || 9999)) : [];
    const memberRows = members.map(member => `<tr><td><a href="/dashboard/simc/tasks/${member.id}/">${value(member.name, `任务 #${member.id}`)}</a></td><td>${value(member.status_label || member.status)}</td><td>${value(member.updated_at)}</td></tr>`).join('');
    const rankRows = ranking.map(item => `<tr><td>${value(item.rank)}</td><td><a href="/dashboard/simc/tasks/${item.id}/">${value(item.label || item.name)}</a></td><td class="right">${number(item.dps)}</td></tr>`).join('');
    root.innerHTML = `<section class="hero"><span class="pill">${statusClass(row)}</span><h1>${value(row.name, `批次 #${objectId}`)}</h1><div class="hero-meta"><span class="pill">${number(row.percent)}% 完成</span><span class="pill">${number(row.total)} 个成员</span><span class="pill">更新 ${value(row.updated_at)}</span></div></section><div class="grid">
      ${card('批次进度', `<div class="metrics"><div class="metric"><span>成功</span><b>${number(row.succeeded)}</b></div><div class="metric"><span>运行</span><b>${number(row.running)}</b></div><div class="metric"><span>等待</span><b>${number(row.pending)}</b></div><div class="metric"><span>失败</span><b>${number(row.failed)}</b></div></div>`, true)}
      ${card('DPS 排名', `<div class="table-scroll"><table><thead><tr><th>排名</th><th>候选角色 / 方案</th><th class="right">DPS</th></tr></thead><tbody>${rankRows || '<tr><td colspan="3" class="empty">暂无可排名结果</td></tr>'}</tbody></table></div>`, true)}
      ${card('批次成员', `<div class="table-scroll"><table><thead><tr><th>任务</th><th>状态</th><th>更新时间</th></tr></thead><tbody>${memberRows || '<tr><td colspan="3" class="empty">暂无成员</td></tr>'}</tbody></table></div>`, true)}
      ${card('Artifact / 原生报告', `<p class="muted">产物和原生报告均保持鉴权访问。</p><div class="table-scroll"><table><tbody>${artifactRows(row.artifacts) || '<tr><td class="empty">暂无 Artifact</td></tr>'}</tbody></table></div>`, true)}
    </div>`;
  }

  fetch(`/api/simc-workbench/${kind}/${objectId}/`, {headers: {'Accept': 'application/json'}})
    .then(async response => { const payload = await response.json(); if (!response.ok || !payload.success) throw new Error(payload.error || '详情加载失败'); return payload.data || {}; })
    .then(kind === 'tasks' ? renderTask : renderBatch)
    .catch(() => { root.innerHTML = '<div class="error"><b>详情暂时无法加载</b><p>请返回工作台稍后重试。为避免泄露内部信息，此处不展示原始错误。</p></div>'; });
})();
