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
        rows.forEach((row) => {
            const card = element('article', 'mplus-rank-row');
            card.appendChild(element('div', 'mplus-rank-position', `#${row.rank}`));

            const spec = element('a', 'mplus-rank-spec');
            spec.href = row.detail_url;
            spec.title = `查看${row.class_name_cn} · ${row.spec_name_cn}副本详情`;
            const icon = element('img');
            icon.src = row.icon_url;
            icon.alt = row.spec_name_cn;
            icon.loading = 'lazy';
            const names = element('span');
            const specName = element('strong', '', row.spec_name_cn);
            specName.style.color = row.class_color || '#0f172a';
            names.appendChild(specName);
            names.appendChild(element('small', '', row.class_name_cn));
            spec.append(icon, names);
            card.appendChild(spec);

            const low = Math.max(0, Math.min(100, Number(row.lower_dps || 0) / maximum * 100));
            const average = Math.max(0, Math.min(100, Number(row.average_dps || 0) / maximum * 100));
            const high = Math.max(low, Math.min(100, Number(row.highest_dps || 0) / maximum * 100));
            const plot = element('div', 'mplus-rank-plot');
            const track = element('div', 'mplus-rank-track');
            track.style.setProperty('--low', `${low}%`);
            track.style.setProperty('--average', `${average}%`);
            track.style.setProperty('--high', `${high}%`);
            track.append(element('span', 'mplus-rank-range'), element('span', 'mplus-rank-average'));
            const values = element('div', 'mplus-rank-values');
            values.append(
                element('span', '', `下限 ${formatDps(row.lower_dps)}`),
                element('strong', '', `平均 ${formatDps(row.average_dps)}`),
                element('span', '', `最高 ${formatDps(row.highest_dps)}`)
            );
            plot.append(track, values);
            card.appendChild(plot);

            const primary = element('div', 'mplus-rank-primary');
            primary.append(
                element('strong', '', formatDps(row.average_dps)),
                element('span', '', `${row.sample_size} 个最终样本`)
            );
            card.appendChild(primary);
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
        method.textContent = `总计要求专精覆盖当前赛季全部 ${required} 个副本；单副本先按高层到低层、层内中位数以上及角色去重选样，总计按各副本最终样本数加权。`;
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
