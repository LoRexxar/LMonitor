(function () {
    'use strict';

    const state = {
        payload: null, activeScope: 'overall', source: 'local', localScope: 'overall',
        mythicRole: 'damage', localPayload: null, mythicPayload: null,
        filters: {season: '', dungeon: '0', period: ''}, cache: new Map(), requestId: 0,
    };
    const tabs = document.getElementById('mplus-rank-tabs');
    const list = document.getElementById('mplus-rank-list');
    const status = document.getElementById('mplus-rank-status');
    const updated = document.getElementById('mplus-rank-updated');
    const method = document.getElementById('mplus-rank-method');
    const tierBoard = document.getElementById('mplus-rank-tier-board');
    const tierCount = document.getElementById('mplus-rank-tier-count');
    const tierGroups = document.getElementById('mplus-rank-tier-groups');
    const sourceTabs = [...document.querySelectorAll('[data-source]')];
    const content = document.getElementById('mplus-rank-content');
    const filters = document.getElementById('mplus-mythicstats-controls');

    function formatDps(value) {
        const number = Number(value || 0);
        if (number >= 1000000) {
            return `${(number / 1000000).toFixed(number >= 10000000 ? 1 : 2)}m`;
        }
        if (number >= 1000) {
            return `${(number / 1000).toFixed(number >= 100000 ? 0 : 1)}k`;
        }
        return Math.round(number).toLocaleString('zh-CN');
    }

    function formatTimestamp(value) {
        if (!value) return '暂无来源时间';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return value;
        return new Intl.DateTimeFormat('zh-CN', {
            month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
            hour12: false
        }).format(date);
    }

    function element(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function renderTabs() {
        tabs.replaceChildren();
        (state.payload.scopes || []).forEach((scope) => {
            const button = element('button', `mplus-rank-tab${scope.key === state.activeScope ? ' active' : ''}`, scope.label);
            button.type = 'button';
            button.role = 'tab';
            button.setAttribute('aria-selected', scope.key === state.activeScope ? 'true' : 'false');
            button.title = scope.name || scope.label;
            button.addEventListener('click', () => {
                state.activeScope = scope.key;
                if (state.source === 'local') state.localScope = scope.key;
                else state.mythicRole = scope.key;
                renderTabs();
                renderRankings();
            });
            tabs.appendChild(button);
        });
    }

    const tierBands = [
        [95, 'S', '≥95%'],
        [90, 'A', '90–<95%'],
        [85, 'B', '85–<90%'],
        [80, 'C', '80–<85%'],
        [75, 'D', '75–<80%'],
        [70, 'E', '70–<75%'],
        [0, 'F', '<70%']
    ];

    function tierForAverage(average, leaderAverage) {
        const ratio = leaderAverage > 0 ? Number(average || 0) / leaderAverage * 100 : 0;
        return (tierBands.find(([threshold]) => ratio >= threshold) || [0, 'F'])[1];
    }

    function resolvedTier(row, leaderAverage) {
        const suppliedTier = String(row.tier || '').toUpperCase();
        if (state.source === 'mythicstats') return /^[SABCDEF]$/.test(suppliedTier) ? suppliedTier : '—';
        return /^[SABCDEF]$/.test(suppliedTier)
            ? suppliedTier
            : tierForAverage(row.average_dps, leaderAverage);
    }

    function safeClassColor(value) {
        const color = String(value || '').trim();
        return /^#[0-9a-f]{6}$/i.test(color) ? color : '#64748b';
    }

    function displaySpecName(row) {
        return row.spec_name_cn === '恶魔学识' ? '恶魔' : row.spec_name_cn;
    }

    function specIconUrl(row) {
        // 兼容尚未重新生成的榜单快照，避免继续放大 18px 缩略图。
        return String(row.icon_url || '').replace('/wow_icons_oss/small/', '/wow_icons_oss/large/');
    }

    function metric(label, value, primary) {
        const node = element('div', `mplus-rank-metric${primary ? ' primary' : ''}`);
        node.append(element('span', '', label), element('strong', '', value));
        return node;
    }

    function setDetailLink(link, row) {
        link.href = row.detail_url;
        if (state.source === 'mythicstats') {
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            link.title = `查看 ${displaySpecName(row)} 的 Mythicstats 详情`;
        } else {
            link.title = `查看${row.class_name_cn} · ${row.spec_name_cn}副本详情`;
        }
    }

    function renderTierBoard(rows, leaderAverage) {
        tierGroups.replaceChildren();
        if (!rows.length) {
            tierBoard.hidden = true;
            tierCount.textContent = '';
            return;
        }

        tierCount.textContent = `${rows.length} 个专精 · ${state.source === 'mythicstats' ? 'Mythicstats 来源评级' : '当前范围独立评级'}`;
        const bands = state.source === 'mythicstats' ? [...tierBands, [0, '—', '']] : tierBands;
        bands.forEach(([, tier, rangeLabel]) => {
            const members = rows.filter((row) => resolvedTier(row, leaderAverage) === tier);
            if (state.source === 'mythicstats' && !members.length) return;
            const group = element('article', `mplus-rank-tier-group mplus-rank-tier-group-${tier.toLowerCase()}`);
            const label = element('div', 'mplus-rank-tier-label');
            label.append(
                element('strong', `mplus-rank-tier-letter tier-${tier.toLowerCase()}`, tier),
                element('span', '', state.source === 'mythicstats' ? '' : rangeLabel),
                element('small', '', `${members.length} 个`)
            );

            const items = element('div', 'mplus-rank-tier-items');
            if (!members.length) {
                items.appendChild(element('span', 'mplus-rank-tier-empty', '当前范围暂无专精'));
            }
            members.forEach((row) => {
                const classColor = safeClassColor(row.class_color);
                const card = element('a', 'mplus-rank-tier-card');
                setDetailLink(card, row);
                card.style.setProperty('--class-color', classColor);
                card.setAttribute(
                    'aria-label',
                    `${row.class_name_cn} ${row.spec_name_cn}，${tier} 评级，平均 DPS ${formatDps(row.average_dps)}`
                );

                const icon = element('img');
                icon.src = specIconUrl(row);
                icon.alt = row.spec_name_cn;
                icon.loading = 'lazy';

                const identity = element('span', 'mplus-rank-tier-identity');
                const specName = element('strong', '', displaySpecName(row));
                const averageDps = element(
                    'span',
                    'mplus-rank-tier-dps',
                    formatDps(row.average_dps)
                );
                identity.append(specName, averageDps);
                card.append(icon, identity);
                items.appendChild(card);
            });

            group.append(label, items);
            tierGroups.appendChild(group);
        });
        tierBoard.hidden = false;
    }

    function renderRankings() {
        const rows = (state.payload.rankings || {})[state.activeScope] || [];
        list.replaceChildren();
        const isMythicstats = state.source === 'mythicstats';
        list.classList.toggle('mplus-rank-list-mythicstats', isMythicstats);
        const leaderAverage = Math.max(...rows.map((row) => Number(row.average_dps || 0)), 0);
        renderTierBoard(rows, leaderAverage);
        if (!rows.length) {
            list.hidden = true;
            status.hidden = false;
            status.textContent = state.source === 'mythicstats' ? '当前筛选范围暂无该职责的 DPS 数据' : state.activeScope === 'overall'
                ? '暂无覆盖全部赛季副本的专精数据'
                : '该副本暂无可用 DPS 样本';
            return;
        }

        const dpsScale = Math.max(leaderAverage, ...rows.map((row) => Number(row.highest_dps || 0)), 1);
        const heading = element('div', 'mplus-rank-list-heading');
        heading.append(
            element('h2', '', '平均 DPS 排名'),
            element('span', '', '实色：平均 DPS · 虚色 / 虚线：最高 DPS')
        );
        list.appendChild(heading);
        const header = element('div', 'mplus-rank-header');
        const metricHeader = element('span', 'mplus-rank-metrics-header');
        metricHeader.append(
            element('span', '', '评级'),
            element('span', '', '平均'),
            element('span', '', '最高')
        );
        header.append(
            element('span', '', '#'),
            metricHeader
        );
        if (isMythicstats) {
            header.append(
                element('span', 'mplus-rank-extra-header', '日志量'),
                element('span', 'mplus-rank-extra-header', '排名变化')
            );
        }
        header.appendChild(element('span', '', '专精 / DPS 对比'));
        list.appendChild(header);

        let previousTier = null;
        rows.forEach((row) => {
            const rank = Number(row.rank || 0);
            const card = element('article', `mplus-rank-row${rank >= 1 && rank <= 3 ? ` mplus-rank-top-${rank}` : ''}`);
            card.appendChild(element('div', 'mplus-rank-position', String(row.rank)));

            const classColor = safeClassColor(row.class_color);
            card.style.setProperty('--class-color', classColor);

            const tier = resolvedTier(row, leaderAverage);
            card.classList.add(`mplus-rank-tier-${tier.toLowerCase()}`);
            if (previousTier !== null && previousTier !== tier) {
                card.classList.add('mplus-rank-tier-break');
            }
            previousTier = tier;
            const metrics = element('div', 'mplus-rank-metrics');
            const tierBadge = element('span', `mplus-rank-tier tier-${tier.toLowerCase()}`, tier);
            tierBadge.setAttribute('aria-label', `评级 ${tier}`);
            const peakMetric = metric('最高', formatDps(row.highest_dps), false);
            peakMetric.classList.add('peak');
            metrics.append(
                tierBadge,
                metric('平均', formatDps(row.average_dps), true),
                peakMetric
            );
            card.appendChild(metrics);
            if (isMythicstats) {
                const runs = metric('日志量', row.runs || '—', false);
                runs.classList.add('mplus-rank-extra', 'mplus-rank-runs');
                const change = metric('排名变化', row.diff_raw || '0', false);
                change.classList.add('mplus-rank-extra', 'mplus-rank-change');
                const difference = Number(row.diff_value || 0);
                change.classList.add(difference > 0 ? 'change-up' : difference < 0 ? 'change-down' : 'change-same');
                card.append(runs, change);
            }

            const average = Math.max(0, Math.min(100, Number(row.average_dps || 0) / dpsScale * 100));
            const peak = Math.max(0, Math.min(100, Number(row.highest_dps || 0) / dpsScale * 100));
            const plot = element('a', 'mplus-rank-plot');
            setDetailLink(plot, row);
            plot.setAttribute('aria-label', `${row.spec_name_cn}，平均 DPS ${formatDps(row.average_dps)}，最高 DPS ${formatDps(row.highest_dps)}`);
            const icon = element('img');
            icon.src = specIconUrl(row);
            icon.alt = '';
            icon.loading = 'lazy';
            const track = element('span', 'mplus-rank-track');
            const peakBar = element('span', 'mplus-rank-peak-bar');
            peakBar.style.width = `${peak.toFixed(1)}%`;
            peakBar.setAttribute('aria-hidden', 'true');
            track.appendChild(peakBar);
            const averageBar = element('span', 'mplus-rank-average-bar');
            averageBar.style.width = `${average.toFixed(1)}%`;
            track.appendChild(averageBar);
            track.appendChild(element('strong', 'mplus-rank-bar-label', displaySpecName(row)));
            plot.append(icon, track);
            card.appendChild(plot);
            list.appendChild(card);
        });

        status.hidden = true;
        list.hidden = false;
    }

    function render(payload) {
        state.payload = payload;
        const generated = payload.generated_at;
        const source = payload.source_updated_at;
        updated.textContent = `生成 ${formatTimestamp(generated)}${source ? ` · 来源 ${formatTimestamp(source)}` : ''}`;
        const required = ((payload.method || {}).required_dungeon_count) || Math.max(0, (payload.scopes || []).length - 1);
        const sampleCap = payload.method?.sample_cap_per_spec_dungeon || 100;
        const explanation = element('dl', 'mplus-rank-explanation');
        const notes = [
            ['数据来源', '来自本站收录的 Warcraft Logs（WCL）当前赛季大秘境实战日志排名，仅统计输出专精，DPS 使用 WCL 提供的数值。'],
            ['样本筛选', `每个专精、每个副本最多取 ${sampleCap} 条记录：优先高层钥石，同层按 DPS 从高到低选择，只保留不低于该层中位数的记录；同一地区、服务器、角色仅保留一条。因此，本榜反映筛选后样本的表现。`],
            ['数值计算', `单副本：平均 DPS 为入选样本的算术平均，最高 DPS 为样本中的最大值。总计：需在全部 ${required} 个副本均有样本；平均与最高分别按各副本的最终样本数加权，公式为 Σ（副本数值 × 样本数）÷ 总样本数。总计的最高值也是加权结果。`],
            ['评级规则', '按平均 DPS 从高到低排名；总计与各副本独立评级。以当前范围榜首的平均 DPS 为 100%，计算「该专精平均 DPS ÷ 榜首平均 DPS × 100%」，每相差 5 个百分点划分一档。最高 DPS 仅供参考，不参与排名和评级。'],
        ];
        notes.forEach(([label, text]) => {
            explanation.append(element('dt', '', label), element('dd', '', text));
        });
        const bands = element('div', 'mplus-rank-explanation-bands');
        tierBands.forEach(([, tier, range]) => {
            bands.appendChild(element('span', '', `${tier}：${tier === 'F' ? '<70%' : range}`));
        });
        explanation.lastElementChild.appendChild(bands);
        method.replaceChildren(explanation);
        renderTabs();
        renderRankings();
    }

    function sourceUrl(value) {
        try {
            const url = new URL(value || '/dps', 'https://mythicstats.com');
            return url.protocol === 'https:' && url.hostname === 'mythicstats.com' ? url.href : 'https://mythicstats.com/dps';
        } catch (_) {
            return 'https://mythicstats.com/dps';
        }
    }

    function renderMythicstatsFilters(payload) {
        filters.replaceChildren();
        const seasons = [...new Set([...(payload.seasons || []), payload.season].filter(Boolean))];
        const definitions = [
            ['season', '赛季', [{value: '', label: '当前赛季'}, ...seasons.map(value => ({value, label: value}))]],
            ['dungeon', '副本', (payload.dungeons || [{id: 0}]).map(d => ({value: String(d.id), label: Number(d.id) === 0 ? '全部副本' : d.name}))],
            ['period', '周次', (payload.periods || []).map(p => ({value: String(p.id), label: p.label || String(p.id)}))],
        ];
        definitions.forEach(([key, label, options]) => {
            const wrapper = element('label', 'mplus-rank-filter');
            const select = element('select');
            select.id = `mythicstats-${key}-select`;
            select.setAttribute('aria-label', label);
            options.forEach(item => {
                const option = element('option', '', item.label);
                option.value = item.value;
                select.appendChild(option);
            });
            select.value = state.filters[key];
            select.addEventListener('change', () => {
                state.filters[key] = select.value;
                if (key === 'season') state.filters.dungeon = '0';
                if (key !== 'period') state.filters.period = '';
                if (key === 'season') {
                    try { localStorage.setItem('portal_mythicstats_season', select.value); } catch (_) {}
                }
                loadSource();
            });
            wrapper.append(element('span', '', label), select);
            filters.appendChild(wrapper);
        });
        filters.hidden = false;
    }

    function renderMythicstats(payload) {
        state.mythicPayload = payload;
        state.filters = {
            season: payload.season || state.filters.season,
            dungeon: String(payload.dungeon_id || 0),
            period: payload.active_period ? String(payload.active_period) : '',
        };
        renderMythicstatsFilters(payload);
        const roles = [['damage', '输出'], ['tank', '坦克'], ['healer', '治疗']];
        const rankings = {};
        roles.forEach(([key]) => {
            rankings[key] = (payload.roles?.[key] || []).map(row => ({
                ...row,
                spec_name_cn: row.spec_name_cn || row.spec_name || row.spec_slug,
                average_dps: row.avg_value,
                highest_dps: row.top_value,
                detail_url: sourceUrl(row.spec_url),
            }));
        });
        state.payload = {scopes: roles.map(([key, label]) => ({key, label})), rankings};
        state.activeScope = state.mythicRole;
        const timestamps = Object.values(rankings).flat().map(row => row.updated_at).filter(Boolean).sort();
        const period = (payload.periods || []).find(p => String(p.id) === state.filters.period);
        updated.textContent = timestamps.length ? timestamps[timestamps.length - 1] : period?.label || '来源未提供更新时间';
        method.replaceChildren();
        let scopeNote = payload.source_note || '';
        if (Number(payload.key_min) > 0) {
            scopeNote = Number(payload.key_max) > 0 ? `${payload.key_min}–${payload.key_max} 层` : `${payload.key_min}+ 层`;
        }
        const sourceLink = element('a', '', 'Mythicstats');
        sourceLink.href = sourceUrl(payload.source_url);
        sourceLink.target = '_blank';
        sourceLink.rel = 'noopener noreferrer';
        method.append(
            element('span', '', `${scopeNote ? `数据口径：${scopeNote}。` : ''}评级沿用来源数据，输出、坦克、治疗分别排名。来源：`),
            sourceLink,
            element('div', 'mplus-rank-source-note', '榜单随美服每周三更新；周初日志较少，请结合日志数量参考。')
        );
        renderTabs();
        renderRankings();
    }

    async function loadSource() {
        const requestId = ++state.requestId;
        const source = state.source;
        content.setAttribute('aria-busy', 'true');
        list.hidden = true;
        tierBoard.hidden = true;
        tabs.hidden = true;
        status.hidden = false;
        status.replaceChildren(element('span', '', '正在加载排名…'));
        updated.textContent = '加载中…';
        method.replaceChildren();
        const query = new URLSearchParams(state.filters).toString();
        const cacheKey = source === 'local' ? 'local' : query;
        try {
            let payload = source === 'local' ? state.localPayload : state.cache.get(cacheKey);
            if (!payload) {
                const url = source === 'local' ? '/portal/api/mplus/dps-rankings/' : `/portal/api/mythicstats/dps/?${query}`;
                const response = await fetch(url, {headers: {'Accept': 'application/json'}});
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const result = await response.json();
                payload = source === 'local' ? result : result.data;
                if (!payload || (source === 'mythicstats' && !payload.roles)) throw new Error('数据格式无效');
                if (source === 'local') state.localPayload = payload;
                else state.cache.set(cacheKey, payload);
            }
            if (requestId !== state.requestId) return;
            if (source === 'local') {
                state.activeScope = state.localScope;
                render(payload);
            } else {
                renderMythicstats(payload);
                state.cache.set(new URLSearchParams(state.filters).toString(), payload);
            }
            tabs.hidden = false;
        } catch (_) {
            if (requestId !== state.requestId) return;
            status.replaceChildren(element('span', '', '排名数据暂时不可用。'));
            const retry = element('button', 'mplus-rank-retry', '重试');
            retry.type = 'button';
            retry.addEventListener('click', loadSource);
            status.appendChild(retry);
            updated.textContent = '加载失败';
        } finally {
            if (requestId === state.requestId) content.setAttribute('aria-busy', 'false');
        }
    }

    function selectSource(source) {
        state.source = source;
        sourceTabs.forEach(tab => {
            const active = tab.dataset.source === source;
            tab.classList.toggle('active', active);
            tab.setAttribute('aria-selected', String(active));
            tab.tabIndex = active ? 0 : -1;
        });
        content.setAttribute('aria-labelledby', `mplus-source-${source}`);
        filters.hidden = source !== 'mythicstats';
        if (source === 'mythicstats') renderMythicstatsFilters(state.mythicPayload || {});
        const url = new URL(window.location.href);
        if (source === 'mythicstats') url.searchParams.set('source', source);
        else url.searchParams.delete('source');
        window.history.replaceState(null, '', url);
        loadSource();
    }

    sourceTabs.forEach((tab, index) => {
        tab.addEventListener('click', () => selectSource(tab.dataset.source));
        tab.addEventListener('keydown', event => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault();
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? sourceTabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + sourceTabs.length) % sourceTabs.length;
            sourceTabs[next].focus();
            selectSource(sourceTabs[next].dataset.source);
        });
    });
    try {
        const saved = localStorage.getItem('portal_mythicstats_season') || '';
        state.filters.season = saved === 'season-mn-1' ? '' : saved;
    } catch (_) {}
    selectSource(new URLSearchParams(window.location.search).get('source') === 'mythicstats' ? 'mythicstats' : 'local');
}());
