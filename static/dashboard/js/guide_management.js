(() => {
  'use strict';
  const disclaimer = document.getElementById('guide-disclaimer-workspace');
  const terms = document.getElementById('wow-localization-workspace');
  if (!disclaimer || !terms) return;
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const names = {spell:'技能', talent:'天赋', item:'物品', phrase:'专有名词', macro:'宏名称'};
  async function request(path, options = {}) {
    const csrf = document.cookie.split('; ').find(v => v.startsWith('csrftoken='))?.slice(10) || '';
    const response = await fetch(path.startsWith('terms/') ? '/api/dashboard/wow-localization/' + path.slice(6) : '/api/dashboard/class-guides/' + path, {...options, headers:{'Content-Type':'application/json', 'X-CSRFToken':csrf}});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || data.message || `请求失败 ${response.status}`);
    return data;
  }
  function message(root, text, error = false) {
    const node = root.querySelector('[data-message]');
    node.textContent = text;
    node.hidden = !text;
    node.classList.toggle('error', error);
  }
  const guard = (root, fn) => async (...args) => {
    try { await fn(...args); } catch (error) { message(root, error.message, true); }
  };
  let disclaimerLoaded = false, disclaimerLoading = false, savedText = '';
  const disclaimerForm = disclaimer.querySelector('form');
  async function loadDisclaimer() {
    if (disclaimerLoaded || disclaimerLoading) return;
    disclaimerLoading = true;
    try {
      const data = await request('disclaimer/');
      savedText = data.text;
      disclaimerForm.elements.text.value = savedText;
      disclaimerForm.elements.text.disabled = false;
      disclaimerForm.querySelector('button').disabled = false;
      disclaimerLoaded = true;
      message(disclaimer, '');
    } finally { disclaimerLoading = false; }
  }
  disclaimerForm.addEventListener('input', () => {
    disclaimer.querySelector('[data-save-state]').textContent = disclaimerForm.elements.text.value === savedText ? '' : '有未保存修改';
  });
  disclaimerForm.addEventListener('submit', guard(disclaimer, async event => {
    event.preventDefault();
    const button = disclaimerForm.querySelector('button'), text = disclaimerForm.elements.text.value;
    button.disabled = true;
    try {
      await request('disclaimer/', {method:'PATCH', body:JSON.stringify({text})});
      savedText = text;
      disclaimer.querySelector('[data-save-state]').textContent = disclaimerForm.elements.text.value === savedText ? '已保存' : '有未保存修改';
      message(disclaimer, '免责声明已保存，所有带 maxroll 标签的攻略统一生效。');
    } finally { button.disabled = false; }
  }));
  const search = terms.querySelector('#wow-localization-search');
  const editor = terms.querySelector('#guide-term-form');
  const dialog = terms.querySelector('dialog');
  let page = 1, rows = [], versions = [], sequence = 0, termsLoaded = false, editing = false, editingRow = null;
  async function loadTerms() {
    const currentSequence = ++sequence;
    const params = new URLSearchParams(new FormData(search));
    if (!params.get('version')) params.delete('version');
    params.set('page', page);
    const data = await request('terms/?' + params);
    if (currentSequence !== sequence) return;
    rows = data.records;
    versions = data.versions;
    const selectedVersion = search.elements.version.value;
    search.elements.version.innerHTML = '<option value="">全部版本</option>' + [...new Set([...versions, selectedVersion].filter(Boolean))].map(v => `<option value="${escape(v)}">${escape(v)}</option>`).join('');
    search.elements.version.value = selectedVersion;
    terms.querySelector('#guide-term-versions').innerHTML = versions.map(v => `<option value="${escape(v)}"></option>`).join('');
    terms.querySelector('#wow-localization-results').innerHTML = rows.map((row, index) => `<tr><td>${escape(row.name_zh)}</td><td>${escape(row.name_en)}</td><td>${escape(names[row.kind])}<br><small>${escape(row.object_id)}</small></td><td>${escape(row.game_version)}<br><small>${row.supplemental ? '名称资料' : `天赋节点${row.duplicate_count > 1 ? ` × ${row.duplicate_count}` : ''}`}</small></td><td class="term-evidence">${escape(row.evidence)}</td><td><button type="button" class="small secondary" data-edit-term="${index}">编辑</button></td></tr>`).join('') || '<tr><td colspan="6" class="guide-list-empty">没有匹配的术语</td></tr>';
    terms.querySelector('#wow-localization-page').textContent = `第 ${page} 页 · 共 ${data.total} 条`;
    terms.querySelector('#wow-localization-prev').disabled = page === 1;
    terms.querySelector('#wow-localization-next').disabled = page * 100 >= data.total;
    termsLoaded = true;
  }
  function updateKind() {
    const phrase = ['phrase', 'macro'].includes(editor.elements.kind.value);
    editor.querySelector('[data-object-field]').hidden = phrase;
    editor.elements.object_id.required = !phrase;
    editor.elements.name_en.required = phrase;
    editor.elements.name_en.readOnly = editing && phrase;
  }
  function editTerm(row = {}) {
    editing = Boolean(row.id);
    editingRow = editing ? row : null;
    editor.reset();
    for (const key of ['kind','object_id','game_version','name_en','name_zh','icon','evidence']) editor.elements[key].value = row[key] ?? (key === 'kind' ? 'spell' : '');
    editor.elements.game_version.readOnly = editing;
    editor.elements.object_id.readOnly = editing;
    editor.elements.kind.disabled = editing;
    editor.elements.evidence.required = !editing || Boolean(row.evidence);
    editor.querySelector('h2').textContent = editing ? '编辑术语' : '新增术语';
    editor.querySelector('[data-editor-message]').hidden = true;
    updateKind();
    dialog.showModal();
  }
  editor.elements.kind.addEventListener('change', updateKind);
  terms.querySelector('#guide-term-new').addEventListener('click', () => editTerm({game_version:search.elements.version.value || versions.at(-1) || ''}));
  terms.querySelector('#guide-term-cancel').addEventListener('click', () => dialog.close());
  terms.querySelector('#wow-localization-results').addEventListener('click', event => {
    const button = event.target.closest('[data-edit-term]');
    if (button) editTerm(rows[Number(button.dataset.editTerm)]);
  });
  editor.addEventListener('submit', async event => {
    event.preventDefault();
    const button = editor.querySelector('[type="submit"]');
    const data = Object.fromEntries(new FormData(editor));
    data.kind = editor.elements.kind.value;
    data.object_id = Number(data.object_id);
    if (editingRow) {
      data.record_pk = editingRow.pk;
      data.edit_state = {name_en:editingRow.name_en, name_zh:editingRow.name_zh,
        icon:editingRow.icon, evidence:editingRow.evidence, duplicate_count:editingRow.duplicate_count || 1};
    } else data.create = true;
    button.disabled = true;
    try {
      await request('terms/', {method:'POST', body:JSON.stringify(data)});
      dialog.close();
      message(terms, '术语已保存。技能、天赋和物品引用会自动更新；专有名词和宏名称用于后续翻译。');
      await loadTerms();
    } catch (error) {
      const node = dialog.open ? editor.querySelector('[data-editor-message]') : terms.querySelector('[data-message]');
      node.textContent = error.message; node.hidden = false; node.classList.add('error');
    } finally { button.disabled = false; }
  });
  search.addEventListener('submit', guard(terms, async event => { event.preventDefault(); page = 1; await loadTerms(); }));
  for (const [id, delta] of [['wow-localization-prev', -1], ['wow-localization-next', 1]]) {
    terms.querySelector('#' + id).addEventListener('click', guard(terms, async () => { page += delta; await loadTerms(); }));
  }
  window.addEventListener('beforeunload', event => {
    if (disclaimerLoaded && disclaimerForm.elements.text.value !== savedText) { event.preventDefault(); event.returnValue = ''; }
  });
  window.loadGuideManagementPage = section => {
    if (section === 'guide-disclaimers') return guard(disclaimer, loadDisclaimer)();
    if (section === 'wow-localization') return guard(terms, async () => {
      if (termsLoaded) return;
      const params = new URLSearchParams(location.search);
      for (const key of ['q', 'kind']) search.elements[key].value = params.get(key) || '';
      if (params.get('version')) {
        search.elements.version.add(new Option(params.get('version'), params.get('version')));
        search.elements.version.value = params.get('version');
      }
      await loadTerms();
      if (params.get('edit') && names[params.get('kind')]) {
        const requestedName = params.get('name_en');
        const row = rows.find(row => (row.identifiers || [row.object_id, ...(row.aliases || [])]).map(String).includes(params.get('edit')) && row.kind === params.get('kind') && row.game_version === params.get('version') && (!requestedName || row.name_en === requestedName));
        editTerm(row || {kind:params.get('kind'), object_id:params.get('edit'), game_version:params.get('version'), name_en:requestedName});
      }
    })();
  };
})();
