/* Independent, server-authoritative analysis administration. No analysis browser storage. */
(function (global) {
    'use strict';
    const BASE = '/dashboard/api/player-analyses/';
    const names = {draft:'草稿',pending:'待处理',ready:'就绪',stale:'已过期',error:'失败',archived:'已归档',approved:'已批准',rejected:'已拒绝',manual:'人工维护',ai:'AI 分析',local_deterministic:'本地规则分析'};
    const label = value => names[value] || value || '—';
    function safeURL(value) {
        if (typeof value !== 'string' || !/^https?:\/\//i.test(value)) return '';
        try { const u = new URL(value); return ['http:', 'https:'].includes(u.protocol) ? value : ''; } catch (_) { return ''; }
    }
    function buildPatch(row, values) {
        const patch = {revision:row.revision};
        for (const key of ['manual','review_status','is_published','review_note']) {
            if (Object.hasOwn(values,key) && JSON.stringify(values[key]) !== JSON.stringify(row[key])) patch[key] = values[key];
        }
        const review = patch.review_status ?? row.review_status;
        const published = patch.is_published ?? row.is_published;
        const manual = Object.hasOwn(patch,'manual') ? patch.manual : row.manual;
        if (published && review !== 'approved' && (manual || patch.is_published)) throw Error('请先批准审核，再公开发布。');
        return patch;
    }
    class Client {
        constructor(fetcher, csrf) { this.fetcher=fetcher; this.csrf=csrf; this.busy=false; }
        async request(path, method='GET', body) {
            const headers = {'Accept':'application/json'};
            if (method !== 'GET') { headers['Content-Type']='application/json'; headers['X-CSRFToken']=this.csrf(); }
            const response = await this.fetcher(BASE+path,{method,headers,credentials:'same-origin',cache:'no-store', ...(body === undefined ? {} : {body:JSON.stringify(body)})});
            const errors = {401:'登录已失效，请返回后台重新登录。',403:'权限不足或 CSRF 校验失败，请刷新页面并确认超级管理员权限。',409:'版本冲突：其他人已修改此记录。请重新读取详情后再操作；当前输入未保存。'};
            if (errors[response.status]) throw Error(errors[response.status]);
            let result;
            try { result = await response.json(); } catch (_) { throw Error('服务器未返回有效 JSON，请检查登录状态后重试。'); }
            if (!response.ok || result.status !== 'success') throw Error(result.message || result.error || '请求失败，请稍后重试。');
            return result.data;
        }
        async write(method, id, body) {
            if (this.busy) throw Error('操作进行中，请勿重复提交。');
            this.busy=true;
            try {
                const result=await this.request(id == null ? '' : id+'/',method,body);
                const exactID=id ?? result.id;
                try { return await this.request(exactID+'/'); }
                catch (error) { throw Error('变更已提交，但重新读取失败。请刷新详情，不要重复提交。'+error.message); }
            } finally { this.busy=false; }
        }
    }
    function mount(root, options={}) {
        const doc=root.ownerDocument, win=doc.defaultView;
        const client=new Client(options.fetch || win.fetch.bind(win),()=>doc.querySelector('meta[name="csrf-token"]')?.content || '');
        const confirm=options.confirm || win.confirm.bind(win);
        let current=null, editing=false, creating=false, selected=null, listPage=1, characterPage=1;
        let pending=Promise.resolve(), readSequence=0, detailSequence=0, characterSequence=0;
        const filters={}, characterFilters={q:''};
        function el(tag, text, attrs={}) {
            const node=doc.createElement(tag);
            if (text != null) node.textContent=String(text);
            Object.entries(attrs).forEach(([k,v])=>node.setAttribute(k,String(v)));
            return node;
        }
        function button(text, action, run) {
            const b=el('button',text,{type:'button','data-action':action});
            b.addEventListener('click',()=>task(run));return b;
        }
        function task(run) {
            pending=Promise.resolve().then(run).catch(error=>{message.textContent=error.message;message.dataset.kind='error';});
            return pending;
        }
        function inputField(parent,title,value,attrs={},tag='input') {
            const wrap=el('label'), titleNode=el('span',title), input=el(tag,null,attrs);
            input.value=value ?? '';wrap.append(titleNode,input);parent.append(wrap);return input;
        }
        function selectField(parent,title,value,key,choices) {
            const input=inputField(parent,title,value,{'data-field':key},'select');
            choices.forEach(([v,t])=>input.append(el('option',t,{value:v})));
            input.value=value;return input;
        }
        function evidenceView(parent, items) {
            if (!Array.isArray(items) || !items.length) return;
            const ul=el('ul',null,{class:'pa-evidence'});
            items.forEach(item=>{
                const li=el('li'), text=typeof item==='string'?item:item.text;
                li.append(el('span',text || '证据'));
                const url=safeURL(item.url || item.run_url || item.source_url);
                if (url) li.append(el('a','查看来源',{href:url,target:'_blank',rel:'noopener noreferrer'}));
                ul.append(li);
            });parent.append(ul);
        }
        function projection(parent,title,data) {
            const section=el('section',null,{class:'pa-projection'});section.append(el('h3',title));
            if (!data) { section.append(el('p','暂无内容'));parent.append(section);return; }
            (Array.isArray(data.tags)?data.tags:[]).forEach(tag=>{
                const item=el('div',null,{class:'pa-tag-view'});
                item.append(el('strong',typeof tag==='string'?tag:[tag.label,tag.value].filter(Boolean).join(' · ')));
                if (tag.reason) item.append(el('p',tag.reason));evidenceView(item,tag.evidence);section.append(item);
            });
            section.append(el('p',data.conclusion || '暂无结论',{class:'pa-conclusion'}));
            evidenceView(section,data.evidence);parent.append(section);
        }
        function pageControls(parent,data,change) {
            const nav=el('nav',null,{'aria-label':'分页',class:'pa-actions'});
            const prev=button('上一页','prev',()=>change(data.page-1));prev.disabled=data.page<=1;
            const next=button('下一页','next',()=>change(data.page+1));next.disabled=data.page>=data.total_pages;
            nav.append(prev,el('span',`第 ${data.page} / ${data.total_pages || 1} 页 · 共 ${data.total} 条`),next);parent.append(nav);
        }
        function staleReview(row) {
            return row.source_kind !== 'manual' && row.reviewed_at && row.review_matches_ai === false;
        }
        function playerText(player,id) {
            return player ? `${player.character_name} · ${player.region || ''} ${player.realm || ''} · ${player.class_name || ''} ${player.spec_name || ''} · #${player.id ?? id} · 赛季 ${player.season_id ?? '—'}` : `角色 #${id ?? '—'}（角色资料不可用）`;
        }
        const message=el('div','', {role:'status','aria-live':'polite',class:'pa-message'});
        const filterForm=el('form',null,{class:'pa-filters','aria-label':'筛选分析'});
        const query=inputField(filterForm,'角色 ID / 名字','',{type:'search',name:'q'});
        const selects={};
        for (const [key,title,values] of [
            ['status','生成状态',['pending','ready','error']],
            ['review_status','审核状态',['pending','approved','rejected']],
            ['source','来源',['manual','ai','local_deterministic']],['visibility','发布状态',['published','hidden']]
        ]) selects[key]=selectField(filterForm,title,'',key,[['','全部'],...values.map(v=>[v,v==='published'?'已发布':v==='hidden'?'隐藏':label(v)])]);
        const pageSize=selectField(filterForm,'每页','20','page_size',[['20','20'],['50','50']]);
        const search=el('button','搜索',{type:'submit'});filterForm.append(search,button('新增人工分析','new',()=>newRecord()));
        filterForm.addEventListener('submit',event=>{event.preventDefault();task(()=>loadList(1));});
        const results=el('section',null,{'aria-label':'分析列表',class:'pa-results'});
        const detail=el('section',null,{'aria-label':'分析详情',class:'pa-detail',tabindex:'-1'});detail.hidden=true;
        root.append(message,filterForm,results,detail);
        async function loadList(page=1) {
            const sequence=++readSequence;
            Object.assign(filters,{q:query.value,page:String(page),page_size:pageSize.value});
            for (const key of Object.keys(selects)) filters[key]=selects[key].value;
            results.setAttribute('aria-busy','true');
            try {
                const data=await client.request('?'+new URLSearchParams(filters));
                if(sequence!==readSequence)return;
                listPage=data.page;results.replaceChildren(el('h2','角色分析'));
                if(!data.items.length)results.append(el('p','没有匹配的分析记录。'));
                data.items.forEach(row=>{
                    const card=el('article',null,{class:'pa-list-row'});
                    const info=el('div');info.append(el('strong',playerText(row.player,row.player_id)),el('p',`#${row.id} · ${label(row.status)} · ${label(row.review_status)} · ${label(row.source_kind)} · ${row.is_published?'已发布':'隐藏'}`));
                    if(staleReview(row))info.append(el('strong','AI 证据或结论已更新，原审核不适用。'));
                    card.append(info,button('审查详情','open',()=>open(row.id)));results.append(card);
                });pageControls(results,data,loadList);
            } finally { if(sequence===readSequence)results.removeAttribute('aria-busy'); }
        }
        async function open(id) {
            if(client.busy)return;
            if(editing && !confirm('离开将放弃未保存的人工编辑，继续？'))return;
            const seq=++detailSequence;
            message.textContent='正在读取详情…';
            const row=await client.request(id+'/');
            if(seq!==detailSequence)return;
            current=row;creating=false;editing=false;selected=null;renderDetail();message.textContent='';detail.focus();
        }
        function evidenceEditor(parent,items=[]) {
            const group=el('div',null,{class:'pa-evidence-editor'}), rows=el('div');
            function add(value={}) {
                const row=el('div',null,{class:'pa-evidence-row'});
                inputField(row,'证据说明',value.text,{'data-evidence-field':'text',required:''});
                inputField(row,'来源链接（http / https，可留空）',value.url,{'data-evidence-field':'url',type:'url'});
                row.append(button('移除此证据','remove-evidence',()=>row.remove()));rows.append(row);
            }
            items.forEach(add);group.append(rows,button('添加证据','add-evidence',()=>add()));parent.append(group);
            return ()=>Array.from(rows.children).map(row=>{
                const text=row.querySelector('[data-evidence-field="text"]').value.trim();
                const url=row.querySelector('[data-evidence-field="url"]').value.trim();
                if (!text)throw Error('请填写证据说明，或移除空白证据。');
                if(url && !safeURL(url))throw Error('证据链接仅允许完整 http / https 地址。');
                return {text,url};
            });
        }
        function manualEditor(parent,manual) {
            const box=el('fieldset',null,{class:'pa-manual-editor'});box.append(el('legend','人工内容（不会覆盖冻结的 AI 原文）'));
            const tags=el('div'), getters=new Map();
            function addTag(tag={}) {
                const row=el('fieldset',null,{class:'pa-tag-editor'});row.append(el('legend','人工标签'));
                for(const [key,title] of [['label','标签名称'],['value','标签值'],['reason','判断依据']]) inputField(row,title,tag[key],{'data-tag-field':key},key==='reason'?'textarea':'input');
                getters.set(row,evidenceEditor(row,tag.evidence || []));
                row.append(button('移除标签','remove-tag',()=>{getters.delete(row);row.remove();}));tags.append(row);
            }
            (manual?.tags || []).forEach(addTag);
            box.append(tags,button('添加标签','add-tag',()=>addTag()));
            const conclusion=inputField(box,'综合结论',manual?.conclusion,{'data-field':'conclusion',rows:'4'},'textarea');
            box.append(el('h4','结论证据'));const readEvidence=evidenceEditor(box,manual?.evidence || []);parent.append(box);
            return ()=>{
                const values=Array.from(tags.children).map(row=>{
                    const tag={};for(const key of ['label','value','reason'])tag[key]=row.querySelector(`[data-tag-field="${key}"]`).value.trim();
                    if(!tag.label || !tag.value)throw Error('人工标签必须填写名称和值。');
                    tag.evidence=getters.get(row)();return tag;
                });
                const value={tags:values,conclusion:conclusion.value.trim(),evidence:readEvidence()};
                if(!value.tags.length && !value.conclusion)throw Error('请填写至少一个标签或综合结论。');
                return value;
            };
        }
        function setBusy(busy) {
            root.setAttribute('aria-busy',String(busy));
            root.querySelectorAll('button,input,select,textarea').forEach(node=>{
                if(busy){node.dataset.wasDisabled=String(node.disabled);node.disabled=true;}
                else if(Object.hasOwn(node.dataset,'wasDisabled')){node.disabled=node.dataset.wasDisabled==='true';delete node.dataset.wasDisabled;}
            });
        }
        async function save(method,body) {
            if(client.busy)return;
            setBusy(true);message.textContent='正在提交并重新读取…';
            try {
                const row=await client.write(method,creating?null:current.id,body);
                current=row;creating=false;editing=false;renderDetail();
                message.textContent='已保存，并已从服务器重新读取记录。';message.dataset.kind='success';
                try { await loadList(listPage); } catch(error) {message.textContent+=' 列表刷新失败：'+error.message;}
            } finally {setBusy(false);}
        }
        function renderDetail() {
            detail.hidden=false;detail.replaceChildren();
            detail.append(el('h2',creating?'新增人工分析':`审查分析 #${current.id}`));
            if(creating)renderCharacters(detail);
            else {
                detail.append(el('p',playerText(current.player,current.player_id)),el('p',`版本 ${current.revision} · ${label(current.status)} · ${label(current.source_kind)} · ${label(current.review_status)} · ${current.is_published?'已发布':'隐藏'} · 活动 #${current.activity_id ?? '—'}`));
                detail.append(el('p',`生成时间：${current.generated_at || '—'} · 审核时间：${current.reviewed_at || '—'}`,{class:'pa-muted'}));
                if(staleReview(current))detail.append(el('p','AI 证据或结论已更新，原审核不适用于当前 AI；请重新审核。',{'data-review-stale':'',role:'note'}));
                const columns=el('div',null,{class:'pa-projections'});
                projection(columns,'AI 原始标签 / 结论 / 冻结证据（只读）',current.ai);
                projection(columns,'人工维护（独立来源）',current.manual);detail.append(columns);
                const raw=el('details');raw.append(el('summary','展开 AI 原始 JSON（仅供核验）'));
                raw.addEventListener('toggle',()=>{if(raw.open && !raw.querySelector('pre'))raw.append(el('pre',JSON.stringify({result_json:current.ai?.result_json,evidence_json:current.ai?.evidence_json},null,2)));});detail.append(raw);
            }
            const form=el('form',null,{class:'pa-edit-form'});
            const readManual=editing?manualEditor(form,current?.manual):null;
            if(!editing)form.append(button('编辑人工内容','edit-manual',()=>{editing=true;renderDetail();}));
            const review=selectField(form,'审核决定',current?.review_status || 'pending','review_status',[['pending','待审核'],['approved','批准'],['rejected','拒绝']]);
            const visibility=selectField(form,'发布状态',String(current?.is_published || false),'is_published',[['false','隐藏'],['true','公开（须批准审核）']]);
            const note=inputField(form,'审核意见',current?.review_note || '',{'data-field':'review_note',rows:3},'textarea');
            if(editing){review.value='pending';visibility.value='false';review.disabled=true;visibility.disabled=true;}
            if(creating)note.disabled=true;
            form.append(el('p',editing?'人工内容变更将保存为待审核、隐藏状态；保存后请重新批准并发布。未修改内容不会重置审核。':'修改人工内容不会自动恢复 AI。公开人工内容必须获批；隐藏不会删除冻结证据。',{class:'pa-muted'}));
            const actions=el('div',null,{class:'pa-actions'});
            const submit=el('button',creating?'保存人工草稿':'保存变更',{type:'submit','data-action':'save'});actions.append(submit);
            if(!creating){
                if(!editing && staleReview(current))actions.append(button('重新批准当前 AI','reapprove-ai',async()=>{
                    if(!confirm('确认已核验当前 AI 标签、结论与证据，并批准此版本？'))return;
                    await save('PATCH',{revision:current.revision,review_status:'approved'});
                }));
                actions.append(button('重新读取详情','reload',async()=>{if(!confirm('重新读取会放弃当前未保存的修改，继续？'))return;editing=false;await open(current.id);}));
                actions.append(button('恢复 AI 来源','restore',async()=>{
                    if(!confirm('确认移除人工覆盖并恢复 AI 来源？冻结 AI 内容保留；未保存的人工编辑将丢失。'))return;
                    await save('PATCH',{revision:current.revision,manual:null});
                }));
                actions.append(button('归档（从门户隐藏）','archive',async()=>{
                    if(!confirm('确认归档此分析并从门户隐藏？此操作不会删除冻结证据。'))return;
                    await save('DELETE',{revision:current.revision});
                }));
            }
            form.append(actions);detail.append(form);
            form.addEventListener('submit',event=>{event.preventDefault();task(async()=>{
                if(client.busy)return;
                if(creating){
                    if(!selected)throw Error('请先从真实角色搜索结果中选择角色。');
                    await save('POST',{player_id:selected.id,manual:readManual(),review_status:'pending',is_published:false,review_note:''});
                }else{
                    const values={review_status:review.value,is_published:visibility.value==='true',review_note:note.value};
                    if(readManual){
                        values.manual=readManual();
                        const changed=JSON.stringify(values.manual)!==JSON.stringify(current.manual);
                        values.review_status=changed?'pending':current.review_status;
                        values.is_published=changed?false:current.is_published;
                    }
                    const patch=buildPatch(current,values);
                    if(Object.keys(patch).length===1){message.textContent='没有需要保存的变更。';return;}
                    await save('PATCH',patch);
                }
            });});
        }
        function newRecord() {
            if(client.busy)return;
            if(editing && !confirm('放弃未保存编辑并新增记录？'))return;
            ++detailSequence;current=null;creating=true;editing=true;selected=null;characterPage=1;renderDetail();detail.focus();
        }
        function renderCharacters(parent) {
            const section=el('section',null,{class:'pa-character-picker'});section.append(el('h3','选择真实角色'));
            const form=el('form',null,{class:'pa-actions'});
            const q=inputField(form,'角色 ID / 名字',characterFilters.q,{type:'search'});
            form.append(el('button','查找角色',{type:'submit'}));
            const selectedText=el('p',selected?'已选：'+playerText(selected,selected.id):'尚未选择角色。',{'data-selected-character':''});
            const list=el('div');section.append(form,selectedText,list);parent.append(section);
            async function search(page=1) {
                const seq=++characterSequence;characterFilters.q=q.value;
                const data=await client.request('characters/?'+new URLSearchParams({q:q.value,page,page_size:20}));
                if(seq!==characterSequence || !section.isConnected)return;
                characterPage=data.page;list.replaceChildren();
                if(!data.items.length)list.append(el('p','没有匹配角色，请调整 ID 或名字。'));
                data.items.forEach(player=>list.append(button(playerText(player,player.id),'select-character',()=>{selected=player;selectedText.textContent='已选：'+playerText(player,player.id);})));pageControls(list,data,search);
            }
            form.addEventListener('submit',event=>{event.preventDefault();task(()=>search(1));});
            // Search only on demand; no arbitrary typed name can become a player ID.
        }
        const ready=task(()=>loadList());
        return {ready,settled:()=>pending};
    }
    const api={Client,buildPatch,safeURL,mount};
    if(typeof module==='object' && module.exports)module.exports=api;
    else {global.PlayerAnalysisManagement=api;global.document.addEventListener('DOMContentLoaded',()=>{
        const root=global.document.getElementById('player-analysis-app');if(root)mount(root);
        const toggle=global.document.getElementById('analysis-theme-toggle');
        if(toggle)toggle.addEventListener('click',()=>{
            const dark=global.document.documentElement.dataset.dashboardTheme!=='dark';
            global.document.documentElement.dataset.dashboardTheme=dark?'dark':'light';toggle.setAttribute('aria-pressed',String(dark));
            try{global.localStorage.setItem('lmonitor-dashboard-theme',dark?'dark':'light');}catch(_){}
        });
    });}
})(typeof window==='undefined'?globalThis:window);
