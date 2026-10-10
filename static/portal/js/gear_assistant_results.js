/* Presentation only: consume optimizer facts without resolving or modifying gear. */
(() => {
  "use strict";
  const esc = value => String(value ?? "").replace(/[&<>'"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'})[c]);
  const labels = {crit: '暴击', haste: '急速', mastery: '精通', versatility: '全能', stamina: '耐力', strength: '力量', agility: '敏捷', intellect: '智力', armor: '护甲', primary: '主属性'};
  const rarity = {0: '粗糙', 1: '普通', 2: '优秀', 3: '精良', 4: '史诗', 5: '传说', 6: '神器', 7: '传家宝'};
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const statText = stats => Object.entries(stats || {}).filter(([, value]) => number(value) !== 0).map(([key, value]) => `${labels[key] || key} ${number(value) > 0 ? '+' : ''}${number(value).toLocaleString('zh-CN', {maximumFractionDigits: 2})}`).join(' · ');
  const quality = entry => number(entry?.variant?.crafting_quality) ? `品质 ${number(entry.variant.crafting_quality)}` : '';
  const name = entry => entry?.item?.name || '名称未提供';
  function icon(entry) {
    const url = entry?.item?.icon_url || '';
    return /^https?:\/\//i.test(url) || /^\/(?!\/)/.test(url)
      ? `<img class="assistant-result-icon" src="${esc(url)}" alt="" loading="lazy">`
      : '<span class="assistant-result-icon is-placeholder" aria-hidden="true">◇</span>';
  }
  function identity(entry) {
    const v = entry.variant || {};
    return JSON.stringify([entry.item?.item_id, v.id, v.key, v.item_level, v.crafting_quality, v.game_build, v.data_branch, v.bonus_ids, v.stats, v.effects]);
  }
  function effects(entry) {
    return (entry?.resolvedEffects || entry?.variant?.effects || []).map(row => typeof row === 'string' ? row : (row.description_zh || row.description || '')).filter(Boolean);
  }
  function enhancementText(entry) {
    return [name(entry), quality(entry), statText(entry.resolvedStats || entry.variant?.stats), ...effects(entry)].filter(Boolean).join(' · ');
  }
  function tooltipAttrs(title, description) {
    return `data-assistant-tooltip-name="${esc(title)}" data-assistant-tooltip="${esc(description)}"`;
  }
  function slotEntries(plan, slots) {
    return slots.map(slot => ({key: slot.key, label: plan.equipment?.[slot.key]?.slot_label || slot.label, entry: plan.equipment?.[slot.key]}));
  }
  function gemGroups(rows) {
    const groups = new Map();
    rows.forEach(({key, label, entry}) => (entry?.gems || []).forEach((gem, index) => {
      if (!gem?.item) return;
      const id = identity(gem);
      if (!groups.has(id)) groups.set(id, {gem, destinations: []});
      groups.get(id).destinations.push(`${label || key} · 第 ${index + 1} 孔`);
    }));
    return [...groups.values()];
  }
  function recommendations(plan, rows) {
    const flask = plan.flask;
    const flaskName = flask?.key === 'none' ? '不使用属性合剂' : flask?.name || '合剂信息未提供';
    const groups = gemGroups(rows);
    return `<div class="assistant-plan-preparation">
      <div class="assistant-plan-flask assistant-choice-line"><span>合剂</span><button type="button" class="assistant-choice" ${tooltipAttrs(flaskName, statText(flask?.stats) || '无合剂属性加成')}>${esc(flaskName)}</button></div>
      <div class="assistant-plan-gems assistant-choice-line"><span>宝石</span><div>${groups.length ? groups.map((group, index) => `<button type="button" class="assistant-choice" data-gem-group="${index}" ${tooltipAttrs(name(group.gem), `${enhancementText(group.gem)}\n${group.destinations.join('；')}`)}>${esc(name(group.gem))}${quality(group.gem) ? ` <small>${esc(quality(group.gem))}</small>` : ''} <b>×${group.destinations.length}</b></button>`).join('<span class="assistant-choice-separator">、</span>') : '<span class="assistant-result-empty">本方案未镶嵌宝石</span>'}</div></div>
    </div>`;
  }
  function equipmentDescription(entry, label) {
    const v = entry.variant || {};
    const origin = {locked: '已锁定', owned: '来自备选', catalog: '需获取'}[entry.selection_origin] || '状态未提供';
    const track = v.track_label ? `${v.track_label}${v.track_rank ? ` ${v.track_rank}/${v.track_max_rank || '?'}` : ''}` : '';
    const meta = [label, `${entry.itemLevel || v.item_level || '?'} 装等`, track, quality(entry), rarity[entry.item.quality], origin].filter(Boolean).join(' · ');
    // Resolved secondary ratings override source values; do not add enhancements again.
    const stats = statText({...v.stats, ...entry.resolvedStats});
    const selected = (entry.selectedStats || []).map(key => labels[key] || key).join(' / ');
    const gems = (entry.gems || []).filter(gem => gem?.item);
    return [meta, stats, selected && `制造自选：${selected}`, `来源：${entry.acquisition_source_label || '获取来源未提供'}`,
      ...effects(entry),
      ...gems.map((gem, index) => `宝石 第 ${index + 1} 孔：${enhancementText(gem)}`),
      entry.enchant?.item ? `附魔：${enhancementText(entry.enchant)}` : '',
      v.is_intrinsic_embellishment ? '固有美化' : entry.embellishment?.item ? `美化：${enhancementText(entry.embellishment)}` : '',
    ].filter(Boolean).join('\n');
  }
  function equipmentRow({key, label, entry}, catalog) {
    if (!entry?.item) return `<li class="assistant-result-gear" data-plan-slot="${esc(key)}"><span class="assistant-result-slot">${esc(label)}</span><span>未提供装备</span></li>`;
    const source = Array.isArray(entry.acquisition_sources)
      ? globalThis.WowGearSources.shortSourceText({type: entry.variant?.type, sources: entry.acquisition_sources}, catalog)
      : entry.acquisition_source_label || '来源待补全';
    return `<li class="assistant-result-gear" data-plan-slot="${esc(key)}"><span class="assistant-result-slot">${esc(label)}</span><button type="button" class="assistant-item-trigger" ${tooltipAttrs(name(entry), equipmentDescription(entry, label))}>${icon(entry)}<span>${esc(name(entry))}</span></button><small class="assistant-result-level">${esc(entry.itemLevel || entry.variant?.item_level || '—')}</small><small class="assistant-result-source" title="${esc(entry.acquisition_source_label || source)}">${esc(source)}</small></li>`;
  }
  function render(plans, slots, catalog = {}) {
    const best = Math.min(...plans.map(plan => number(plan.distance)));
    return plans.map(plan => {
      const rows = slotEntries(plan, slots);
      const equipped = rows.filter(row => row.entry?.item).length;
      const owned = rows.filter(row => row.entry?.selection_origin === 'owned').length;
      const locked = rows.filter(row => row.entry?.selection_origin === 'locked').length;
      const isBest = number(plan.distance) === best;
      return `<article class="assistant-plan${isBest ? ' is-best' : ''}" data-plan-key="${esc(plan.key)}"><header class="assistant-plan-head"><span class="assistant-plan-title"><strong>${esc(plan.name)}</strong><small>${equipped} 件 · 锁定 ${locked} · 备选 ${owned}</small></span><span class="assistant-plan-badge">${isBest ? '最接近 · ' : ''}偏差 ${esc(plan.distance)}</span></header><div class="assistant-plan-body"><div class="assistant-plan-stats">${['crit','haste','mastery','versatility'].map(key => `<div class="assistant-plan-stat"><span>${labels[key]}</span><strong>${number(plan.percentages?.[key]).toFixed(2)}%</strong></div>`).join('')}</div><div class="assistant-constraint-chips"><span>特效 ${number(plan.effect_count)}</span><span>美化 ${number(plan.embellishment_count)}/2 件</span><span>地下堡神话 ${number(plan.delve_myth_count)}/2 件</span><span>${esc(plan.source_preference_label || '不偏好来源')}</span></div>${recommendations(plan, rows)}<section class="assistant-plan-equipment" aria-label="完整装备清单"><div class="assistant-equipment-heading">装备<span>悬停或点名称查看详情</span></div><ul>${rows.map(row => equipmentRow(row, catalog)).join('')}</ul></section></div><footer class="assistant-plan-actions"><button type="button" class="assistant-btn assistant-btn--primary" data-apply-plan="${esc(plan.key)}">应用到职业配装器</button></footer></article>`;
    }).join('');
  }
  function bindTooltips(root) {
    const tooltip = document.createElement('div');
    tooltip.className = 'wow-item-tooltip assistant-compact-tooltip';
    tooltip.id = 'assistant-item-tooltip';
    tooltip.setAttribute('role', 'tooltip');
    tooltip.hidden = true;
    document.body.append(tooltip);
    let active = null;
    let timer;
    const triggerFor = target => target instanceof Element ? target.closest('[data-assistant-tooltip]') : null;
    function hide() {
      clearTimeout(timer);
      active?.removeAttribute('aria-describedby');
      active = null;
      tooltip.hidden = true;
    }
    function show(trigger) {
      clearTimeout(timer);
      active?.removeAttribute('aria-describedby');
      active = trigger;
      WowItemTooltip.renderContent(tooltip, {name: trigger.dataset.assistantTooltipName, description: trigger.dataset.assistantTooltip});
      trigger.setAttribute('aria-describedby', tooltip.id);
      tooltip.hidden = false;
      const rect = trigger.getBoundingClientRect();
      const gap = 8;
      tooltip.style.left = `${Math.max(gap, Math.min(rect.left, innerWidth - tooltip.offsetWidth - gap))}px`;
      const below = rect.bottom + gap;
      const top = below + tooltip.offsetHeight <= innerHeight - gap ? below : rect.top - tooltip.offsetHeight - gap;
      tooltip.style.top = `${Math.max(gap, Math.min(top, innerHeight - tooltip.offsetHeight - gap))}px`;
    }
    root.addEventListener('pointerover', event => {
      const trigger = triggerFor(event.target);
      if (event.pointerType !== 'touch' && trigger) show(trigger);
    });
    root.addEventListener('pointerout', event => {
      if (event.pointerType === 'touch' || !active || active.contains(event.relatedTarget) || tooltip.contains(event.relatedTarget)) return;
      timer = setTimeout(hide, 100);
    });
    tooltip.addEventListener('pointerenter', () => clearTimeout(timer));
    tooltip.addEventListener('pointerleave', event => { if (event.pointerType !== 'touch') hide(); });
    root.addEventListener('focusin', event => { const trigger = triggerFor(event.target); if (trigger) show(trigger); });
    root.addEventListener('focusout', event => { if (!tooltip.contains(event.relatedTarget)) hide(); });
    // Click always opens: focus/pointerover before a touch click must not toggle it closed.
    root.addEventListener('click', event => { const trigger = triggerFor(event.target); if (trigger) show(trigger); });
    document.addEventListener('click', event => { if (!root.contains(event.target) || !triggerFor(event.target)) { if (!tooltip.contains(event.target)) hide(); } });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') hide(); });
    window.addEventListener('scroll', event => { if (!tooltip.contains(event.target)) hide(); }, true);
    window.addEventListener('resize', hide);
    return hide;
  }
  globalThis.WowGearAssistantResults = {render, bindTooltips};
})();
