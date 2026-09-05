(function () {
    'use strict';

    const state = {payload: null, activeScope: 'overall'};
    const tabs = document.getElementById('mplus-rank-tabs');
    const list = document.getElementById('mplus-rank-list');
    const status = document.getElementById('mplus-rank-status');
    const updated = document.getElementById('mplus-rank-updated');
    const method = document.getElementById('mplus-rank-method');

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
                renderTabs();
                renderRankings();
            });
            tabs.appendChild(button);
        });
    }

    const tierBands = [
        [95, 'S'], [90, 'A'], [85, 'B'], [80, 'C'], [75, 'D'], [70, 'E'], [0, 'F']
    ];

    function tierForAverage(average, leaderAverage) {
        const ratio = leaderAverage > 0 ? Number(average || 0) / leaderAverage * 100 : 0;
        return (tierBands.find(([threshold]) => ratio >= threshold) || [0, 'F'])[1];
    }

    function safeClassColor(value) {
        const color = String(value || '').trim();
        return /^#[0-9a-f]{6}$/i.test(color) ? color : '#64748b';
    }

    function metric(label, value, primary) {
        const node = element('div', `mplus-rank-metric${primary ? ' primary' : ''}`);
        node.append(element('span', '', label), element('strong', '', value));
        return node;
    }

    function renderRankings() {
        const rows = (state.payload.rankings || {})[state.activeScope] || [];
        list.replaceChildren();
        if (!rows.length) {
            list.hidden = true;
            status.hidden = false;
            status.textContent = state.activeScope === 'overall'
                ? '暂无覆盖全部赛季副本的专精数据'
                : '该副本暂无可用 DPS 样本';
            return;
        }

        const maximum = Math.max(...rows.map((row) => Number(row.highest_dps || 0)), 1);
        const leaderAverage = Math.max(...rows.map((row) => Number(row.average_dps || 0)), 0);
        const header = element('div', 'mplus-rank-header');
        const metricHeader = element('span', 'mplus-rank-metrics-header');
        metricHeader.append(
            element('span', '', 'Tier'),
            element('span', '', '下限'),
            element('span', '', 'Avg'),
            element('span', '', '最高'),
            element('span', '', '样本')
        );
        header.append(
            element('span', '', '#'),
            element('span', '', '专精'),
            metricHeader,
            element('span', '', '平均 DPS（职业色）/ 最高（刻度）')
        );
        list.appendChild(header);

        rows.forEach((row) => {
            const rank = Number(row.rank || 0);
            const card = element('article', `mplus-rank-row${rank >= 1 && rank <= 3 ? ` mplus-rank-top-${rank}` : ''}`);
            card.appendChild(element('div', 'mplus-rank-position', `#${row.rank}`));

            const classColor = safeClassColor(row.class_color);
            card.style.setProperty('--class-color', classColor);
            const spec = element('a', 'mplus-rank-spec');
            spec.href = row.detail_url;
            spec.title = `查看${row.class_name_cn} · ${row.spec_name_cn}副本详情`;
            const icon = element('img');
            icon.src = row.icon_url;
            icon.alt = row.spec_name_cn;
            icon.loading = 'lazy';
            const names = element('span');
            const specName = element('strong', '', row.spec_name_cn);
            specName.style.color = classColor;
            names.appendChild(specName);
            names.appendChild(element('small', '', row.class_name_cn));
            spec.append(icon, names);
            card.appendChild(spec);

            const suppliedTier = String(row.tier || '').toUpperCase();
            const tier = /^[SABCDEF]$/.test(suppliedTier)
                ? suppliedTier
                : tierForAverage(row.average_dps, leaderAverage);
            const metrics = element('div', 'mplus-rank-metrics');
            const tierBadge = element('span', `mplus-rank-tier tier-${tier.toLowerCase()}`, tier);
            tierBadge.setAttribute('aria-label', `评级 ${tier}`);
            metrics.append(
                tierBadge,
                metric('下限', formatDps(row.lower_dps), false),
                metric('Avg', formatDps(row.average_dps), true),
                metric('最高', formatDps(row.highest_dps), false),
                metric('样本', Number(row.sample_size || 0).toLocaleString('zh-CN'), false)
            );
            card.appendChild(metrics);

            const average = Math.max(0, Math.min(100, Number(row.average_dps || 0) / maximum * 100));
            const high = Math.max(0, Math.min(100, Number(row.highest_dps || 0) / maximum * 100));
            const plot = element('div', 'mplus-rank-plot');
            const track = element('div', 'mplus-rank-track');
            track.setAttribute('aria-label', `平均 DPS ${formatDps(row.average_dps)}，最高 DPS ${formatDps(row.highest_dps)}`);
            const averageBar = element('span', 'mplus-rank-average-bar');
            averageBar.style.width = `${average.toFixed(1)}%`;
            const peakMarker = element('span', 'mplus-rank-peak-marker');
            peakMarker.style.left = `${high.toFixed(1)}%`;
            track.append(averageBar, peakMarker);
            plot.appendChild(track);
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
        method.textContent = `总计要求专精覆盖当前赛季全部 ${required} 个副本；单副本按代表样本统计，总计按各副本样本数加权。评级以当前范围榜首平均 DPS 为基准，每 5% 一档：S≥95%、A≥90%、B≥85%、C≥80%、D≥75%、E≥70%、F<70%。`;
        renderTabs();
        renderRankings();
    }

    fetch('/portal/api/mplus/dps-rankings/', {headers: {'Accept': 'application/json'}})
        .then((response) => {
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            return response.json();
        })
        .then(render)
        .catch(() => {
            status.textContent = '排名数据暂时不可用，请稍后再试';
            updated.textContent = '加载失败';
        });
}());
