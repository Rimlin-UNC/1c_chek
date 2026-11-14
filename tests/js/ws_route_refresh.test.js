import { describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';

const source = readFileSync('app/static/js/app.js', 'utf8');
const start = source.indexOf('let _routeRefreshTimer = null;');
const end = source.indexOf('\nfunction handleWsEvent(', start);
if (start < 0 || end < 0) throw new Error('Не найден планировщик обновления');
const scheduler = source.slice(start, end);

function harness() {
  const state = { me: { id: 'one' }, view: 'dashboard' };
  let seq = 0;
  const route = vi.fn();
  const refresh = vi.fn();
  const factory = new Function('state', 'route', 'viewReceipts', 'routeGuard',
    `${scheduler}; return scheduleRouteRefresh;`);
  const schedule = factory(state, route, { _refresh: refresh }, () => {
    const version = seq;
    return () => version === seq;
  });
  return { state, route, refresh, schedule, navigate(view) { seq++; state.view = view; } };
}

describe('отложенное обновление по событиям', () => {
  it('не перерисовывает новый экран после перехода', () => {
    vi.useFakeTimers();
    try {
      const h = harness(); h.schedule(); h.navigate('primary');
      vi.advanceTimersByTime(1200);
      expect(h.route).not.toHaveBeenCalled();
      expect(h.refresh).not.toHaveBeenCalled();
    } finally { vi.useRealTimers(); }
  });
  it('сохраняет обновление текущего экрана и не теряет новое после перехода', () => {
    vi.useFakeTimers();
    try {
      const h = harness(); h.schedule(); vi.advanceTimersByTime(600); h.navigate('receipts'); h.schedule();
      vi.advanceTimersByTime(600);
      expect(h.refresh).not.toHaveBeenCalled();
      vi.advanceTimersByTime(600);
      expect(h.refresh).toHaveBeenCalledTimes(1);
      expect(h.route).not.toHaveBeenCalled();
      h.navigate('dashboard'); h.schedule(); h.schedule();
      vi.advanceTimersByTime(1200);
      expect(h.route).toHaveBeenCalledTimes(1);
    } finally { vi.useRealTimers(); }
  });
  it('даже возврат к прежнему разделу не возрождает старый таймер', () => {
    vi.useFakeTimers();
    try {
      const h = harness(); h.schedule(); h.navigate('primary'); h.navigate('dashboard');
      vi.advanceTimersByTime(1200);
      expect(h.route).not.toHaveBeenCalled();
    } finally { vi.useRealTimers(); }
  });
});
