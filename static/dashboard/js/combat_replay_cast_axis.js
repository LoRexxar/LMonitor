window.createReplayCastAxis = function ({data, players, selected, badge, precise, jump, query, redraw}) {
    const $ = id => document.getElementById(id);
    const scroll = $('cast-scroll'), sheet = $('cast-sheet'), head = $('cast-head');
    const body = $('cast-body'), ruler = $('cast-ruler'), tracks = $('activity-casts');
    const cursor = $('cast-cursor'), followButton = $('cast-follow'), targets = $('cast-targets');
    const periods = new Map(), requests = new Map(), failures = new Set(), missingIcons = new Set();
    const lanes = new Map(), markers = new Map();
    const shortNames = players.map(p => p.name.split('-')[0]);
    const phases = {SPELL_CAST_START:'读条', SPELL_CAST_SUCCESS:'释放', SPELL_CAST_FAILED:'失败'};
    const pxPerMs = 0.048, padding = 16, bucketMs = 2000;
    const y = t => padding + t * pxPerMs;
    let time = 0, following = true, expectedTop = 0, frame = 0;
    let layoutKey = '', windowKey = '', revision = 0, selectionRevision = -1;
    let roster = [];
    let mode = query.get('castView') === 'text' ? 'text' : 'icons';
    targets.checked = query.get('castTargets') === '1';
    body.style.height = (y(data.duration) + padding) + 'px';

    function queuePaint() {
        if (!frame) frame = requestAnimationFrame(() => { frame = 0; paint(); });
    }
    function follow(enable = true) {
        following = enable;
        expectedTop = scroll.scrollTop;
        followButton.setAttribute('aria-pressed', String(enable));
        followButton.textContent = enable ? '跟随回放' : '回到当前时间';
    }
    followButton.onclick = () => { follow(!following); paint(); };
    scroll.addEventListener('scroll', () => {
        queuePaint();
    });
    new ResizeObserver(queuePaint).observe(scroll);
    body.addEventListener('click', e => {
        if (e.target.closest('button')) return;
        const t = Math.round((e.clientY - body.getBoundingClientRect().top - padding) / pxPerMs);
        jump(Math.min(data.duration, Math.max(0,t)));
    });
    function setMode(value) {
        mode = sheet.dataset.mode = value;
        for (const m of ['icons','text']) $('casts-'+m).setAttribute('aria-pressed', String(m === value));
        windowKey = '';
        queuePaint();
    }
    for (const m of ['icons','text']) $('casts-'+m).onclick = () => { setMode(m); redraw(); };
    targets.onchange = () => { windowKey = ''; redraw(); };
    setMode(mode);

    function prime(n, rows) {
        // 地图分片也携带相邻历史施法，只将本段的记录放入时间轴缓存。
        if (periods.has(n)) return;
        periods.set(n, rows.filter(c => Math.floor(c.t / data.chunk) === n));
        if (periods.size > 8) periods.delete(periods.keys().next().value);
        failures.delete(n);
        revision++;
        queuePaint();
    }
    async function load(n) {
        if (periods.has(n) || requests.has(n) || failures.has(n)) return;
        const controller = new AbortController();
        requests.set(n,controller);
        try {
            const params = new URLSearchParams({track:'casts',chunk:n,revision:data.revision});
            const response = await fetch('?' + params, {signal:controller.signal});
            if (!response.ok) throw new Error();
            const result = await response.json();
            if (!controller.signal.aborted) prime(n,result.casts);
        } catch {
            if (!controller.signal.aborted) { failures.add(n); revision++; }
        } finally {
            if (requests.get(n) === controller) requests.delete(n);
            queuePaint();
        }
    }
    $('cast-retry').onclick = () => { failures.clear(); windowKey = ''; paint(); };

    function buildLanes(roster) {
        const key = roster.map(p => p.id).join(',');
        if (key === layoutKey && head.childElementCount) return;
        layoutKey = key;
        windowKey = '';
        head.replaceChildren();
        tracks.replaceChildren();
        markers.clear();
        lanes.clear();
        $('replay-activity').classList.toggle('no-casters', !roster.length);
        if (!roster.length) {
            for (const controller of requests.values()) controller.abort();
            requests.clear();
            $('cast-retry').hidden = true;
            $('casts-empty').textContent = '请勾选要查看施法的单位。';
            return;
        }
        const scale = document.createElement('span');
        scale.textContent = '时间';
        scale.className = 'cast-axis-label';
        head.append(scale);
        sheet.style.setProperty('--players', String(Math.max(1,roster.length)));
        for (const p of roster) {
            const header = document.createElement('div'), name = document.createElement('b'), spec = document.createElement('small');
            const lane = document.createElement('div');
            header.className = 'cast-player';
            header.style.setProperty('--class-color',p.color || 'var(--line)');
            const shortName = p.name.split('-')[0];
            const isBoss = p.family !== 'Player';
            name.textContent = !isBoss && shortNames.indexOf(shortName) === shortNames.lastIndexOf(shortName) ? shortName : p.name;
            spec.textContent = isBoss ? '首领' : p.class_label || '职业未知';
            header.title = p.name + ' · ' + (isBoss ? '首领' : p.spec || '职业未知');
            header.append(badge(p), name, spec);
            head.append(header);
            lane.className = 'cast-lane';
            lane.dataset.actor = p.id;
            lane.setAttribute('role','group');
            lane.setAttribute('aria-label',header.title);
            tracks.append(lane);
            lanes.set(p.id,lane);
        }
    }
    function attachIcon(button, cast) {
        if (button.dataset.tried || missingIcons.has(cast.spell_id) || !/^\d{1,10}$/.test(cast.spell_id)) return;
        button.dataset.tried = '1';
        const img = document.createElement('img');
        img.alt = '';
        img.loading = 'lazy';
        img.referrerPolicy = 'no-referrer';
        const lookup = '?' + new URLSearchParams({cast_icon:cast.spell_id, revision:data.revision});
        img.onload = () => button.classList.add('has-cast-icon');
        img.onerror = () => {
            if (img.getAttribute('src') !== lookup) { img.src = lookup; return; }
            missingIcons.add(cast.spell_id);
            img.remove();
        };
        img.src = data.cast_icons[cast.spell_id] || lookup;
        button.prepend(img);
    }
    function marker(cast) {
        let button = markers.get(cast.line);
        if (!button) {
            button = document.createElement('button');
            button.type = 'button';
            button.className = 'cast-marker';
            button.dataset.line = cast.line;
            button.dataset.phase = cast.kind;
            button.style.top = y(cast.t) + 'px';
            const name = document.createElement('strong');
            name.textContent = cast.spell;
            button.append(name);
            button.onclick = () => jump(cast.t);
            lanes.get(cast.actor).append(button);
            markers.set(cast.line,button);
        }
        const target = targets.checked && cast.target && !['未知','(null)','nil','无目标'].includes(cast.target) ? ' → ' + cast.target : '';
        button.title = precise(cast.t) + ' · ' + phases[cast.kind] + ' ' + cast.spell + target;
        button.setAttribute('aria-label',button.title);
        if (target) button.dataset.target = target;
        else delete button.dataset.target;
        if (mode === 'icons') attachIcon(button,cast);
        return button;
    }
    function drawLane(casts, visible) {
        // 同一玩家连续瞬发或同毫秒的读条与释放，在原时间高度并排放置。
        const ends = [], group = [];
        function finish() {
            const width = 100 / ends.length;
            for (const [button,slot] of group) {
                button.style.left = (slot*width) + '%';
                button.style.width = width + '%';
            }
            ends.length = group.length = 0;
        }
        for (const cast of casts) {
            const pos = y(cast.t);
            if (ends.length && ends.every(end => end <= pos)) finish();
            let slot = ends.findIndex(end => end <= pos);
            if (slot < 0) slot = ends.length;
            ends[slot] = pos + (targets.checked ? 42 : 28);
            const button = marker(cast);
            group.push([button,slot]);
            visible.add(cast.line);
        }
        if (group.length) finish();
    }
    function paint() {
        if (!scroll.clientHeight) return;
        // 滚动事件可能晚于数据刷新，先接纳用户的位置，再决定是否移动时间轴。
        if (following && Math.abs(scroll.scrollTop - expectedTop) > 1) follow(false);
        if (following) {
            const dest = y(time) - (scroll.clientHeight - head.offsetHeight)*0.45;
            scroll.scrollTop = Math.max(0,dest);
            expectedTop = scroll.scrollTop;
        }
        cursor.style.top = y(time) + 'px';
        cursor.firstElementChild.textContent = precise(time);
        const first = Math.max(0,(Math.floor((scroll.scrollTop-padding)/pxPerMs/bucketMs)-1)*bucketMs);
        const last = Math.min(data.duration,(Math.ceil((scroll.scrollTop+scroll.clientHeight)/pxPerMs/bucketMs)+1)*bucketMs);
        const key = [first,last,layoutKey,revision,mode,targets.checked,$('activity-attempts').checked].join('|');
        if (key === windowKey) return;
        windowKey = key;
        const low = Math.floor(first/data.chunk), high = Math.floor(last/data.chunk);
        for (const [n,controller] of requests) {
            if (n < low || n > high || !roster.length) { controller.abort(); requests.delete(n); }
        }
        const grouped = new Map(roster.map(p=>[p.id,[]]));
        let pending = false, failed = false;
        for (let n = low; roster.length && n <= high; n++) {
            if (!periods.has(n)) { pending = true; failed ||= failures.has(n); load(n); continue; }
            for (const c of periods.get(n)) {
                if (c.t >= first && c.t <= last && grouped.has(c.actor) && ($('activity-attempts').checked || c.kind === 'SPELL_CAST_SUCCESS')) grouped.get(c.actor).push(c);
            }
        }
        const visible = new Set();
        for (const casts of grouped.values()) drawLane(casts,visible);
        for (const [line,node] of markers) {
            if (!visible.has(line)) { node.remove(); markers.delete(line); }
        }
        ruler.replaceChildren();
        for (let t = first; t <= last; t += bucketMs) {
            const tick = document.createElement('span');
            tick.textContent = precise(t).split('.')[0];
            tick.style.top = y(t)+'px';
            ruler.append(tick);
        }
        $('casts-empty').textContent = !roster.length ? '请勾选要观察的玩家。' : failed ? '此段施法加载失败。' :
            pending ? '正在读取施法…' : !visible.size ? '这一段没有所选玩家的施法。' : '';
        $('cast-retry').hidden = !failed;
    }
    return {
        prime, follow,
        update(t, version, bosses, mutedBosses) {
            time = t;
            if (selectionRevision !== version) {
                roster = [...bosses.filter(b => !mutedBosses.has(b.guid)), ...players.filter(p => selected.has(p.id))];
                buildLanes(roster);
                selectionRevision = version;
            }
            paint();
        },
        share(params) {
            params.set('castView',mode);
            if (targets.checked) params.set('castTargets','1');
        }
    };
};
