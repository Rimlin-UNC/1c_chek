import { describe, expect, it, vi } from 'vitest';
import { createNavigationCounts } from '../../app/static/js/navigation_counts.js';

describe('счётчики навигации', () => {
  it('показывают ноль и значения, неизвестные или недоступные разделы скрывают', async () => {
    document.body.innerHTML = '<span data-nav-count="clients" class="hidden"></span><span data-nav-count="primary" class="hidden"></span><span data-nav-count="users" class="hidden"></span>';
    const context = { key: 'a', company: '', client: '' };
    const counts = createNavigationCounts({ getContext: () => context, get: async () => ({ clients: 2, primary: 0 }) });
    await counts.refresh();
    expect(document.querySelector('[data-nav-count="clients"]').textContent).toBe('2');
    expect(document.querySelector('[data-nav-count="primary"]').textContent).toBe('0');
    expect(document.querySelector('[data-nav-count="primary"]').classList.contains('hidden')).toBe(false);
    expect(document.querySelector('[data-nav-count="users"]').classList.contains('hidden')).toBe(true);
  });

  it('поздний ответ старой компании не перезаписывает новую', async () => {
    document.body.innerHTML = '<span data-nav-count="primary" class="hidden"></span>';
    let context = { key: 'a', company: 'a', client: '' };
    let first;
    const get = vi.fn().mockImplementationOnce(() => new Promise(resolve => { first = resolve; }))
      .mockResolvedValueOnce({ primary: 7 });
    const counts = createNavigationCounts({ getContext: () => context, get });
    const old = counts.refresh();
    context = { key: 'b', company: 'b', client: '' };
    await counts.refresh();
    first({ primary: 1 });
    await old;
    expect(document.querySelector('[data-nav-count="primary"]').textContent).toBe('7');
  });

  it('не отправляет повторный запрос чаще раза в пять секунд при навигации', async () => {
    vi.useFakeTimers();
    document.body.innerHTML = '<span data-nav-count="primary" class="hidden"></span>';
    const get = vi.fn().mockResolvedValue({ primary: 1 });
    const counts = createNavigationCounts({ getContext: () => ({ key: 'a', company: '', client: '' }), get });
    counts.schedule();
    await vi.advanceTimersByTimeAsync(250);
    expect(get).toHaveBeenCalledTimes(1);
    counts.schedule(); counts.schedule();
    await vi.advanceTimersByTimeAsync(1000);
    expect(get).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(4000);
    expect(get).toHaveBeenCalledTimes(2);
    counts.reset();
    vi.useRealTimers();
  });

  it('при ошибке не показывает прежний результат или ложный ноль', async () => {
    document.body.innerHTML = '<span data-nav-count="primary"></span>';
    const get = vi.fn().mockResolvedValueOnce({ primary: 8 }).mockRejectedValueOnce(new Error('сеть'));
    const counts = createNavigationCounts({ getContext: () => ({ key: 'a', company: '', client: '' }), get });
    await counts.refresh();
    await counts.refresh();
    expect(document.querySelector('[data-nav-count="primary"]').classList.contains('hidden')).toBe(true);
  });
});
