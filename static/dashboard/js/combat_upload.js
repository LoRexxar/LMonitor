(() => {
    const root = document.querySelector('#importer'), fileInput = document.querySelector('#source-file');
    const list = document.querySelector('#scene-list'), send = document.querySelector('#send-log');
    const status = document.querySelector('#upload-status'), bar = document.querySelector('#upload-progress');
    const stop = document.querySelector('#stop-log'), uploaded = document.querySelector('#uploaded');
    const modes = [...root.querySelectorAll('[name=mode]')], selections = new Set(), keys = new Map();
    const token = root.querySelector('[name=csrfmiddlewaretoken]').value;
    let worker, file, scenes = [], busy = false, pauseRequested = false;
    const manual = () => modes.some(r => r.checked && r.value === 'manual');
    const size = n => n >= 2**30 ? (n/2**30).toLocaleString('zh-CN',{maximumFractionDigits:2})+' GiB' :
        n >= 1048576 ? (n/1048576).toFixed(1)+' MiB' : Math.ceil(n/1024)+' KiB';
    const maxSourceBytes = Number(root.dataset.maxSourceBytes);
    const sourceSize = document.querySelector('#source-size');
    const sourceLimit = '本地原文件上限 '+size(maxSourceBytes);
    sourceSize.textContent = sourceLimit;
    function chosen() { return scenes.filter((_, i) => !manual() || selections.has(i)); }
    function refresh() {
        const items = chosen();
        document.querySelector('#selection-total').textContent = '选中 '+items.length+' 场 · 场次原文 '+size(items.reduce((a,s)=>a+s.size,0));
        send.disabled = busy || !items.length || items.length > Number(root.dataset.limit);
        send.textContent = root.dataset.resume ? '继续上传' : '上传 '+items.length+' 场';
        if (items.length > Number(root.dataset.limit)) status.textContent = '选择的场次数超过账号上限，请使用手动选择。';
    }
    function render() {
        list.replaceChildren();
        scenes.forEach((scene,i) => {
            const row = document.createElement('label'), box = document.createElement('input');
            box.type = 'checkbox'; box.checked = !manual() || selections.has(i); box.disabled = busy || !manual();
            box.onchange = () => { if (box.checked) selections.add(i); else selections.delete(i); refresh(); };
            const desc = document.createElement('small');
            const result = scene.boundary ? scene.result+'（'+scene.boundary+'，可分析已记录时段）' : scene.result;
            desc.textContent = [scene.stamp, result, size(scene.size)].filter(Boolean).join(' · ');
            const level = scene.difficulty ? (scene.kind==='大秘境' ? ' +' : ' · 难度 ')+scene.difficulty : '';
            row.append(box, document.createTextNode(scene.kind+' · '+scene.name+level), desc); list.append(row);
        });
        if (!scenes.length) list.textContent = '没有可上传的副本场次。';
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
        const id = JSON.stringify(['combat-import',root.dataset.user,file.name,file.size,file.lastModified,scene.start,scene.end,scene.size]);
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
    modes.forEach(r=>r.onchange=()=>{status.textContent='';render();});
    const dropZone = document.querySelector('#file-drop');
    const isFileDrag = event => Array.from(event.dataTransfer?.types || []).includes('Files');
    document.addEventListener('dragover', event => {
        if (!isFileDrag(event)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = busy ? 'none' : 'copy';
        dropZone.classList.toggle('is-dragging', !busy);
    });
    document.addEventListener('dragleave', event => {
        if (!event.relatedTarget) dropZone.classList.remove('is-dragging');
    });
    document.addEventListener('drop', event => {
        if (!isFileDrag(event)) return;
        event.preventDefault();
        dropZone.classList.remove('is-dragging');
        if (busy) return;
        const files = event.dataTransfer.files;
        if (files.length !== 1) {
            fileInput.value = ''; scan();
            status.textContent = '请一次拖入一个 .txt 日志文件。';
            return;
        }
        fileInput.files = files;
        scan();
    });
    fileInput.onchange = scan;
    function scan() {
        worker?.terminate(); worker=null; file=fileInput.files[0]; scenes=[]; selections.clear(); uploaded.replaceChildren();
        bar.hidden=true;stop.hidden=true;sourceSize.textContent=sourceLimit;
        status.textContent='';list.replaceChildren();refresh(); if (!file) return;
        if (!/\.txt$/i.test(file.name)) {
            fileInput.value=''; status.textContent='请选择 .txt 格式的日志文件。'; return;
        }
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
            if(root.dataset.resume) scenes=scenes.filter(s=>filename(s)===root.dataset.name && s.size===Number(root.dataset.size));
            status.textContent=data.error || (root.dataset.resume && scenes.length!==1 ? '请重新选择包含该场次的原日志。' : '扫描完成，尚未发送文件。');
            if(root.dataset.resume && scenes.length!==1) scenes=[];
            render();
        };
        scanning.onerror=()=>{if(worker!==scanning) return; scanning.terminate();worker=null;bar.hidden=true;stop.hidden=true;status.textContent='扫描失败，请重新选择文件。';render();};
        scanning.postMessage(file);
    }
    stop.onclick=()=>{
        if(worker){worker.terminate();worker=null;bar.hidden=true;stop.hidden=true;list.textContent='扫描已取消。';fileInput.value='';}
        if (busy) {
            pauseRequested = true;
            stop.disabled = true;
            status.textContent = '正在等待当前请求结束并保存续传进度。';
        }
    };
    async function api(url,method='GET',body,headers={}) {
        const r=await fetch(url,{method,body,headers:{'X-CSRFToken':token,...headers}});
        if(!r.headers.get('Content-Type')?.includes('application/json')) throw Error('请确认登录状态和网络连接。');
        const data=await r.json(); if(!r.ok) throw Error(data.message || '请求失败。');return data;
    }
    send.onclick=async()=>{
        if(busy) return;
        const items=chosen(); pauseRequested=false;stop.disabled=false;setBusy(true);uploaded.replaceChildren();
        bar.hidden=false;bar.value=0;stop.textContent='暂停上传';
        let urls = [], finished = 0;
        try {
            const batch=items.map(s=>({key:identify(s),name:filename(s),size:s.size}));
            urls=root.dataset.resume ? [root.dataset.resume] : (await api(root.dataset.create,'POST',JSON.stringify(batch),{'Content-Type':'application/json'})).reports;
            const bytesTotal=items.reduce((n,s)=>n+s.size,0); let done=0;
            for(const [i,s] of items.entries()){
                if (pauseRequested) break;
                const url=urls[i], info=await api(url+'parts/');
                const payload=new Blob(s.ranges.map(([a,b])=>file.slice(a,b)));
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
