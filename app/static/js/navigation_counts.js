// Счётчики доступны только для подтверждённого текущего контекста.
export function createNavigationCounts({ getContext, get }) {
  let sequence = 0;
  let activeKey = '';
  let timer = null;
  let lastStart = 0;
  const badges = () => document.querySelectorAll('[data-nav-count]');
  const clear = () => {
    badges().forEach(el => { el.classList.add('hidden'); el.textContent = ''; });
  };
  function schedule() {
    const ctx = getContext();
    if (!ctx || ctx.key !== activeKey) { sequence += 1; activeKey = ctx?.key || ''; lastStart = 0; clear(); }
    clearTimeout(timer);
    const delay = Math.max(250, lastStart ? 5000 - (Date.now() - lastStart) : 0);
    if (ctx) timer = setTimeout(() => { refresh(); }, delay);
  }
  async function refresh() {
    const ctx = getContext();
    const current = ++sequence;
    if (!ctx) { activeKey = ''; clear(); return; }
    if (ctx.key !== activeKey) { activeKey = ctx.key; clear(); }
    const query = ctx.company ? '?company_id=' + encodeURIComponent(ctx.company) : '';
    lastStart = Date.now();
    try {
      const data = await get('/api/v1/navigation/counts' + query,
        { retries: 1, noClientFallback: true, clientId: ctx.client || null });
      if (current !== sequence || getContext()?.key !== ctx.key) return;
      badges().forEach(el => {
        const n = data?.[el.dataset.navCount];
        if (!Number.isSafeInteger(n) || n < 0) { el.classList.add('hidden'); el.textContent = ''; return; }
        el.textContent = new Intl.NumberFormat('ru-RU').format(n);
        if (el.title) el.setAttribute('aria-label', `${el.title}: ${el.textContent}`);
        el.classList.remove('hidden');
      });
    } catch {
      if (current === sequence && getContext()?.key === ctx.key) clear();
    }
  }
  function reset() { sequence += 1; activeKey = ''; lastStart = 0; clearTimeout(timer); clear(); }
  return { schedule, refresh, reset };
}
