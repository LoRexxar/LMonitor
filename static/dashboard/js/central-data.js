/* 后台中央资料只读查询；所有来源文字按纯文本输出。 */
(() => {
    'use strict';
    function init() {
        const root = document.getElementById('central-data-workspace');
        if (!root) return;
        const $ = selector => root.querySelector(selector);
        const form = $('#cd-filters'), section = document.getElementById('central-data');
        const dialog = $('#cd-detail');
        let resource = 'items', page = 1, pages = 1, loaded = false, listRequest, detailRequest;
        let listSequence = 0, detailSequence = 0, detailRow, detailPage = 1, detailPages = 1;
        const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
        const date = value => value ? new Date(value).toLocaleString('zh-CN') : '未记录';
        const source = value => ({wago:'Wago 游戏资料',wowhead:'Wowhead 补充资料',db2_backfill:'DB2 资料回填',db2_repair:'DB2 资料修复',db2:'DB2 游戏资料',derived:'关联资料推导','db2+wowhead_live':'DB2 与 Wowhead 补充资料'}[value] || value);
        const statNames = {strength:'力量',agility:'敏捷',intellect:'智力',stamina:'耐力',armor:'护甲',crit:'暴击',haste:'急速',mastery:'精通',versatility:'全能',leech:'吸血',avoidance:'闪避',speed:'速度'};
        function fieldsForResource() {
            root.querySelectorAll('[data-cd-fields]').forEach(group => {
                const active = group.dataset.cdFields === resource;
                group.hidden = !active;
                group.querySelectorAll('input,select').forEach(input => { input.disabled = !active; });
            });
            root.querySelectorAll('[data-cd-resource]').forEach(button => button.setAttribute('aria-selected', String(button.dataset.cdResource === resource)));
            form.elements.q.placeholder = resource === 'items' ? '中文名、英文名、物品 ID 或 SimC 标识' : '中文名、英文名、描述或技能／节点 ID';
            $('#cd-result-hint').textContent = resource === 'items' ? '按物品 ID 聚合，详情展示当前目录中的装等与掉落变体。' : '同一技能合并展示关联节点；没有技能关联的入口节点独立列出。';
        }
        function parameters() {
            const params = new URLSearchParams(new FormData(form));
            params.set('resource', resource);
            params.set('page', page);
            params.set('page_size', $('#cd-page-size').value);
            return params;
        }
        function saveLocation(params) {
            const url = new URL(location.href);
            [...url.searchParams.keys()].filter(key => key.startsWith('cd_')).forEach(key => url.searchParams.delete(key));
            params.forEach((value, key) => { if (value) url.searchParams.set(`cd_${key}`, value); });
            history.replaceState(history.state, '', url);
        }
        function restoreLocation() {
            const params = new URLSearchParams(location.search);
            resource = params.get('cd_resource') === 'talents' ? 'talents' : 'items';
            form.reset();
            for (const control of form.elements) {
                if (!control.name || !params.has(`cd_${control.name}`)) continue;
                const value = params.get(`cd_${control.name}`);
                if (control.tagName === 'SELECT' && ![...control.options].some(option => option.value === value)) control.add(new Option(value, value));
                control.value = value;
            }
            page = Number(params.get('cd_page')) || 1;
            const size = params.get('cd_page_size');
            $('#cd-page-size').value = ['25', '50', '100'].includes(size) ? size : '25';
            fieldsForResource();
        }
        function fillFacets(facets) {
            const names = {seasons:'season',slots:'slot',qualities:'quality',types:'variant_type',tracks:'upgrade_track',classes:'class_name',specs:'spec_name',trees:'tree_type'};
            for (const [key, values] of Object.entries(facets || {})) {
                const select = form.elements[names[key]];
                if (!select) continue;
                const value = select.value, first = select.options[0].cloneNode(true);
                select.replaceChildren(first, ...values.map(row => new Option(row.label, row.value)));
                select.value = values.some(row => row.value === value) ? value : '';
            }
        }
        async function fetchData(params, signal) {
            const response = await fetch(`/api/dashboard/central-data/?${params}`, {signal, credentials:'same-origin', headers:{Accept:'application/json'}});
            let data;
            try { data = await response.json(); } catch (_) { throw new Error('无法读取查询结果，请确认登录状态后重试。'); }
            if (!response.ok || !data.success) throw new Error(data.error || data.message || '查询失败，请重试。');
            return data;
        }
        function artwork(row) {
            return row.icon_url ? `<img class="cd-icon" src="${escape(row.icon_url)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : '<span class="cd-icon" aria-hidden="true"></span>';
        }
        function renderRows(records) {
            const equipment = resource === 'items';
            const labels = equipment ? ['装备','品质 / 部位','装等范围','匹配变体','操作'] : ['天赋 / 技能','所属职业 / 专精','最终描述','关联节点','操作'];
            $('#cd-table-head').innerHTML = `<tr>${labels.map(label => `<th scope="col">${label}</th>`).join('')}</tr>`;
            $('#cd-table-body').innerHTML = records.length ? records.map((row, index) => `<tr>
                <td><div class="cd-name">${artwork(row)}<div><strong>${escape(row.name)}</strong><span class="cd-secondary">${equipment ? '物品' : row.identity_kind === 'node' ? '节点' : '技能'} #${escape(row.id)}</span>${row.name_en && row.name_en !== row.name ? `<span class="cd-secondary">${escape(row.name_en)}</span>` : ''}${equipment && !row.translated ? '<span class="cd-warning">名称待补中文</span>' : ''}</div></div></td>
                <td>${escape(equipment ? row.quality : row.class_name)}<span class="cd-secondary">${escape(equipment ? row.slot || '未指定部位' : row.spec_name + ' · ' + row.tree)}</span></td>
                <td>${equipment ? `${escape(row.minimum)}${row.maximum !== row.minimum ? ' – ' + escape(row.maximum) : ''}` : `<span class="cd-description">${escape(row.description || '描述待补齐')}</span>`}</td>
                <td>${escape(row.count)}</td><td><button type="button" class="cd-detail-button" data-cd-detail="${index}">查看详情</button></td></tr>`).join('') : '<tr><td colspan="5"><div class="cd-empty">没有符合条件的资料<br>可尝试清空关键词或调整筛选条件。</div></td></tr>';
            $('#cd-table-body').querySelectorAll('[data-cd-detail]').forEach(button => button.addEventListener('click', () => {
                detailRow = records[Number(button.dataset.cdDetail)]; detailPage = 1;
                $('#cd-detail-title').textContent = detailRow.name;
                $('#cd-detail-subtitle').textContent = `${form.elements.branch.value === 'ptr' ? '测试服 PTR' : '正式服'} · ${resource === 'items' ? '装备' : '天赋'}资料`;
                dialog.showModal(); loadDetail();
            }));
        }
        async function load() {
            loaded = true;
            const sequence = ++listSequence;
            listRequest?.abort(); listRequest = new AbortController();
            const params = parameters(); saveLocation(params);
            $('.cd-results').setAttribute('aria-busy', 'true');
            $('#cd-message').hidden = true; $('#cd-result-count').textContent = '正在查询…';
            $('#cd-prev').disabled = $('#cd-next').disabled = true;
            $('#cd-table-body').replaceChildren();
            try {
                const data = await fetchData(params, listRequest.signal);
                if (sequence !== listSequence) return;
                fillFacets(data.facets); renderRows(data.records);
                page = data.pagination.page; pages = data.pagination.pages;
                params.set('page', page); saveLocation(params);
                $('#cd-result-count').textContent = `共 ${data.pagination.total.toLocaleString()} ${resource === 'items' ? '件装备资料' : '组天赋资料'}`;
                $('#cd-page-info').textContent = `第 ${page} / ${pages} 页 · 本页 ${data.records.length} 条`;
                $('#cd-prev').disabled = page <= 1; $('#cd-next').disabled = page >= pages;
            } catch (error) {
                if (error.name !== 'AbortError' && sequence === listSequence) {
                    $('#cd-message').textContent = error.message; $('#cd-message').hidden = false;
                    $('#cd-result-count').textContent = '查询未完成'; $('#cd-page-info').textContent = '';
                }
            } finally { if (sequence === listSequence) $('.cd-results').setAttribute('aria-busy', 'false'); }
        }
        function metadata(values) {
            return `<dl class="cd-detail-meta">${values.map(([key, value]) => `<div><dt>${escape(key)}</dt><dd>${escape(value || '未记录')}</dd></div>`).join('')}</dl>`;
        }
        function effects(rows, status) {
            if (status === 'none') return '<p class="cd-secondary">此掉落变体已确认没有特效。</p>';
            if (!rows?.length) return '<p class="cd-secondary">尚无可用的特效描述。</p>';
            const notes = status === 'reference' ? '<p class="cd-secondary">以下为公共机制参考；具体数值以对应装等变体为准。</p>' : '';
            return notes + rows.map(row => `<div class="cd-effect">${escape(typeof row === 'string' ? row : row.description_zh || row.name_zh || row.description || row.name || '描述待补齐')}</div>`).join('');
        }
        async function loadDetail() {
            const sequence = ++detailSequence;
            detailRequest?.abort(); detailRequest = new AbortController();
            const params = parameters(); params.set('id', detailRow.id); params.set('page', detailPage); params.set('page_size', '10');
            if (detailRow.identity_kind) params.set('identity_kind', detailRow.identity_kind);
            $('#cd-detail-body').innerHTML = '<p class="cd-empty">正在读取详情…</p>';
            $('#cd-detail-prev').disabled = $('#cd-detail-next').disabled = true;
            try {
                const data = await fetchData(params, detailRequest.signal);
                if (sequence !== detailSequence || !dialog.open) return;
                const row = data.record;
                let html = metadata([['数据分支',data.branch === 'ptr' ? '测试服 PTR' : '正式服'],['采集来源',source(row.source)],['资料更新时间',date(row.updated_at)],['图标名称',row.icon],['图标来源',row.icon_source]]);
                if (resource === 'items') {
                    html += `<section class="cd-detail-section"><h3>装备说明</h3><p class="cd-copy">${escape(row.description || '无额外说明')}</p></section><section class="cd-detail-section"><h3>公共特效</h3>${effects(row.effects, row.effects_status)}</section><section class="cd-detail-section"><h3>装等与掉落变体 · ${data.pagination.total} 条</h3>`;
                    html += data.entries.map(entry => `<article class="cd-entry"><h4>装等 ${escape(entry.item_level)} · ${escape(entry.kind)} · ${escape(entry.quality)}${entry.track ? ' · ' + escape(entry.track) + (entry.rank ? ' ' + escape(entry.rank) : '') : ''}</h4><span class="cd-secondary">${escape(entry.season)} · 来源构建 ${escape(entry.source_build || '未记录')}</span><div class="cd-stats">${Object.entries(entry.stats || {}).map(([key, value]) => `<span>${escape(statNames[key] || key)} ${escape(typeof value === 'object' ? JSON.stringify(value) : value)}</span>`).join('') || '<span>属性待补齐</span>'}</div>${effects(entry.effects, entry.effects_status)}<details><summary>查看掉落来源</summary><pre>${escape(JSON.stringify(entry.sources, null, 2))}</pre></details></article>`).join('') + '</section>';
                } else {
                    html += `<section class="cd-detail-section"><h3>最终技能描述</h3><p class="cd-copy">${escape(row.central?.description_zh || row.central?.description || row.description || '描述待补齐')}</p><span class="cd-secondary">来源构建 ${escape(row.source_build || '未记录')} · 同一分支共用</span></section><section class="cd-detail-section"><h3>当前筛选下的关联节点 · ${data.pagination.total} 条</h3>`;
                    html += data.entries.map(entry => `<article class="cd-entry"><h4>${escape(entry.class_name)} / ${escape(entry.spec_name)} · ${escape(entry.tree)}</h4><span class="cd-secondary">节点 #${escape(entry.node_id)} · 天赋 #${escape(entry.talent_id || '—')} · 技能 #${escape(entry.spell_id || '—')}</span><p class="cd-copy">${escape(entry.description || '描述待补齐')}</p><span class="cd-secondary">来源：${escape(source(entry.source) || '未记录')}</span></article>`).join('') + '</section>';
                }
                $('#cd-detail-body').innerHTML = html;
                detailPage = data.pagination.page; detailPages = data.pagination.pages;
                $('#cd-detail-page-info').textContent = `第 ${detailPage} / ${detailPages} 页`;
                $('#cd-detail-prev').disabled = detailPage <= 1; $('#cd-detail-next').disabled = detailPage >= detailPages;
            } catch (error) {
                if (error.name !== 'AbortError' && sequence === detailSequence) $('#cd-detail-body').innerHTML = `<p class="cd-empty">${escape(error.message)}</p>`;
            }
        }
        root.addEventListener('error', event => { if (event.target.matches?.('img.cd-icon')) event.target.hidden = true; }, true);
        form.addEventListener('submit', event => { event.preventDefault(); page = 1; load(); });
        form.addEventListener('change', event => {
            if (event.target.name === 'q') return;
            if (event.target.name === 'class_name') form.elements.spec_name.value = '';
            page = 1; load();
        });
        $('#cd-reset').addEventListener('click', () => { const branch = form.elements.branch.value; form.reset(); form.elements.branch.value = branch; fieldsForResource(); page = 1; load(); });
        root.querySelectorAll('[data-cd-resource]').forEach(button => button.addEventListener('click', () => {
            if (resource === button.dataset.cdResource) return;
            resource = button.dataset.cdResource; form.elements.q.value = ''; fieldsForResource(); page = 1; load();
        }));
        $('#cd-page-size').addEventListener('change', () => { page = 1; load(); });
        $('#cd-prev').addEventListener('click', () => { if (page > 1) { page--; load(); } });
        $('#cd-next').addEventListener('click', () => { if (page < pages) { page++; load(); } });
        $('#cd-detail-prev').addEventListener('click', () => { if (detailPage > 1) { detailPage--; loadDetail(); } });
        $('#cd-detail-next').addEventListener('click', () => { if (detailPage < detailPages) { detailPage++; loadDetail(); } });
        $('#cd-detail-close').addEventListener('click', () => dialog.close());
        dialog.addEventListener('close', () => { detailRequest?.abort(); ++detailSequence; $('#cd-detail-page-info').textContent = ''; });
        function activate() { if (!loaded && section.style.display !== 'none') load(); }
        restoreLocation(); activate();
        new MutationObserver(activate).observe(section, {attributes:true, attributeFilter:['style','class']});
        window.addEventListener('popstate', () => { if (section.style.display !== 'none') { restoreLocation(); load(); } });
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
