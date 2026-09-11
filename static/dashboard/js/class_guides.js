(() => {
  'use strict';
  const root = document.getElementById('class-guide-workspace');
  if (!root) return;
  const $ = id => root.querySelector('#' + id), api = '/api/dashboard/class-guides/';
  const state = {catalog:null, guide:null, dirty:false, page:1};
  const names = {html:'正文',heading:'章节标题',columns:'分栏',column:'栏目',accordion:'折叠组',details:'折叠说明',callout:'提示',rating:'能力评级',changelog:'更新记录',code:'宏',image:'图片',talents:'天赋',gear:'配装',rotation:'技能循环',priority:'优先级',timeline:'时间轴',simulation:'模拟结果',group:'组合',separator:'分隔线',unsupported:'待转换'};
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
      return `<tr><td class="guide-title-cell"><a href="${href}">${escape(g.title)}</a>${g.is_visible?'':'<span class="guide-visibility-label">未显示</span>'}</td><td class="guide-spec-cell">${escape(g.specialization_label)}</td><td class="guide-tags-cell">${(g.tags||[]).map(t=>`<span class="guide-tag">${escape(t)}</span>`).join('')||'<span class="muted">—</span>'}</td><td>${escape(g.author_display_name||g.author||'站内编辑')}</td><td><a href="${href}" aria-label="编辑${escape(g.title)}">编辑</a></td></tr>`;
    }).join('')||'<tr><td colspan="5" class="guide-list-empty">没有符合筛选条件的攻略。</td></tr>';
    $('guide-page-info').textContent=`第 ${state.page} 页 · 共 ${data.total} 篇`;$('page-prev').disabled=state.page===1;$('page-next').disabled=state.page*100>=data.total;}
  function updateSpecs(form,blank){
    if(!state.catalog)return;
    const cls=form.elements.class_name?.value;
    fillSpecs(form.elements.spec_id,state.catalog.specializations.filter(s=>!cls||s.class_name===cls),state.catalog.classes,blank);
  }
  function fillSpecs(select,specs,classes,blank){
    const current=select.value;
    select.innerHTML=`<option value="">${blank?'全部专精':'请选择专精'}</option>`+Object.entries(classes).map(([key,label])=>{
      const rows=specs.filter(s=>s.class_name===key);
      return rows.length?`<optgroup label="${escape(label)}">${rows.map(s=>`<option value="${s.spec_id}">${escape(s.label)}</option>`).join('')}</optgroup>`:'';
    }).join('');
    if([...select.options].some(o=>o.value===current))select.value=current;
  }
  function showAudit(audit){
    const mismatches=audit.source_name_mismatches||[], historical=audit.historical_references||[], macros=audit.source_macro_repairs||[];
    const entries=[['待翻译',audit.untranslated?.length||0],['待校订引用',audit.unresolved_references?.length||0],['待转换组件',audit.unsupported_blocks?.length||0],['来源名称差异',mismatches.length],['引用历史名称',historical.length]];
    $('audit-summary').innerHTML=entries.map(([name,count])=>`<div class="audit-number"><span>${name}</span><strong>${count}</strong></div>`).join('')+`<p class="hint">${audit.complete?'内容检查通过。':'请核对以上内容问题，检查提示不会阻止保存。'}</p>`;
    $('missing-terms').innerHTML=(audit.unresolved_references||[]).map(token=>{const ref=audit.references[token];return `<div class="term-row">${escape(ref.source_name||ref.name)}<br><code>${escape(token)}</code><br><a class="button small secondary" target="_blank" rel="noopener" href="/dashboard/?${escape(new URLSearchParams({section:'wow-localization',version:state.guide.game_version,kind:ref.kind,q:ref.id,edit:ref.id,name_en:ref.source_name||''}))}">校订中文 ↗</a></div>`;}).join('')||'<p class="muted">没有未解析引用</p>';
    $('source-name-mismatches').innerHTML=mismatches.map(token=>{const ref=audit.references[token];return `<div class="term-row"><code>${escape(token)}</code><br>原文标注：${escape(ref.source_name)}<br>编号对应：${escape(ref.name)}（${escape(ref.name_en)}）</div>`;}).join('')||'<p class="muted">没有名称差异</p>';
    $('missing-blocks').innerHTML=(audit.untranslated||[]).map(row=>`<p class="term-row">${escape((row.source_text||row.reason).slice(0,160))}</p>`).join('')+(audit.unsupported_blocks||[]).map(id=>`<p>待转换组件 ${escape(id)}</p>`).join('')+macros.map(row=>`<p class="term-row">宏原文修正：${escape(row.notes.join(' '))}<br><code>${escape(row.source)}</code></p>`).join('');
  }
  let previewSequence=0, previewTimer;
  async function updatePreview(){
    const sequence=++previewSequence, markdown=$('article-markdown').value;
    $('document-size').textContent=`${markdown.length.toLocaleString()} 字符`;
    const data=await request(state.guide.id+'/',{method:'POST',body:JSON.stringify({content_markdown:markdown,spec_id:Number($('article-specialization').value)})});
    if(sequence!==previewSequence)return;
    window.GuideReferenceTooltip?.hide();
    $('document-preview').innerHTML=data.html;
    window.initializeGuideReader($('document-preview'));
    $('document-outline').innerHTML=data.toc.map(row=>`<button type="button" class="outline-link" data-line="${row.line}" data-anchor="${row.id}" style="padding-left:${Math.max(0,row.level-2)*10+6}px">${escape(plain(row.title))}</button>`).join('')||'<p class="hint">添加标题后，这里会自动出现目录。</p>';
  }
  async function loadGuide(){
    const id=root.dataset.guideId;const data=await request(id+'/');
    state.guide=data;$('author-use-source').checked=data.author_profile===null;loadAuthor(data);state.dirty=false;$('dirty-state').textContent='尚未修改';$('catalog').hidden=true;$('editor').hidden=false;
    fillSpecs($('article-specialization'),data.specializations,data.classes,false);$('article-specialization').value=String(data.spec_id);$('article-visible').checked=data.is_visible;$('editor-heading').textContent=data.title;$('article-title').value=data.title;
    $('article-markdown').value=data.content_markdown||'';$('source-compare').textContent=data.source_markdown||'此文章由站内新建。';
    $('editor-meta').textContent=`${data.author_display_name||data.author||'站内编辑'}`;tagSuggestions(data.available_tags||[]);tagEditors['article-tags'].set(data.tags||[]);
    $('preview-link').href=data.is_visible?`/portal/class-guides/${id}/`:`/dashboard/class-guides/${id}/preview/`;$('preview-link').textContent=data.is_visible?'查看文章':'预览文章';showAudit(data.checks);await updatePreview();
  }
  async function save(action='save'){
    const data={spec_id:Number($('article-specialization').value),is_visible:$('article-visible').checked,author_profile:authorPayload(),tags:tagEditors['article-tags'].values(),action,expected_updated_at:state.guide.updated_at,title:$('article-title').value,content_markdown:$('article-markdown').value};
    $('save').disabled=true;
    try{const result=await request(state.guide.id+'/',{method:'PATCH',body:JSON.stringify(data)});await loadGuide();notify('文章已保存。');}finally{$('save').disabled=false;}
  }
  function setMode(preview){$('markdown-panel').hidden=preview;$('document-preview').hidden=!preview;$('mode-edit').classList.toggle('secondary',preview);$('mode-preview').classList.toggle('secondary',!preview);}
  $('mode-edit').addEventListener('click',()=>setMode(false));
  $('mode-preview').addEventListener('click',guard(async()=>{await updatePreview();setMode(true);}));
  ['article-title'].forEach(id=>$(id).addEventListener('input',markDirty));
  function markdownChanged(){markDirty();previewSequence++;clearTimeout(previewTimer);previewTimer=setTimeout(guard(updatePreview),900);}
  $('article-markdown').addEventListener('input',markdownChanged);
  $('article-specialization').addEventListener('change',guard(async()=>{markDirty();await updatePreview();}));
  $('article-visible').addEventListener('change',markDirty);
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
  $('create-form').addEventListener('submit',guard(async e=>{e.preventDefault();const data=Object.fromEntries(new FormData(e.target));data.tags=tagEditors['create-tags'].values();data.is_visible=$('create-visible').checked;data.content_markdown='## 概览\n\n在这里编写攻略正文。\n';const response=await request('',{method:'POST',body:JSON.stringify(data)});location.href=`/dashboard/?section=class-guides&guide=${response.id}`;}));
  $('save').addEventListener('click',guard(()=>save()));
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
