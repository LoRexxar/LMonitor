/* 数据动态独立加载，只把今日更新送入原有重点短条。 */
(() => {
  if (!document.getElementById('portal-today-strip-items')) return;
  let loading = false;
  let lastChecked = 0;
  async function load() {
    if (loading) return;
    loading = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch('/portal/api/site-updates/', {
        credentials: 'same-origin', cache: 'no-cache', signal: controller.signal,
      });
      if (!response.ok) throw new Error('更新状态暂不可用');
      const data = await response.json();
      if (!Array.isArray(data.items)) throw new Error('更新状态格式无效');
      PORTAL_STATE.todayUpdates = data.items.filter((item) => item.status === 'today');
      lastChecked = Date.now();
    } catch (_) {
      // 无法确认今天是否更新时，不在重点中继续展示旧状态。
      PORTAL_STATE.todayUpdates = [];
    } finally {
      clearTimeout(timeout);
      loading = false;
      PORTAL_STATE.todayUpdatesSettled = true;
      renderTodayStrip();
    }
  }
  function refreshIfDue() {
    if (!document.hidden && Date.now() - lastChecked >= 60000) load();
  }
  document.addEventListener('visibilitychange', refreshIfDue);
  window.addEventListener('focus', refreshIfDue);
  setInterval(refreshIfDue, 5 * 60000);
  load();
})();
