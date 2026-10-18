/* Server-authoritative Dashboard panel. No analysis browser storage. */
(function (global) {
    'use strict';
    const BASE = '/dashboard/api/player-analyses/';
    const mounts = new WeakMap(), panels = new WeakMap();
    const names = {draft:'草稿',pending:'待处理',ready:'就绪',stale:'已过期',error:'失败',archived:'已归档',approved:'已批准',rejected:'已拒绝',manual:'人工维护',ai:'AI 分析',local_deterministic:'本地规则分析'};
    const label = value => names[value] || value || '—';
    // Database JSON object key order is not a content change (notably MySQL).
    function stableJSON(value) {
        if(Array.isArray(value))return '['+value.map(stableJSON).join(',')+']';
        if(value && typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+stableJSON(value[k])).join(',')+'}';
        return JSON.stringify(value);
    }
    function safeURL(value) {
        if (typeof value !== 'string' || !/^https?:\/\//i.test(value)) return '';
        try { const u = new URL(value); return ['http:', 'https:'].includes(u.protocol) ? value : ''; } catch (_) { return ''; }
    }
    function buildPatch(row, values) {
        const patch = {revision:row.revision};
        for (const key of ['manual','review_status','is_published','review_note']) {
            if (Object.hasOwn(values,key) && stableJSON(values[key]) !== stableJSON(row[key])) patch[key] = values[key];
        }
        const review = patch.review_status ?? row.review_status;
        const published = patch.is_published ?? row.is_published;
        const manual = Object.hasOwn(patch,'manual') ? patch.manual : row.manual;
        if (published && review !== 'approved' && (manual || patch.is_published)) throw Error('请先批准审核，再公开发布。');
        return patch;
    }
    class Client {
        constructor(fetcher, csrf, signal) { this.fetcher=fetcher; this.csrf=csrf; this.signal=signal; this.busy=false; }
        async request(path, method='GET', body) {
            const headers = {'Accept':'application/json'};
            if (method !== 'GET') { headers['Content-Type']='application/json'; headers['X-CSRFToken']=this.csrf(); }
            const response = await this.fetcher(BASE+path,{method,headers,credentials:'same-origin',cache:'no-store', ...(method === 'GET' && this.signal ? {signal:this.signal} : {}), ...(body === undefined ? {} : {body:JSON.stringify(body)})});
            const errors = {401:'登录已失效，请返回后台重新登录。',403:'权限不足或 CSRF 校验失败，请刷新页面并确认超级管理员权限。',409:'版本冲突：其他人已修改此记录。请重新读取详情后再操作；当前输入未保存。'};
            const failure=message=>Object.assign(Error(message),{httpStatus:response.status});
            if (errors[response.status]) throw failure(errors[response.status]);
            let result;
            try { result = await response.json(); } catch (_) { throw failure('服务器未返回有效 JSON，请检查登录状态后重试。'); }
            if (!response.ok || result.status !== 'success') throw failure(result.message || result.error || '请求失败，请稍后重试。');
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
        if (mounts.has(root)) return mounts.get(root);
        const doc=root.ownerDocument, win=doc.defaultView;
        const lifetime=new win.AbortController();
        const listen=(node,type,handler)=>node.addEventListener(type,handler,{signal:lifetime.signal});
        const client=new Client(options.fetch || win.fetch.bind(win),()=>doc.querySelector('meta[name="csrf-token"]')?.content || '',lifetime.signal);
        const confirm=options.confirm || win.confirm.bind(win);
        let destroyed=false, active=true, drawerOpen=false, returnFocus=null, returnID=null, baseline='', previousOverflow='';
        const checks=new Map();
        const fingerprint=()=>JSON.stringify(Array.from(detail.querySelectorAll('input,textarea,select')).map(n=>[n.dataset.field || n.dataset.tagField || n.dataset.evidenceField,n.value]));
        const dirty=()=>drawerOpen && fingerprint()!==baseline;
        function showDrawer() {
            drawerOpen=true;detail.hidden=!active;backdrop.hidden=!active;
            if(active){previousOverflow=doc.body.style.overflow;doc.body.style.overflow='hidden';messageSlot.append(message);}
        }
        function hideDrawer(discard=false) {
            if(client.busy)return;
            if(discard && dirty() && !confirm('放弃未保存的修改并关闭审查？'))return;
            drawerOpen=false;detail.hidden=true;backdrop.hidden=true;doc.body.style.overflow=previousOverflow;
            root.prepend(message);++detailSequence;
            if(discard){current=null;creating=false;editing=false;detail.replaceChildren();}
            const target=returnFocus?.isConnected?returnFocus:results.querySelector(`[data-row-id="${returnID}"] [data-action="open"]`) || query;
            target.focus();
        }
        function setActive(value) {
            if(active===value)return;active=value;
            detail.hidden=!active || !drawerOpen;backdrop.hidden=detail.hidden;
            if(drawerOpen){if(active){previousOverflow=doc.body.style.overflow;doc.body.style.overflow='hidden';}else doc.body.style.overflow=previousOverflow;}
            checks.forEach((check,id)=>{win.clearTimeout(check.timer);if(active && doc.visibilityState!=='hidden')task(()=>pollCheck(id));});
        }
        function checkBusy(check){return check && (check.submitting || ['queued','running','unknown'].includes(check.state));}
        function updateChecks() {
            root.querySelectorAll('[data-action="recheck"]').forEach(b=>{const c=checks.get(Number(b.dataset.id));b.disabled=b.dataset.allowed==='false'||checkBusy(c)||client.busy;b.textContent=checkBusy(c)?'检查进行中…':'重新检查';});
            const status=detail.querySelector('[data-check-status]');
            if(status){const c=checks.get(current?.id);status.textContent=c ? `${({idle:'尚未重查',queued:'已排队',running:'正在检查',succeeded:'检查完成',failed:'检查失败',blocked:'检查受阻',unknown:'状态待确认',unconfirmed:'本次提交未确认',rejected:'提交被拒绝'})[c.state] || '检查状态'} · ${c.notice || ''}${c.message || ''}` : '重新检查只处理当前角色；人工草稿与冻结历史不会被直接覆盖。';}
        }
        function recheckButton(row) {
            const b=button('重新检查','recheck',()=>startCheck(row));b.dataset.id=row.id;b.dataset.allowed=String(row.can_recheck!==false);b.title=row.can_recheck===false?'此角色当前不能重新检查':'重新采集并分析此角色';return b;
        }
        async function pollCheck(id) {
            const check=checks.get(id);if(!check || check.reading || check.submitting || !active || doc.visibilityState==='hidden' || destroyed)return;
            win.clearTimeout(check.timer);check.reading=true;
            try {
                const data=await client.request(id+'/recheck/');if(destroyed || checks.get(id)!==check)return;
                // A changed server task is observable even when the POST response
                // was lost; observing it does not prove who submitted it.
                if(['unknown','rejected'].includes(check.submission)){
                    const changed=Boolean(data.token && data.token!==check.previousToken);
                    const liveWithoutToken=!check.previousToken && ['queued','running'].includes(data.state);
                    if(!changed && !(check.submission==='unknown' && liveWithoutToken)){
                        if(check.submission==='unknown'){check.state='unconfirmed';check.message='没有确认到新的检查；不会自动重复提交，请刷新状态核实。';}
                        updateChecks();return;
                    }
                    check.notice=check.submission==='rejected'?'上次提交被拒绝；以下为服务器最新检查。':'本次提交归属未确认；以下为服务器最新检查。';
                    check.submission='observing';check.expectedToken=data.token;
                }
                if(check.expectedToken && data.token!==check.expectedToken){
                    if(check.previousToken && data.token===check.previousToken){
                        check.state='unknown';check.message='收到旧任务响应，正在重新核对当前任务。';updateChecks();return;
                    }
                    // The endpoint stores only the latest job: an unseen successor
                    // must not leave this tab waiting forever for an obsolete token.
                    check.notice='原检查已被后续状态替代；以下为服务器最新检查。';
                    check.submission='observing';check.expectedToken=data.token;
                }
                const terminalKey=JSON.stringify([data.token || '',data.state,data.finished_at || data.message || '']);
                Object.assign(check,data);updateChecks();
                if(['succeeded','failed','blocked'].includes(data.state) && check.readbackKey!==terminalKey) {
                    const row=await client.request(id+'/');if(destroyed || checks.get(id)!==check)return;check.readbackKey=terminalKey;
                    if(current?.id===id && !dirty() && !client.busy){current=row;renderDetail();}
                    else if(current?.id===id){message.textContent='检查状态已更新；未保存草稿已保留。保存前请核对新版本，版本冲突时先复制草稿再重新读取。';}
                    await loadList(listPage);
                }
            } catch(error){check.message='状态读取失败，请恢复页面后重试：'+error.message;updateChecks();}
            finally {
                check.reading=false;
                if(!destroyed && checks.get(id)===check && active && doc.visibilityState!=='hidden' && checkBusy(check))check.timer=win.setTimeout(()=>task(()=>pollCheck(id)),options.pollInterval || 2500);
            }
        }
        async function startCheck(row) {
            if(client.busy || checkBusy(checks.get(row.id)) || row.can_recheck===false)return;
            const previous=checks.get(row.id);win.clearTimeout(previous?.timer);
            const check={state:'unknown',message:'正在提交…',submitting:true,readback:false,submission:'unknown',previousToken:previous?.token || previous?.previousToken || row.recheck?.token};checks.set(row.id,check);updateChecks();
            try {
                const data=await client.request(row.id+'/recheck/','POST',{revision:row.revision});
                Object.assign(check,data);check.submission='accepted';check.expectedToken=data.token;
            }
            catch(error){
                const rejected=error.httpStatus>=400 && error.httpStatus<500;
                if(rejected){check.state='rejected';check.submission='rejected';}
                check.message=(rejected?'提交被拒绝，未启动本次检查。':'本次提交未确认；不会自动重复提交。')+error.message;message.textContent=check.message;message.dataset.kind='error';
            }
            finally {check.submitting=false;updateChecks();if(check.submission!=='rejected')await pollCheck(row.id);}
        }
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
            listen(b,'click',()=>task(run));return b;
        }
        function task(run) {
            pending=Promise.resolve().then(()=>{if(!destroyed)return run();}).catch(error=>{
                if(!destroyed){message.textContent=error.message;message.dataset.kind='error';}
            });
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
        function investigationView(parent, ai) {
            // Only read the detail API's frozen publication. Opening this view
            // must not discover identities, fetch sources or schedule questions.
            const object=value=>value && typeof value==='object' && !Array.isArray(value);
            const array=value=>Array.isArray(value)?value:[];
            const text=value=>typeof value==='string' || typeof value==='number'?String(value):'';

            const ids=value=>array(value).map(text).filter(Boolean);
            const facts=ai?.evidence_json?.metrics?.investigation;
            const model=ai?.result_json?.investigation;
            const hasFacts=object(facts) && Object.keys(facts).length>0;
            const hasModel=object(model) && Object.keys(model).length>0;
            if(!hasFacts && !hasModel)return; // Old records keep their existing UI.
            const frozen=hasFacts?facts:{}, result=hasModel?model:{};
            const evidence=array(frozen.evidence).filter(item=>object(item) && text(item.id));
            const hypotheses=array(frozen.hypotheses).filter(item=>object(item) && text(item.id));
            const questions=array(frozen.questions).filter(item=>object(item) && text(item.id));
            const evidenceByID=new Map(evidence.map(item=>[text(item.id),item]));
            const hypothesesByID=new Map(hypotheses.map(item=>[text(item.id),item]));
            const questionsByID=new Map(questions.map(item=>[text(item.id),item]));
            const coreMembers=ids(frozen.core_members);
            const questionName=key=>questionsByID.has(key)?`问题 ${questions.findIndex(item=>text(item.id)===key)+1}`:'未提供的冻结问题';
            const states={supported:'有支持线索，尚未证实',weakened:'有反证，解释被削弱',
                unresolved:'尚待核查',uncertain:'尚不确定',possible:'可能解释，尚未证实',
                compatible:'与现有事实相容，尚未证实',mixed:'支持与反证并存，尚无定论',
                unsupported:'现有证据未支持（不等于排除）',unknown:'未知',inconclusive:'尚无定论'};
            const status=value=>Object.hasOwn(states,text(value))?states[text(value)]:(text(value)?`记录状态：${text(value)}（不代表已证实）`:'状态未提供');
            const reasons={repeated_below_own_current_with_stable_core:'本人重复低于当前同本成绩上限，且存在稳定核心',
                no_recent_signal:'当前近期样本未触发调查（不代表没有此类行为）',
                invalid_identity_or_stale_source:'身份或来源时效不足，无法启动调查',
                evidence_sufficient_for_comparison:'现有证据足以比较候选解释（不代表动机已证实）',
                targeted_scores_needed:'仍需定向核查相关成员当前计分记录',not_triggered:'未触发定向调查'};
            const reason=value=>Object.hasOwn(reasons,text(value))?`${reasons[text(value)]}（${text(value)}）`:text(value);
            const section=el('section',null,{class:'pa-investigation','data-investigation':'','aria-label':'调查依据与剩余问题'});
            section.append(el('h3','调查依据与剩余问题（冻结记录 · 只读）'),
                el('p','已有线索、尚未证实：候选解释和 AI 判断不是确定的带人或商业身份结论。',{class:'pa-warning'}),
                el('p','当前成绩仅作采集时点的代理，不代表完成当场加分；付款与钥匙归属仍属未知，不能从分差或阵容推定。',{class:'pa-muted'}));
            function block(title,attrs={}) {
                const node=el('section',null,{class:'pa-investigation-block',...attrs});
                node.append(el('h4',title));section.append(node);return node;
            }
            function line(node,title,value,fallback='未提供') {
                const p=el('p');p.append(el('strong',title+'：'),el('span',text(value) || fallback));node.append(p);
            }
            function evidenceItem(item) {
                const li=el('li');li.append(el('strong',text(item.id)+' · '),el('span',text(item.text) || '证据文字未提供'));
                const url=safeURL(item.run_url);
                if(url)li.append(el('a','查看来源',{href:url,target:'_blank',rel:'noopener noreferrer'}));
                return li;
            }
            function references(node,title,values) {
                const group=el('div',null,{class:'pa-investigation-refs'});group.append(el('strong',title));
                const keys=ids(values);
                if(!keys.length)group.append(el('p','未提供相关引用（不代表不存在）',{class:'pa-muted'}));
                else {
                    const ul=el('ul',null,{class:'pa-evidence'});
                    keys.forEach(key=>{
                        const item=evidenceByID.get(key), question=questionsByID.get(key);
                        ul.append(item?evidenceItem(item):el('li',question?`${questionName(key)} · 待核查：${text(question.reason) || '原因未提供'}`:`${key} · 冻结引用未提供，无法核验`));
                    });group.append(ul);
                }
                node.append(group);
            }
            const grounds=block('为什么开始调查 / 冻结事实',{'data-investigation-facts':''});
            if(!hasFacts)grounds.append(el('p','冻结调查依据未提供，无法据此核验模型解释。',{class:'pa-warning'}));
            else {
                line(grounds,'触发状态',frozen.triggered===true?'已由已有线索触发定向调查':frozen.triggered===false?'未触发定向调查':'触发状态未提供');
                line(grounds,'触发原因',reason(frozen.trigger_reason),'触发原因未提供');
                line(grounds,'调查范围',frozen.scope);
                line(grounds,'起始场次',ids(frozen.seed_run_ids).join('、'));
                line(grounds,'核心成员',coreMembers.length?`${coreMembers.length} 名（身份引用保存在冻结记录）`:'未提供');
                if(evidence.length){const ul=el('ul',null,{class:'pa-evidence'});evidence.forEach(item=>ul.append(evidenceItem(item)));grounds.append(ul);}
                else grounds.append(el('p','未提供可展示的冻结证据。',{class:'pa-muted'}));
            }
            const alternatives=block('多种候选解释 / 支持与反证',{'data-investigation-hypotheses':''});
            alternatives.append(el('p','以下为基于冻结事实的候选解释，不是已验证的行为身份。',{class:'pa-muted'}));
            if(!hypotheses.length)alternatives.append(el('p','未保存候选解释。',{class:'pa-muted'}));
            hypotheses.forEach(item=>{
                const card=el('article',null,{class:'pa-investigation-card','data-investigation-hypothesis':text(item.id)});
                card.append(el('h5',`${text(item.label) || '未命名解释'} · ${text(item.id)}`));
                line(card,'线索状态',status(item.status));
                references(card,'支持依据',item.support_ids);references(card,'反证',item.counter_ids);references(card,'未知 / 缺口',item.unknown_ids);
                alternatives.append(card);
            });
            const interpretation=block('AI 模型解释（不是已验证事实）',{'data-investigation-model':'',class:'pa-investigation-block pa-investigation-model'});
            if(!hasModel)interpretation.append(el('p','未保存模型调查解释；仅展示上方冻结依据。',{class:'pa-muted'}));
            else {
                line(interpretation,'模型概述',result.summary,'模型概述未提供');
                line(interpretation,'模型置信度',result.confidence);
                array(result.hypotheses).filter(object).forEach(item=>{
                    const key=text(item.id), hypothesis=hypothesesByID.get(key);
                    const card=el('article',null,{class:'pa-investigation-card','data-model-hypothesis':key});
                    card.append(el('h5',`${text(hypothesis?.label) || '冻结候选解释未提供'} · ${key || 'ID 未提供'}`));
                    line(card,'模型判断',status(item.assessment));line(card,'模型解释',item.explanation);
                    // evidence_ids may cite support OR gaps. Membership is
                    // scoped to this frozen hypothesis, never the global facts.
                    const supportIDs=new Set(ids(hypothesis?.support_ids));
                    const counterIDs=new Set(ids(hypothesis?.counter_ids));
                    const unknownIDs=new Set(ids(hypothesis?.unknown_ids));
                    const support=[], counter=[], unknown=new Set(), unrecognized=new Set();
                    function classify(values,allowed,matched) {
                        ids(values).forEach(id=>{
                            if(unknownIDs.has(id))unknown.add(id);
                            else if(allowed.has(id) && evidenceByID.has(id))matched.push(id);
                            else unrecognized.add(id);
                        });
                    }
                    classify(item.evidence_ids,supportIDs,support);
                    classify(item.counter_evidence_ids,counterIDs,counter);
                    references(card,'模型引用的支持依据',support);
                    references(card,'模型引用的反证',counter);
                    references(card,'模型引用的未知 / 缺口',Array.from(unknown));
                    if(unrecognized.size)references(card,'未识别 / 无法核验引用（不作为支持或反证）',Array.from(unrecognized));
                    interpretation.append(card);
                });
            }
            const remaining=block('待核查问题和停止原因',{'data-investigation-questions':''});
            remaining.append(el('p','这里只展示已保存的问题；打开抽屉不会启动采集，也不会自动执行模型建议。',{class:'pa-muted'}));
            if(!questions.length)remaining.append(el('p','未提供待核查问题，不代表所有解释已解决。',{class:'pa-muted'}));
            questions.forEach(item=>{
                const card=el('article',null,{class:'pa-investigation-card','data-investigation-question':text(item.id),'data-identity-digest':text(item.identity_digest)});
                card.append(el('h5',questionName(text(item.id))));line(card,'核查原因',item.reason);
                line(card,'类型',item.kind==='scored_runs'?'当前计分记录（不是历史加分）':item.kind);
                line(card,'优先级（冻结值）',item.priority);
                line(card,'目标成员',text(item.identity_digest)?(coreMembers.includes(text(item.identity_digest))?'核心成员（冻结身份已关联）':'相关成员（冻结身份已关联）'):'身份未提供');
                line(card,'相关场次',ids(item.run_ids).join('、'));
                line(card,'关联解释',ids(item.hypothesis_ids).map(key=>`${text(hypothesesByID.get(key)?.label) || '候选解释未提供'} · ${key}`).join('；'));
                remaining.append(card);
            });
            if(hasModel)line(remaining,'模型建议后续问题（未执行）',ids(result.next_question_ids).map(key=>`${questionName(key)} · ${text(questionsByID.get(key)?.reason) || '冻结问题未提供，无法核验'}`).join('；'),'未提供');
            line(remaining,'停止原因',reason(frozen.stop_reason),'停止原因未提供；不能据此判定调查已完成');
            parent.append(section);
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
        function playerText(player,id,character) {
            const identity=player || character;
            if(!identity)return `角色 #${id ?? '—'}（角色资料不可用） · 赛季未知`;
            const profession=player ? `${player.class_label || player.class_name || '职业未知'} ${player.spec_label || player.spec_name || '专精未知'}` : '角色资料不可用';
            return `${identity.character_name} · ${identity.region || ''} ${identity.realm || ''} · ${profession} · #${player?.id ?? id ?? '—'} · ${identity.season_name || '赛季未知'}`;
        }
        function refreshTagOptions(values) {
            const selected=selects.tag.value;
            const choices=new Set((Array.isArray(values)?values:[]).filter(v=>typeof v==='string' && v));
            // A narrower search must not silently discard the active filter.
            if(selected)choices.add(selected);
            selects.tag.replaceChildren(el('option','全部',{value:''}),...Array.from(choices,v=>el('option',v,{value:v})));
            selects.tag.value=selected;
        }
        function listTags(tags) {
            const group=el('div',null,{class:'pa-list-tags','aria-label':'分析标签'});
            if(!Array.isArray(tags) || !tags.length){group.append(el('span','暂无标签',{class:'pa-muted'}));return group;}
            tags.forEach(tag=>{
                const text=typeof tag==='string'?tag:[tag.label,tag.value].filter(Boolean).join(' · ');
                const confidence=tag.confidence_label || tag.confidence;
                group.append(el('span',text+(confidence?` · 置信度：${label(confidence)}`:''),{class:'pa-badge pa-list-tag'}));
            });
            return group;
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
        selects.tag=selectField(filterForm,'分析标签','','tag',[['','全部']]);
        const pageSize=selectField(filterForm,'每页','20','page_size',[['20','20'],['50','50']]);
        const search=el('button','搜索',{type:'submit'});filterForm.append(search,button('新增人工分析','new',()=>newRecord()));
        listen(filterForm,'submit',event=>{event.preventDefault();task(()=>loadList(1));});
        const results=el('section',null,{'aria-label':'分析列表',class:'pa-results'});
        const backdrop=el('div',null,{class:'pa-backdrop','aria-hidden':'true'});backdrop.hidden=true;
        const detail=el('section',null,{'aria-label':'分析详情',role:'dialog','aria-modal':'true',class:'pa-detail',tabindex:'-1'});detail.hidden=true;
        const messageSlot=el('div',null,{class:'pa-drawer-message'});
        root.append(message,filterForm,results,backdrop,detail);
        listen(backdrop,'click',()=>hideDrawer(true));
        listen(doc,'keydown',event=>{
            if(!drawerOpen || !active)return;
            if(event.key==='Escape'){event.preventDefault();hideDrawer(true);}
            if(event.key==='Tab'){
                const nodes=Array.from(detail.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],summary')).filter(n=>!n.closest('[hidden]'));
                const first=nodes[0],last=nodes.at(-1);
                if(event.shiftKey && (doc.activeElement===first || doc.activeElement===detail)){event.preventDefault();last?.focus();}
                else if(!event.shiftKey && (doc.activeElement===last || !detail.contains(doc.activeElement))){event.preventDefault();first?.focus();}
            }
        });
        listen(doc,'visibilitychange',()=>{checks.forEach((c,id)=>{win.clearTimeout(c.timer);if(active && doc.visibilityState!=='hidden')task(()=>pollCheck(id));});});
        listen(win,'beforeunload',event=>{if(dirty()){event.preventDefault();event.returnValue='';}});
        async function loadList(page=1) {
            const sequence=++readSequence;
            Object.assign(filters,{q:query.value,page:String(page),page_size:pageSize.value});
            for (const key of Object.keys(selects)) filters[key]=selects[key].value;
            results.setAttribute('aria-busy','true');
            try {
                const data=await client.request('?'+new URLSearchParams(filters));
                if(sequence!==readSequence)return;
                refreshTagOptions(data.tag_options);
                listPage=data.page;results.replaceChildren(el('h2','角色分析'));
                if(!data.items.length)results.append(el('p','没有匹配的分析记录。'));
                data.items.forEach(row=>{
                    const card=el('article',null,{class:'pa-list-row','data-row-id':row.id});
                    const info=el('div');info.append(el('strong',playerText(row.player,row.player_id,row.character)));
                    const badges=el('div',null,{class:'pa-badges'});
                    [row.status,row.review_status,row.source_kind,row.is_published?'已发布':'隐藏'].forEach(value=>badges.append(el('span',label(value),{class:'pa-badge','data-state':value})));
                    info.append(badges,listTags(row.tags));
                    if(staleReview(row))info.append(el('strong','AI 证据或结论已更新，原审核不适用。',{class:'pa-warning'}));
                    const actions=el('div',null,{class:'pa-row-actions'});
                    actions.append(button('审查详情','open',()=>open(row.id)),button('人工编辑','edit-row',()=>open(row.id,true)),recheckButton(row));
                    if(row.recheck && !checks.has(row.id))checks.set(row.id,{...row.recheck});
                    card.append(info,actions);results.append(card);
                });pageControls(results,data,loadList);updateChecks();
                checks.forEach((c,id)=>{if(checkBusy(c) && !c.timer && !c.reading && !c.submitting)task(()=>pollCheck(id));});
            } finally { if(sequence===readSequence)results.removeAttribute('aria-busy'); }
        }
        async function open(id,focusEditor=false,force=false) {
            if(client.busy)return;
            if(!force && current?.id===id && drawerOpen){detail.focus();if(focusEditor)detail.querySelector('[data-field="conclusion"]')?.focus();return;}
            if(dirty() && !confirm('离开将放弃未保存的修改，继续？'))return;
            if(!detail.contains(doc.activeElement)){returnFocus=doc.activeElement;returnID=id;}
            const seq=++detailSequence;
            message.textContent='正在读取详情…';
            const row=await client.request(id+'/');
            if(seq!==detailSequence)return;
            current=row;creating=false;editing=false;selected=null;renderDetail();message.textContent='';detail.scrollTop=0;detail.focus();
            if(focusEditor)detail.querySelector('[data-field="conclusion"]')?.focus();
            if(!checks.has(id))checks.set(id,{state:'idle',...row.recheck});await pollCheck(id);
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
                if(!value.tags.length && !value.conclusion){if(!creating && !current?.manual && !value.evidence.length)return null;throw Error('请填写至少一个标签或综合结论。清空已有覆盖请使用“清空人工覆盖”操作。');}
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
                if(destroyed)return;
                current=row;creating=false;editing=false;renderDetail();
                message.textContent='已保存，并已从服务器重新读取记录。';message.dataset.kind='success';
                try { await loadList(listPage); } catch(error) {message.textContent+=' 列表刷新失败：'+error.message;}
            } finally {if(!destroyed){setBusy(false);updateChecks();}}
        }
        function renderDetail() {
            const alreadyOpen=drawerOpen, restoreFocus=detail.contains(doc.activeElement);
            detail.replaceChildren();
            const header=el('header',null,{class:'pa-drawer-header'});
            header.append(el('h2',creating?'新增人工分析':`审查分析 #${current.id}`),button('关闭 ×','close',()=>hideDrawer(true)));
            detail.append(header,messageSlot);
            if(!alreadyOpen)showDrawer();else{detail.hidden=!active;messageSlot.append(message);}
            if(creating)renderCharacters(detail);
            else {
                detail.append(el('p',playerText(current.player,current.player_id,current.character)),el('p',`版本 ${current.revision} · ${label(current.status)} · ${label(current.source_kind)} · ${label(current.review_status)} · ${current.is_published?'已发布':'隐藏'} · 活动 #${current.activity_id ?? '—'}`));
                detail.append(el('p',`生成时间：${current.generated_at || '—'} · 审核时间：${current.reviewed_at || '—'}`,{class:'pa-muted'}));
                if(staleReview(current))detail.append(el('p','AI 证据或结论已更新，原审核不适用于当前 AI；请重新审核。',{'data-review-stale':'',role:'note'}));
                const columns=el('div',null,{class:'pa-projections'});
                projection(columns,'AI 原始标签 / 结论 / 冻结证据（只读）',current.ai);
                detail.append(columns);
                investigationView(detail,current.ai);
                const raw=el('details');raw.append(el('summary','展开 AI 原始 JSON（仅供核验）'));
                listen(raw,'toggle',()=>{if(raw.open && !raw.querySelector('pre'))raw.append(el('pre',JSON.stringify({result_json:current.ai?.result_json,evidence_json:current.ai?.evidence_json},null,2)));});detail.append(raw);
            }
            const form=el('form',null,{class:'pa-edit-form'});
            const manualSlot=el('div',null,{class:'pa-manual-slot'});
            const tools=el('div',null,{class:'pa-actions'});
            tools.append(el('h3','人工编辑'),button('定位人工结论','edit-manual',()=>manualSlot.querySelector('[data-field="conclusion"]').focus()));
            let readManual=manualEditor(manualSlot,current?.manual);
            if(!creating && current.ai)tools.append(button('从 AI 副本填入','copy-ai',()=>{
                if(dirty() && !confirm('使用 AI 副本替换当前人工编辑区？尚未保存的人工输入会被替换，AI 原文不变。'))return;
                const ai=current.ai;
                const cleanEvidence=items=>(Array.isArray(items)?items:[]).map(item=>({text:typeof item==='string'?item:item.text || '',url:safeURL(item.url || item.run_url || item.source_url)}));
                const copy={tags:(ai.tags || []).map(t=>({label:t.label || '',value:t.value || '',reason:t.reason || '',evidence:cleanEvidence(t.evidence)})),conclusion:ai.conclusion || '',evidence:cleanEvidence(ai.evidence)};
                manualSlot.replaceChildren();readManual=manualEditor(manualSlot,copy);manualSlot.querySelector('[data-field="conclusion"]').focus();
            }));
            form.append(tools,manualSlot);
            const review=selectField(form,'审核决定',current?.review_status || 'pending','review_status',[['pending','待审核'],['approved','批准'],['rejected','拒绝']]);
            const visibility=selectField(form,'发布状态',String(current?.is_published || false),'is_published',[['false','隐藏'],['true','公开（须批准审核）']]);
            const note=inputField(form,'审核意见',current?.review_note || '',{'data-field':'review_note',rows:3},'textarea');
            if(creating){review.value='pending';visibility.value='false';review.disabled=true;visibility.disabled=true;}
            if(creating)note.disabled=true;
            form.append(el('p','人工内容变更将保存为待审核、隐藏状态，须保存后重新批准；未修改人工内容时仅更新审核决定。AI 冻结原文不会被改写。',{class:'pa-muted'}));
            const actions=el('div',null,{class:'pa-actions'});
            const submit=el('button',creating?'保存人工草稿':'保存变更',{type:'submit','data-action':'save'});actions.append(submit);
            if(!creating){
                if(staleReview(current))actions.append(button('重新批准当前 AI','reapprove-ai',async()=>{
                    if(dirty()){message.textContent='请先保存或放弃未保存的修改，再重新批准。';return;}
                    if(!confirm('确认已核验当前 AI 标签、结论与证据，并批准此版本？'))return;
                    await save('PATCH',{revision:current.revision,review_status:'approved'});
                }));
                actions.append(button('重新读取详情','reload',async()=>{if(!confirm('重新读取会放弃当前未保存的修改，继续？'))return;baseline=fingerprint();editing=false;await open(current.id,false,true);}));
                actions.append(button('清空人工覆盖 · 恢复 AI','restore',async()=>{
                    if(!confirm('确认移除人工覆盖并恢复 AI 来源？冻结 AI 内容保留；未保存的人工编辑将丢失。'))return;
                    await save('PATCH',{revision:current.revision,manual:null});
                }));
                actions.append(button('归档（从门户隐藏）','archive',async()=>{
                    if(!confirm('确认归档此分析并从门户隐藏？此操作不会删除冻结证据，但会放弃未保存的修改。'))return;
                    await save('DELETE',{revision:current.revision});
                }));
            }
            form.append(actions);detail.append(form);
            if(!creating){const checkSection=el('section',null,{class:'pa-recheck'});checkSection.append(recheckButton(current),button('刷新检查状态','refresh-check',()=>pollCheck(current.id)),el('p','',{'data-check-status':'',role:'status'}));detail.insertBefore(checkSection,form);}
            baseline=fingerprint();updateChecks();if(restoreFocus && active)detail.focus();
            listen(form,'submit',event=>{event.preventDefault();task(async()=>{
                if(client.busy)return;
                if(creating){
                    if(!selected)throw Error('请先从真实角色搜索结果中选择角色。');
                    await save('POST',{player_id:selected.id,manual:readManual(),review_status:'pending',is_published:false,review_note:''});
                }else{
                    const values={review_status:review.value,is_published:visibility.value==='true',review_note:note.value};
                    if(readManual){
                        const manual=readManual();
                        const changed=stableJSON(manual)!==stableJSON(current.manual);
                        if(changed){values.manual=manual;values.review_status='pending';values.is_published=false;}
                    }
                    const patch=buildPatch(current,values);
                    if(Object.keys(patch).length===1){message.textContent='没有需要保存的变更。';return;}
                    await save('PATCH',patch);
                }
            });});
        }
        function newRecord() {
            if(client.busy)return;
            if(dirty() && !confirm('放弃未保存编辑并新增记录？'))return;
            returnFocus=doc.activeElement;
            ++detailSequence;current=null;creating=true;editing=true;selected=null;characterPage=1;renderDetail();detail.scrollTop=0;detail.focus();
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
            listen(form,'submit',event=>{event.preventDefault();task(()=>search(1));});
            // Search only on demand; no arbitrary typed name can become a player ID.
        }
        const ready=task(()=>loadList());
        const controller={ready,settled:()=>pending,setActive,destroy:()=>{
            if(destroyed)return;
            destroyed=true;++readSequence;++detailSequence;++characterSequence;
            // Cancel reads and detach listeners, never cancel/replay a submitted write.
            checks.forEach(c=>win.clearTimeout(c.timer));if(drawerOpen && active)doc.body.style.overflow=previousOverflow;
            lifetime.abort();mounts.delete(root);root.replaceChildren();root.removeAttribute('aria-busy');
        }};
        mounts.set(root,controller);
        return controller;
    }
    function initDashboard(doc) {
        const section=doc.getElementById('player-analyses');
        const root=section?.querySelector('#player-analysis-app');
        if(!root)return null;
        if(panels.has(section))return panels.get(section);
        const win=doc.defaultView;
        let app=null, destroyed=false;
        // Follow the existing Dashboard workspace lifecycle (AI/B站): observe
        // the router's visibility, not a second set of navigation listeners.
        const activate=()=>{
            const visible=section.classList.contains('active') || section.style.display==='block';
            if(visible)app=mount(root);
            app?.setActive(visible);
        };
        const observer=new win.MutationObserver(activate);
        const destroy=()=>{
            if(destroyed)return;
            destroyed=true;
            observer.disconnect();win.removeEventListener('pagehide',onPageHide);
            app?.destroy();app=null;panels.delete(section);
        };
        const onPageHide=event=>{if(!event.persisted)destroy();};
        const panel={destroy};
        panels.set(section,panel);
        observer.observe(section,{attributes:true,attributeFilter:['class','style']});
        win.addEventListener('pagehide',onPageHide);
        // Hiding retains filters, selected record and unsaved edits; returning
        // reuses the mount and any in-flight request instead of fetching twice.
        activate();
        return panel;
    }
    const api={Client,buildPatch,safeURL,mount,initDashboard};
    if(typeof module==='object' && module.exports)module.exports=api;
    else {
        global.PlayerAnalysisManagement=api;
        if(global.document.readyState==='loading')global.document.addEventListener('DOMContentLoaded',()=>initDashboard(global.document),{once:true});
        else initDashboard(global.document);
    }
})(typeof window==='undefined'?globalThis:window);
