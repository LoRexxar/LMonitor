(() => {
    const publishedImageUrl = window.publishedImageUrl || (() => '');
    const setIcon = window.setPublishedWowImage || ((img, src) => { const url = window.publishedImageUrl?.(src); if (url) img.src = url; });
    const $ = id => document.getElementById(id);
    const eventLabels = {
        DAMAGE:['damage','伤害'], HEAL:['heal','治疗'], UNIT_DIED:['death','死亡'],
        SPELL_CAST_START:['cast','读条'], SPELL_INTERRUPT:['interrupt','打断'], SPELL_DISPEL:['dispel','驱散'],
        BUFF:['buff','增益'], DEBUFF:['debuff','减益'], 完成:['cast','完成'], 失败:['cast','失败'],
        打断:['interrupt','打断'], 未知:['neutral','未知']
    };
    const damageKinds = new Set(['SPELL_DAMAGE','SPELL_PERIODIC_DAMAGE','RANGE_DAMAGE','SWING_DAMAGE',
        'ENVIRONMENTAL_DAMAGE','DAMAGE_SHIELD','DAMAGE_SPLIT']);
    const healKinds = new Set(['SPELL_HEAL','SPELL_PERIODIC_HEAL']);
    function eventType(kind, aura) {
        if (aura) return aura;
        if (damageKinds.has(kind)) return 'DAMAGE';
        if (healKinds.has(kind)) return 'HEAL';
        return kind;
    }
    function eventBadge(badge, type) {
        const [tone,label] = Object.hasOwn(eventLabels,type) ? eventLabels[type] : ['neutral',badge.textContent || '事件'];
        badge.dataset.tone = tone;
        badge.textContent = label;
    }
    const reviewData = $('review-timeline-data');
    if (reviewData) {
        const packet = JSON.parse(reviewData.textContent);
        const totals = {DAMAGE:0, HEAL:0}, unknown = new Set();
        const rows = document.querySelectorAll('.review-timeline tbody tr');
        for (const [i,event] of packet.items.entries()) {
            const type = eventType(event.outcome || event.kind);
            eventBadge(rows[i].querySelector('.combat-event-badge'),type);
            if (Object.hasOwn(totals,type)) {
                if (event.amount == null) unknown.add(type);
                else totals[type] += event.amount;
            }
        }
        if (packet.counts) $('review-event-total').textContent = Object.values(packet.counts).reduce((a,b) => a+b,0);
        else for (const type of Object.keys(totals)) {
            $(type === 'DAMAGE' ? 'review-damage-total' : 'review-heal-total').textContent =
                (unknown.has(type) ? '≥ ' : '') + totals[type].toLocaleString('zh-CN');
        }
    }
    if (!$('replay-data')) return;
    const data = JSON.parse($('replay-data').textContent);
    const canvas = $('arena'), ctx = canvas.getContext('2d');
    const clock = $('clock'), actorSelect = $('actor'), floorSelect = $('floor');
    const compareSelect = $('compare');
    const zoomSelect = $('zoom'), customZoomOption = $('zoom-custom');
    const tabs = Array.from(document.querySelectorAll('.replay-tabs [role=tab]'));
    const replayQuery = new URLSearchParams(location.search);
    function selectTab(active) {
        for (const tab of tabs) {
            const selected = tab === active;
            tab.setAttribute('aria-selected', String(selected));
            tab.tabIndex = selected ? 0 : -1;
            const panel = $(tab.getAttribute('aria-controls'));
            panel.setAttribute('role', 'tabpanel');
            panel.tabIndex = 0;
            panel.hidden = !selected;
        }
    }
    for (const [index, tab] of tabs.entries()) {
        tab.onclick = () => { selectTab(tab); draw(); updateMoment(); };
        tab.onkeydown = event => {
            const destinations = {ArrowLeft:(index+tabs.length-1)%tabs.length, ArrowRight:(index+1)%tabs.length, Home:0, End:tabs.length-1};
            if (!Object.hasOwn(destinations, event.key)) return;
            event.preventDefault();
            const next = tabs[destinations[event.key]];
            next.focus();
            next.click();
        };
    }
    selectTab(tabs.find(tab => tab.id === 'tab-' + replayQuery.get('tab')) || tabs[0]);
    tabs[0].parentElement.hidden = false;
    document.querySelector('a[href="#replay-activity"]').addEventListener('click', () => {
        setAside(true);
        tabs[0].click();
        tabs[0].focus({preventScroll:true});
    });
    const actors = new Map(data.actors.map(a => [a.id,a]));
    const maps = new Map(data.maps.map(m => [m.id,m]));
    const cache = new Map(), requests = new Map();
    const deaths = new Map();
    const format = ms => Math.floor(ms / 60000) + ':' + String(Math.floor(ms / 1000) % 60).padStart(2,'0');
    const precise = ms => format(ms) + '.' + String(Math.floor(ms) % 1000).padStart(3,'0');
    const clamp = t => Math.max(0, Math.min(data.duration,t));
    let time = 0, playing = false, frame = 0, previous = 0;
    let current = null, currentChunk = -1, wanted = -1, pan = null;
    let prefetch = -1, loop = null;
    let scrubPointer = null, scrubTarget = null, scrubFrame = 0, lastScrub = 0;
    let hits = [], camera = null, eventKey = '', drag = null;
    let customZoom = null;
    const layers = {};
    const layerControls = [...document.querySelectorAll('[data-layer]')];
    const preferenceKey = 'combat-replay.display.v1';
    const layout = canvas.closest('main');
    let savedLayers;
    try { savedLayers = JSON.parse(localStorage.getItem(preferenceKey)); } catch {}
    for (const control of layerControls) {
        const key = control.dataset.layer;
        control.checked = savedLayers?.[key] === true;
        layers[key] = control.checked;
        layout.classList.toggle('show-'+key,control.checked);
    }
    const specImages = new Map();
    function specImage(actor) {
        if (!actor.spec_icon) return null;
        if (!specImages.has(actor.spec_icon)) {
            const image = new Image();
            specImages.set(actor.spec_icon,image);
            image.onload = () => draw();
            setIcon(image, actor.spec_icon);
        }
        const image = specImages.get(actor.spec_icon);
        return image.complete && image.naturalWidth ? image : null;
    }
    function badge(actor) {
        const icon = document.createElement('span');
        icon.className = 'spec-badge';
        icon.setAttribute('aria-hidden','true');
        icon.textContent = '#'+actor.id;
        if (actor.spec_icon) {
            const img = document.createElement('img');
            img.alt = '';
            img.onerror = () => img.remove();
            setIcon(img, actor.spec_icon);
            icon.append(img);
        }
        return icon;
    }
    let chunkGeneration = 0;
    for (const death of data.deaths) {
        if (!deaths.has(death.actor)) deaths.set(death.actor,[]);
        deaths.get(death.actor).push(death.t);
        $('death').add(new Option(format(death.t) + ' · ' + death.name, death.line));
    }
    function jumpDeath(death) {
        stop(); setLoop(null); pan=null; floorSelect.value='';
        actorSelect.value=actors.has(death.actor) ? String(death.actor) : '';
        seek(death.t);
    }
    // 相邻死亡合并为一个位置标记，列表仍能精确选择组内每一条。
    let lastMark = -Infinity;
    for (const d of data.deaths) {
        const percent = 100*d.t/Math.max(1,data.duration);
        if (percent-lastMark < 1.5) continue;
        lastMark = percent;
        const mark = document.createElement('button');
        mark.type = 'button';
        mark.title = format(d.t)+' · '+d.name+' 死亡';
        mark.setAttribute('aria-label',mark.title);
        mark.style.left = percent+'%';
        mark.onclick = () => jumpDeath(d);
        $('death-strip').append(mark);
    }
    function load(n) {
        if (cache.has(n)) return Promise.resolve(cache.get(n));
        if (requests.has(n)) return requests.get(n).promise;
        const controller = new AbortController();
        const url = new URL(location.href);
        url.search = new URLSearchParams({chunk:n, revision:data.revision});
        if (layers.buffs || layers.debuffs) url.searchParams.set('party_auras','1');
        url.hash = '';
        const promise = fetch(url,{signal:controller.signal}).then(async response => {
            if (response.redirected) throw Error('登录状态已失效，请重新登录后刷新回放。');
            if (!response.ok) throw Error(response.status === 409 ? '索引已更新，请刷新页面。' : '读取回放失败，可重试此时段。');
            const payload = await response.json();
            const tracks = new Map();
            for (const [id,t,map,x,y,hp,max,facing] of payload.points) {
                if (!tracks.has(id)) tracks.set(id,[]);
                tracks.get(id).push({t,map,x,y,hp,max,facing});
            }
            const partyAuras = payload.party_auras ? new Map() : null;
            for (const [id,t,buffs,debuffs] of payload.party_auras || []) {
                if (!partyAuras.has(id)) partyAuras.set(id,[]);
                partyAuras.get(id).push({t,buffs,debuffs});
            }
            const value = {activity:activity.prepare(n,payload.activity),tracks,events:payload.events,vitals:payload.vitals || [],partyAuras};
            if (controller.signal.aborted) return value;
            cache.set(n,value);
            while (cache.size > 4) cache.delete(cache.keys().next().value);
            return value;
        }).finally(() => {
            if (requests.get(n)?.controller === controller) requests.delete(n);
        });
        requests.set(n,{promise,controller});
        return promise;
    }
    function stop() {
        playing = false;
        cancelAnimationFrame(frame);
        $('play').textContent = time >= data.duration ? '重播' : '播放';
    }
    async function switchChunk(n) {
        const generation = ++chunkGeneration;
        wanted = n;
        current = null;
        for (const [key,entry] of requests) {
            if (key !== n) {entry.controller.abort();requests.delete(key);}
        }
        $('load-state').textContent = '正在读取此时段…';
        $('retry').hidden = true;
        draw();
        try {
            const value = await load(n);
            if (generation !== chunkGeneration) return;
            current = value;
            currentChunk = n;
            $('load-state').textContent = '';
            previous = performance.now();
            draw();
            if (playing) schedule();
        } catch (error) {
            if (generation !== chunkGeneration || error.name === "AbortError") return;
            stop();
            $('load-state').textContent = error.message || '读取失败。';
            $('retry').hidden = false;
        }
    }
    function setLoop(center, start=clamp(center-5000), end=clamp(center+5000)) {
        loop = center === null || !data.duration ? null : {
            center, start, end
        };
        $('loop-toggle').setAttribute('aria-pressed',String(!!loop));
        $('loop-editor').hidden = !loop;
        const band = $('loop-band');
        band.hidden = !loop;
        if (loop) {
            $('loop-start').value=start;$('loop-end').value=end;
            $('loop-start').setAttribute('aria-valuetext',precise(start));
            $('loop-end').setAttribute('aria-valuetext',precise(end));
            band.style.left = 100*loop.start/data.duration+'%';
            band.style.width = 100*(loop.end-loop.start)/data.duration+'%';
        }
        $('loop-window').textContent = loop ? '循环区间　'+precise(loop.start)+' 至 '+precise(loop.end) : '';
    }
    function startLoop(center) {
        setLoop(Math.round(center));
        if (!loop) return;
        seek(loop.start);
        if (!playing) toggle();
    }
    function seek(value, follow = true) {
        if (follow) activity.follow();
        if (loop && (value < loop.start || value > loop.end)) setLoop(null);
        time = clamp(value);
        const n = Math.floor(time / data.chunk);
        if (n !== wanted) switchChunk(n);
        else draw();
    }
    function schedule() {
        cancelAnimationFrame(frame);
        frame = requestAnimationFrame(tick);
    }
    function tick(now) {
        if (!playing || !current) return;
        const delta = Math.max(0, now - previous) * Number($('speed').value);
        previous = Math.max(previous, now);
        let next = time + delta;
        if (loop && next >= loop.end) next = loop.start + (next-loop.start) % (loop.end-loop.start);
        seek(next, false);
        if (!loop && time >= data.duration) { stop(); draw(); }
        else if (current) {
            if (prefetch !== currentChunk + 1 && (currentChunk + 1) * data.chunk - time < 4000 && (currentChunk + 1) * data.chunk <= data.duration) {
                prefetch = currentChunk + 1;
                load(currentChunk + 1).catch(() => {});
            }
            schedule();
        }
    }
    function toggle() {
        if (playing) { stop(); draw(); return; }
        if (loop && time >= loop.end) seek(loop.start);
        else if (time >= data.duration) seek(0);
        playing = true;
        $('play').textContent = '暂停';
        previous = performance.now();
        if (current) schedule();
        else if (!$('retry').hidden) switchChunk(wanted);
    }
    // 对同一单位按时间二分，只在连续、同图且距离合理的两个实测点之间插值。
    function at(id, track) {
        let lo = 0, hi = track.length;
        while (lo < hi) {
            const mid = (lo + hi) >>> 1;
            if (track[mid].t <= time) lo = mid + 1;
            else hi = mid;
        }
        const p = track[lo-1], q = track[lo];
        if (!p || time-p.t > data.gap) return null;
        const death = (deaths.get(id) || []).find(t => t >= p.t && (!q || t <= q.t));
        const dead = p.hp === 0 || (death != null && death <= time);
        let x = p.x, y = p.y;
        const interpolate = q && q.map === p.map && q.t - p.t <= data.gap && death == null &&
            Math.hypot(q.x-p.x,q.y-p.y) <= Math.max(12, (q.t-p.t)*0.016);
        if (interpolate) {
            const f = (time-p.t)/(q.t-p.t);
            x += (q.x-p.x)*f;
            y += (q.y-p.y)*f;
        }
        const quality = time === p.t ? '采样' : interpolate ? '插值' : '上次采样';
        return {id, ...p, x,y, dead, quality, track, index:lo-1,
            alpha:interpolate ? 1 : 1 - 0.65 * (time-p.t)/data.gap};
    }
    function mapFor(points, selected) {
        if (floorSelect.value) return maps.get(Number(floorSelect.value));
        if (selected) return maps.get(selected.map);
        const scores = new Map();
        for (const point of points) {
            const weight = actors.get(point.id)?.family === 'Player' ? 10 : 1;
            scores.set(point.map,(scores.get(point.map) || 0) + weight * point.alpha);
        }
        if (scores.size) return maps.get([...scores].sort((a,b) => b[1]-a[1])[0][0]);
        return data.maps.filter(map => map.first <= time).at(-1) || data.maps[0];
    }
    function eventItem(e) {
        const li = document.createElement('li'), jump = document.createElement('button'), raw = document.createElement('a');
        const timestamp = document.createElement('time'), summary = document.createElement('div');
        const badge = document.createElement('span'), description = document.createElement('p');
        const actions = document.createElement('div');
        const action = {SPELL_AURA_APPLIED:'获得',SPELL_AURA_REFRESH:'刷新',SPELL_AURA_APPLIED_DOSE:'叠层',
            SPELL_AURA_REMOVED_DOSE:'减层',SPELL_AURA_REMOVED:'移除',SPELL_AURA_BROKEN:'打破',SPELL_AURA_BROKEN_SPELL:'打破'};
        const type = eventType(e.kind,e.aura_type);
        li.className = 'replay-event-card';
        li.classList.toggle('combat-event-death',type === 'UNIT_DIED');
        timestamp.textContent = precise(e.t);
        timestamp.dateTime = 'PT'+(e.t/1000)+'S';
        badge.className = 'combat-event-badge';
        eventBadge(badge,type);
        const subjectName = e.kind === 'UNIT_DIED' ? e.target : [e.source,e.target].filter(name => name && name !== '未知').join(' → ');
        const skill = e.spell && e.spell !== '未知' && type !== 'UNIT_DIED' ? e.spell : '';
        const detail = e.aura_type ? [action[e.kind],e.aura_stacks == null ? '' : '×'+e.aura_stacks].filter(Boolean).join(' ') : e.interrupted_spell;
        const value = e.amount == null ? (type === 'DAMAGE' || type === 'HEAL' ? '数值未知' : '') : e.amount.toLocaleString('zh-CN');
        description.textContent = [subjectName,skill,detail,value].filter(Boolean).join(' · ');
        summary.append(badge,description);
        jump.type = 'button';
        jump.textContent = '跳转';
        const title = timestamp.textContent+' '+badge.textContent+' '+description.textContent;
        jump.setAttribute('aria-label','跳转回放 '+title);
        function showMap() {
            if (!window.matchMedia('(max-width:800px)').matches) return;
            (canvas.clientWidth ? canvas : $('state-title')).scrollIntoView({block:'start'});
            $('play').focus({preventScroll:true});
        }
        const subject = e.kind==='SPELL_CAST_START' ? e.source_id : e.target_id;
        function followTarget() {
            if (!actorSelect.value && actors.has(subject)) actorSelect.value=subject;
        }
        jump.onclick = () => { stop(); followTarget(); setLoop(null); seek(e.t); showMap(); };
        const repeat = document.createElement('button');
        repeat.type = 'button';
        repeat.textContent = '循环';
        repeat.setAttribute('aria-label','循环回看 '+title+' 前后 5 秒');
        repeat.disabled = !data.duration;
        repeat.onclick = () => { followTarget(); startLoop(e.t); showMap(); };
        raw.href = e.url; raw.textContent = '原文';
        raw.setAttribute('aria-label','查看原文 '+title);
        actions.className = 'replay-event-actions';
        actions.append(jump,repeat,raw);
        li.append(timestamp,summary,actions);
        return li;
    }
    const recentKind = $('recent-kind'), recentRetry = $('recent-retry'), recentOverflow = $('recent-overflow');
    const eventNodes = new Map();
    const keyEvents = new Set(['UNIT_DIED','SPELL_CAST_START','SPELL_INTERRUPT']);
    const recentLimit = 60;
    let recent = null;
    async function loadRecent(kind, chunk) {
        const controller = new AbortController();
        const request = recent = {kind, chunk, controller, events:[]};
        const url = new URL(location.href);
        url.search = new URLSearchParams({chunk, kind, revision:data.revision});
        try {
            const response = await fetch(url,{signal:controller.signal});
            if (response.status === 409) throw Error('索引已更新，请刷新回放。');
            if (response.redirected || !response.ok) throw Error('读取近期事件失败。');
            request.events = (await response.json()).events;
        } catch (error) {
            if (!controller.signal.aborted) request.error = error.message;
        } finally {
            request.done = true;
            if (recent === request) eventList();
        }
    }
    function eventList() {
        const kind = recentKind.value;
        const extra = kind && !keyEvents.has(kind);
        const visible = !$('replay-aside').hidden && !$('replay-events').hidden;
        if (recent && (!extra || !visible || !current || recent.kind !== kind || recent.chunk !== currentChunk)) {
            recent.controller.abort();
            recent = null;
        }
        if (!visible) return;
        if (extra && current && !recent) loadRecent(kind,currentChunk);
        const rows = (extra ? recent?.events : current?.events) || [];
        let lo = 0, hi = rows.length;
        while (lo < hi) {
            const mid = (lo + hi) >>> 1;
            if (rows[mid].t <= time) lo = mid + 1;
            else hi = mid;
        }
        const items = [];
        for (let i = lo-1; i >= 0 && rows[i].t >= time-5000; i--) {
            const event = rows[i];
            if (!kind || extra || eventType(event.kind,event.aura_type) === kind) items.push(event);
            if (items.length > recentLimit) break;
        }
        recentOverflow.hidden = items.length <= recentLimit;
        if (!recentOverflow.hidden) items.pop();
        const message = !current || extra && !recent?.done ? '正在读取事件…' :
            recent?.error || '此时段没有所选类型的事件。';
        recentRetry.hidden = !recent?.error;
        const key = kind + '|' + (items.length ? items.map(e => e.line).join(',') : message);
        if (key === eventKey) return;
        eventKey = key;
        const list = $('event-list');
        const shown = new Set(items.map(e => e.line));
        for (const [line,node] of eventNodes) {
            if (!shown.has(line)) { node.remove(); eventNodes.delete(line); }
        }
        list.querySelector('.replay-event-empty')?.remove();
        if (!items.length) {
            const li = document.createElement('li');
            li.className = 'replay-event-empty';
            li.textContent = message;
            list.append(li);
        }
        for (const [i,e] of items.entries()) {
            if (!eventNodes.has(e.line)) eventNodes.set(e.line,eventItem(e));
            const node = eventNodes.get(e.line);
            if (list.children[i] !== node) list.insertBefore(node,list.children[i] || null);
        }
    }
    recentKind.onchange = () => { $('event-list').scrollTop = 0; eventList(); };
    recentRetry.onclick = () => { recent = null; eventList(); };
    let eventRequest = null, resultsPage = 0, searchFilter = null;
    async function findEvents(page) {
        eventRequest?.abort();
        const request = new AbortController();
        eventRequest = request;
        const url = new URL(location.href);
        url.search = new URLSearchParams({...searchFilter, event_page:page, revision:data.revision});
        const list = $('found-events'), pages = $('prev-events').parentElement;
        pages.hidden = true;
        list.replaceChildren();
        $('finder-state').textContent = '正在查找…';
        try {
            const response = await fetch(url,{signal:request.signal});
            if (response.status === 409) throw Error('索引已更新，请刷新回放。');
            if (response.redirected) throw Error('登录状态已失效，请重新登录后刷新回放。');
            if (!response.ok) throw Error('读取事件失败，可重新查找。');
            const result = await response.json();
            if (request !== eventRequest) return;
            resultsPage = page;
            list.replaceChildren(...result.events.map(eventItem));
            list.scrollTop = 0;
            $('finder-state').textContent = result.events.length ? '' : '没有匹配的事件。';
            pages.hidden = !page && !result.more;
            $('prev-events').disabled = page === 0;
            $('next-events').disabled = !result.more;
            $('event-page').textContent = '第 '+(page+1)+' 页';
        } catch (error) {
            if (request === eventRequest && !request.signal.aborted) $('finder-state').textContent = error.message;
        }
    }
    function submitSearch() {
        searchFilter = {q:$('event-query').value.trim(),kind:$('event-kind').value};
        findEvents(0);
    }
    $('event-finder').ontoggle = () => { if ($('event-finder').open && !searchFilter) submitSearch(); };
    $('event-search').onsubmit = event => { event.preventDefault(); submitSearch(); };
    $('event-kind').onchange = submitSearch;
    $('prev-events').onclick = () => findEvents(resultsPage-1);
    $('next-events').onclick = () => findEvents(resultsPage+1);
    let rosterKey = '';
    function updateRoster(points, selectedId) {
        const groups = new Map();
        for (const point of points) {
            if (actors.get(point.id).family !== 'Player') continue;
            if (!groups.has(point.map)) groups.set(point.map,[]);
            groups.get(point.map).push(point.id);
        }
        const ordered = [...groups].sort(([a],[b]) => a-b);
        for (const [,ids] of ordered) ids.sort((a,b) => a-b);
        const key = JSON.stringify(ordered);
        const roster = $('floor-roster');
        if (key !== rosterKey) {
            rosterKey = key;
            const content = document.createDocumentFragment();
            for (const [mapId,ids] of ordered) {
                const row = document.createElement('div');
                const label = document.createElement('small');
                label.textContent = (maps.get(mapId).terrain?.name || '地图 '+mapId)+' · '+ids.length+' 人';
                row.append(label);
                for (const id of ids) {
                    const button = document.createElement('button');
                    button.type = 'button';
                    button.dataset.actor = id;
                    const actor = actors.get(id);
                    button.title = actor.name+' · '+(actor.spec || '专精未知')+' #'+id;
                    button.setAttribute('aria-label',button.title);
                    const name = document.createElement('span');
                    name.className = 'unit-name';name.textContent = actor.name;
                    button.append(badge(actor),name);
                    row.append(button);
                }
                content.append(row);
            }
            roster.replaceChildren(content);
        }
        const selected = String(selectedId);
        if (roster.dataset.selected !== selected || roster.dataset.members !== key) {
            for (const b of roster.querySelectorAll('button')) b.setAttribute('aria-pressed',b.dataset.actor === selected);
            roster.dataset.selected = selected;
            roster.dataset.members = key;
        }
    }
    $('floor-roster').onclick = event => {
        const button = event.target.closest('button[data-actor]');
        if (!button) return;
        actorSelect.value = button.dataset.actor;
        floorSelect.value = '';
        if (['battle','1'].includes(zoomSelect.value)) setZoomPreset('2');
        pan = null;
        draw();
    };
    const artCache = new Map();
    function imageFor(terrain) {
        let entry = artCache.get(terrain.url);
        if (entry) return entry;
        const image = new Image();
        entry = {image, status:'loading'};
        artCache.set(terrain.url, entry);
        image.onload = () => { entry.status = 'ready'; draw(); };
        image.onerror = () => { entry.status = 'error'; draw(); };
        // Native client artwork stays on its existing static or build/art view.
        // Never fall back to a supplied host, report media, or an arbitrary path.
        const native = typeof terrain.url === 'string' && !/\s/.test(terrain.url) &&
            /^(?:\/static\/dashboard\/maps\/\d+(?:\.\d+){3}\/\d+\.webp|\/portal\/combat-logs\/maps\/\d+(?:\.\d+){3}\/\d+\/)$/.test(terrain.url);
        const display = publishedImageUrl(terrain.url) || (native ? terrain.url : '');
        if (display) image.src = display;
        else entry.status = 'error';
        return entry;
    }
    function onAtlas(point, terrain) {
        const [x0,y0,x1,y1] = terrain.bounds;
        const u = (y1-point.y)/(y1-y0), v = (x1-point.x)/(x1-x0);
        const [a,b,c,d,e,f] = terrain.matrix;
        const x = a*u+c*v+e, y = b*u+d*v+f;
        if (x<0 || x>1 || y<0 || y>1) return false;
        return !terrain.clip.length || terrain.clip.some(polygon => {
            let inside = false;
            for (let i=0, j=polygon.length-1; i<polygon.length; j=i++) {
                const [xi,yi]=polygon[i], [xj,yj]=polygon[j];
                if ((yi>y)!==(yj>y) && x<(xj-xi)*(y-yi)/(yj-yi)+xi) inside=!inside;
            }
            return inside;
        });
    }
    function chooseTerrain(map, points) {
        const mode = $('map-mode').value;
        const primary = map.terrain;
        if (mode === 'grid' || !primary) return null;
        if (!primary.nativeUrl) return primary;
        if (mode === 'native' || artCache.get(primary.url)?.status === 'error' ||
            points.some(p => p.map === map.id && actors.get(p.id).family === 'Player' && !onAtlas(p,primary))) {
            return {name:primary.name, bounds:primary.bounds, url:primary.nativeUrl,
                    source:'客户端', matrix:[1,0,0,1,0,0], clip:[]};
        }
        return primary;
    }
    function drawFloor(terrain, image, px, py) {
        const [x0,y0,x1,y1] = terrain.bounds;
        // 先从客户端楼层平面投影到视口，再把 MDT 总图逆变换到这一层。
        ctx.save();
        ctx.transform(px(y0)-px(y1), 0, 0, py(x0)-py(x1), px(y1), py(x1));
        ctx.beginPath();
        ctx.rect(0,0,1,1);
        ctx.clip();
        const inverse = new DOMMatrix(terrain.matrix).inverse();
        ctx.transform(inverse.a, inverse.b, inverse.c, inverse.d, inverse.e, inverse.f);
        if (terrain.clip.length) {
            ctx.beginPath();
            for (const polygon of terrain.clip) {
                polygon.forEach(([u,v],i) => i ? ctx.lineTo(u,v) : ctx.moveTo(u,v));
                ctx.closePath();
            }
            ctx.clip();
        }
        ctx.drawImage(image,0,0,1,1);
        ctx.restore();
    }
    const activity = window.createReplayActivity({
        data, actors, badge, precise, redraw:draw,
        jump(t) { stop(); seek(t); }
    });
    const states = window.createReplayState({
        data, actors, precise, layers, badge,
        select(id) { actorSelect.value = id; pan = null; draw(); },
        seek(t) { stop(); seek(t); },
        retry() { draw(); }
    });
    function draw() {
        const w = canvas.clientWidth, h = canvas.clientHeight;
        const dpr = Math.min(window.devicePixelRatio || 1,2);
        const selectedId = Number(actorSelect.value);
        if (compareSelect.dataset.primary !== actorSelect.value) {
            compareSelect.dataset.primary = actorSelect.value;
            if (!selectedId || compareSelect.value === actorSelect.value) compareSelect.value = '';
            compareSelect.disabled = !selectedId || compareSelect.options.length <= 1;
            for (const option of compareSelect.options) option.disabled = !!selectedId && Number(option.value) === selectedId;
        }
        const compareId = Number(compareSelect.value);
        activity.update(time, current?.activity, playing);
        states.update(time, selectedId, compareId, current, playing);
        if (data.maps.length && w && h) drawMap(w,h,dpr,selectedId,compareId);
        const clockTime = scrubTarget ?? time;
        clock.value=Math.floor(clockTime);
        $('time').textContent=(playing ? format(clockTime) : precise(clockTime))+' / '+format(data.duration);
        clock.setAttribute('aria-valuetext',playing ? format(clockTime) : precise(clockTime));
        // 播放时只在使用链接前更新网址，避免逐帧重建查询参数。
        if (!playing) updateMoment();
        eventList();
    }
    function drawMap(w,h,dpr,selectedId,compareId) {
        if (canvas.width !== Math.round(w*dpr) || canvas.height !== Math.round(h*dpr)) {
            canvas.width = Math.round(w*dpr); canvas.height = Math.round(h*dpr);
        }
        ctx.setTransform(dpr,0,0,dpr,0,0);
        ctx.fillStyle = '#16202c'; ctx.fillRect(0,0,w,h);
        const points = [];
        for (const [id, track] of current?.tracks || []) {
            const p = at(id,track);
            if (p) points.push(p);
        }
        updateRoster(points,selectedId);
        const selected = points.find(p => p.id === selectedId);
        const partner = points.find(p => p.id === compareId);
        const sameFloor = selected && partner && selected.map === partner.map;
        const measured = sameFloor && selected.quality !== '上次采样' && partner.quality !== '上次采样';
        const distance = measured ? Math.hypot(selected.x-partner.x,selected.y-partner.y) : null;
        const floorName = id => maps.get(id)?.terrain?.name || '地图 '+id;
        const pairReading = $('pair-reading');
        pairReading.hidden = !compareId;
        if (compareId) {
            let reading;
            if (!selected || !partner) {
                const missing = [!selected && actors.get(selectedId).name, !partner && actors.get(compareId).name].filter(Boolean);
                reading = missing.join('、')+' 此刻缺少位置采样';
            } else if (!sameFloor) reading = '分处两层 · A 在'+floorName(selected.map)+'，B 在'+floorName(partner.map);
            else if (!measured) reading = '同层 · '+[selected,partner].filter(p => p.quality === '上次采样').map(p => actors.get(p.id).name).join('、')+' 仅有上次位置';
            else reading = '平面间距 '+distance.toFixed(1)+' 码 · '+(selected.quality === '插值' || partner.quality === '插值' ? '插值估算' : '同步采样');
            if (pairReading.textContent !== reading) pairReading.textContent = reading;
        }
        const map = mapFor(points,selected || partner);
        const split = selected && partner && !sameFloor;
        $('pair-floors').hidden = !split;
        if (split) {
            $('floor-a').dataset.floor = selected.map;
            $('floor-b').dataset.floor = partner.map;
            $('floor-a').disabled = map.id === selected.map;
            $('floor-b').disabled = map.id === partner.map;
        }
        const terrain = chooseTerrain(map,points);
        const entry = terrain ? imageFor(terrain) : null;
        const base = customZoom?.base || ({battle:'battle',1:'full'}[zoomSelect.value] ?? 'region');
        const battle = base === 'battle';
        const zoom = customZoom?.ratio ?? (battle ? 1 : Number(zoomSelect.value));
        const pull = activity.currentPull(time);
        const activePlayers = battle ? points.filter(p => p.map === map.id && !p.dead &&
            actors.get(p.id).family === 'Player' && (!pull || p.t >= pull.t && pull.actors.includes(p.id))) : [];
        let bounds = terrain && base === 'full' ? terrain.bounds : [map.x0,map.y0,map.x1,map.y1];
        if (activePlayers.length) {
            const xs = activePlayers.map(p => p.x), ys = activePlayers.map(p => p.y);
            bounds = [Math.min(...xs),Math.min(...ys),Math.max(...xs),Math.max(...ys)];
        }
        const [x0,y0,x1,y1] = bounds;
        const margin = activePlayers.length ? 1.23 : terrain && base !== 'full' ? 1.45 : 1;
        const minSpan = terrain && base !== 'full' && !activePlayers.length ? 90 : 20;
        let scale = Math.min(Math.max(1,w-52)/Math.max(minSpan,(y1-y0)*margin),
                             Math.max(1,h-56)/Math.max(minSpan,(x1-x0)*margin))*zoom;
        const onFloor = selected?.map === map.id ? selected : partner?.map === map.id ? partner : null;
        const follow = !battle && zoom > 1 && !!onFloor;
        const pairFollow = follow && sameFloor;
        if (pairFollow) scale = Math.min(scale, Math.max(1,w-96)/Math.max(1,Math.abs(selected.y-partner.y)),
            Math.max(1,h-80)/Math.max(1,Math.abs(selected.x-partner.x)));
        const focus = pairFollow ? {x:(selected.x+partner.x)/2, y:(selected.y+partner.y)/2} : onFloor;
        const cx = pan?.map === map.id ? pan.x : follow ? focus.x : (x0+x1)/2;
        const cy = pan?.map === map.id ? pan.y : follow ? focus.y : (y0+y1)/2;
        if (pan?.map === map.id && pan.scale) scale = pan.scale;
        camera = {cx,cy,scale,map:map.id,zoom,base};
        const px = y => w/2 - (y-cy)*scale, py = x => h/2 - (x-cx)*scale;
        const unit = Math.pow(10,Math.floor(Math.log10(75/scale)));
        const grid = [unit,unit*2,unit*5,unit*10].find(n => n*scale >= 42) || unit*10;
        const spacing = grid*scale;
        ctx.beginPath();ctx.strokeStyle='#2b3948';ctx.lineWidth=1;
        for (let x = ((px(0)%spacing)+spacing)%spacing; x<w; x+=spacing) {ctx.moveTo(x,0);ctx.lineTo(x,h);}
        for (let y = ((py(0)%spacing)+spacing)%spacing; y<h; y+=spacing) {ctx.moveTo(0,y);ctx.lineTo(w,y);}
        ctx.stroke();
        if (entry?.status === 'ready') drawFloor(terrain,entry.image,px,py);
        ctx.font = '12px "Segoe UI",sans-serif';ctx.fillStyle='rgba(20,30,40,.8)';
        ctx.fillRect(8,7,38,21);
        ctx.fillRect(8,h-32,Math.max(spacing+16,72),28);
        ctx.fillStyle='#f2f5f8';ctx.fillText('↑ 北',14,22);
        ctx.fillText(Number(grid.toPrecision(3))+' 码',14,h-16);
        ctx.fillRect(14,h-11,spacing,2);
        hits = [];
        const labels = [];
        const shown = points.filter(p => p.map === map.id && ($('npcs').checked || actors.get(p.id).family === 'Player' || p.id === selectedId));
        if ($('effects').checked && current) {
            const positions = new Map(shown.map(p => [p.id,p]));
            for (const event of current.events) {
                const elapsed = time - event.t;
                if (elapsed < 0 || elapsed >= 1200) continue;
                const dead = event.kind === 'UNIT_DIED';
                const start = positions.get(dead ? event.target_id : event.source_id);
                if (!start) continue;
                const target = positions.get(event.target_id);
                ctx.strokeStyle = dead ? '#ff747b' : event.kind === 'SPELL_INTERRUPT' ? '#78d9c6' : '#f6be68';
                ctx.globalAlpha = 1-elapsed/1200;
                ctx.lineWidth = 2;
                ctx.beginPath();ctx.arc(px(start.y),py(start.x),10+elapsed/100,0,Math.PI*2);ctx.stroke();
                if (!dead && target && target !== start) {
                    ctx.setLineDash([5,4]);
                    ctx.beginPath();ctx.moveTo(px(start.y),py(start.x));ctx.lineTo(px(target.y),py(target.x));ctx.stroke();
                    ctx.setLineDash([]);
                }
            }
            ctx.globalAlpha = 1;
        }
        if (measured && selected.map === map.id) {
            ctx.save();
            ctx.strokeStyle='#ade2f5';ctx.lineWidth=2;ctx.setLineDash([4,4]);
            ctx.beginPath();ctx.moveTo(px(selected.y),py(selected.x));ctx.lineTo(px(partner.y),py(partner.x));ctx.stroke();
            const midX = px((selected.y+partner.y)/2), midY = py((selected.x+partner.x)/2);
            const label = (selected.quality === '插值' || partner.quality === '插值' ? '约 ' : '')+distance.toFixed(1)+' 码';
            const width = ctx.measureText(label).width;
            if (midX-width/2>4 && midX+width/2<w-4 && midY+36<h) {
                ctx.fillStyle='#16202c';ctx.fillRect(midX-width/2-4,midY+18,width+8,18);
                ctx.fillStyle='#d7f1ff';ctx.textAlign='center';ctx.fillText(label,midX,midY+31);
            }
            ctx.restore();
        }
        shown.sort((a,b) => Number(a.id === selectedId || a.id === compareId)-Number(b.id === selectedId || b.id === compareId));
        for (const p of shown) {
            const a = actors.get(p.id), isPlayer = a.family === 'Player', chosen = p.id === selectedId || p.id === compareId;
            const accent = p.id === compareId ? '#83dbff' : '#ffe297';
            const x = px(p.y), y = py(p.x);
            if (x < -12 || y < -12 || x > w+12 || y > h+12) continue;
            if (chosen) {
                ctx.beginPath();ctx.moveTo(x,y);
                let last = {...p,t:time};
                for (let i=p.index;i>=0;i--) {
                    const v = p.track[i];
                    if (time-v.t>data.gap || v.map!==p.map || Math.hypot(v.x-last.x,v.y-last.y)>Math.max(12,(last.t-v.t)*0.016)) break;
                    if ((deaths.get(p.id) || []).some(t => t > v.t && t <= last.t)) break;
                    ctx.lineTo(px(v.y),py(v.x));last=v;
                }
                ctx.strokeStyle=accent;ctx.lineWidth=2;ctx.stroke();
            }
            ctx.globalAlpha=p.alpha;
            ctx.fillStyle = p.dead ? '#a4a7ac' : isPlayer ? (a.color || '#66c8e0') : a.family==='Pet' ? '#a89bd8' : '#e8a75d';
            ctx.strokeStyle=chosen ? accent : '#12202d';ctx.lineWidth=chosen?3:1.5;
            const icon = isPlayer ? specImage(a) : null;
            const radius = isPlayer ? 10 : 7;
            ctx.beginPath();
            if (isPlayer) ctx.arc(x,y,radius,0,Math.PI*2);
            else {ctx.moveTo(x,y-8);ctx.lineTo(x+7,y);ctx.lineTo(x,y+8);ctx.lineTo(x-7,y);ctx.closePath();}
            ctx.fill();
            if (icon) {
                ctx.save();ctx.clip();
                ctx.drawImage(icon,x-radius,y-radius,2*radius,2*radius);
                ctx.restore();
            }
            ctx.stroke();
            if (isPlayer && !icon && !p.dead) {
                ctx.save();ctx.font='bold 9px sans-serif';ctx.textAlign='center';
                ctx.strokeStyle='#17202b';ctx.lineWidth=2;ctx.strokeText(String(a.id),x,y+3,18);
                ctx.fillStyle='#fff';ctx.fillText(String(a.id),x,y+3,18);ctx.restore();
            }
            if (p.dead) {
                ctx.fillStyle='#28313b';ctx.fill();ctx.fillStyle='#fff';ctx.fillText('×',x-4,y+4);
            }
            else if (p.facing != null) {
                ctx.beginPath();ctx.moveTo(x-radius*Math.sin(p.facing),y-radius*Math.cos(p.facing));ctx.lineTo(x-(radius+4)*Math.sin(p.facing),y-(radius+4)*Math.cos(p.facing));
                ctx.strokeStyle='#c8d8e7';ctx.lineWidth=2;ctx.stroke();
            }
            if (isPlayer || chosen) {
                const detail = chosen ? states.marker(p.id,time) : null;
                const party = states.partyMarker(p.id,time,current);
                const hp = detail?.hp ?? (p.dead ? 0 : p.hp/p.max);
                if (layers.health && Number.isFinite(hp)) {
                    ctx.fillStyle='#16202c';ctx.fillRect(x-12,y+12,24,5);
                    ctx.fillStyle=hp<0.35?'#ffc274':'#8adeb5';ctx.fillRect(x-11,y+13,22*hp,3);
                }
                const counts = [];
                const buffCount = detail?.buffs ?? party?.buffs;
                const debuffCount = detail?.debuffs ?? party?.debuffs;
                if (layers.buffs && buffCount) counts.push('增 '+buffCount);
                if (layers.debuffs && debuffCount) counts.push('减 '+debuffCount);
                if (counts.length) {
                    const text=counts.join(' · ');
                    ctx.strokeStyle='#16202c';ctx.lineWidth=3;ctx.strokeText(text,x+13,y+18);
                    ctx.fillStyle='#fff1c6';ctx.fillText(text,x+13,y+18);
                }
            }
            const name = (compareId && chosen ? (p.id === compareId ? 'B · ' : 'A · ') : '')+a.name;
            const width = ctx.measureText(name).width;
            const labelX = Math.max(3,Math.min(x+11,w-width-4));
            const labelY = Math.max(13,Math.min(h-4,y+(compareId && chosen && p.id === compareId ? -12 : 4)));
            const room = labelX+width;
            if (layers.names && (chosen || !labels.some(l => Math.abs(l.y-labelY)<14 && l.x<room && l.end>labelX))) {
                ctx.lineWidth=3;ctx.strokeStyle='#16202c';ctx.strokeText(name,labelX,labelY);
                ctx.fillStyle=chosen ? accent : '#f4f6f8';ctx.fillText(name,labelX,labelY);labels.push({x:labelX,end:room,y:labelY});
            }
            ctx.globalAlpha=1;hits.push({id:p.id,x,y});
        }
        if (!hits.length) {
            ctx.textAlign='center';ctx.fillStyle='#bdc9d4';
            ctx.fillText(current ? '该时刻视野内没有已观测单位' : '读取坐标中…',w/2,h/2);ctx.textAlign='left';
        }
        const source = !terrain ? (map.terrain ? '坐标网格' : '未校准 · 坐标网格') :
            entry.status === 'ready' ? terrain.source+' 底图' :
            entry.status === 'loading' ? '底图加载中' : '底图读取失败 · 坐标网格';
        $('map-reading').textContent = (map.terrain ? map.terrain.name+' · ' : '')+'地图 '+map.id+' · '+source+' · 视野内 '+hits.length+' 个单位';
        $('retry-map').hidden = entry?.status !== 'error';
        $('unit-reading').textContent=selected ? (layers.names ? actors.get(selected.id).name : 'A')+' · '+selected.quality+'坐标 ('+selected.x.toFixed(1)+', '+selected.y.toFixed(1)+') · '+(maps.get(selected.map).terrain?.name || '地图 '+selected.map) :
            selectedId ? '此时没有该单位的位置采样。' : '点击图标或从列表中选择单位。';
    }
    function updateMoment() {
        const url = new URL(location.href);
        url.search = new URLSearchParams({t:Math.floor(time)});
        const tab = tabs.find(item => item.getAttribute('aria-selected') === 'true');
        if (tab !== tabs[0]) url.searchParams.set('tab', tab.id.slice(4));
        if (replayQuery.has('back')) url.searchParams.set('back', replayQuery.get('back'));
        activity.share(url.searchParams);
        if (actorSelect.value) url.searchParams.set('unit',actors.get(Number(actorSelect.value)).guid);
        if (compareSelect.value) url.searchParams.set('compare',actors.get(Number(compareSelect.value)).guid);
        if (canvas.closest('.player-shell').classList.contains('focus-state')) url.searchParams.set('view','state');
        if (loop) {
            url.searchParams.set('loop',loop.center);
            if (loop.start!==clamp(loop.center-5000) || loop.end!==clamp(loop.center+5000)) {
                url.searchParams.set('from',loop.start);url.searchParams.set('to',loop.end);
            }
        }
        if (floorSelect.value) url.searchParams.set('map',floorSelect.value);
        if (customZoom) {
            url.searchParams.set('zoom',customZoom.ratio.toFixed(4));
            url.searchParams.set('zoomBase',customZoom.base);
        } else if (zoomSelect.value !== 'battle') url.searchParams.set('zoom',zoomSelect.value);
        if ($('map-mode').value !== 'terrain') url.searchParams.set('background',$('map-mode').value);
        $('moment').href = url;
    }
    for (const event of ['focus','pointerdown','click','contextmenu']) $('moment').addEventListener(event,updateMoment);
    $('retry-map').onclick = () => {
        const active = maps.get(camera?.map)?.terrain;
        if (active) { artCache.delete(active.url); artCache.delete(active.nativeUrl); draw(); }
    };
    $('play').onclick=toggle;
    $('loop-toggle').disabled = !data.duration;
    $('loop-toggle').onclick = () => {
        if (loop) { setLoop(null); draw(); return; }
        startLoop(time);
    };
    for (const button of [$('floor-a'),$('floor-b')]) {
        button.onclick = () => {
            floorSelect.value = button.dataset.floor;
            pan = null;
            if (['battle','1'].includes(zoomSelect.value)) setZoomPreset('2');
            draw();
            if (window.matchMedia('(max-width:800px)').matches) canvas.scrollIntoView({block:'start'});
        };
    }
    $('retry').onclick=() => switchChunk(wanted);
    for (const edge of ['start','end']) {
        const control = $('loop-'+edge);
        control.onpointerdown = stop;
        control.oninput = () => {
            if (!loop) return;
            stop();
            const start = edge==='start' ? Math.min(Number(control.value),loop.end-1) : loop.start;
            const end = edge==='end' ? Math.max(Number(control.value),loop.start+1) : loop.end;
            setLoop(Math.round((start+end)/2),start,end);
            seek(edge==='start' ? start : end);
        };
    }
    const scrubTip = $('scrub-time');
    function timeAtPointer(event) {
        const rect = clock.getBoundingClientRect();
        const inset = 8;
        const fraction = (event.clientX-rect.left-inset)/Math.max(1,rect.width-2*inset);
        const t = Math.round(clamp(fraction*data.duration));
        scrubTip.textContent = precise(t);
        const x = Math.min(rect.width,Math.max(0,event.clientX-rect.left));
        scrubTip.style.left = 'clamp(34px,'+x+'px,calc(100% - 34px))';
        return t;
    }
    function previewScrub(now) {
        scrubFrame = 0;
        if (scrubPointer === null) return;
        const n = Math.floor(scrubTarget/data.chunk);
        if (wanted===n || cache.has(n) || now-lastScrub>=90) {
            lastScrub=now;
            seek(scrubTarget);
        } else scrubFrame=requestAnimationFrame(previewScrub);
    }
    function moveScrub(event) {
        scrubTarget = timeAtPointer(event);
        clock.value = scrubTarget;
        if (!scrubFrame) scrubFrame=requestAnimationFrame(previewScrub);
    }
    clock.onpointerdown = event => {
        if (event.button!==0 || scrubPointer!==null) return;
        event.preventDefault();
        clock.focus({preventScroll:true});
        clock.setPointerCapture(event.pointerId);
        scrubPointer=event.pointerId;
        stop();scrubTip.hidden=false;
        moveScrub(event);
    };
    clock.onpointermove = event => {
        if (scrubPointer===event.pointerId) moveScrub(event);
        else if (scrubPointer===null && event.pointerType==='mouse') {
            scrubTip.hidden=false;timeAtPointer(event);
        }
    };
    function finishScrub(event) {
        if (scrubPointer!==event.pointerId) return;
        const target = event.type==='pointerup' ? timeAtPointer(event) : scrubTarget;
        scrubPointer=null;scrubTarget=null;
        cancelAnimationFrame(scrubFrame);scrubFrame=0;
        if (clock.hasPointerCapture(event.pointerId)) clock.releasePointerCapture(event.pointerId);
        scrubTip.hidden=true;
        seek(target);
        stop();
    }
    clock.onpointerup=clock.onpointercancel=clock.onlostpointercapture=finishScrub;
    clock.onpointerleave=()=>{if (scrubPointer===null) scrubTip.hidden=true;};
    clock.oninput=() => {stop();seek(Number(clock.value));};
    clock.onkeydown=e => {
        if (!['ArrowLeft','ArrowRight'].includes(e.key)) return;
        e.preventDefault();stop();seek(time+((e.key==='ArrowRight'?1:-1)*(e.shiftKey?250:1000)));
    };
    document.querySelectorAll('[data-skip]').forEach(b => b.onclick=() => {stop();seek(time+Number(b.dataset.skip));});
    for (const control of [actorSelect,compareSelect,floorSelect,$('map-mode')]) control.onchange=() => {
        pan = null;
        draw();
    };
    for (const control of [$('npcs'),$('effects')]) control.onchange = draw;
    function setCustomZoom(view) {
        customZoom = view;
        customZoomOption.textContent = '自定义 '+Number(view.ratio.toFixed(2))+'×';
        customZoomOption.hidden = false;
        zoomSelect.value = 'custom';
    }
    function setZoomPreset(value) {
        customZoom = pan = null;
        customZoomOption.hidden = true;
        zoomSelect.value = value;
    }
    zoomSelect.onchange = () => {
        if (zoomSelect.value !== 'custom') setZoomPreset(zoomSelect.value);
        draw();
    };
    function zoomAt(factor, px, py) {
        if (!camera || drag) return;
        const {cx,cy,scale,map,zoom,base} = camera;
        const ratio = Math.max(0.5,Math.min(16,zoom*factor));
        if (ratio === zoom) return;
        const nextScale = scale * ratio / zoom;
        const delta = 1 / nextScale - 1 / scale;
        pan = {map,x:cx + (py - canvas.clientHeight/2)*delta,y:cy + (px - canvas.clientWidth/2)*delta,scale:nextScale};
        setCustomZoom({ratio,base});
        draw();
    }
    $('zoom-in').onclick = () => zoomAt(1.3,canvas.clientWidth/2,canvas.clientHeight/2);
    $('zoom-out').onclick = () => zoomAt(1/1.3,canvas.clientWidth/2,canvas.clientHeight/2);
    canvas.addEventListener('wheel', event => {
        event.preventDefault();
        const rect = canvas.getBoundingClientRect();
        const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? rect.height : 1;
        zoomAt(Math.exp(-event.deltaY*unit*0.002),event.clientX-rect.left,event.clientY-rect.top);
    }, {passive:false});
    for (const control of layerControls) {
        control.onchange = () => {
            const requestedAuras = layers.buffs || layers.debuffs;
            layers[control.dataset.layer] = control.checked;
            layout.classList.toggle('show-'+control.dataset.layer,control.checked);
            try { localStorage.setItem(preferenceKey,JSON.stringify(layers)); } catch {}
            states.changeLayers();
            if (!requestedAuras && (layers.buffs || layers.debuffs)) {
                for (const request of requests.values()) request.controller.abort();
                requests.clear();
                for (const [n,chunk] of cache) if (!chunk.partyAuras) cache.delete(n);
                prefetch=-1;
                switchChunk(Math.floor(time/data.chunk));
            } else draw();
        };
    }
    $('death').onchange=() => {
        const death=data.deaths.find(d=>d.line===Number($('death').value));
        if (!death) return;
        jumpDeath(death);$('death').value='';
    };
    canvas.onpointerdown=e => {
        if (e.button!==0 || !camera || drag) return;
        drag={id:e.pointerId,x:e.clientX,y:e.clientY,camera,moved:false};canvas.setPointerCapture(e.pointerId);
    };
    canvas.onpointermove=e => {
        if (drag?.id !== e.pointerId) return;
        const dx=e.clientX-drag.x,dy=e.clientY-drag.y;
        if (!drag.moved && Math.hypot(dx,dy)<5) return;
        drag.moved=true;
        const {map,cx,cy,scale} = drag.camera;
        pan={map,x:cx+dy/scale,y:cy+dx/scale,scale};draw();
    };
    canvas.onpointerup=e => {
        if (drag?.id !== e.pointerId) return;
        if (!drag.moved) {
            const rect=canvas.getBoundingClientRect();
            const near=hits.map(p=>({...p,d:Math.hypot(e.clientX-rect.left-p.x,e.clientY-rect.top-p.y)})).filter(p=>p.d<=14).sort((a,b)=>a.d-b.d);
            if (near.length) {actorSelect.value=String(near[0].id);pan=null;draw();}
        }
        drag=null;
    };
    canvas.onpointercancel=canvas.onlostpointercapture=e => {if (drag?.id === e.pointerId) drag=null;};
    $('focus-players').onclick=$('reset-view').onclick=canvas.ondblclick=() => {setZoomPreset('battle');draw();};
    const aside = $('replay-aside'), asideToggle = $('aside-toggle');
    function setAside(open) {
        aside.hidden = !open;
        asideToggle.setAttribute('aria-expanded',String(open));
        asideToggle.textContent = open ? '收起右侧面板' : '展开右侧面板';
        draw();
    }
    asideToggle.onclick = () => setAside(aside.hidden);
    const stage = canvas.parentElement, meter = stage.querySelector('.damage-meter');
    const meterHead = meter.querySelector('.meter-head');
    let meterPosition = null, meterDrag = null;
    function placeMeter(x,y) {
        if (!canvas.clientWidth) return;
        meterPosition = {
            x:Math.max(0,Math.min(x,stage.clientWidth-meter.offsetWidth)),
            y:Math.max(0,Math.min(y,stage.clientHeight-meter.offsetHeight))
        };
        meter.style.setProperty('--meter-x',meterPosition.x+'px');
        meter.style.setProperty('--meter-y',meterPosition.y+'px');
        meter.classList.add('is-positioned');
    }
    meterHead.onpointerdown = event => {
        if (event.button !== 0 || meterDrag || !canvas.clientWidth || event.target.closest('select,button')) return;
        meterDrag = {id:event.pointerId,x:event.clientX-meter.offsetLeft,y:event.clientY-meter.offsetTop};
        meterHead.setPointerCapture(event.pointerId);
        meterHead.classList.add('is-dragging');
    };
    meterHead.onpointermove = event => {
        if (meterDrag?.id === event.pointerId) placeMeter(event.clientX-meterDrag.x,event.clientY-meterDrag.y);
    };
    meterHead.onpointerup = meterHead.onpointercancel = meterHead.onlostpointercapture = event => {
        if (meterDrag?.id !== event.pointerId) return;
        meterDrag = null;
        meterHead.classList.remove('is-dragging');
    };
    const meterResize = new ResizeObserver(() => {
        if (meterPosition) placeMeter(meterPosition.x,meterPosition.y);
    });
    meterResize.observe(stage);
    meterResize.observe(meter);
    document.addEventListener('visibilitychange',() => {if (document.hidden) stop();});
    new ResizeObserver(draw).observe(canvas);
    const params = new URLSearchParams(location.search);
    const unit = data.actors.find(a => a.guid === params.get('unit'));
    if (unit) {
        actorSelect.value = unit.id;
        const partner = data.actors.find(a => a.guid === params.get('compare') && a.family === 'Player' && a.id !== unit.id);
        if (partner) compareSelect.value = partner.id;
    }
    if (params.has('loop') && /^\d{1,12}$/.test(params.get('loop'))) {
        const center = Number(params.get('loop'));
        if (center <= data.duration) {
            const start=Number(params.get('from')), end=Number(params.get('to'));
            if (params.has('from') && params.has('to') && Number.isInteger(start) && Number.isInteger(end) &&
                start>=0 && end<=data.duration && start<end) setLoop(center,start,end);
            else setLoop(center);
        }
    }
    if (maps.has(Number(params.get('map')))) floorSelect.value = params.get('map');
    if (['native','grid'].includes(params.get('background'))) $('map-mode').value = params.get('background');
    const ratio = Number(params.get('zoom')), base = params.get('zoomBase');
    if (['battle','full','region'].includes(base) && ratio >= 0.5 && ratio <= 16) setCustomZoom({ratio,base});
    else if (['1','2','4','8'].includes(params.get('zoom'))) setZoomPreset(params.get('zoom'));
    const shell = canvas.closest('.player-shell');
    function setMapVisible(visible) {
        shell.classList.toggle('focus-state',!visible);
        $('with-map').setAttribute('aria-pressed',String(visible));
        $('state-only').setAttribute('aria-pressed',String(!visible));
        draw();
    }
    if ($('with-map')) {
        $('with-map').onclick=()=>setMapVisible(true);
        $('state-only').onclick=()=>setMapVisible(false);
        if (params.get('view')==='state') setMapVisible(false);
    }
    if (!data.maps.length && !unit) actorSelect.value=data.actors.find(a=>a.family==='Player')?.id || '';
    const requested=params.get('t');
    const initial=requested===null ? (data.maps[0]?.first || 0) : Number(requested);
    seek(Number.isFinite(initial)?initial:0);
})();
