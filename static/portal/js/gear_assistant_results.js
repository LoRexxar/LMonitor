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
    // IDs alone are insufficient across quality/build/stat projections. Never merge by name.
    return JSON.stringify([entry.item?.item_id, v.id, v.key, v.item_level, v.crafting_quality, v.game_build, v.data_branch, v.bonus_ids, v.stats, v.effects]);
  }
  function effects(entry) {
    return (entry?.resolvedEffects || entry?.variant?.effects || []).map(row => typeof row === 'string' ? row : (row.description_zh || row.description || '')).filter(Boolean);
  }
  function enhancement(entry) {
    if (!entry?.item) return '';
    const parts = [quality(entry), statText(entry.resolvedStats || entry.variant?.stats)].filter(Boolean);
    return `<strong>${esc(name(entry))}</strong>${parts.length ? `<small>${esc(parts.join(' · '))}</small>` : ''}`;
  }
  function enhancementDetail(entry, label) {
    if (!entry?.item) return '';
    return `<div class="assistant-enhancement-detail"><span>${esc(label)}</span><div>${enhancement(entry)}${effects(entry).map(text => `<p>${esc(text)}</p>`).join('')}<small class="assistant-result-id">物品 ID ${esc(entry.item.item_id ?? '未提供')} · 变体 ID ${esc(entry.variant?.id ?? '未提供')} · ${esc(entry.variant?.key || '')}</small></div></div>`;
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
    const noFlask = flask?.key === 'none';
    const groups = gemGroups(rows);
    const gemCount = groups.reduce((sum, row) => sum + row.destinations.length, 0);
    const enchants = rows.filter(row => row.entry?.enchant?.item);
    const beauties = rows.filter(row => row.entry?.embellishment?.item || row.entry?.variant?.is_intrinsic_embellishment);
    return `<section class="assistant-plan-preparation" aria-label="消耗品与强化清单">
      <div class="assistant-flask-recommendation assistant-plan-flask"><span class="assistant-section-kicker">属性合剂</span><strong>${esc(noFlask ? '不使用属性合剂' : flask?.name || '合剂信息未提供')}</strong><small>${esc(noFlask ? '无合剂属性加成' : statText(flask?.stats) || '属性加成未提供')}</small></div>
      <div class="assistant-gem-recommendation assistant-plan-gems"><h4>宝石 <span>${gemCount} 颗</span></h4>${groups.length ? groups.map((group, index) => `<div class="assistant-gem-group" data-gem-group="${index}">${icon(group.gem)}<div>${enhancement(group.gem)}<small class="assistant-enhancement-destinations">${esc(group.destinations.join('；'))}</small></div><b>×${group.destinations.length}</b></div>`).join('') : '<p class="assistant-result-empty">本方案未镶嵌宝石</p>'}</div>
      <details class="assistant-plan-enhancements"><summary>附魔 ${enchants.length} 处 · 美化 ${beauties.length} 件 <span>查看逐部位清单</span></summary><div class="assistant-enhancement-columns"><section><h4>附魔</h4>${enchants.length ? enchants.map(row => enhancementDetail(row.entry.enchant, row.label)).join('') : '<p class="assistant-result-empty">本方案未选择附魔</p>'}</section><section><h4>美化</h4>${beauties.length ? beauties.map(row => row.entry.variant?.is_intrinsic_embellishment ? `<div class="assistant-enhancement-detail"><span>${esc(row.label)}</span><div><strong>${esc(name(row.entry))}</strong><small>固有美化 · 详见装备特效</small></div></div>` : enhancementDetail(row.entry.embellishment, row.label)).join('') : '<p class="assistant-result-empty">本方案未选择美化</p>'}</section></div></details>
    </section>`;
  }
  function equipmentRow({key, label, entry}) {
    if (!entry?.item) return `<li class="assistant-result-gear is-empty" data-plan-slot="${esc(key)}"><span>${esc(label)}</span><p>未提供装备</p></li>`;
    const v = entry.variant || {};
    const origin = {locked: '已锁定', owned: '来自备选', catalog: '需获取'}[entry.selection_origin] || '状态未提供';
    const track = v.track_label ? `${v.track_label}${v.track_rank ? ` ${v.track_rank}/${v.track_max_rank || '?'}` : ''}` : '';
    const meta = [entry.itemLevel || v.item_level ? `${entry.itemLevel || v.item_level} 装等` : '装等未提供', track, quality(entry), rarity[entry.item.quality]].filter(Boolean).join(' · ');
    // resolvedStats is already resolved by the optimizer; never add gem/enchant stats here.
    const stats = statText({...v.stats, ...entry.resolvedStats});
    const selected = (entry.selectedStats || []).map(key => labels[key] || key).join(' / ');
    const gems = (entry.gems || []).filter(gem => gem?.item);
    const enhancements = [gems.length ? `宝石：${gemGroups([{key, label, entry}]).map(group => `${name(group.gem)}${quality(group.gem) ? `（${quality(group.gem)}）` : ''} ×${group.destinations.length}`).join('；')}` : '未镶嵌宝石', entry.enchant?.item ? `附魔：${name(entry.enchant)}` : '未选择附魔', v.is_intrinsic_embellishment ? '固有美化' : entry.embellishment?.item ? `美化：${name(entry.embellishment)}` : '无美化'];
    return `<li class="assistant-result-gear" data-plan-slot="${esc(key)}"><div class="assistant-result-slot">${esc(label)}<span class="assistant-origin ${entry.selection_origin === 'catalog' ? 'needs-item' : ''}">${esc(origin)}</span></div>${icon(entry)}<div class="assistant-result-gear-copy"><strong class="assistant-result-item-name">${esc(name(entry))}</strong><span class="assistant-result-item-meta">${esc(meta)}</span><span class="assistant-result-item-stats" title="采用后端槽位解析值；如已含强化，不再叠加下方强化明细">解析属性：${esc(stats || '无常驻属性数据')}${selected ? ` <span>· 自选 ${esc(selected)}</span>` : ''}</span><p class="assistant-result-source">来源：${esc(entry.acquisition_source_label || '获取来源未提供')}</p><p class="assistant-result-enhancements">${esc(enhancements.join(' · '))}</p><details class="assistant-item-details"><summary>属性强化与特效详情</summary><div>${gems.map((gem, index) => enhancementDetail(gem, `宝石 · 第 ${index + 1} 孔`)).join('')}${enhancementDetail(entry.enchant, '附魔')}${enhancementDetail(entry.embellishment, '附加美化')}<div class="assistant-result-effects">${effects(entry).map(text => `<p>${esc(text)}</p>`).join('') || '<p>未提供额外特效说明</p>'}</div><p class="assistant-result-id">物品 ID ${esc(entry.item.item_id ?? '未提供')} · 变体 ID ${esc(v.id ?? '未提供')} · ${esc(v.key || '')}</p></div></details></div></li>`;
  }
  function render(plans, slots) {
    const best = Math.min(...plans.map(plan => number(plan.distance)));
    return plans.map((plan, index) => {
      const rows = slotEntries(plan, slots);
      const equipped = rows.filter(row => row.entry?.item).length;
      const required = rows.filter(row => row.entry?.selection_origin === 'catalog').length;
      const owned = rows.filter(row => row.entry?.selection_origin === 'owned').length;
      const locked = rows.filter(row => row.entry?.selection_origin === 'locked').length;
      const isBest = number(plan.distance) === best;
      return `<article class="assistant-plan${isBest ? ' is-best' : ''}" data-plan-key="${esc(plan.key)}"><header class="assistant-plan-head"><span class="assistant-plan-number">${String(index + 1).padStart(2, '0')}</span><span class="assistant-plan-title"><strong>${esc(plan.name)}</strong><small>${equipped}/${slots.length} 件装备 · 锁定 ${locked} · 备选 ${owned} · 需获取 ${required}</small></span><span class="assistant-plan-badge">${isBest ? '最接近目标 · ' : ''}偏差 ${esc(plan.distance)}</span></header><div class="assistant-plan-body"><div class="assistant-plan-stats">${['crit','haste','mastery','versatility'].map(key => `<div class="assistant-plan-stat"><span>${labels[key]}</span><strong>${number(plan.percentages?.[key]).toFixed(2)}<small>%</small></strong></div>`).join('')}</div><div class="assistant-constraint-chips"><span>特效装备 ${number(plan.effect_count)} 件</span><span>美化 ${number(plan.embellishment_count)}/2 件</span><span>地下堡神话 ${number(plan.delve_myth_count)}/2 件</span><span>${esc(plan.source_preference_label || '不偏好来源')}</span></div>${recommendations(plan, rows)}<details class="assistant-plan-equipment"><summary><strong>完整装备清单</strong><span>${equipped}/${slots.length} 件 · 查看来源、强化与特效</span></summary><ul>${rows.map(equipmentRow).join('')}</ul></details></div><footer class="assistant-plan-actions"><span>以选定变体与实际获取来源为准</span><button type="button" class="assistant-btn assistant-btn--primary" data-apply-plan="${esc(plan.key)}">应用到职业配装器 <span aria-hidden="true">↗</span></button></footer></article>`;
    }).join('');
  }
  globalThis.WowGearAssistantResults = {render};
})();
