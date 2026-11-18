(() => {
    'use strict';
    const form = document.querySelector('#query');
    const status = document.querySelector('#status');
    const results = document.querySelector('#results');
    const history = document.querySelector('#history');
    const cancel = document.querySelector('#cancel');
    let selected = new URLSearchParams(location.search).get('job') || '';
    let timer;
    let revision = 0;

    function element(tag, text) {
        const node = document.createElement(tag);
        node.textContent = text;
        return node;
    }

    function clock(ms) {
        if (!Number.isFinite(ms)) return '未知';
        const seconds = Math.floor(Math.abs(ms) / 1000);
        return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
    }

    function show(run, index) {
        const card = document.createElement('article');
        const header = element('div', '');
        header.className = 'heading';
        const title = element('h3', run.dungeon + ' +' + run.level);
        const rank = element('span', '#' + (index + 1));
        rank.className = 'rank';
        title.prepend(rank);
        const score = element('span', run.score);
        score.className = 'score';
        score.append(element('small', 'IO 分'));
        header.append(title, score);
        const stamp = new Date(run.completed).toLocaleString('zh-CN', {dateStyle: 'medium', timeStyle: 'short'});
        const metrics = element('p', [stamp, '用时 ' + clock(run.duration), '剩余 ' + clock(run.remaining),
            '钥匙 +' + run.chests, run.deaths == null ? null : run.deaths + ' 次死亡'].filter(Boolean).join(' · '));
        metrics.className = 'metrics';
        const lineup = document.createElement('ul');
        lineup.className = 'lineup';
        const roles = {tank: '坦克', healer: '治疗', dps: '输出'};
        for (const player of run.roster) {
            const item = document.createElement('li');
            item.style.setProperty('--class-color', player.color);
            item.append(element('small', roles[player.role] + ' · ' + player.spec + player.cls),
                element('strong', player.name), element('small', player.region + ' / ' + player.realm));
            if (player.ilvl) item.append(element('small', '装等 ' + Number(player.ilvl).toFixed(1)));
            lineup.append(item);
        }
        const detail = document.createElement('details');
        detail.append(element('summary', '场次详情'));
        if (run.affixes.length) detail.append(element('p', '词缀　' + run.affixes.join(' · ')));
        const guilds = run.roster.filter(p => p.guild).map(p => p.name + '（' + p.guild + '）');
        if (guilds.length) detail.append(element('p', '公会　' + guilds.join('、')));
        if (run.encounters.length) {
            const bosses = document.createElement('ol');
            for (const boss of run.encounters) bosses.append(element('li',
                boss.name + ' · ' + (boss.success ? '击败' : '尝试') + ' · 战斗 ' + clock(boss.duration) + ' · 结束于 ' + clock(boss.end)));
            detail.append(bosses);
        } else detail.append(element('p', '暂无首领分段记录。'));
        for (const player of run.roster) {
            if (!player.talent && !player.equipment.length) continue;
            const build = document.createElement('details');
            build.className = 'build';
            build.append(element('summary', player.name + ' · 装备与天赋'));
            const gear = element('ul', '');
            for (const item of player.equipment) {
                gear.append(element('li', item.slot + ' · ' + item.name + (item.level ? ' (' + item.level + ')' : '')));
            }
            if (gear.childElementCount) build.append(gear);
            if (player.talent) {
                const label = element('label', '天赋导入码');
                const code = element('input', '');
                code.readOnly = true;
                code.value = player.talent;
                code.onfocus = () => code.select();
                label.append(code);
                build.append(label);
            }
            detail.append(build);
        }
        const links = document.createElement('div');
        links.className = 'links';
        for (const [label, url] of [['Raider.IO 场次 ↗', run.io], ['WCL 报告 ↗', run.wcl]]) {
            if (!url) continue;
            const link = element('a', label);
            link.href = url;
            link.target = '_blank';
            link.rel = 'noopener';
            links.append(link);
        }
        if (!run.wcl) links.append(element('small', run.wcl_status));
        if (run.mdt) {
            const route = element('button', '查看 MDT 字符串');
            route.onclick = () => {
                const code = element('textarea', '');
                code.readOnly = true;
                code.value = run.mdt;
                code.setAttribute('aria-label', 'MDT 字符串');
                card.append(code);
                route.textContent = '复制 MDT 字符串';
                route.onclick = async () => {
                    try {
                        await navigator.clipboard.writeText(code.value);
                        route.textContent = '已复制';
                    } catch {
                        code.select();
                        route.textContent = '已选中，请手动复制';
                    }
                };
            };
            links.append(route);
        } else links.append(element('small', run.route_status || '暂无公开路线'));
        card.append(header, metrics, lineup, detail, links);
        results.append(card);
    }

    async function post(data) {
        const response = await fetch(location.pathname, {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': form.elements.csrfmiddlewaretoken.value},
            body: JSON.stringify(data),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || '查询失败。');
        return payload;
    }

    const states = {queued: '排队中', running: '抓取中', stopping: '正在停止',
        stopped: '已停止', done: '已完成', failed: '失败'};
    const submit = form.querySelector('[type=submit]');

    function selectJob(id) {
        selected = String(id);
        const url = new URL(location.href);
        url.searchParams.set('job', selected);
        window.history.replaceState(null, '', url);
    }

    function display(job) {
        results.replaceChildren();
        if (!job) {
            status.textContent = '暂无抓取任务。提交后可离开页面，稍后查看结果。';
            return;
        }
        if (!Array.from(history.options).some(option => option.value === String(job.id))) {
            history.add(new Option('#' + job.id + ' · ' + new Date(job.created).toLocaleString('zh-CN'), job.id), 1);
        }
        history.value = job.id;
        const busy = ['queued', 'running', 'stopping'].includes(job.state);
        if (busy && !selected) selectJob(job.id);
        cancel.hidden = !busy || job.state === 'stopping';
        cancel.dataset.job = job.id;
        submit.disabled = busy;
        const choices = Array.from(form.querySelectorAll('select[name=slot]'), (slot, i) =>
            Array.from(slot.options).find(option => option.value === String(job.slots[i]))?.text || '不限专精');
        status.textContent = '#' + job.id + ' ' + states[job.state] + ' · 已核对 ' + job.checked +
            ' 场，匹配 ' + job.found + ' / 5 场。' + (job.message || '') + '　' + choices.join(' / ') +
            (job.complete ? ' · 仅资料齐全' : '');
        (job.results || []).forEach(show);
        if (busy) timer = setTimeout(refresh, 2500);
    }

    async function refresh() {
        clearTimeout(timer);
        const ticket = ++revision;
        try {
            const params = new URLSearchParams({view: 'status'});
            if (selected) params.set('job', selected);
            const response = await fetch(location.pathname + '?' + params, {cache: 'no-store'});
            const data = await response.json();
            if (ticket !== revision) return;
            if (!response.ok) throw new Error(data.error || '无法读取任务状态。');
            display(data.job);
        } catch (error) {
            if (ticket !== revision) return;
            status.textContent = error.message;
            timer = setTimeout(refresh, 5000);
        }
    }

    form.onsubmit = async event => {
        event.preventDefault();
        clearTimeout(timer);
        ++revision;
        submit.disabled = true;
        history.disabled = true;
        try {
            const response = await post({action: 'submit', complete: form.elements.complete.checked,
                slots: Array.from(form.querySelectorAll('select'), s => Number(s.value))});
            selectJob(response.job.id);
            display(response.job);
            if (!response.created) status.textContent = '当前账号已有抓取任务，已打开该任务。' + status.textContent;
        } catch (error) {
            status.textContent = error.message;
            submit.disabled = false;
        } finally { history.disabled = false; }
    };
    history.onchange = () => {
        selected = history.value;
        const url = new URL(location.href);
        if (selected) url.searchParams.set('job', selected);
        else url.searchParams.delete('job');
        window.history.replaceState(null, '', url);
        refresh();
    };
    cancel.onclick = async () => {
        cancel.disabled = true;
        try {
            await post({action: 'stop', id: Number(cancel.dataset.job)});
            await refresh();
        } catch (error) {
            status.textContent = error.message;
        } finally { cancel.disabled = false; }
    };
    refresh();
})();
