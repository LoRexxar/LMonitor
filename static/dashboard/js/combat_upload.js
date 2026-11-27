(() => {
    const root = document.querySelector('#importer'), fileInput = document.querySelector('#source-file');
    const list = document.querySelector('#scene-list'), send = document.querySelector('#send-log');
    const status = document.querySelector('#upload-status'), bar = document.querySelector('#upload-progress');
    const stop = document.querySelector('#stop-log'), uploaded = document.querySelector('#uploaded');
    const modes = [...root.querySelectorAll('[name=mode]')], selections = new Set();
    const token = root.querySelector('[name=csrfmiddlewaretoken]').value;
    let worker, file, sourceHash, unterminated, scenes = [], busy = false, pauseRequested = false;
    const range = document.querySelector('#upload-range');
    const legacy = root.dataset.resume && root.dataset.key[14] !== '8';
    const manual = () => !range.hidden && modes.some(r => r.checked && r.value === 'manual');
    const size = n => n >= 2**30 ? (n/2**30).toLocaleString('zh-CN',{maximumFractionDigits:2})+' GiB' :
        n >= 1048576 ? (n/1048576).toFixed(1)+' MiB' : Math.ceil(n/1024)+' KiB';
    const maxSourceBytes = Number(root.dataset.maxSourceBytes);
    const sourceSize = document.querySelector('#source-size');
    const sourceLimit = '本地原文件上限 '+size(maxSourceBytes);
    sourceSize.textContent = sourceLimit;
    const resumeSelection = JSON.parse(document.getElementById('resume-selection')?.textContent || '[]');
    const autoKinds = new Set(['大秘境', '大秘境首领', '团本首领']);
    function visible(scene) {
        return (root.dataset.resume && range.hidden) || manual() || autoKinds.has(scene.kind);
    }
    function chosen() { return scenes.filter((s, i) => visible(s) && (!manual() || selections.has(i))); }
    function refresh() {
        const items = chosen();
        const bytes = items.reduce((n, s) => n + s.size + joinBytes(s), 0);
        const tooLarge = bytes > Number(root.dataset.maxReportBytes);
        document.querySelector('#selection-total').textContent = '选中 '+items.length+' 场 · '+(items.length ? 1 : 0)+' 份日志 · '+size(bytes)
            +(tooLarge ? ' · 超过单份报告上限 '+size(Number(root.dataset.maxReportBytes)) : '');
        send.disabled = busy || !items.length || tooLarge;
        send.textContent = root.dataset.resume ? '继续上传' : '上传为一份报告';
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
    // Moving an unterminated EOF version header before the opening needs a line break.
    function joinBytes(scene) {
        return Number(!legacy && unterminated && scene.ranges.some((r,i) => r[1] === file.size && i+1 < scene.ranges.length) || false);
    }
    function groupName(items) {
        return items.length === 1 ? filename(items[0]) : (items.length+' 场 · '+filename(items[0]).slice(0,-4)).slice(0,236)+'.txt';
    }
    async function groupKey(items) {
        if (!sourceHash) throw Error('请使用支持安全连接（HTTPS）的浏览器上传。');
        const identity = JSON.stringify([...(!root.dataset.resume || root.dataset.source ? ['combat-group-v2',file.name] : ['combat-group-v1']), root.dataset.user, sourceHash, items.map(s => s.ranges)]);
        const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(identity)));
        hash[6] = (hash[6] & 15) | 128; // UUIDv8: distinguishes grouped uploads from legacy random UUIDv4.
        hash[8] = (hash[8] & 63) | 128;
        const hex = Array.from(hash.subarray(0,16), b=>b.toString(16).padStart(2,'0')).join('');
        return [hex.slice(0,8),hex.slice(8,12),hex.slice(12,16),hex.slice(16,20),hex.slice(20)].join('-');
    }
    const receipt = key => 'combat-group:'+root.dataset.user+':'+key;
    function restore() {
        if (resumeSelection.length) {
            const byStart = new Map(scenes.map(scene => [scene.start, scene]));
            const selected = resumeSelection.map(([start, end, bytes]) => {
                const scene = byStart.get(start);
                return scene && scene.end === end && scene.size + joinBytes(scene) === bytes ? scene : null;
            });
            scenes = file.name === root.dataset.source && selected.every(Boolean) ? selected : [];
            return scenes.length ? '已找到待续传场次，尚未发送文件。' : '这份文件与原先选择的片段不匹配，请重新选择原日志。';
        }
        if (legacy || root.dataset.source) {
            scenes = scenes.filter(s => (!root.dataset.source || file.name === root.dataset.source) &&
                filename(s) === root.dataset.name && s.size + joinBytes(s) === Number(root.dataset.size));
            if (scenes.length !== 1) scenes = [];
        } else {
            let saved;
            try { saved = JSON.parse(localStorage.getItem(receipt(root.dataset.key))); } catch {}
            if (!saved || !Array.isArray(saved.starts)) {
                range.hidden = false;
                return '请重新选择原上传范围，核对一致后才会续传。';
            }
            const starts = new Set(saved.starts);
            scenes = sourceHash === saved.sourceHash ? scenes.filter(s => starts.has(s.start)) : [];
            if (scenes.length !== starts.size) scenes = [];
        }
        return scenes.length ? '已找到待续传场次，尚未发送文件。' : '请选择原上传使用的日志文件。';
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
        range.hidden = !!root.dataset.resume;
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
            sourceHash=data.sourceHash; unterminated=data.unterminated;
            status.textContent=data.error || (root.dataset.resume ? restore() : '扫描完成，尚未发送文件。');
            render();
        };
        scanning.onerror=()=>{if(worker!==scanning) return; scanning.terminate();worker=null;bar.hidden=true;stop.hidden=true;status.textContent='扫描失败，请重新选择文件。';render();};
        scanning.postMessage({file, legacy:!!legacy});
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
        let r;
        try { r=await fetch(url,{method,body,headers:{'X-CSRFToken':token,...headers}}); }
        catch { throw Error('网络连接中断。连接恢复后可再次提交，已接收的分片将用于续传。'); }
        if(!r.headers.get('Content-Type')?.includes('application/json')) throw Error('请确认登录状态和网络连接。');
        const data=await r.json(); if(!r.ok) throw Error(data.message || '请求失败。');return data;
    }
    send.onclick=async()=>{
        if(busy) return;
        const items=chosen(); pauseRequested=false;stop.disabled=false;setBusy(true);uploaded.replaceChildren();
        bar.hidden=false;bar.value=0;stop.textContent='暂停上传';
        let url, finished = false;
        try {
            if (!items.length) return;
            const key = legacy ? root.dataset.key : await groupKey(items);
            if (root.dataset.resume && key !== root.dataset.key) throw Error('文件或所选场次与待续传报告不一致。');
            const pieces = [];
            for (const scene of items) {
                const addBreak = joinBytes(scene);
                scene.ranges.forEach(([a,b], i) => {
                    pieces.push(file.slice(a,b));
                    if (addBreak && b === file.size && i+1 < scene.ranges.length) pieces.push('\n');
                });
            }
            const payload = new Blob(pieces), name = groupName(items);
            if (payload.size > Number(root.dataset.maxReportBytes)) throw Error('所选场次超过单份报告大小上限。');
            if (!legacy) {
                // Only offsets and a fingerprint, never log lines or player names.
                try { localStorage.setItem(receipt(key), JSON.stringify({sourceHash, starts:items.map(s=>s.start)})); } catch {}
            }
            url = root.dataset.resume || (await api(root.dataset.create,'POST',
                JSON.stringify({source_filename:file.name, scenes:[{key,name,size:payload.size}],
                    selection:items.length > 1 ? items.map(s=>[s.start,s.end,s.size + joinBytes(s)]) : []}), {'Content-Type':'application/json'})).reports[0];
            const info = await api(url+'parts/');
            if (payload.size !== info.size) throw Error('报告大小与服务端记录不一致。');
            status.textContent='正在上传 '+items.length+' 场 · 一份报告';
            for (let offset=0,n=0; offset<payload.size; offset+=info.part_size,n++) {
                if (pauseRequested) return;
                const part=payload.slice(offset,offset+info.part_size), expected=info.parts[n];
                if (expected && crypto.subtle) {
                    const bytes=new Uint8Array(await crypto.subtle.digest('SHA-256',await part.arrayBuffer()));
                    if (Array.from(bytes,v=>v.toString(16).padStart(2,'0')).join('')!==expected) throw Error('已接收的分片与此文件不符。');
                } else await api(url+'parts/'+n+'/','PUT',part);
                bar.value=100*(offset+part.size)/payload.size;
            }
            if (pauseRequested) return;
            await api(url+'finish/','POST');
            finished=true;
            if (!legacy) { try { localStorage.removeItem(receipt(key)); } catch {} }
            const a=document.createElement('a');a.href=url;a.textContent='查看解析进度 · '+name;uploaded.append(a);
            document.getElementById('choose-uploads').hidden=false;
            status.textContent='上传完成，正在打开日志详情…';
            window.location.assign(url);
        } catch(e){status.textContent=e.message;}
        finally {
            if (pauseRequested && url && !finished) {
                try {
                    await api(url+'pause/', 'POST', undefined, {Accept:'application/json'});
                    status.textContent='上传已暂停，重新提交将核对并续传。';
                } catch { status.textContent='已停止发送。暂停状态尚未确认，请在日志库核对。'; }
            }
            stop.disabled=false;setBusy(false);
        }
    };
})();
