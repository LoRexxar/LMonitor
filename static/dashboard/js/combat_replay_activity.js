window.createReplayActivity = function ({data, actors, badge, precise, redraw, jump}) {
    const byId = id => document.getElementById(id);
    const players = [...actors.values()].filter(actor => actor.family === 'Player');
    const query = new URLSearchParams(location.search);
    const shared = new Set(query.getAll('casters').flatMap(value => value.split(',')));
    const selected = new Set(players.filter(p => query.has('casters') ? shared.has(p.guid) : p.role === 'tank').map(p => p.id));
    const mutedBosses = new Set((query.get('bossOff') || '').split(',').filter(Boolean));
    const bossWindows = data.boss_windows || [];
    let currentWindow = null, bosses = [];
    let damagePlayers = query.has('casters') ? selected : new Set(players.map(p => p.id));
    document.querySelector('.activity-picker').open = !query.has('casters') || !selected.size;
    const attempts = byId('activity-attempts');
    attempts.checked = query.get('attempts') === '1';
    const number = new Intl.NumberFormat('zh-CN', {maximumFractionDigits:0});
    const zero = {amount:0, unknown:0};
    let version = 0, lastPaint = -Infinity, lastChunk;
    const inputs = new Map(), damageRows = new Map();

    // 每条前缀包含该毫秒的全部伤害。同毫秒的施法仍按原文行号排序。
    function append(series, id, t, amount, unknown) {
        if (!series.has(id)) series.set(id, []);
        const rows = series.get(id), previous = rows.at(-1) || zero;
        rows.push({t, amount:previous.amount + amount, unknown:previous.unknown + unknown});
    }
    function upper(rows, t) {
        let lo = 0, hi = rows.length;
        while (lo < hi) {
            const mid = Math.floor((lo + hi) / 2);
            if (rows[mid].t <= t) lo = mid + 1;
            else hi = mid;
        }
        return lo;
    }
    function prefix(rows, t) {
        return rows?.[upper(rows || [], t) - 1] || zero;
    }
    function damageSeries(rows) {
        const totals = new Map(), scopes = new Map([[0,totals]]);
        for (const [t,id,amount,unknown,pull] of rows) {
            append(totals,id,t,amount,unknown);
            if (pull) {
                if (!scopes.has(pull)) scopes.set(pull,new Map());
                append(scopes.get(pull),id,t,amount,unknown);
            }
        }
        return scopes;
    }
    const buckets = damageSeries(data.damage_buckets);
    const wavePicker = byId('replay-wave');
    const waveJump = byId('replay-wave-jump');
    const pulls = new Map(data.pulls.map(p => [String(p.id),p]));
    let picked = pulls.get(query.get('wave')) || null;
    let rosterPull = null, waveActors = null;
    if (wavePicker) {
        wavePicker.value = picked ? String(picked.id) : 'all';
        waveJump.disabled = !picked;
        waveJump.onclick = () => jump(picked.t);
        wavePicker.addEventListener('change', () => {
            picked = pulls.get(wavePicker.value) || null;
            waveJump.disabled = !picked;
            if (!picked) {
                usePull = false;
                updateScopeButtons();
            }
            lastPaint = -Infinity;
            redraw();
        });
    }
    function currentPull(t) {
        const candidate = data.pulls[upper(data.pulls,t)-1];
        return candidate && t <= candidate.end ? candidate : null;
    }

    function updateCount() {
        const eligible = players.filter(p => !waveActors || waveActors.has(p.id));
        const count = eligible.filter(p => selected.has(p.id)).length;
        byId('activity-count').textContent = (rosterPull?.label || '全场') + ' · ' + count + ' / ' + eligible.length;
    }
    function changed() {
        version++;
        lastPaint = -Infinity;
        updateCount();
        redraw();
    }
    function setSelection(p, on) {
        if (p.family === 'Player') {
            damagePlayers = selected;
            if (on) selected.add(p.id);
            else selected.delete(p.id);
        } else if (on) mutedBosses.delete(p.guid);
        else mutedBosses.add(p.guid);
    }
    function makeChoice(p) {
        const label = document.createElement('label'), input = document.createElement('input');
        input.type = 'checkbox';
        input.value = p.id;
        const isPlayer = p.family === 'Player';
        input.checked = isPlayer ? selected.has(p.id) : !mutedBosses.has(p.guid);
        input.onchange = () => {
            setSelection(p, input.checked);
            changed();
        };
        const role = isPlayer ? (p.role === 'tank' ? '坦克' : '') : '首领';
        label.append(input, badge(p), document.createTextNode(role ? p.name + ' · ' + role : p.name));
        inputs.set(p.id,input);
        return label;
    }
    for (const p of players) byId('activity-players').append(makeChoice(p));
    function syncBosses(t) {
        const candidate = bossWindows[upper(bossWindows,t)-1];
        const next = candidate && t <= candidate.end ? candidate : null;
        if (next === currentWindow) return false;
        currentWindow = next;
        for (const b of bosses) {
            inputs.get(b.id).closest('label').remove();
            inputs.delete(b.id);
        }
        bosses = (next?.actors || []).map(id => actors.get(id)).filter(Boolean);
        byId('activity-bosses').append(...bosses.map(makeChoice));
        return true;
    }
    const search = byId('activity-search');
    function filterPlayers() {
        const term = search.value.trim().toLowerCase();
        let matches = 0;
        for (const [id,input] of inputs) {
            const p = actors.get(id);
            const role = p.family !== 'Player' ? '首领 boss' : p.role === 'tank' ? '坦克 T' : '';
            const inScope = p.family !== 'Player' || !waveActors || waveActors.has(id);
            const visible = inScope && [p.name,p.spec,role].join(' ').toLowerCase().includes(term);
            input.closest('label').hidden = !visible;
            if (visible) matches++;
        }
        byId('activity-all').textContent = term ? '勾选匹配项' : '全选';
        byId('activity-none').textContent = term ? '取消匹配项' : '清空';
        byId('activity-all').disabled = byId('activity-none').disabled = !matches;
        byId('no-players').hidden = matches > 0;
    }
    search.addEventListener('input', filterPlayers);
    filterPlayers();
    function selectAll(on) {
        for (const [id,input] of inputs) {
            if (input.closest('label').hidden) continue;
            input.checked = on;
            setSelection(actors.get(id),on);
        }
        changed();
    }
    byId('activity-all').onclick = () => selectAll(true);
    byId('activity-none').onclick = () => selectAll(false);
    attempts.onchange = redraw;
    updateCount();

    function prepare(n, payload) {
        axis.prime(n, payload.casts);
        return {n, local:damageSeries(payload.damage)};
    }
    const axis = window.createReplayCastAxis({data, players, selected, badge, precise, jump, query, redraw});
    function formatAmount(value, unknown) {
        return (unknown ? '≥ ' : '') + number.format(value);
    }
    function formatDps(damage, seconds, unknown) {
        return seconds > 0 ? formatAmount(damage / seconds, unknown) : '—';
    }
    function write(node, text) {
        if (node.textContent !== text) node.textContent = text;
    }
    const metric = byId('meter-metric'), toggle = byId('meter-toggle');
    if (query.get('damage') === 'amount') metric.value = 'amount';
    const fightButton = byId('meter-fight'), pullButton = byId('meter-pull');
    let usePull = !pullButton.disabled && query.get('damageScope') === 'pull';
    function updateScopeButtons() {
        fightButton.setAttribute('aria-pressed',String(!usePull));
        pullButton.setAttribute('aria-pressed',String(usePull));
    }
    updateScopeButtons();
    for (const button of [fightButton,pullButton]) button.onclick = () => {
        usePull = button === pullButton;
        updateScopeButtons();
        lastPaint = -Infinity;
        redraw();
    };
    metric.onchange = () => { lastPaint = -Infinity; redraw(); };
    toggle.onclick = () => {
        const open = byId('meter-body').hidden;
        byId('meter-body').hidden = metric.hidden = !open;
        toggle.setAttribute('aria-expanded',String(open));
        toggle.setAttribute('aria-label',open ? '收起伤害榜' : '展开伤害榜');
        if (open) { lastPaint = -Infinity; redraw(); }
    };
    const hints = {
        amount:'所选范围内截至当前的有效伤害，含已确认归属的宠物伤害。',
        average:'所选范围内的有效伤害除以该范围开始至当前的秒数，含停手时间。'
    };
    function meterRow(id) {
        const actor = actors.get(id), row = document.createElement('li');
        const part = cls => {
            const node = document.createElement('span');
            node.className = cls;
            return node;
        };
        const fill = part('meter-fill'), rank = part('meter-rank'), name = part('meter-player'), value = part('meter-number');
        fill.setAttribute('aria-hidden','true');
        row.dataset.actor = id;
        row.className = 'meter-row';
        row.style.setProperty('--meter-color',actor.color || 'var(--plot)');
        name.textContent = name.title = actor.name;
        row.append(fill,rank,badge(actor),name,value);
        return {row,fill,rank,value};
    }
    function renderDamage(t, chunk) {
        if (byId('meter-body').hidden) return;
        const body = byId('activity-damage'), mode = metric.value;
        const pull = usePull ? currentPull(t) : null;
        const seconds = (t - (pull?.t ?? 0)) / 1000;
        const display = (amount,missing) => mode === 'amount' ? formatAmount(amount,missing) : formatDps(amount,seconds,missing);
        metric.title = hints[mode];
        const ready = chunk && damagePlayers.size > 0 && (!usePull || pull);
        const scopeLabel = damagePlayers === selected ? '所选合计' : '全队合计';
        write(byId('meter-total-label'),pull ? pull.label+' · '+scopeLabel : scopeLabel);
        body.hidden = !ready;
        byId('meter-empty').hidden = !!ready;
        if (!ready) {
            write(byId('meter-empty'),!damagePlayers.size ? '请在施法面板勾选玩家。' :
                usePull && !pull ? '当前时刻没有交战波次。' : '等待读取此时段。');
            write(byId('meter-value'),'—');
            byId('damage-note').hidden = true;
            return;
        }
        let total = 0, missingTotal = 0;
        const stats = [];
        const scope = pull?.id ?? 0;
        const history = buckets.get(scope), local = chunk.local.get(scope);
        for (const id of damagePlayers) {
            const before = prefix(history?.get(id),chunk.n-1), now = prefix(local?.get(id),t);
            const amount = before.amount+now.amount, missing = before.unknown+now.unknown;
            stats.push({id,amount,missing});
            total += amount;
            missingTotal += missing;
        }
        stats.sort((a,b) => b.amount-a.amount || a.id-b.id);
        const leading = stats[0].amount;
        for (const [id,entry] of damageRows) {
            if (!damagePlayers.has(id)) {entry.row.remove(); damageRows.delete(id);}
        }
        stats.forEach(({id,amount,missing},i) => {
            if (!damageRows.has(id)) damageRows.set(id,meterRow(id));
            const {row,fill,rank,value} = damageRows.get(id);
            write(rank,String(i+1));
            write(value,display(amount,missing));
            fill.style.transform = 'scaleX(' + (leading && (mode === 'amount' || seconds) ? amount/leading : 0) + ')';
            if (body.children[i] !== row) body.insertBefore(row,body.children[i] || null);
        });
        write(byId('meter-value'),display(total,missingTotal));
        byId('damage-note').hidden = !missingTotal;
    }
    return {
        prepare,
        follow:axis.follow,
        currentPull,
        update(time, chunk, playing) {
            const now = performance.now();
            const t = Math.floor(time);
            const pull = picked || (usePull ? currentPull(t) : null);
            const bossesChanged = syncBosses(t);
            if (pull !== rosterPull || bossesChanged) {
                rosterPull = pull;
                waveActors = pull ? new Set(pull.actors) : null;
                version++;
                filterPlayers();
                updateCount();
            }
            axis.update(t,version,bosses,mutedBosses,waveActors);
            if (playing && chunk === lastChunk && now-lastPaint < 80) return;
            lastChunk = chunk;
            lastPaint = now;
            renderDamage(t,chunk);
        },
        share(params) {
            if (picked) params.set('wave',picked.id);
            if (damagePlayers === selected) params.set('casters', players.filter(p => selected.has(p.id)).map(p => p.guid).join(','));
            if (mutedBosses.size) params.set('bossOff', [...mutedBosses].join(','));
            if (attempts.checked) params.set('attempts','1');
            axis.share(params);
            if (metric.value !== 'average') params.set('damage',metric.value);
            if (usePull) params.set('damageScope','pull');
        }
    };
};
