(() => {
    const root = document.querySelector('#importer'), fileInput = document.querySelector('#source-file');
    const list = document.querySelector('#scene-list'), send = document.querySelector('#send-log');
    const status = document.querySelector('#upload-status'), bar = document.querySelector('#upload-progress');
    const stop = document.querySelector('#stop-log'), uploaded = document.querySelector('#uploaded');
    const modes = [...root.querySelectorAll('[name=mode]')], selections = new Set(), keys = new Map();
    const token = root.querySelector('[name=csrfmiddlewaretoken]').value;
    let worker, file, scenes = [], busy = false, pauseRequested = false;
    const manual = () => modes.some(r => r.checked && r.value === 'manual');
    const resumeSelection = JSON.parse(document.getElementById('resume-selection')?.textContent || '[]');
    const autoKinds = new Set(['大秘境', '大秘境首领', '团本首领']);
    const size = n => n >= 2**30 ? (n/2**30).toLocaleString('zh-CN',{maximumFractionDigits:2})+' GiB' :
        n >= 1048576 ? (n/1048576).toFixed(1)+' MiB' : Math.ceil(n/1024)+' KiB';
    const maxSourceBytes = Number(root.dataset.maxSourceBytes);
    const sourceSize = document.querySelector('#source-size');
    const sourceLimit = '本地原文件上限 '+size(maxSourceBytes);
    sourceSize.textContent = sourceLimit;
    function visible(scene) { return root.dataset.resume || manual() || autoKinds.has(scene.kind); }
    function chosen() { return scenes.filter((s,i) => visible(s) && (!manual() || selections.has(i))); }
    function mergeMode() { return manual() || resumeSelection.length > 0; }
    function refresh() {
        const items = chosen();
        const reports = mergeMode() && items.length ? 1 : items.length;
        document.querySelector('#selection-total').textContent = '选中 '+items.length+' 场 · '+reports+' 份日志 · '+size(items.reduce((a,s)=>a+s.size,0));
        send.disabled = busy || !items.length || reports > Number(root.dataset.limit);
        send.textContent = root.dataset.resume ? '继续上传' : mergeMode() ? '上传为一份日志' : '上传 '+reports+' 份日志';
        if (reports > Number(root.dataset.limit)) status.textContent = '按场归档会超过账号份数上限，可使用手动选择合并上传。';
    }
    function render() {
        list.replaceChildren();
        scenes.forEach((scene,i) => {
            if (!visible(scene)) return;
            const row = document.createElement('label'), box = document.createElement('input');
            box.type = 'checkbox'; box.checked = !manual() || selections.has(i); box.disabled = busy || !manual();
            box.onchange = () => { if (box.checked) selections.add(i); else selections.delete(i); refresh(); };
            const desc = document.createElement('small');
            const result = scene.boundary ? scene.result+'（'+scene.boundary+'，可分析已记录时段）' : scene.result;
            const bosses = scene.bosses?.length ? '首领交战：'+scene.bosses.join('、') : '';
            desc.textContent = [scene.stamp, result, scene.levelNote, bosses, size(scene.size)].filter(Boolean).join(' · ');
            const level = scene.kind === '大秘境' ? (/^[1-9][0-9]*$/.test(scene.difficulty) ? ' +'+scene.difficulty :
                scene.difficulty === '冲突' ? ' · 层数冲突' : ' · 层数未知') :
                scene.kind === '大秘境首领' ? ' · 层数未知' : scene.kind === '地下城' && scene.difficulty === '23' ? ' · 史诗（非钥匙）' : ' · 难度 '+scene.difficulty;
            row.append(box, document.createTextNode(scene.kind+' · '+scene.name+level), desc); list.append(row);
        });
        if (!list.childElementCount) list.textContent = scenes.length ? '没有大秘境或团本首领记录，手动选择可查看其他地下城片段。' : '没有可上传的副本场次。';
        refresh();
    }
    function setBusy(value) {
        busy=value; fileInput.disabled=value; modes.forEach(r=>r.disabled=value); stop.hidden=!value;
        list.querySelectorAll('input').forEach(b=>b.disabled=value || !manual()); refresh();
    }
    function filename(s) {
        return [s.stamp,s.name,s.kind,s.difficulty].filter(Boolean).join(' ').replace(/[\\/:*?"<>|\x00-\x1f]/g,'-').slice(0,230)+'.txt';
    }
    function identify(scene) {
        if(root.dataset.resume) return root.dataset.key;
        const identity = scene.selection ? ['selection',scene.selection] : [scene.start,scene.end,scene.size];
        const id = JSON.stringify(['combat-import',root.dataset.user,file.name,file.size,file.lastModified,...identity]);
        let key = keys.get(id);
        try { key ||= localStorage.getItem(id); } catch {}
        if (!key) {
            const bytes=crypto.getRandomValues(new Uint8Array(16));
            bytes[6]=(bytes[6]&15)|64; bytes[8]=(bytes[8]&63)|128;
            const hex=Array.from(bytes,n=>n.toString(16).padStart(2,'0')).join('');
            key=[hex.slice(0,8),hex.slice(8,12),hex.slice(12,16),hex.slice(16,20),hex.slice(20)].join('-');
            try { localStorage.setItem(id,key); } catch {}
        }
        keys.set(id,key); return key;
    }
    function reportsFor(items) {
        if (!mergeMode() || items.length < 2) return items.map(s=>({...s, filename:filename(s)}));
        const suffix = '（已选 '+items.length+' 场）.txt';
        const stem = file.name.replace(/\.txt$/i,'').replace(/[\\/:*?"<>|\x00-\x1f]/g,'-');
        const name = stem.slice(0,240-suffix.length)+suffix;
        return [{name, filename:name, size:items.reduce((n,s)=>n+s.size,0), ranges:items.flatMap(s=>s.ranges),
            selection:items.map(s=>[s.start,s.end,s.size])}];
    }
    function resumeScenes(found) {
        if (!resumeSelection.length) {
            const matches=found.filter(s=>filename(s)===root.dataset.name && s.size===Number(root.dataset.size));
            return matches.length === 1 ? matches : [];
        }
        if (file.name !== root.dataset.source) return [];
        const byStart = new Map(found.map(s=>[s.start,s]));
        const matched = [];
        for (const [start,end,bytes] of resumeSelection) {
            const scene = byStart.get(start);
            if (!scene || scene.end !== end || scene.size !== bytes) return [];
            matched.push(scene);
        }
        return matched;
    }
    modes.forEach(r=>r.onchange=()=>{status.textContent='';render();});
    fileInput.onchange = () => {
        worker?.terminate(); worker=null; file=fileInput.files[0]; scenes=[]; selections.clear(); uploaded.replaceChildren();
        bar.hidden=true;stop.hidden=true;sourceSize.textContent=sourceLimit;
        status.textContent='';list.replaceChildren();refresh(); if (!file) return;
        sourceSize.textContent='当前原文件 '+size(file.size)+' · '+sourceLimit;
        if (file.size > maxSourceBytes) {
            status.textContent='原文件超过本地扫描上限，请选择 '+size(maxSourceBytes)+' 以内的日志。';
            return;
        }
        list.textContent='正在本地扫描…'; bar.hidden=false; bar.value=0; stop.hidden=false; stop.textContent='取消扫描';
        const scanning = new Worker(root.dataset.worker); worker=scanning;
        scanning.onmessage=({data})=>{
            if(worker!==scanning) return;
            if(data.progress !== undefined) {bar.value=100*data.progress/file.size;return;}
            scanning.terminate();worker=null;stop.hidden=true;bar.hidden=true;
            scenes=data.scenes||[];
            if(root.dataset.resume) scenes=resumeScenes(scenes);
            status.textContent=data.error || (root.dataset.resume && !scenes.length ? '这份文件与原先选择的片段不匹配，请重新选择原日志。' : '扫描完成，尚未发送文件。');
            render();
        };
        scanning.onerror=()=>{scanning.terminate();worker=null;bar.hidden=true;stop.hidden=true;status.textContent='扫描失败，请重新选择文件。';render();};
        scanning.postMessage(file);
    };
    stop.onclick=()=>{
        if(worker){worker.terminate();worker=null;bar.hidden=true;stop.hidden=true;list.textContent='扫描已取消。';fileInput.value='';}
        if (busy) {
            pauseRequested = true;
            stop.disabled = true;
            status.textContent = '正在等待当前请求结束并保存续传进度。';
        }
    };
    async function api(url,method='GET',body,headers={}) {
        let r;
        try { r=await fetch(url,{method,body,headers:{'X-CSRFToken':token,...headers}}); }
        catch { throw Error('网络连接中断。连接恢复后可再次提交，已接收的分片将用于续传。'); }
        if(!r.headers.get('Content-Type')?.includes('application/json')) throw Error('请确认登录状态和网络连接。');
        const data=await r.json(); if(!r.ok) throw Error(data.message || '请求失败。');return data;
    }
    send.onclick=async()=>{
        if(busy) return;
        const items=reportsFor(chosen()); pauseRequested=false;stop.disabled=false;setBusy(true);uploaded.replaceChildren();
        bar.hidden=false;bar.value=0;stop.textContent='暂停上传';
        let urls = [], finished = 0;
        try {
            const batch=items.map(s=>({key:identify(s),name:s.filename,size:s.size}));
            urls=root.dataset.resume ? [root.dataset.resume] : (await api(root.dataset.create,'POST',JSON.stringify({source_filename:file.name,scenes:batch,selection:items[0].selection}),{'Content-Type':'application/json'})).reports;
            const bytesTotal=items.reduce((n,s)=>n+s.size,0); let done=0;
            for(const [i,s] of items.entries()){
                if (pauseRequested) break;
                const url=urls[i], info=await api(url+'parts/');
                const payload=new Blob(s.ranges.flatMap(([a,b,newline])=>newline ? [file.slice(a,b),'\n'] : [file.slice(a,b)]));
                if(payload.size!==info.size) throw Error('场次大小与服务端记录不一致。');
                status.textContent='上传 '+(i+1)+' / '+items.length+' · '+s.name;
                for(let offset=0,n=0;offset<payload.size;offset+=info.part_size,n++){
                    if (pauseRequested) break;
                    const part=payload.slice(offset,offset+info.part_size), expected=info.parts[n];
                    if(expected && crypto.subtle){
                        const bytes=new Uint8Array(await crypto.subtle.digest('SHA-256',await part.arrayBuffer()));
                        if(Array.from(bytes,v=>v.toString(16).padStart(2,'0')).join('')!==expected) throw Error('已接收的分片与此文件不符。');
                    } else await api(url+'parts/'+n+'/','PUT',part);
                    done+=part.size;bar.value=100*done/bytesTotal;
                }
                if (pauseRequested) break;
                await api(url+'finish/','POST');
                finished++;
                const a=document.createElement('a');a.href=url;a.textContent='查看解析进度 · '+s.name;uploaded.append(a);
                document.getElementById('choose-uploads').hidden=false;
            }
            if (finished === urls.length) status.textContent='上传完成，已提交解析。';
        } catch(e){status.textContent=e.message;}
        finally {
            if (pauseRequested && finished < urls.length) {
                let failures = 0;
                for (const url of urls.slice(finished)) {
                    try { await api(url+'pause/', 'POST', undefined, {Accept:'application/json'}); }
                    catch { failures++; }
                }
                status.textContent = failures ? '已停止发送。部分场次的暂停状态尚未确认，请在日志库核对。'
                    : '上传已暂停，重新提交将核对并续传。';
            }
            stop.disabled=false;setBusy(false);
        }
    };
})();
