// Loaded before the main bundle: a broken or blocked bootstrap must not disable recovery.
(() => {
  const key = 'home_retry';
  const ready = [
    '#wow-today-panel[data-state="ready"]', '#portal-today-strip-items .portal-today-chip',
    '#featured-news-card a.portal-daily-report-title', '.home-news-list a[href]', '#nga-list a[href]',
    '#videos-list .portal-video-row', '#events-list [data-cal-day]', '#peak-spec-rankings .portal-peak-card',
    '#mplus-cutoffs tbody tr', '#mplus-rankings tbody tr',
  ].join(', ');
  let timer, done = false;
  function check() {
    if (done || document.hidden || navigator.onLine === false) return;
    const url = new URL(location.href);
    done = true;
    if (document.querySelector(ready)) {
      if (url.searchParams.has(key)) {
        url.searchParams.delete(key);
        history.replaceState(history.state, '', url);
      }
      return;
    }
    // A retry URL never retries itself, including when browser storage is disabled.
    if (url.searchParams.has(key)) return;
    const now = Date.now();
    try {
      if (now - Number(sessionStorage.getItem('portal:home:last-retry')) < 300000) return;
      sessionStorage.setItem('portal:home:last-retry', String(now));
    } catch (_) { /* The URL marker alone still prevents a loop. */ }
    url.searchParams.set(key, String(now));
    location.replace(url.href);
  }
  function arm() {
    clearTimeout(timer);
    if (!done && !document.hidden && navigator.onLine !== false) timer = setTimeout(check, 30000);
  }
  document.addEventListener('visibilitychange', arm);
  addEventListener('online', arm);
  addEventListener('offline', arm);
  arm();
})();
