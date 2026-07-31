(() => {
  'use strict';
  const root = document.querySelector('[data-benchmark-panel-edit-page]');
  if (!root) return;
  const id = root.dataset.benchmarkPanelId;
  const form = root.querySelector('[data-panel-edit-form]');
  const save = root.querySelector('[data-panel-save]');
  const status = root.querySelector('[data-panel-save-status]');
  const notice = root.querySelector('[data-panel-notification]');
  let panel = null;
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || '';
  const localDate = value => { if (!value) return ''; const date = new Date(value); if (Number.isNaN(date.getTime())) return ''; const pad = value => String(value).padStart(2, '0'); return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`; };
  const notify = (text, kind) => { notice.hidden = false; notice.className = `simc-benchmark-notification ${kind}`; notice.textContent = text; };
  const fetchPanel = async (method = 'GET', body = null) => {
    const response = await fetch(`/api/simc-benchmarks/panels/${encodeURIComponent(id)}/`, {method, credentials: 'same-origin', headers: method === 'GET' ? {} : {'Content-Type': 'application/json', 'X-CSRFToken': csrf()}, body});
    const payload = await response.json();
    if (!response.ok || payload.success !== true) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload.data;
  };
  const fill = data => { panel = data; form.elements.panel_name.value = data.name || ''; form.elements.slug.value = data.slug || ''; form.elements.description.value = data.description || ''; form.elements.is_active.checked = !!data.is_active; form.elements.is_public.checked = !!data.is_public; form.elements.schedule_enabled.checked = !!data.schedule_enabled; form.elements.interval_seconds.value = data.interval_seconds || 86400; form.elements.next_run_at.value = localDate(data.next_run_at); status.textContent = '面板已载入'; save.disabled = false; };
  const payload = () => ({name: form.elements.panel_name.value.trim(), slug: form.elements.slug.value.trim(), description: form.elements.description.value.trim(), is_active: form.elements.is_active.checked, is_public: form.elements.is_public.checked, schedule_enabled: form.elements.schedule_enabled.checked, interval_seconds: Number(form.elements.interval_seconds.value), next_run_at: form.elements.next_run_at.value ? new Date(form.elements.next_run_at.value).toISOString() : null});
  save.addEventListener('click', async () => { const data = payload(); if (!data.name || !/^[a-z0-9][a-z0-9_-]*$/.test(data.slug) || !Number.isInteger(data.interval_seconds) || data.interval_seconds < 1) { notify('请检查名称、slug 和定时间隔。', 'error'); return; } save.disabled = true; status.textContent = '正在保存…'; try { fill(await fetchPanel('PATCH', JSON.stringify(data))); notify('面板已保存', 'success'); } catch (error) { status.textContent = '保存失败'; notify(`保存失败：${error.message}`, 'error'); save.disabled = false; } });
  fetchPanel().then(fill).catch(error => { status.textContent = '载入失败'; notify(`载入失败：${error.message}`, 'error'); });
})();