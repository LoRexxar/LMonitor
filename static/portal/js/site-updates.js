/* 首页数据动态独立加载，避免新闻接口阻塞；返回页面时重新检查。 */
(() => {
  const root = document.getElementById('portal-update-items');
  const summary = document.getElementById('portal-update-summary');
  const refresh = document.getElementById('portal-update-refresh');
  if (!root || !summary || !refresh) return;
  let loading = false;
  let lastChecked = 0;
  let hasData = false;

  async function load() {
    if (loading) return;
    loading = true;
    refresh.disabled = true;
    refresh.textContent = '查询中…';
    root.setAttribute('aria-busy', 'true');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/portal/api/site-updates/', {
        credentials: 'same-origin', cache: 'no-cache', signal: controller.signal,
      });
      if (!response.ok) throw new Error('更新状态暂不可用');
      const data = await response.json();
      if (!Array.isArray(data.items) || !data.items.length) throw new Error('更新状态格式无效');
      const nodes = data.items.map((item) => {
        const link = document.createElement('a');
        link.className = `portal-update-item${item.status === 'today' ? ' is-today' : ''}`;
        link.href = sanitizeHref(item.url) || '/portal/wow-updates/';
        link.dataset.updateKey = item.key;
        const label = document.createElement('strong');
        label.textContent = item.label;
        const status = document.createElement('span');
        status.className = 'portal-update-status';
        status.textContent = item.summary;
        const time = document.createElement('span');
        time.className = 'portal-update-time';
        time.textContent = item.updated_label ? `最近 ${item.updated_label}` : '等待首次更新';
        link.title = [item.label, item.summary, item.updated_at, item.detail].filter(Boolean).join(' · ');
        link.append(label, status, time);
        return link;
      });
      root.replaceChildren(...nodes);
      summary.textContent = `今日 ${data.today_modules} 个板块更新 · ${data.date}（北京时间）`;
      summary.title = `状态统计于 ${data.checked_at}`;
      hasData = true;
      lastChecked = Date.now();
    } catch (_) {
      summary.textContent = hasData ? '刷新失败，以下为上次查询结果' : '暂时无法查询更新，请重试';
      summary.removeAttribute('title');
    } finally {
      clearTimeout(timeout);
      loading = false;
      refresh.disabled = false;
      refresh.textContent = '刷新';
      root.setAttribute('aria-busy', 'false');
    }
  }

  refresh.addEventListener('click', load);
  function refreshIfDue() {
    if (!document.hidden && Date.now() - lastChecked >= 60000) load();
  }
  document.addEventListener('visibilitychange', refreshIfDue);
  window.addEventListener('focus', refreshIfDue);
  setInterval(refreshIfDue, 5 * 60000);
  load();
})();
