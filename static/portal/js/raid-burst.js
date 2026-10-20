/* 登录后加载的战斗统计；原始样本仅由受保护的页面提供。 */
(() => {
    'use strict';
    const root = document.getElementById('raid-burst-page');
    const dataNode = document.getElementById('raid-burst-data');
    if (!root || !dataNode) return;
    const $ = (selector) => root.querySelector(selector);
    const colors = {107574: '#2563eb', 167105: '#0d9488', 446035: '#7c3aed'};
    let payload;
    let spell;
    try {
        payload = JSON.parse(dataNode.textContent);
    } catch {
        $('#coverage').textContent = '样本读取失败，请刷新重试。';
        return;
    }
    if (!payload) return;
    const names = payload.skill_names;
    spell = Object.keys(names)[0];
    for (const [key, label] of new Map(payload.samples.map((row) => [row.lust_group, row.lust_label]))) {
        const option = document.createElement('option');
        option.value = key;
        option.textContent = label;
        $('#group').append(option);
    }

    function element(tag, text, className) {
        const node = document.createElement(tag);
        if (text !== undefined) node.textContent = text;
        if (className) node.className = className;
        return node;
    }

    function formatTime(value) {
        const ticks = Math.round(Math.abs(value) * 10);
        return `${Math.floor(ticks / 600)}分${ticks % 600 / 10}秒`;
    }

    function inputTime(value) {
        return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`;
    }

    function relativeTime(value) {
        return `${value < 0 ? '提前' : value > 0 ? '延后' : ''}${formatTime(value)}`;
    }

    const tooltip = element('div', undefined, 'burst-tooltip');
    tooltip.id = 'burst-sample-tooltip';
    tooltip.setAttribute('role', 'tooltip');
    tooltip.hidden = true;
    root.append(tooltip);
    let tooltipOwner = null;

    function hideSample() {
        tooltip.hidden = true;
        tooltipOwner?.removeAttribute('aria-describedby');
        tooltipOwner = null;
    }

    function placeSample(event, target) {
        const box = target.getBoundingClientRect();
        const pointer = Number.isFinite(event.clientX) && event.type !== 'focus';
        let left = pointer ? event.clientX + 14 : box.left + 12;
        let top = pointer ? event.clientY + 14 : box.bottom + 8;
        const width = tooltip.offsetWidth;
        const height = tooltip.offsetHeight;
        if (top + height > window.innerHeight - 8) {
            top = (pointer ? event.clientY : box.top) - height - 10;
        }
        left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
        top = Math.max(8, Math.min(top, window.innerHeight - height - 8));
        tooltip.style.left = `${left}px`;
        tooltip.style.top = `${top}px`;
    }

    function bindSample(target, row, sampleLabel = '样本') {
        target.classList.add('burst-sample-trigger');
        target.setAttribute('tabindex', '0');
        target.setAttribute('aria-label', `${sampleLabel} ${row.rank}，${row.player?.name || '玩家未知'}，查看样本详情`);
        const show = (event) => {
            if (tooltipOwner !== target) {
                hideSample();
                tooltipOwner = target;
                target.setAttribute('aria-describedby', tooltip.id);
                tooltip.replaceChildren();
                tooltip.append(element('strong', row.player?.name || '玩家未知', 'burst-tooltip-name'));
                const server = [row.player?.region, row.player?.realm].filter(Boolean).join(' · ');
                tooltip.append(element('div', server || '服务器未知', 'burst-muted'));
                const fields = [
                    [sampleLabel, String(row.rank).padStart(3, '0')],
                    ['DPS', Number.isFinite(row.dps) ? new Intl.NumberFormat('zh-CN', {maximumFractionDigits: 1}).format(row.dps) : '未记录'],
                    ['战斗时长', formatTime(row.duration)],
                    ['嗜血时间', row.lust_start === null ? '未记录' : `开怪后${formatTime(row.lust_start)}`],
                    [names[spell], `${row.times[spell].length}次`],
                ];
                const list = element('dl', undefined, 'burst-tooltip-fields');
                fields.forEach(([label, value]) => list.append(element('dt', label), element('dd', value)));
                tooltip.append(list, element('div', 'DPS 来自该场战斗的排名记录', 'burst-muted'));
            }
            tooltip.hidden = false;
            placeSample(event, target);
        };
        target.addEventListener('pointerenter', show);
        target.addEventListener('pointermove', (event) => {
            if (tooltipOwner === target) placeSample(event, target);
        });
        target.addEventListener('pointerleave', () => {
            if (tooltipOwner === target) hideSample();
        });
        target.addEventListener('focus', show);
        target.addEventListener('blur', () => {
            if (tooltipOwner === target) hideSample();
        });
        target.addEventListener('click', show);
    }

    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') hideSample();
    });
    document.addEventListener('click', (event) => {
        if (!event.target.closest('.burst-sample-trigger')) hideSample();
    });
    window.addEventListener('resize', hideSample);
    window.addEventListener('scroll', hideSample, true);

    function readTime(selector) {
        const raw = $(selector).value.trim();
        if (!raw) return null;
        const match = raw.match(/^(\d+):([0-5]\d(?:\.\d)?)$/);
        return match ? Number(match[1]) * 60 + Number(match[2]) : NaN;
    }

    function svgElement(tag, attributes, text) {
        const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
        Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
        if (text !== undefined) node.textContent = text;
        return node;
    }

    function drawChart(rows, axis) {
        const container = $('#chart');
        container.replaceChildren();
        if (!rows.length) {
            container.append(element('p', '当前条件没有样本，请扩大时间范围。'));
            return;
        }
        const ordered = [...rows].sort((a, b) => a.duration - b.duration);
        const anchor = (row) => axis === 'lust' ? row.lust_start : 0;
        const minimum = Math.floor(Math.min(...ordered.map((row) => -anchor(row))) / 30) * 30;
        const maximum = Math.max(60, Math.ceil(Math.max(...ordered.map((row) => row.duration - anchor(row))) / 30) * 30);
        const height = 70 + ordered.length * 42;
        const x = (time) => 200 + (time - minimum) / (maximum - minimum) * 900;
        const svg = svgElement('svg', {
            viewBox: `0 0 1140 ${height}`, role: 'group',
            'aria-label': `${names[spell]}：${rows.length}份日志，${axis === 'lust' ? '从嗜血起算' : '从开怪起算'}，横轴为分:秒`,
        });
        svg.append(svgElement('title', {}, `${names[spell]}真实施放时间轴`));
        const line = (parent, x1, y1, x2, y2, stroke, width = 1) =>
            parent.append(svgElement('line', {x1, y1, x2, y2, stroke, 'stroke-width': width}));
        // 范围较长时减少刻度数量，避免分秒标签重叠。
        const step = maximum - minimum > 450 ? 60 : 30;
        for (let tick = minimum; tick <= maximum; tick += step) {
            line(svg, x(tick), 10, x(tick), height - 40, '#94a3b8', .3);
            svg.append(svgElement('text', {x: x(tick), y: height - 18, 'text-anchor': 'middle', 'font-size': 11},
                `${tick < 0 ? '−' : ''}${inputTime(Math.abs(tick))}`));
        }
        if (axis === 'lust') line(svg, x(0), 10, x(0), height - 40, '#f59e0b', 1.5);
        ordered.forEach((row, index) => {
            const y = 30 + index * 42;
            const offset = anchor(row);
            const sampleGroup = svgElement('g', {'data-sample-rank': row.rank});
            bindSample(sampleGroup, row);
            sampleGroup.append(svgElement('rect', {
                x: 0, y: y - 20, width: 1120, height: 40, fill: 'transparent',
                class: 'burst-sample-hit',
            }));
            svg.append(sampleGroup);
            sampleGroup.append(svgElement('text', {x: 4, y: y + 4, 'font-size': 11},
                `样本 ${String(row.rank).padStart(3, '0')} · ${formatTime(row.duration)}`));
            line(sampleGroup, x(-offset), y, x(row.duration - offset), y, '#94a3b8', .6);
            row.lust_windows.forEach((window) =>
                line(sampleGroup, x(window.start - offset), y + 7, x(window.end - offset), y + 7, '#f59e0b', 4));
            row.phases.filter((phase) => phase.id !== 1).forEach((phase) => {
                const px = x(phase.start - offset);
                sampleGroup.append(svgElement('polygon', {
                    points: `${px},${y - 17} ${px + 4},${y - 13} ${px},${y - 9} ${px - 4},${y - 13}`,
                    fill: '#94a3b8',
                }));
                sampleGroup.append(svgElement('text', {x: px + 6, y: y - 10, 'font-size': 9}, `P${phase.id}`));
            });
            line(sampleGroup, x(row.duration - offset), y - 4, x(row.duration - offset), y + 4, '#64748b', 2);
            row.times[spell].forEach((time) => {
                const circle = svgElement('circle', {cx: x(time - offset), cy: y, r: 3.5, fill: colors[spell] || '#2563eb'});
                circle.append(svgElement('title', {}, `开怪后${formatTime(time)} · 嗜血${relativeTime(time - row.lust_start)}`));
                sampleGroup.append(circle);
            });
        });
        container.append(svg);
    }

    function renderGroups(rows) {
        const container = $('#groups');
        container.replaceChildren();
        const keys = [...new Set(rows.map((row) => row.lust_group))]
            .sort((a, b) => (a === 'opener' ? -1 : 0) - (b === 'opener' ? -1 : 0));
        for (const key of keys) {
            const members = rows.filter((row) => row.lust_group === key);
            const section = element('section', undefined, 'burst-group');
            section.append(element('h3', `${members[0].lust_label} · ${members.length}份`));
            section.append(element('p', '相对嗜血标注提前或延后。每个区间独立计数，同一日志可能出现在多个窗口。', 'burst-muted'));
            const windows = element('div', undefined, 'burst-windows');
            const selected = new Set(members.map((row) => row.rank));
            const published = payload.groups[key][spell];
            const groups = published.windows.map((window) => ({
                ...window, ranks: window.ranks.filter((rank) => selected.has(rank)),
            })).filter((window) => window.ranks.length);
            for (const window of groups) {
                const label = Math.abs(window.end - window.start) < .05
                    ? relativeTime(window.start) : `${relativeTime(window.start)}～${relativeTime(window.end)}`;
                windows.append(element('span', `${names[spell]} ${label} · ${window.ranks.length}/${members.length}份`, 'burst-window'));
            }
            if (!groups.length) windows.append(element('span', '嗜血附近未记录到此技能的施放。', 'burst-muted'));
            section.append(windows);
            const plans = published.plans.map((plan) => {
                const matches = members.filter((row) => plan.ranks.includes(row.rank));
                return {...plan, rows: matches, reference: matches[0]};
            }).filter((plan) => plan.rows.length)
                .sort((a, b) => b.rows.length - a.rows.length || a.reference.rank - b.reference.rank);
            section.append(element('p', `${plans.length}种完整选择 · 以下节点从开怪起算，保留真实代表日志`, 'burst-muted'));
            plans.forEach((plan, index) => {
                const row = element('div', undefined, 'burst-scheme');
                const head = element('div');
                head.append(element('div', `方案 ${index + 1} · ${plan.rows.length}份`, 'burst-scheme-title'));
                head.append(element('div', `${plan.ranges.length}次 · ${formatTime(plan.reference.duration)}`, 'burst-muted'));
                bindSample(head, plan.reference, '参考样本');
                const points = element('div', undefined, 'burst-points');
                plan.ranges.forEach((window, i) => {
                    const label = Math.abs(window.end - window.start) < .05
                        ? formatTime(window.start) : `${formatTime(window.start)}～${formatTime(window.end)}`;
                    points.append(element('span', `${i + 1} · ${label}`, 'burst-point'));
                });
                const link = element('a', '原始日志 ↗');
                link.href = plan.reference.url;
                link.target = '_blank';
                link.rel = 'noopener noreferrer';
                row.append(head, points, link);
                section.append(row);
            });
            container.append(section);
        }
        if (!rows.length) container.append(element('p', '当前条件没有样本，请扩大时间范围。'));
    }

    function render() {
        hideSample();
        const minimum = readTime('#minimum');
        const maximum = readTime('#maximum');
        if ((minimum !== null && !Number.isFinite(minimum))
            || (maximum !== null && !Number.isFinite(maximum))
            || (minimum !== null && maximum !== null && minimum > maximum)) {
            $('#selection').textContent = '请按分:秒填写（如 4:30），秒数须小于60，且最短时间不能大于最长时间。';
            $('#groups').replaceChildren();
            $('#chart').replaceChildren();
            return;
        }
        const strategy = $('#group').value;
        const base = payload.samples.filter((row) => strategy === 'all' || row.lust_group === strategy);
        const bins = [[0, 240, '4分钟以下'], [240, 300, '4–5分钟'], [300, 360, '5–6分钟'],
            [360, 420, '6–7分钟'], [420, Infinity, '7分钟及以上']];
        $('#distribution').textContent = '该嗜血策略的时长分布：' + bins.map(([lo, hi, label]) =>
            `${label} ${base.filter((row) => row.duration >= lo && row.duration < hi).length}份`).join(' · ');
        const rows = base.filter((row) => (minimum === null || row.duration >= minimum)
            && (maximum === null || row.duration <= maximum));
        $('#selection').textContent = `当前 ${rows.length} 份日志 · ${new Set(rows.map((row) => row.lust_group)).size} 类嗜血策略`;
        drawChart(rows, $('#axis').value);
        renderGroups(rows);
    }

    $('#coverage').textContent = `${payload.attempted}份日志 · ${payload.successful}份完整 · `
        + `${payload.excluded_pi}份灌注剔除 · ${payload.excluded_no_lust}份无嗜血记录剔除 · `
        + `${payload.excluded_invalid}份身份或战斗不符 · ${payload.samples.length}份可分析`;
    $('#duration').addEventListener('change', () => {
        const value = $('#duration').value;
        if (value === 'all') {
            $('#minimum').value = '';
            $('#maximum').value = '';
        } else if (value !== 'custom') {
            const [minimum, maximum] = value.split(',').map(Number);
            $('#minimum').value = inputTime(minimum);
            $('#maximum').value = inputTime(maximum);
        }
        render();
    });
    ['#minimum', '#maximum'].forEach((id) => $(id).addEventListener('input', () => {
        $('#duration').value = 'custom';
        render();
    }));
    ['#group', '#axis'].forEach((id) => $(id).addEventListener('change', render));
    root.querySelectorAll('button[data-spell]').forEach((button) => button.addEventListener('click', () => {
        spell = button.dataset.spell;
        root.querySelectorAll('button[data-spell]').forEach((item) =>
            item.setAttribute('aria-pressed', String(item === button)));
        render();
    }));
    render();
    // 浏览器从往返缓存恢复页面时，重新向服务器确认登录状态。
    window.addEventListener('pageshow', (event) => {
        if (event.persisted) window.location.reload();
    });
})();
