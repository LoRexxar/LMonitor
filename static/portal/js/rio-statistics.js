(() => {
  'use strict';
  const $ = (id) => document.getElementById(`rio-${id}`);
  const host = $('statistics');
  if (!host) return;
  const number = (v, digits = 0) => typeof v === 'number' && Number.isFinite(v)
    ? v.toLocaleString('zh-CN', { maximumFractionDigits: digits }) : '—';
  const time = (v) => v && !Number.isNaN(Date.parse(v))
    ? new Date(v).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—';
  const initial = new URLSearchParams(location.search);
  const modules = ['overview', 'runs', 'heatmaps', 'raids'];
  const idFilters = ['include_classes', 'exclude_classes', 'include_specs', 'exclude_specs', 'death_classes', 'death_specs',
    'exclude_death_classes', 'exclude_death_specs', 'include_affixes', 'exclude_affixes'];
  const numericFilters = ['item_min', 'item_max', 'deaths_min', 'deaths_max', 'timer_min', 'timer_max'];
  const controls = ['season', 'region', 'days', 'percentile', 'dungeon', 'level', 'period', 'raid', 'difficulty', 'week_start', 'week_end'];
  const state = { tab: modules.includes(initial.get('tab')) ? initial.get('tab') : 'overview',
    layer: ['player_death', 'player_spell', 'npc_death'].includes(initial.get('layer')) ? initial.get('layer') : 'player_death',
    catalog: null, payload: null, envelope: null, floor: null, controller: null, serial: 0 };

  function element(tag, text, cls) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  }
  function select(node, rows, preferred) {
    const old = preferred ?? (node.multiple ? [...node.selectedOptions].map((o) => o.value).join(',') : node.value);
    node.replaceChildren();
    const groups = new Map();
    rows.forEach((r) => {
      const option = element('option', r.label ?? r.name);
      option.value = r.value ?? r.slug;
      if (r.group) {
        if (!groups.has(r.group)) {
          const group = document.createElement('optgroup'); group.label = r.group;
          groups.set(r.group, group); node.append(group);
        }
        groups.get(r.group).append(option);
      } else node.append(option);
    });
    if (node.multiple) {
      const values = String(old || '').split(',');
      [...node.options].forEach((o) => o.selected = values.includes(o.value));
    } else if (rows.some((r) => String(r.value ?? r.slug) === String(old))) node.value = old;
  }
  function tableRow(values, current = false) {
    const row = element('tr', undefined, current ? 'is-current' : '');
    values.forEach((v) => row.append(element('td', v)));
    return row;
  }
  function activeSeason() { return state.catalog?.seasons.find((s) => s.slug === $('season').value); }
  function populateDungeons(preferred) {
    select($('dungeon'), (activeSeason()?.dungeons || []).map((d) => ({ value: d.slug, label: d.name })), preferred);
  }
  function currentQuery() {
    const query = new URLSearchParams({ season: $('season').value, region: $('region').value });
    if (state.tab === 'heatmaps') {
      ['dungeon', 'level', 'period'].forEach((k) => query.set(k, $(k).value));
      query.set('layer', state.layer);
      query.set('zero', $('zero').checked ? '1' : '0');
      idFilters.forEach((key) => {
        const values = [...$(key).selectedOptions].map((o) => o.value);
        if (values.length && !$(key).disabled) query.set(key, values.join(','));
      });
      numericFilters.forEach((key) => { if ($(key).value !== '') query.set(key, $(key).value); });
    } else if (state.tab === 'runs') {
      ['level', 'period'].forEach((k) => query.set(k, $(k).value));
    } else if (state.tab === 'raids') {
      ['raid', 'difficulty'].forEach((k) => query.set(k, $(k).value));
    }
    if (['heatmaps', 'runs'].includes(state.tab)) {
      if ($('period').value === 'custom') ['week_start', 'week_end'].forEach((key) => query.set(key, $(key).value));
    }
    return query;
  }
  function updateUrl() {
    const query = currentQuery();
    query.set('tab', state.tab);
    if (state.tab === 'overview') {
      query.set('days', $('days').value);
      query.set('percentile', $('percentile').value);
    }
    if (state.tab === 'heatmaps' && $('floor').value) query.set('floor', $('floor').value);
    history.replaceState(null, '', `${location.pathname}?${query}`);
  }
  async function json(url, signal) {
    const response = await fetch(url, { signal, cache: 'no-cache', headers: { Accept: 'application/json' } });
    let result;
    try { result = await response.json(); } catch { throw new Error('数据读取失败，请稍后重试。'); }
    if (!response.ok || !result.success) throw new Error(result.message || '读取失败，请稍后重试。');
    return result.data;
  }
  function error(title, message, retry = true) {
    $('error').hidden = false;
    $('error-title').textContent = title;
    $('error-message').textContent = message;
    $('retry').hidden = !retry;
    modules.forEach((tab) => $(tab).hidden = true);
    $('prepare').hidden = true;
  }
  function setTab(tab, load = true) {
    state.tab = tab;
    document.querySelectorAll('[data-tab]').forEach((button) => {
      const selected = button.dataset.tab === tab;
      button.setAttribute('aria-selected', String(selected));
      button.tabIndex = selected ? 0 : -1;
    });
    modules.forEach((name) => $(name).hidden = name !== tab);
    $('overview-filters').hidden = tab !== 'overview';
    $('heatmap-filters').hidden = !['heatmaps', 'runs'].includes(tab);
    $('dungeon-filter').hidden = tab !== 'heatmaps';
    $('advanced').hidden = tab !== 'heatmaps';
    $('raid-filters').hidden = tab !== 'raids';
    if (load && tab === 'runs' && state.catalog) {
      const ready = state.catalog.available.filter((r) => r.coordinate.module === 'runs' && r.coordinate.region === $('region').value);
      if (!ready.some((r) => r.coordinate.level === $('level').value)) $('level').value = ready[0]?.coordinate.level || '10-12';
    }
    rangeControls();
    if (load && state.catalog) refresh();
  }
  function layerControls() {
    document.querySelectorAll('[data-layer]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.layer === state.layer)));
    $('zero').disabled = state.layer === 'player_death';
    if ($('zero').disabled) $('zero').checked = false;
    document.querySelectorAll('[data-death-filter]').forEach((node) => {
      node.disabled = state.layer !== 'player_death';
      if (node.disabled) [...node.options].forEach((o) => o.selected = false);
    });
  }

  function rangeControls() {
    const visible = ['heatmaps', 'runs'].includes(state.tab);
    $('week-range').hidden = !visible || $('period').value !== 'custom';
  }

  function populateWeeks(preferred = initial) {
    const weeks = state.catalog.weeks?.[$('region').value] || [];
    const last = String(Math.max(1, weeks.length - 1));
    select($('week_start'), weeks, preferred.get('week_start') || $('week_start').value || last);
    select($('week_end'), weeks, preferred.get('week_end') || $('week_end').value || last);
  }

  async function refresh() {
    updateUrl();
    const serial = ++state.serial;
    state.controller?.abort();
    state.controller = new AbortController();
    state.payload = null;
    host.setAttribute('aria-busy', 'true');
    $('error').hidden = true;
    modules.forEach((tab) => $(tab).hidden = true);
    $('prepare').hidden = true; $('prepare').disabled = false; $('prepare').textContent = '加入凌晨更新';
    $('status').textContent = '正在读取所选范围…';
    try {
      const apiQuery = currentQuery();
      if (state.tab === 'runs') ['period', 'week_start', 'week_end'].forEach((key) => apiQuery.delete(key));
      const envelope = await json(`/portal/api/rio-statistics/${state.tab}/?${apiQuery}`, state.controller.signal);
      if (serial !== state.serial) return;
      state.envelope = envelope;
      if (state.tab === 'heatmaps') {
        const count = [...idFilters, ...numericFilters].filter((key) => apiQuery.has(key)).length;
        $('filter-summary').textContent = count ? `（已应用 ${count} 项）` : '';
      }
      if (envelope.scope && envelope.scope.token !== state.catalog.scope?.token) {
        await bootstrap(true); return;
      }
      state.payload = envelope.data;
      const snapshot = envelope.snapshot || {};
      $('status').dataset.state = snapshot.state || 'pending';
      $('status').textContent = snapshot.fetched_at
        ? `${snapshot.message ? snapshot.message + ' ' : ''}采集时间：${time(snapshot.fetched_at)}${snapshot.upstream_at ? ` · 来源更新时间：${time(snapshot.upstream_at)}` : ''}`
        : '所选范围暂无已采集的数据';
      if (!envelope.data) {
        const available = state.catalog.available.filter((r) => r.coordinate.module === state.tab && r.coordinate.season === $('season').value);
        error('这个范围的数据尚未准备好', snapshot.message || (available.length
          ? '请切换地区、副本或其他筛选范围。已有数据会保留，后台将分批补齐。'
          : '后台将在每日凌晨准备本模块的数据，请稍后查看。'));
        $('prepare').hidden = state.tab !== 'heatmaps';
        return;
      }
      $(state.tab).hidden = false;
      if (state.tab === 'overview') renderOverview();
      else if (state.tab === 'runs') renderRuns();
      else if (state.tab === 'heatmaps') renderHeatmap();
      else renderRaids();
    } catch (err) {
      if (err.name !== 'AbortError' && serial === state.serial) {
        $('status').textContent = '本次读取未成功';
        error('暂时无法读取数据', err.message);
      }
    } finally {
      if (serial === state.serial) host.setAttribute('aria-busy', 'false');
    }
  }

  function drawLine(target, rows, valueFor, label, digits = 0) {
    const container = $(target);
    container.replaceChildren();
    const valid = rows.filter((r) => typeof valueFor(r) === 'number' && Number.isFinite(valueFor(r)));
    if (!valid.length) { container.append(element('p', '这个范围没有可绘制的历史数据。', 'rio-note')); return; }
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 720 260');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', `${label}，${rows[0].date} 至 ${rows.at(-1).date}。完整数值可在趋势明细中查看。`);
    svg.setAttribute('tabindex', '0');
    function part(tag, attrs, text) {
      const node = document.createElementNS(ns, tag);
      Object.entries(attrs).forEach(([k, v]) => node.setAttribute(k, v));
      if (text !== undefined) node.textContent = text;
      svg.append(node);
      return node;
    }
    const chartWidth = Math.max(280, container.clientWidth || 720);
    svg.setAttribute('viewBox', `0 0 ${chartWidth} 260`);
    svg.setAttribute('preserveAspectRatio', 'none');
    const left = chartWidth < 450 ? 60 : 72, right = chartWidth - 15, top = 18, bottom = 222;
    const values = valid.map(valueFor);
    let low = Math.min(...values), high = Math.max(...values);
    const pad = Math.max((high - low) * 0.15, high * 0.005, 1);
    low = Math.max(0, low - pad); high += pad;
    const x = (i) => left + (right - left) * (rows.length === 1 ? 0.5 : i / (rows.length - 1));
    const y = (v) => bottom - (bottom - top) * (v - low) / (high - low);
    for (let i = 0; i <= 4; i++) {
      const yy = top + (bottom - top) * i / 4;
      part('line', { x1: left, y1: yy, x2: right, y2: yy, class: 'chart-grid' });
      const tickValue = high - (high - low) * i / 4;
      part('text', { x: left - 10, y: yy + 4, 'text-anchor': 'end' }, chartWidth < 450 && tickValue >= 10000 ? `${number(tickValue / 10000, 1)}万` : number(tickValue, 0));
    }
    const tickCount = Math.min(chartWidth < 450 ? 4 : 6, rows.length);
    const ticks = new Set(Array.from({ length: tickCount }, (_, i) => Math.round(i * (rows.length - 1) / Math.max(1, tickCount - 1))));
    ticks.forEach((i) => {
      part('line', { x1: x(i), y1: top, x2: x(i), y2: bottom, class: 'chart-grid' });
      part('text', { x: x(i), y: 245, 'text-anchor': 'middle' }, rows[i].date.slice(5));
    });
    let path = '', connected = false;
    rows.forEach((r, i) => {
      const v = valueFor(r);
      if (typeof v !== 'number' || !Number.isFinite(v)) { connected = false; return; }
      path += `${connected ? 'L' : 'M'}${x(i)},${y(v)} `;
      connected = true;
    });
    part('path', { d: path, class: 'chart-line' });
    if (rows.length === 1) part('circle', { cx: x(0), cy: y(valueFor(rows[0])), r: 4, class: 'chart-point' });
    const tooltip = element('div', '', 'rio-tooltip'); tooltip.hidden = true;
    let focused = rows.length - 1;
    const show = (i) => {
      focused = Math.max(0, Math.min(rows.length - 1, i));
      tooltip.textContent = `${rows[focused].date} · ${label}：${number(valueFor(rows[focused]), digits)}`;
      tooltip.hidden = false; tooltip.style.left = '14px'; tooltip.style.top = '0';
    };
    const hit = part('rect', { x: left, y: top, width: right - left, height: bottom - top, class: 'chart-hit' });
    hit.addEventListener('pointermove', (event) => {
      const box = svg.getBoundingClientRect();
      show(Math.round(((event.clientX - box.left) / box.width * chartWidth - left) / (right - left) * (rows.length - 1)));
    });
    hit.addEventListener('pointerleave', () => tooltip.hidden = true);
    svg.addEventListener('focus', () => show(focused));
    svg.addEventListener('blur', () => tooltip.hidden = true);
    svg.addEventListener('keydown', (event) => {
      if (['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); show(focused + (event.key === 'ArrowLeft' ? -1 : 1)); }
    });
    container.append(svg, tooltip);
  }

  function renderOverview() {
    const data = state.payload;
    const summary = data.populations.find((p) => p.faction === 'all');
    if (!summary) { error('整体样本暂不可用', '来源未提供所有阵营汇总，暂不将不同阵营相加。'); return; }
    const percentile = Number($('percentile').value);
    const score = summary.percentiles.find((p) => p.percentile === percentile)?.score;
    let rows = data.history;
    const days = Number($('days').value);
    if (days && rows.length) {
      const cutoff = Date.parse(rows.at(-1).date) - (days - 1) * 86400000;
      rows = rows.filter((r) => Date.parse(r.date) >= cutoff);
    }
    const cutoffFor = (r) => r.cutoffs.find((p) => p.percentile === percentile)?.score ?? null;
    $('population').textContent = number(summary.population);
    $('score').textContent = number(score, 2);
    $('score-label').textContent = `前 ${percentile}% 分数线`;
    $('date').textContent = data.snapshot_date || '—';
    const populationRows = rows.filter((r) => typeof r.population === 'number');
    if (populationRows.length >= 2 && populationRows[0].population > 0) {
      const diff = populationRows.at(-1).population - populationRows[0].population;
      $('change').textContent = `${diff > 0 ? '+' : ''}${number(diff)}（${(diff / populationRows[0].population * 100).toFixed(1)}%）`;
    } else $('change').textContent = '样本不足';
    drawLine('population-chart', rows, (r) => r.population, '参与角色');
    drawLine('score-chart', rows, cutoffFor, `前 ${percentile}% 分数线`, 2);
    $('percentiles').replaceChildren(...summary.percentiles.map((p) => tableRow([`前 ${p.percentile}%`, number(p.score, 2)], p.percentile === percentile)));
    $('history').replaceChildren(...rows.map((r) => tableRow([r.date, number(r.population), number(cutoffFor(r), 2)])));
  }

  function renderRaids() {
    const rows = state.payload.progression;
    const raid = state.catalog.raids.find((r) => r.slug === $('raid').value);
    $('raid-heading').textContent = `${raid?.name || '团本'} · 公会击杀进度分布`;
    $('raid-chart').replaceChildren();
    const max = Math.max(1, ...rows.map((r) => r.guilds));
    if (!rows.length) $('raid-chart').append(element('p', '该难度下暂无公会进度记录。', 'rio-note'));
    rows.forEach((r) => {
      const row = element('div', undefined, 'rio-raid-row');
      const track = element('div', undefined, 'rio-raid-track');
      const bar = element('div', undefined, 'rio-raid-fill');
      bar.style.width = `${r.guilds / max * 100}%`; track.append(bar);
      row.append(element('span', `${r.progress} / ${raid?.bosses || '?'} 首领`), track, element('span', number(r.guilds)));
      $('raid-chart').append(row);
    });
    $('raid-rows').replaceChildren(...rows.map((r) => tableRow([`${r.progress} / ${raid?.bosses || '?'} 首领`, number(r.guilds)])));
  }

  function renderRuns() {
    const weeks = state.catalog.weeks?.[$('region').value] || [];
    let first = weeks[0]?.period ?? -Infinity, last = weeks.at(-1)?.period ?? Infinity;
    const period = $('period').value;
    if (period === 'last-reset') first = last = last - 1;
    else if (period.startsWith('last-')) { last -= 1; first = Math.max(first, last - Number(period.split('-')[1]) + 1); }
    else if (period === 'this-reset') first = last;
    else if (period === 'custom') {
      first = weeks.find((w) => w.value === $('week_start').value)?.period ?? Infinity;
      last = weeks.find((w) => w.value === $('week_end').value)?.period ?? -Infinity;
    }
    const rows = state.payload.rows.filter((r) => r.period >= first && r.period <= last);
    const total = rows.reduce((v, r) => v + r.quantity, 0), timed = rows.reduce((v, r) => v + r.timed, 0);
    $('runs-total').textContent = rows.length ? number(total) : '—';
    $('runs-timed').textContent = rows.length ? number(timed) : '—';
    $('runs-rate').textContent = total ? `${number(timed / total * 100, 2)}%` : '—';
    $('runs-note').textContent = rows.length ? '限时率按所选周区间已收录的完成记录计算，不包含全部放弃或解散尝试。' : '所选周区间暂无已收录的完成记录。';
    drawLine('runs-chart', rows, (r) => r.quantity, '完成次数');
    $('runs-rows').replaceChildren(...rows.map((r) => tableRow([r.label, number(r.quantity), number(r.timed), number(r.non_timed), r.timed_rate === null ? '—' : `${number(r.timed_rate, 2)}%`])));
  }

  function renderHeatmap() {
    const allFloors = state.payload.floors;
    const floors = allFloors.filter((f) => f.map?.image_url);
    const desired = $('floor').value || initial.get('floor');
    const preferred = floors.find((f) => f.id === desired) || floors.find((f) => f.default && f.points.length)
      || floors.find((f) => f.points.length) || floors[0];
    select($('floor'), floors.map((f) => ({ value: f.id, label: f.name })), preferred?.id);
    $('floor').disabled = !floors.length;
    const query = new URLSearchParams({ season: $('season').value, region: $('region').value,
      dungeon: $('dungeon').value, viewType: state.layer, scope: $('period').value === 'last-reset' ? 'last-reset' : 'season' });
    const level = state.catalog.levels.find((r) => r.value === $('level').value);
    if (level) { query.set('minMythicLevel', level.min); query.set('maxMythicLevel', level.max); }
    const fieldNames = {include_classes:'includeClassIds',exclude_classes:'excludeClassIds',include_specs:'includeSpecIds',exclude_specs:'excludeSpecIds',death_classes:'includePlayerDeathClassIds',death_specs:'includePlayerDeathSpecIds',exclude_death_classes:'excludePlayerDeathClassIds',exclude_death_specs:'excludePlayerDeathSpecIds',include_affixes:'includeAffixIds',exclude_affixes:'excludeAffixIds',item_min:'minItemLevel',item_max:'maxItemLevel',deaths_min:'minPlayerDeaths',deaths_max:'maxPlayerDeaths',timer_min:'minTimerFraction',timer_max:'maxTimerFraction'};
    const selected = currentQuery();
    Object.entries(fieldNames).forEach(([key, name]) => { if (selected.has(key)) query.set(name, selected.get(key)); });
    const weeks = state.catalog.weeks?.[$('region').value] || [];
    if (weeks.length && $('period').value !== 'season') {
      let first, last = weeks.at(-1).period;
      if ($('period').value === 'last-reset') first = --last;
      else if ($('period').value === 'this-reset') first = last;
      else if ($('period').value === 'custom') { first = weeks.find((w) => w.value === $('week_start').value)?.period; last = weeks.find((w) => w.value === $('week_end').value)?.period; }
      else { last -= 1; first = Math.max(weeks[0].period, last - Number($('period').value.split('-')[1]) + 1); }
      query.set('minPeriod', first); query.set('maxPeriod', last);
    }
    if (state.layer === 'player_spell') query.set('spells', 'bloodlust');
    if ($('zero').checked) query.set('maxPlayerDeaths', '0');
    $('source-map').href = `https://raider.io/stats/mythic-plus-heatmaps?${query}`;
    drawHeatmap();
    updateUrl();
  }
  function drawHeatmap() {
    if (state.tab !== 'heatmaps' || !state.payload) return;
    const floor = state.payload.floors.find((f) => f.id === $('floor').value);
    const canvas = $('heatmap'), context = canvas.getContext('2d');
    const width = Math.max(280, canvas.parentElement.clientWidth);
    const mobile = width < 450, height = Math.round(width / 1.5);
    const left = 0, right = width, top = 0, bottom = height;
    canvas.width = width * 2; canvas.height = height * 2;
    context.scale(2, 2);
    context.fillStyle = '#20252e'; context.fillRect(0, 0, width, height);
    $('floor-empty').hidden = !!floor?.points.length;
    $('floor-empty').textContent = '该楼层暂无数据。';
    if (!floor?.map?.image_url) {
      canvas.hidden = true; $('map-message').hidden = false;
      $('map-message').textContent = '此范围的楼层地图尚未准备好，将由夜间任务采集。';
      $('map-stats').replaceChildren(); return;
    }
    state.mapImages ||= new Map();
    const mapUrl = floor.map.image_url;
    let bitmap = state.mapImages.get(mapUrl);
    if (!bitmap) {
      bitmap = new Image(); state.mapImages.set(mapUrl, bitmap);
      bitmap.onload = () => { if (state.tab === 'heatmaps') drawHeatmap(); };
      bitmap.onerror = () => { bitmap.failed = true; drawHeatmap(); };
      bitmap.src = mapUrl;
    }
    if (!bitmap.complete || !bitmap.naturalWidth || bitmap.failed) {
      canvas.hidden = true; $('map-message').hidden = false;
      $('map-message').textContent = bitmap.failed ? '地图读取失败，请重新读取页面或打开来源地图。' : '正在加载地图…';
      return;
    }
    canvas.hidden = false; $('map-message').hidden = true;
    const mapIdentity = `${floor.id}:${mapUrl}`;
    if (state.mapFloor !== mapIdentity) { state.mapFloor = mapIdentity; state.zoom = 1; state.pan = [0, 0]; }
    const zoom = state.zoom || 1, pan = state.pan || [0, 0];
    $('map-scale').textContent = `${Math.round(zoom * 100)}%`;
    context.drawImage(bitmap, width / 2 - width * zoom / 2 + pan[0], height / 2 - height * zoom / 2 + pan[1], width * zoom, height * zoom);
    // 与来源坐标服务一致：保留有向边界，不排序、不交换 X/Y。
    const b = floor.map.bounds;
    const x = (v) => width / 2 + ((b[2] - v) / (b[2] - b[0]) - .5) * width * zoom + pan[0];
    const y = (v) => height / 2 + ((b[3] - v) / (b[3] - b[1]) - .5) * height * zoom + pan[1];
    const max = Math.max(1, ...floor.points.map((p) => p[2]));
    const opacity = Number($('opacity').value) / 100;
    context.save(); context.beginPath(); context.rect(left, top, right - left, bottom - top); context.clip();
    const colors = ['#3156df', '#23b4db', '#55dba6', '#f1d84b', '#f38036', '#d94338'];
    [...floor.points].sort((a, b) => a[2] - b[2]).forEach((p) => {
      const xx = x(p[0]), yy = y(p[1]);
      const factor = Math.sqrt(p[2] / max);
      const radius = (mobile ? 2 : 5) + factor * (mobile ? 6 : 10);
      const color = colors[Math.min(5, Math.floor(factor * 6))];
      const gradient = context.createRadialGradient(xx, yy, 0, xx, yy, radius);
      gradient.addColorStop(0, color); gradient.addColorStop(1, `${color}00`);
      context.globalAlpha = opacity * (.3 + factor * .7); context.fillStyle = gradient;
      context.fillRect(xx - radius, yy - radius, radius * 2, radius * 2);
    });
    context.restore(); context.globalAlpha = 1;
    const layerName = {player_death: '死亡位置', player_spell: '嗜血位置', npc_death: '怪物击杀'}[state.layer];
    canvas.setAttribute('aria-label', `${floor.name} · ${layerName}热力图`);
    const stats = [['样本场次', number(state.payload.num_runs)]];
    $('map-stats').replaceChildren(...stats.map(([label, value]) => { const row = element('div'); row.append(element('dt', label), element('dd', value)); return row; }));
  }

  async function bootstrap(preserve = false) {
    const selection = preserve ? new URLSearchParams(location.search) : initial;
    host.setAttribute('aria-busy', 'true');
    try {
      state.catalog = await json('/portal/api/rio-statistics/');
      const scope = state.catalog.scope;
      $('version').textContent = scope ? `当前数据库赛季：${scope.season_name} · 版本 ${scope.game_build} · 每日凌晨更新${state.catalog.scope_message ? ' · ' + state.catalog.scope_message : ''}` : state.catalog.scope_message;
      select($('season'), state.catalog.seasons.map((s) => ({ value: s.slug, label: s.name })), state.catalog.current_season);
      select($('region'), state.catalog.regions, selection.get('region') || 'cn');
      const requestedLevel = selection.get('level');
      if (requestedLevel && !state.catalog.levels.some((row) => row.value === requestedLevel)) throw new Error('链接中的层数范围已停用，请使用10–12层、16层及以上或20层及以上。');
      select($('level'), state.catalog.levels, requestedLevel || '10-12');
      select($('period'), state.catalog.periods, selection.get('period') || 'season');
      populateWeeks(selection);
      idFilters.forEach((key) => {
        const rows = key.includes('classes') ? state.catalog.classes : key.includes('specs') ? state.catalog.specs : (state.catalog.affixes || []).map((a) => ({value:String(a.id),label:a.name}));
        const requested = selection.get(key) || '';
        select($(key), rows, requested);
        if (requested && requested.split(',').some((id) => !rows.some((r) => String(r.value) === id))) throw new Error('当前版本未收录链接中的筛选条件，请重新选择。');
      });
      numericFilters.forEach((key) => { if (selection.has(key)) $(key).value = selection.get(key); });
      const raids = [...state.catalog.raids].sort((a, b) => String(b.starts?.cn).localeCompare(String(a.starts?.cn)));
      select($('raid'), raids, selection.get('raid'));
      populateDungeons(selection.get('dungeon'));
      for (const name of ['days', 'percentile', 'difficulty']) {
        if ([...$(name).options].some((o) => o.value === selection.get(name))) $(name).value = selection.get(name);
      }
      $('zero').checked = selection.get('zero') === '1';
      layerControls(); setTab(state.tab, false);
      if (!state.catalog.seasons.length) { $('status').textContent = '当前赛季尚未就绪'; error('当前赛季尚未就绪', state.catalog.scope_message || '请在赛季管理中配置当前赛季。'); return; }
      // 优先使用已有快照中的副本，保留分享链接显式指定的范围。
      if (!selection.get('dungeon')) {
        const ready = state.catalog.available.find((r) => r.coordinate.module === 'heatmaps' && r.coordinate.season === $('season').value
          && r.coordinate.region === $('region').value && r.coordinate.level === $('level').value && r.coordinate.period === $('period').value);
        if (ready) $('dungeon').value = ready.coordinate.dungeon;
      }
      await refresh();
    } catch (err) { $('status').textContent = '目录读取未成功'; error('暂时无法读取统计目录', err.message); }
    finally { host.setAttribute('aria-busy', 'false'); }
  }
  $('filters').addEventListener('submit', (event) => { event.preventDefault(); refresh(); });
  controls.forEach((name) => $(name).addEventListener('change', () => {
    if (name === 'season') populateDungeons();
    if (name === 'region') populateWeeks();
    rangeControls();
    if (['days', 'percentile'].includes(name) && state.tab === 'overview' && state.payload) { updateUrl(); renderOverview(); }
    else if (['period', 'week_start', 'week_end'].includes(name) && state.tab === 'runs' && state.payload) { updateUrl(); renderRuns(); }
    else refresh();
  }));
  document.querySelectorAll('[data-tab],[data-open]').forEach((button) => button.addEventListener('click', () => setTab(button.dataset.tab || button.dataset.open)));
  document.querySelectorAll('[data-tab]').forEach((button, i, buttons) => button.addEventListener('keydown', (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (i + (event.key === 'ArrowLeft' ? -1 : 1) + buttons.length) % buttons.length;
      buttons[index].focus(); setTab(buttons[index].dataset.tab);
    }
  }));
  document.querySelectorAll('[data-layer]').forEach((button) => button.addEventListener('click', () => {
    state.layer = button.dataset.layer; layerControls(); refresh();
  }));
  $('zero').addEventListener('change', refresh);
  $('apply').addEventListener('click', () => {
    const invalid = numericFilters.map((key) => $(key)).find((node) => !node.checkValidity());
    if (invalid) invalid.reportValidity(); else refresh();
  });
  $('clear-advanced').addEventListener('click', () => {
    idFilters.forEach((key) => [...$(key).options].forEach((o) => o.selected = false));
    numericFilters.forEach((key) => $(key).value = ''); refresh();
  });
  $('prepare').addEventListener('click', async () => {
    const serial = state.serial;
    const button = $('prepare'); button.disabled = true;
    try {
      const response = await fetch('/portal/api/rio-statistics/prepare/', {method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':document.querySelector('[name=csrfmiddlewaretoken]').value}, body:JSON.stringify({module:state.tab,params:Object.fromEntries(currentQuery())})});
      if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('登记未成功，请刷新页面后重试。');
      const body = await response.json(); if (!response.ok || !body.success) throw new Error(body.message || '登记失败，请稍后重试。');
      if (serial !== state.serial) return;
      $('error-message').textContent = body.data.message; button.textContent = '已加入凌晨更新';
    } catch (err) { if (serial === state.serial) { $('error-message').textContent = err.message; button.disabled = false; } }
  });
  $('opacity').addEventListener('input', drawHeatmap);
  $('floor').addEventListener('change', () => { drawHeatmap(); updateUrl(); });
  $('heatmap').addEventListener('pointermove', (event) => {
    if (state.drag) {
      state.pan = [state.drag.pan[0] + (event.clientX - state.drag.x), state.drag.pan[1] + (event.clientY - state.drag.y)];
      drawHeatmap();
    }
  });
  $('heatmap').addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    state.drag = {x: event.clientX, y: event.clientY, pan: [...(state.pan || [0, 0])]};
    $('heatmap').setPointerCapture(event.pointerId);
  });
  $('heatmap').addEventListener('pointerup', () => state.drag = null);
  $('heatmap').addEventListener('pointercancel', () => state.drag = null);
  $('map-zoom-in').addEventListener('click', () => { state.zoom = Math.min(4, (state.zoom || 1) * 1.4); drawHeatmap(); });
  $('map-zoom-out').addEventListener('click', () => { state.zoom = Math.max(1, (state.zoom || 1) / 1.4); if (state.zoom === 1) state.pan = [0, 0]; drawHeatmap(); });
  $('map-reset').addEventListener('click', () => { state.zoom = 1; state.pan = [0, 0]; drawHeatmap(); });
  $('retry').addEventListener('click', () => state.catalog?.seasons.length ? refresh() : bootstrap(true));
  $('share').addEventListener('click', async () => {
    updateUrl();
    try { await navigator.clipboard.writeText(location.href); $('status').textContent = '当前筛选链接已复制。'; }
    catch { $('status').textContent = '请复制浏览器地址栏中的当前链接。'; }
  });
  new MutationObserver(() => { if (state.tab === 'overview' && state.payload) renderOverview(); }).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
  window.addEventListener('resize', () => {
    if (state.tab === 'overview' && state.payload) renderOverview();
    else if (state.tab === 'runs' && state.payload) renderRuns();
    else if (state.tab === 'heatmaps' && state.payload) drawHeatmap();
  });
  setInterval(async () => {
    if (document.hidden || !state.catalog) return;
    try {
      const next = await json('/portal/api/rio-statistics/?status=1');
      if (next.scope?.token !== state.catalog.scope?.token || next.scope_state !== state.catalog.scope_state
          || next.revision !== state.catalog.revision) await bootstrap(true);
    } catch { /* 本地目录读取失败不清空已有页面。 */ }
  }, 60000);
  bootstrap();
})();
