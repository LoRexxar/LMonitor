(() => {
  'use strict';
  const root = document.getElementById('class-guide-workspace');
  if (!root) return;
  const $ = id => root.querySelector('#' + id), api = '/api/dashboard/class-guides/';
  const state = {catalog:null, guide:null, dirty:false, page:1, termPage:1, terms:[]};
  const names = {html:'正文',heading:'章节标题',tabs:'选项卡组',tab:'方案',columns:'分栏',column:'栏目',accordion:'折叠组',details:'折叠说明',callout:'提示',rating:'能力评级',changelog:'更新记录',code:'宏',image:'图片',talents:'天赋',gear:'配装',rotation:'技能循环',priority:'优先级',timeline:'时间轴',simulation:'模拟结果',group:'组合',separator:'分隔线',unsupported:'待转换'};
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const plain = value => { const node = document.createElement('div'); node.innerHTML = value || ''; return node.textContent || ''; };
  function notify(message, error=false) { $('message').textContent=message; $('message').classList.toggle('error',error); $('message').hidden=false; }
  async function request(path='', options={}) { const csrf = document.cookie.split('; ').find(v=>v.startsWith('csrftoken='))?.slice(10) || ''; const response=await fetch(api+path,{...options,headers:{'Content-Type':'application/json','X-CSRFToken':csrf,...options.headers}}); const data=await response.json(); if(!response.ok) throw new Error(data.error || data.message || `请求失败 ${response.status}`); return data; }
  const guard = fn => async (...args) => { try {await fn(...args);} catch(error){notify(error.message,true);} };
  function options(select, values, blank=true) { const current=select.value; select.innerHTML=(blank?'<option value="">全部</option>':'')+Object.entries(values).map(([key,label])=>`<option value="${escape(key)}">${escape(label)}</option>`).join(''); if([...select.options].some(v=>v.value===current))select.value=current; }

  function tagSuggestions(names){$('guide-tag-options').innerHTML=names.map(name=>`<option value="${escape(name)}"></option>`).join('');}
  const tagEditors={};
  for(const id of ['article-tags','create-tags']){
    const host=$(id);let tags=[];
    host.innerHTML='<div class="guide-tag-chips"></div><div class="guide-tag-entry"><input type="text" maxlength="60" list="guide-tag-options" placeholder="输入或选择标签，回车添加" aria-label="输入标签"><button type="button" class="small secondary">添加</button></div>';
    const chips=host.querySelector('.guide-tag-chips'),input=host.querySelector('input');
    const render=()=>{chips.innerHTML=tags.map((name,i)=>`<span class="guide-tag">${escape(name)}<button type="button" data-guide-tag-remove="${i}" aria-label="移除标签 ${escape(name)}">×</button></span>`).join('');};
    const changed=()=>{if(id==='article-tags')markDirty();};
    const add=()=>{const name=input.value.trim();if(!name)return;if(name.length>60||tags.length>=30)throw new Error('每篇最多 30 个标签，每个最多 60 字符');if(!tags.some(t=>t.toLocaleLowerCase()===name.toLocaleLowerCase())){tags.push(name);changed();}input.value='';render();};
    host.querySelector('.guide-tag-entry button').addEventListener('click',guard(add));
    input.addEventListener('input',changed);
    input.addEventListener('keydown',guard(e=>{if(e.key==='Enter'&&!e.isComposing){e.preventDefault();add();}}));
    chips.addEventListener('click',e=>{const button=e.target.closest('[data-guide-tag-remove]');if(button){tags.splice(Number(button.dataset.guideTagRemove),1);render();changed();}});
    tagEditors[id]={set(values){tags=[...values];input.value='';render();},values(){add();return [...tags];}};
  }
  const authorFields=['name','title','avatar','bio','links'];
  function loadAuthor(data){
    const source=$('author-use-source').checked, p=source?(data.source_author_profile?.name?data.source_author_profile:{name:data.author}):(data.author_profile||data.display_author_profile||{});
    for(const key of authorFields)$('author-'+key).value=key==='links'?(p.links||[]).map(l=>l.label+' | '+l.url).join('\n'):p[key]||'';
    $('author-custom-fields').disabled=source;
  }
  function authorPayload(){
    if($('author-use-source').checked)return null;
    const p=Object.fromEntries(authorFields.filter(k=>k!=='links').map(k=>[k,$('author-'+k).value.trim()]));
    p.links=$('author-links').value.split('\n').filter(l=>l.trim()).map(line=>{const split=line.indexOf('|');if(split<1)throw new Error('作者链接格式应为：名称 | https://…');return {label:line.slice(0,split).trim(),url:line.slice(split+1).trim()};});
    return p;
  }
  $('author-use-source').addEventListener('change',()=>{if($('author-use-source').checked)loadAuthor(state.guide);else $('author-custom-fields').disabled=false;markDirty();});
  authorFields.forEach(key=>$('author-'+key).addEventListener('input',markDirty));
  function markDirty(){state.dirty=true;$('dirty-state').textContent='有未保存修改';}
  async function loadCatalog(){const params=new URLSearchParams(new FormData($('filters')));params.set('page',state.page);const data=await request('?'+params);state.catalog=data;options($('filters').elements.class_name,data.classes);options($('filters').elements.tag,Object.fromEntries(data.tags.map(v=>[v,v])));tagSuggestions(data.tags);updateSpecs($('filters'),true);updateSpecs($('create-form'),false);
    $('guide-list').innerHTML=data.records.map(g=>{
      const href=`/dashboard/?section=class-guides&guide=${g.id}`;
      return `<tr><td class="guide-title-cell"><a href="${href}">${escape(g.title)}</a></td><td class="guide-spec-cell">${escape(g.specialization_label)}</td><td class="guide-tags-cell">${(g.tags||[]).map(t=>`<span class="guide-tag">${escape(t)}</span>`).join('')||'<span class="muted">—</span>'}</td><td>${escape(g.author_display_name||g.author||'站内编辑')}</td><td><span class="guide-list-status ${g.published_revision_id?'is-approved':''}">${g.published_revision_id?'已有审核版本':'待编辑审核'}</span></td><td>${g.revision_number}</td><td><a href="${href}" aria-label="编辑${escape(g.title)}">编辑</a></td></tr>`;
    }).join('')||'<tr><td colspan="7" class="guide-list-empty">没有符合筛选条件的攻略。</td></tr>';
    $('guide-page-info').textContent=`第 ${state.page} 页 · 共 ${data.total} 篇`;$('page-prev').disabled=state.page===1;$('page-next').disabled=state.page*100>=data.total;}
  function updateSpecs(form,blank){
    if(!state.catalog)return;
    const cls=form.elements.class_name?.value,select=form.elements.spec_id,current=select.value;
    const specs=state.catalog.specializations.filter(s=>!cls||s.class_name===cls);
    select.innerHTML=`<option value="">${blank?'全部专精':'请选择专精'}</option>`+Object.entries(state.catalog.classes).map(([key,label])=>{
      const rows=specs.filter(s=>s.class_name===key);
      return rows.length?`<optgroup label="${escape(label)}">${rows.map(s=>`<option value="${s.spec_id}">${escape(s.label)}</option>`).join('')}</optgroup>`:'';
    }).join('');
    if([...select.options].some(o=>o.value===current))select.value=current;
  }
  function showAudit(audit){
    const mismatches=audit.source_name_mismatches||[], historical=audit.historical_references||[], macros=audit.source_macro_repairs||[];
    const entries=[['待翻译',audit.untranslated?.length||0],['待校订引用',audit.unresolved_references?.length||0],['待转换组件',audit.unsupported_blocks?.length||0],['来源名称差异',mismatches.length],['引用历史名称',historical.length]];
    $('audit-summary').innerHTML=entries.map(([name,count])=>`<div class="audit-number"><span>${name}</span><strong>${count}</strong></div>`).join('')+`<p class="hint">${!audit.publishable?'完成检查项后可审核。':mismatches.length||historical.length?'正文检查通过；审核前请核对来源名称差异与历史技能是否仍适用。':'内容检查通过，可以审核。'}</p>`;
    $('approve').disabled=!audit.publishable;
    $('missing-terms').innerHTML=(audit.unresolved_references||[]).map(token=>{const ref=audit.references[token];return `<div class="term-row">${escape(ref.source_name||ref.name)}<br><code>${escape(token)}</code><br><button data-term="${escape(token)}" class="small secondary">校订中文</button></div>`;}).join('')||'<p class="muted">没有未解析引用</p>';
    $('source-name-mismatches').innerHTML=mismatches.map(token=>{const ref=audit.references[token];return `<div class="term-row"><code>${escape(token)}</code><br>原文标注：${escape(ref.source_name)}<br>编号对应：${escape(ref.name)}（${escape(ref.name_en)}）</div>`;}).join('')||'<p class="muted">没有名称差异</p>';
    $('missing-blocks').innerHTML=(audit.untranslated||[]).map(row=>`<p class="term-row">${escape((row.source_text||row.reason).slice(0,160))}</p>`).join('')+(audit.unsupported_blocks||[]).map(id=>`<p>待转换组件 ${escape(id)}</p>`).join('')+macros.map(row=>`<p class="term-row">宏原文修正：${escape(row.notes.join(' '))}<br><code>${escape(row.source)}</code></p>`).join('');
  }
  let previewSequence=0, previewTimer;
  async function updatePreview(){
    const sequence=++previewSequence, markdown=$('article-markdown').value;
    $('document-size').textContent=`${markdown.length.toLocaleString()} 字符`;
    const data=await request(state.guide.id+'/',{method:'POST',body:JSON.stringify({content_markdown:markdown})});
    if(sequence!==previewSequence)return;
    $('document-preview').innerHTML=data.html;
    window.initializeGuideReader($('document-preview'));
    $('document-outline').innerHTML=data.toc.map(row=>`<button type="button" class="outline-link" data-line="${row.line}" data-anchor="${row.id}" style="padding-left:${Math.max(0,row.level-2)*10+6}px">${escape(plain(row.title))}</button>`).join('')||'<p class="hint">添加标题后，这里会自动出现目录。</p>';
  }
  async function loadGuide(revision){
    const id=root.dataset.guideId;const data=await request(id+'/' +(revision?'?revision='+revision:''));
    state.guide=data;$('author-use-source').checked=data.author_profile===null;loadAuthor(data);state.dirty=false;$('dirty-state').textContent='尚未修改';$('catalog').hidden=true;$('editor').hidden=false;
    $('article-specialization').value=data.specialization_label;$('editor-heading').textContent=data.revision.title;$('article-title').value=data.revision.title;
    $('article-markdown').value=data.revision.content_markdown||'';$('source-compare').textContent=data.revision.source_markdown||'此文章由站内新建。';
    $('editor-meta').textContent=`${data.author_display_name||data.author||'站内编辑'} · ${data.revision_number} 次修订`;tagSuggestions(data.available_tags||[]);tagEditors['article-tags'].set(data.tags||[]);
    $('revision-select').innerHTML=data.revisions.map(r=>`<option value="${r.id}">${r.number} · ${{manual:'人工编辑',import:'来源导入',translation:'中文转换'}[r.origin]||r.origin} · ${r.created_at.slice(0,10)}</option>`).join('');
    $('revision-select').value=data.revision.id;$('preview-link').href=`/portal/class-guides/${id}/?revision=${data.revision.id}`;
    $('revision-note').value='';showAudit(data.revision.audit);await updatePreview();
  }
  async function save(action='save'){
    if(action!=='save'&&state.dirty)throw new Error('请先保存正文修改，再审核或恢复修订。');
    const data={author_profile:authorPayload(),tags:tagEditors['article-tags'].values(),action,expected_number:state.guide.revision_number,base_revision_id:state.guide.revision.id,revision_id:state.guide.revision.id,title:$('article-title').value,content_markdown:$('article-markdown').value,note:$('revision-note').value||'人工编辑'};
    $('save').disabled=true;
    try{const result=await request(state.guide.id+'/',{method:'PATCH',body:JSON.stringify(data)});await loadGuide(result.revision_id);notify(action==='approve'?'当前修订已审核通过，仍仅供内部预览。':'已保存整篇正文，历史修订保留。');}finally{$('save').disabled=false;}
  }
  function setMode(preview){$('markdown-panel').hidden=preview;$('document-preview').hidden=!preview;$('mode-edit').classList.toggle('secondary',preview);$('mode-preview').classList.toggle('secondary',!preview);}
  $('mode-edit').addEventListener('click',()=>setMode(false));
  $('mode-preview').addEventListener('click',guard(async()=>{await updatePreview();setMode(true);}));
  ['article-title','revision-note'].forEach(id=>$(id).addEventListener('input',markDirty));
  function markdownChanged(){markDirty();previewSequence++;clearTimeout(previewTimer);previewTimer=setTimeout(guard(updatePreview),900);}
  $('article-markdown').addEventListener('input',markdownChanged);
  $('document-outline').addEventListener('click',event=>{
    const button=event.target.closest('[data-line]');if(!button)return;
    if(!$('document-preview').hidden){window.revealGuideHeading($(button.dataset.anchor));return;}
    const field=$('article-markdown'),line=Number(button.dataset.line),offset=field.value.split('\n').slice(0,line).reduce((sum,value)=>sum+value.length+1,0);
    field.focus();field.setSelectionRange(offset,offset);field.scrollTop=line*parseFloat(getComputedStyle(field).lineHeight);
  });
  function insertText(before,after=''){const field=$('article-markdown');setMode(false);const start=field.selectionStart,end=field.selectionEnd;field.setRangeText(before+field.value.slice(start,end)+after,start,end,'end');field.focus();markdownChanged();}
  root.querySelectorAll('[data-wrap]').forEach(button=>button.addEventListener('click',()=>insertText(button.dataset.wrap,button.dataset.wrap)));
  $('insert-heading').addEventListener('click',()=>insertText('\n\n## '));$('insert-list').addEventListener('click',()=>insertText('\n- '));
  $('insert-ref').addEventListener('click',()=>{const id=$('ref-id').value;if(!/^\d+$/.test(id)||Number(id)<1){notify('请输入有效的引用编号',true);return;}insertText(`[[${$('ref-kind').value}:${id}]]`);});
  $('filters').addEventListener('submit',guard(async e=>{e.preventDefault();state.page=1;await loadCatalog();}));$('filters').elements.class_name.addEventListener('change',()=>updateSpecs($('filters'),true));
  $('page-prev').addEventListener('click',guard(async()=>{state.page--;await loadCatalog();}));$('page-next').addEventListener('click',guard(async()=>{state.page++;await loadCatalog();}));
  $('new-open').addEventListener('click',()=>$('create-dialog').showModal());root.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>b.closest('dialog').close()));
  $('create-form').addEventListener('submit',guard(async e=>{e.preventDefault();const data=Object.fromEntries(new FormData(e.target));data.tags=tagEditors['create-tags'].values();data.content_markdown='## 概览\n\n在这里编写攻略正文。\n';const response=await request('',{method:'POST',body:JSON.stringify(data)});location.href=`/dashboard/?section=class-guides&guide=${response.id}`;}));
  $('save').addEventListener('click',guard(()=>save()));$('approve').addEventListener('click',guard(()=>save('approve')));$('restore').addEventListener('click',guard(()=>save('restore')));$('archive').addEventListener('click',guard(async()=>{await request(state.guide.id+'/',{method:'PATCH',body:JSON.stringify({action:'archive',expected_number:state.guide.revision_number,archived:true})});state.dirty=false;location.href='/dashboard/?section=class-guides';}));
  $('revision-select').addEventListener('change',guard(async()=>{if(state.dirty){notify('请先保存当前修改，再切换修订。',true);$('revision-select').value=state.guide.revision.id;return;}await loadGuide($('revision-select').value);}));
  $('missing-terms').addEventListener('click',e=>{const b=e.target.closest('[data-term]');if(!b)return;const ref=state.guide.revision.audit.references[b.dataset.term],f=$('term-form');f.elements.game_version.value=state.guide.game_version;f.elements.kind.value=ref.kind;f.elements.object_id.value=ref.id;f.elements.name_en.value=ref.source_name||'';f.elements.name_zh.value='';f.elements.icon.value='';f.elements.evidence.value='';$('term-dialog').showModal();});
  async function loadTerms(){
    const params=new URLSearchParams(new FormData($('terms-search')));params.set('page',state.termPage);
    const data=await request('terms/?'+params);state.terms=data.records;
    $('terms-results').innerHTML=data.records.map((row,index)=>`<div class="term-row"><strong>${escape(row.name_zh)}</strong> · ${escape(row.name_en)}<br><small>${escape({spell:'技能',talent:'天赋',item:'物品',phrase:'专有名词',macro:'宏名称'}[row.kind]||row.kind)} · ${escape(row.object_id)}</small><p class="hint">${escape(row.evidence)}</p><button type="button" data-edit-term="${index}" class="small secondary">编辑术语</button></div>`).join('')||'<p class="muted">没有匹配的术语</p>';
    $('terms-page').textContent=`第 ${state.termPage} 页 · 共 ${data.total} 条`;$('terms-prev').disabled=state.termPage===1;$('terms-next').disabled=state.termPage*100>=data.total;
  }
  $('terms-open').addEventListener('click',guard(async()=>{state.termPage=1;$('terms-search').elements.version.value=state.guide?.game_version||state.catalog?.versions.at(-1)||'';$('terms-browser').showModal();await loadTerms();}));
  $('terms-search').addEventListener('submit',guard(async e=>{e.preventDefault();state.termPage=1;await loadTerms();}));
  $('terms-prev').addEventListener('click',guard(async()=>{state.termPage--;await loadTerms();}));$('terms-next').addEventListener('click',guard(async()=>{state.termPage++;await loadTerms();}));
  function editTerm(row){const form=$('term-form');for(const key of ['kind','object_id','game_version','name_en','name_zh','icon','evidence'])form.elements[key].value=row[key]||'';$('term-dialog').showModal();}
  $('terms-results').addEventListener('click',e=>{const button=e.target.closest('[data-edit-term]');if(button)editTerm(state.terms[Number(button.dataset.editTerm)]);});
  $('phrase-new').addEventListener('click',()=>editTerm({kind:'phrase',game_version:$('terms-search').elements.version.value}));
  $('macro-new').addEventListener('click',()=>editTerm({kind:'macro',game_version:$('terms-search').elements.version.value}));
  $('term-form').addEventListener('submit',guard(async e=>{e.preventDefault();const data=Object.fromEntries(new FormData(e.target));data.object_id=Number(data.object_id);await request('terms/',{method:'POST',body:JSON.stringify(data)});$('term-dialog').close();if(state.guide){const updated=await request(state.guide.id+'/?revision='+state.guide.revision.id);state.guide.revision.audit=updated.revision.audit;showAudit(updated.revision.audit);}if($('terms-browser').open)await loadTerms();notify(['phrase','macro'].includes(data.kind)?'名称已保存，将用于后续翻译；已有正文或宏也可在编辑器内修正。':'术语已校订，正文引用会自动更新。');}));
  $('disclaimer-open').addEventListener('click',guard(async()=>{const data=await request('disclaimer/');$('disclaimer-form').elements.text.value=data.text;$('disclaimer-dialog').showModal();}));
  $('disclaimer-form').addEventListener('submit',guard(async e=>{e.preventDefault();await request('disclaimer/',{method:'PATCH',body:JSON.stringify({text:e.target.elements.text.value})});$('disclaimer-dialog').close();notify('免责声明已保存，所有带 maxroll 标签的攻略统一生效。');}));
  $('feed-open').addEventListener('click',guard(async()=>{const data=await request('feed/'),f=$('feed-form');f.elements.enabled.checked=data.enabled;f.elements.interval_minutes.value=data.interval_minutes;f.elements.authorization_note.value=data.authorization_note;$('feed-status').textContent=`上次检查：${data.last_checked_at||'尚未检查'} · ${data.lease_until?'同步运行中':'当前空闲'}`;$('sync-runs').innerHTML=data.runs.map(run=>`<details class="sync-run"><summary>批次 ${run.id} · ${{running:'运行中',completed:'完成',partial:'部分完成',failed:'失败'}[run.status]} · ${run.results.length} 篇</summary><pre>${escape(JSON.stringify({coverage:run.coverage,results:run.results,error:run.error},null,2))}</pre></details>`).join('');$('feed-dialog').showModal();}));
  $('feed-form').addEventListener('submit',guard(async e=>{e.preventDefault();const f=e.target;await request('feed/',{method:'PATCH',body:JSON.stringify({enabled:f.elements.enabled.checked,interval_minutes:Number(f.elements.interval_minutes.value),authorization_note:f.elements.authorization_note.value})});$('feed-dialog').close();notify('来源监控设置已保存。');}));
  window.addEventListener('beforeunload',e=>{if(state.dirty){e.preventDefault();e.returnValue='';}});
  let initialized = false;
  window.loadClassGuides = guard(async () => {
    if (initialized) return;
    initialized = true;
    try {
      if (root.dataset.guideId) { $('catalog').hidden = true; await loadGuide(); }
      else await loadCatalog();
    } catch (error) { initialized = false; throw error; }
  });
  if (new URLSearchParams(location.search).get('section') === 'class-guides') window.loadClassGuides();
})();
