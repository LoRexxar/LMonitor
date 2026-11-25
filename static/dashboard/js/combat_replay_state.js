/* 状态跟随回放时钟；每个观察单位独立读取当前分段。 */
window.createReplayState = function ({data, actors, precise, select, seek, retry, layers, badge}) {
    const team = document.getElementById('team-health'), host = document.getElementById('subject-states');
    const cache = new Map(), panels = new Map(), roster = [];
    const format = new Intl.NumberFormat('zh-CN');
    const element = (tag, cls, text) => {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text != null) node.textContent = text;
        return node;
    };
    function upper(rows, time, stamp) {
        let lo = 0, hi = rows.length;
        while (lo < hi) {
            const mid = (lo+hi) >>> 1;
            if (stamp(rows[mid]) <= time) lo = mid+1;
            else hi = mid;
        }
        return lo;
    }
    const pointTime = p => p.t, arrayTime = p => p[0];
    function setText(node, value) {
        if (node.textContent !== value) node.textContent = value;
    }
    for (const actor of data.actors.filter(a => a.family === 'Player')) {
        const button = element('button','team-unit'), name = element('span','unit-name',actor.name);
        const value = element('small','','—'), bar = element('i');
        button.type = 'button';
        const buff = element('small','team-buffs'), debuff = element('small','team-debuffs');
        value.className='team-hp';
        button.title=actor.name+' · '+(actor.spec || '专精未知')+' #'+actor.id;
        button.setAttribute('aria-label',button.title);
        button.append(badge(actor),name,value,bar,buff,debuff);
        button.onclick = () => select(actor.id);
        team.append(button);
        roster.push({id:actor.id,button,value,bar,buff,debuff});
    }
    const empty = element('p','state-hint','选择玩家，查看此刻的生命、增益、减益和战斗记录。');
    host.append(empty);
    let lastChunk, teamStamp, otherHealth = new Map();

    function partyMarker(id,time,chunk) {
        if (!chunk?.partyAuras) return null;
        const rows=chunk.partyAuras.get(id);
        return rows?.[upper(rows,time,pointTime)-1] || {buffs:0,debuffs:0};
    }
    function showTeam(time, a, b, chunk, playing) {
        const changed = lastChunk !== chunk;
        if (changed) {
            lastChunk = chunk;
            otherHealth = new Map();
            for (const [id,t,hp,max] of chunk?.vitals || []) {
                if (!otherHealth.has(id)) otherHealth.set(id,[]);
                otherHealth.get(id).push({t,hp,max});
            }
        }
        const stamp = [playing?Math.floor(time/100):time,a,b].join(',');
        if (!changed && teamStamp === stamp) return;
        teamStamp = stamp;
        for (const member of roster) {
            const track = chunk?.tracks.get(member.id) || [], health = otherHealth.get(member.id) || [];
            const p = track[upper(track,time,pointTime)-1], q = health[upper(health,time,pointTime)-1];
            const last = !p || (q && q.t>=p.t) ? q : p;
            const deaths = teamDeaths.get(member.id) || [];
            const death = deaths[upper(deaths,time,t=>t)-1];
            const freshDeath = death != null && time-death <= data.gap && (!last || last.t<death);
            const percent = freshDeath ? 0 : last && time-last.t<=data.gap ? Math.round(last.hp*100/last.max) : null;
            const counts=partyMarker(member.id,time,chunk);
            if (layers.buffs) setText(member.buff,'增 '+(counts?.buffs ?? '—'));
            if (layers.debuffs) setText(member.debuff,'减 '+(counts?.debuffs ?? '—'));
            setText(member.value, freshDeath ? '死亡' : percent === null ? '—' : percent+'%');
            member.bar.style.width = (percent || 0)+'%';
            member.button.dataset.low = String(percent !== null && percent<=35);
            member.button.setAttribute('aria-pressed',String(member.id===a || member.id===b));
        }
    }
    const teamDeaths = new Map();
    for (const death of data.deaths) {
        if (!teamDeaths.has(death.actor)) teamDeaths.set(death.actor,[]);
        teamDeaths.get(death.actor).push(death.t);
    }
    function panel(id) {
        const root = element('article','unit-state');
        const heading = element('h3'), identity=element('span'), status = element('p','state-hint');
        heading.append(badge(actors.get(id)),identity);
        const again = element('button','','重读状态');
        again.type = 'button';
        const body = element('div'), hp = element('strong','hp-reading');
        const meter = element('progress');
        meter.max = 100;
        meter.setAttribute('aria-label','最近采样生命比例');
        const graph = element('div','hp-history');
        const cursor = element('i','history-cursor');
        const range = element('small','chart-caption');
        const buffs = element('div','aura-list'), debuffs = element('div','aura-list');
        const filter = element('select'), events = element('ul','unit-events');
        filter.setAttribute('aria-label','筛选单位事件');
        for (const [label,value] of [['全部记录',''],['承伤','承伤'],['治疗','治疗'],['光环变化','光环'],['驱散、死亡与复活施法','机制']]) {
            filter.add(new Option(label,value));
        }
        const logHeader = element('div','state-log-head');
        logHeader.append(element('h4','','单位事件'),filter);
        const healthGroup=element('div','monitor-health');
        const buffGroup=element('div','monitor-buffs'), debuffGroup=element('div','monitor-debuffs');
        healthGroup.append(hp,meter,graph,range);
        buffGroup.append(element('h4','','增益'),buffs);
        debuffGroup.append(element('h4','','减益'),debuffs);
        body.append(healthGroup,buffGroup,debuffGroup,logHeader,events);
        root.append(heading,status,again,body);
        const view = {id,root,heading:identity,status,again,body,hp,meter,graph,cursor,range,buffs,debuffs,filter,events};
        healthGroup.append(thresholdFinder(view));
        const laneSelect=element('select'), lane=element('div','aura-track');
        const laneLabel=element('label','aura-track-label','追踪光环');
        laneSelect.setAttribute('aria-label','追踪光环');
        laneLabel.append(laneSelect);
        debuffGroup.after(laneLabel,lane);
        view.laneSelect=laneSelect;view.lane=lane;view.laneCursor=element('i','history-cursor');
        laneSelect.onchange=()=>{
            view.pin=laneSelect.value;
            view.pinLabel=laneSelect.selectedOptions[0]?.textContent;
            drawLane(view);
        };
        filter.onchange = () => { view.eventKey = null; refresh(view,view.time); };
        again.onclick = () => { view.key = null; retry(); };
        return view;
    }
    function thresholdFinder(view) {
        const box=element('details','threshold-finder'), form=element('form');
        const label=element('label','','生命 ≤ '), input=element('input'), submit=element('button','','查找');
        input.type='number';input.min=1;input.max=100;input.value=35;input.required=true;
        label.append(input,' %');form.append(label,submit);
        const status=element('p','state-hint'), list=element('ul','unit-events'), nav=element('div','finder-pages');
        status.setAttribute('role','status');
        const prev=element('button','','上一页'), next=element('button','','下一页');
        nav.append(prev,next);nav.hidden=true;
        box.append(element('summary','','全场低血量时刻'),form,status,list,nav);
        let page=0, percent=35;
        async function find(index) {
            view.thresholdRequest?.abort();
            const request=new AbortController();
            view.thresholdRequest=request;
            nav.hidden=true;list.replaceChildren();status.textContent='读取生命采样…';
            const url=new URL(location.href);
            url.search=new URLSearchParams({subject:view.id,threshold:percent,threshold_page:index,revision:data.revision});
            try {
                const response=await fetch(url,{signal:request.signal});
                if (response.status===409) throw Error('索引已更新，请刷新回放。');
                if (response.redirected) throw Error('登录状态已失效，请重新登录后刷新回放。');
                if (!response.ok) throw Error('读取失败，可重新查找。');
                const result=await response.json();
                if (request.signal.aborted) return;
                page=index;
                status.textContent=result.samples.length?'生命 ≤ '+percent+'% · 第 '+(index+1)+' 页':'没有符合阈值的生命采样。';
                for (const sample of result.samples) {
                    const li=element('li'), button=element('button','',precise(sample.t)+'　'+sample.reason+' · '+
                        (100*sample.hp/sample.maximum).toFixed(1)+'%');
                    button.onclick=()=>seek(sample.t);li.append(button);list.append(li);
                }
                nav.hidden=!page && !result.more;prev.disabled=!page;next.disabled=!result.more;
            } catch (error) { if (!request.signal.aborted) status.textContent=error.message; }
        }
        box.ontoggle=()=>{ if (box.open && !view.thresholdRequest) find(0); };
        form.onsubmit=event=>{event.preventDefault();percent=Number(input.value);find(0);};
        prev.onclick=()=>find(page-1);next.onclick=()=>find(page+1);
        return box;
    }
    function plot(view) {
        const {graph, payload, start, end} = view;
        const width = Math.max(1,end-start);
        const x = t => (Math.max(start,Math.min(end,t))-start)*1000/width;
        let path = '';
        for (let i=0; i<payload.health.length; i++) {
            const [t,,hp,max] = payload.health[i];
            if (max === null || t+data.gap<start) continue;
            const next = payload.health[i+1];
            const y = (1-hp/max)*64+2;
            path += 'M'+x(t)+','+y+'H'+x(Math.min(t+data.gap,next?.[0] ?? end));
            if (next && next[0]-t<=data.gap && next[3]) path += 'V'+((1-next[2]/next[3])*64+2);
        }
        const svg = document.createElementNS('http://www.w3.org/2000/svg','svg');
        svg.setAttribute('viewBox','0 0 1000 68');
        svg.setAttribute('preserveAspectRatio','none');
        svg.setAttribute('aria-hidden','true');
        const line = document.createElementNS(svg.namespaceURI,'path');
        line.setAttribute('d',path);
        svg.append(line); graph.replaceChildren(svg,view.cursor);
        graph.title='点击生命曲线定位时间';
        graph.onclick = event => {
            const rect = graph.getBoundingClientRect();
            seek(Math.round(start+width*Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width))));
        };
        view.range.textContent = precise(start)+' — '+precise(end)+' · 生命采样';
    }
    const auraId = a => [a.category,a.spell_id,a.source_id].join('/');
    const visibleAura = a => a.category==='BUFF' ? layers.buffs : layers.debuffs;
    function laneOptions(view) {
        const options=new Map();
        if (view.pin && !(view.pin.startsWith('BUFF/') ? layers.buffs : layers.debuffs)) view.pin='';
        if (view.pin) options.set(view.pin,view.pinLabel);
        for (const a of view.payload.auras) {
            if (!visibleAura(a)) continue;
            options.set(auraId(a),(a.category==='BUFF'?'增益':'减益')+' · '+a.spell+' · '+a.source);
        }
        view.laneSelect.replaceChildren(new Option('选择本段光环',''));
        for (const [key,label] of options) view.laneSelect.add(new Option(label,key));
        view.laneSelect.value=view.pin || '';
        drawLane(view);
    }
    function drawLane(view) {
        const lane=view.lane;
        lane.hidden=!view.pin;
        if (!view.pin) return;
        const spans=view.payload.auras.filter(a=>auraId(a)===view.pin);
        const width=Math.max(1,view.end-view.start);
        lane.replaceChildren();
        for (const a of spans) {
            const begin=Math.max(view.start,a.t), end=Math.min(view.end,a.until ?? view.end);
            if (end<=begin) continue;
            const block=element('button','aura-span',a.stacks>1?'×'+a.stacks:'');
            block.type='button';block.dataset.category=a.category;
            block.style.left=100*(begin-view.start)/width+'%';
            block.style.width=100*(end-begin)/width+'%';
            block.title=precise(begin)+' — '+precise(end)+' · '+a.spell+' · '+a.action;
            block.setAttribute('aria-label',block.title);
            block.onclick=()=>seek(begin);lane.append(block);
        }
        if (!lane.childElementCount) lane.append(element('small','','本段无已观测区间'));
        lane.append(view.laneCursor);
        view.laneCursor.style.left=100*(view.time-view.start)/width+'%';
    }
    function showAuras(view,time) {
        const step = upper(view.transitions,time,t=>t);
        if (step === view.auraStep) return;
        view.auraStep = step;
        const active = view.payload.auras.filter(a => a.t<=time && (a.until===null || time<a.until));
        view.debuffCount=active.filter(a=>a.category==='DEBUFF').length;
        view.buffCount=active.length-view.debuffCount;
        const key = active.map(a=>a.line).join(',');
        if (view.auraKey === key) return;
        view.auraKey = key;
        for (const [category,group] of [['BUFF',view.buffs],['DEBUFF',view.debuffs]]) {
            const entries = active.filter(a=>a.category===category);
            const chips = entries.map(a => {
                const chip = element('button','aura-chip');
                chip.type='button';
                chip.onclick=()=>{
                    view.laneSelect.value=auraId(a);
                    view.laneSelect.onchange();
                };
                chip.dataset.category = category;
                chip.append(element('b','',a.spell+(a.stacks>1 ? ' ×'+a.stacks : '')),element('small','',a.source));
                chip.title = '追踪此光环 · '+a.action+' · '+(a.t<0?'开战前':precise(a.t))+' · 技能 '+a.spell_id+(a.stacks===null?' · 层数未记录':'');
                return chip;
            });
            group.replaceChildren(...chips);
            if (!entries.length) group.textContent = view.payload.detailed ? '此刻无已观测光环' : '需补建光环索引';
        }
    }
    function refresh(view,time) {
        if (!view.payload) return;
        view.time = time;
        const rows = view.payload.health;
        const p = rows[upper(rows,time,arrayTime)-1];
        let reading = '尚无生命采样';
        if (p) {
            const age = Math.floor((time-p[0])/1000);
            reading = p[3]===null ? '最近记录为死亡' :
                format.format(p[2])+' / '+format.format(p[3])+' · '+Math.round(100*p[2]/p[3])+'%';
            reading += age>0 ? ' · '+age+' 秒前' : ' · '+precise(p[0]);
        }
        setText(view.hp,reading);
        view.meter.hidden = !p || p[3]===null;
        if (p && p[3]) view.meter.value = 100*p[2]/p[3];
        view.cursor.style.left = 100*(time-view.start)/Math.max(1,view.end-view.start)+'%';
        if (view.pin) view.laneCursor.style.left=view.cursor.style.left;
        showAuras(view,time);
        const filter = view.filter.value;
        if (view.lastFilter!==filter) {
            view.lastFilter=filter;
            view.filtered=view.payload.events.filter(e=>!filter || e.label===filter ||
                (filter==='光环' && e.kind.startsWith('SPELL_AURA_')) ||
                (filter==='机制' && ['驱散','死亡','复活施法'].includes(e.label)));
        }
        const events = view.filtered, i = upper(events,time,pointTime);
        const first = Math.max(upper(events,time-5001,pointTime),i-8);
        const key = [filter,first,i].join(',');
        if (key===view.eventKey) return;
        view.eventKey=key;
        const items = events.slice(first,i+2).map(e=>{
            const item = element('li');
            const jump = element('button','',precise(e.t)+'　'+e.label+(e.aura_type==='BUFF'?'增益':e.aura_type==='DEBUFF'?'减益':'')+' · '+e.spell+(e.aura_stacks!=null?' ×'+e.aura_stacks:'')+(e.extra?' / '+e.extra:''));
            jump.type='button';jump.onclick=()=>seek(e.t);
            item.dataset.future=String(e.t>time);
            const detail=element('small','',(e.t>time?'即将发生 · ':'')+e.source+(e.amount!=null?' · '+format.format(e.amount):''));
            const raw=element('a','','原文');raw.href=e.url;
            item.append(jump,detail,raw);
            return item;
        });
        view.events.replaceChildren(...items);
        if (!items.length) view.events.append(element('li','state-hint','此时段没有匹配记录。'));
    }
    function ready(view,payload) {
        view.payload=payload;
        view.transitions=payload.auras.flatMap(a=>a.until===null?[a.t]:[a.t,a.until]).sort((a,b)=>a-b);
        view.auraStep=-1;view.lastFilter=null;view.auraKey=null;view.eventKey=null;
        view.body.hidden=false;view.status.hidden=true;
        plot(view);laneOptions(view);refresh(view,view.time);
    }
    async function load(view,n) {
        const key = view.id+'/'+n;
        view.controller?.abort();
        view.key=key;view.payload=null;view.start=n*data.chunk;view.end=Math.min(data.duration,view.start+data.chunk);
        view.again.hidden=true;
        if (cache.has(key)) { ready(view,cache.get(key)); return; }
        view.body.hidden=true;view.status.hidden=false;view.status.textContent='读取单位状态…';
        const controller=new AbortController();view.controller=controller;
        const url = new URL(location.href);
        url.search = new URLSearchParams({subject:view.id,chunk:n,revision:data.revision});
        try {
            const response = await fetch(url,{signal:controller.signal});
            if (response.status===409) throw Error('索引已更新，请刷新回放。');
            if (response.redirected) throw Error('登录状态已失效，请重新登录后刷新回放。');
            if (!response.ok) throw Error('读取单位状态失败。');
            const payload=await response.json();
            if (controller.signal.aborted || view.key!==key) return;
            cache.set(key,payload);
            if (cache.size>8) cache.delete(cache.keys().next().value);
            ready(view,payload);retry();
        } catch (error) {
            if (controller.signal.aborted) return;
            view.status.textContent=error.message;view.again.hidden=false;
        }
    }
    return {
        partyMarker,
        changeLayers() {
            teamStamp=null;
            for (const view of panels.values()) if (view.payload) laneOptions(view);
        },
        marker(id,time) {
            const view=panels.get(id);
            if (!view?.payload) return null;
            const rows=view.payload.health, p=rows[upper(rows,time,arrayTime)-1];
            return {hp:p && p[3] && time-p[0]<=data.gap ? p[2]/p[3] : null,
                buffs:view.payload.detailed ? view.buffCount : null, debuffs:view.payload.detailed ? view.debuffCount : null};
        },
        update(time,a,b,chunk,playing) {
        showTeam(time,a,b,chunk,playing);
        const ids=[a,b].filter(Boolean);
        empty.hidden=!!ids.length;
        for (const [id,view] of panels) {
            if (!ids.includes(id)) { view.controller?.abort();view.thresholdRequest?.abort();view.root.remove();panels.delete(id); }
        }
        ids.forEach((id,i)=>{
            if (!panels.has(id)) panels.set(id,panel(id));
            const view=panels.get(id);
            if (host.children[i+1]!==view.root) host.insertBefore(view.root,host.children[i+1] || null);
            const actor=actors.get(id);
            const label=layers.names ? actor.name : actor.spec || '单位 #'+id;
            setText(view.heading,(i?'B · ':'A · ')+label);
            const n=Math.floor(time/data.chunk);
            view.time=time;
            if (view.key!==id+'/'+n) load(view,n);
            else refresh(view,time);
        });
    }};
};
