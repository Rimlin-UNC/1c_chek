/* PWA: временная ошибка или зависание сервера не уничтожает доступную оболочку. */
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
import { afterEach, describe, expect, it, vi } from 'vitest';

function worker(fetchImpl, hit = new Response('cached', { status: 200 })) {
  const handlers = {};
  const cache = { match: vi.fn(async () => hit), put: vi.fn(), addAll: vi.fn() };
  const caches = { open: vi.fn(async () => cache), match: vi.fn(async () => hit), keys: vi.fn(async () => []) };
  const self = { addEventListener: (name, fn) => { handlers[name] = fn; }, location: { origin: 'https://app.test' } };
  const src = readFileSync('app/static/sw.js', 'utf8');
  runInNewContext(src, { self, caches, fetch: fetchImpl, URL, Response, AbortController, setTimeout, clearTimeout,
    location: self.location });
  return { handlers, caches };
}

function request(handlers, url = 'https://app.test/', mode = 'navigate') {
  let result;
  handlers.fetch({ request: { url, mode, method: 'GET' }, respondWith(p) { result = p; } });
  return result;
}

afterEach(() => vi.useRealTimers());

describe('оболочка при сбоях сервера', () => {
  it('503 на страницу использует сохранённую оболочку', async () => {
    const { handlers } = worker(async () => new Response('ошибка', { status: 503 }));
    const result = await request(handlers);
    expect(await result.text()).toBe('cached');
  });
  it('зависший запрос прерывается и отдаёт кэш', async () => {
    vi.useFakeTimers();
    let aborted = false;
    const { handlers } = worker((_req, opts) => new Promise((_resolve, reject) => {
      opts?.signal?.addEventListener('abort', () => { aborted = true; reject(new Error('timeout')); });
    }));
    const pending = request(handlers, 'https://app.test/js/app.js?v=1.72.12', 'cors');
    await vi.advanceTimersByTimeAsync(10000);
    expect(aborted).toBe(true);
    expect(await (await pending).text()).toBe('cached');
  });
  it('обычная 404 не подменяется оболочкой, API не перехватывается', async () => {
    const { handlers } = worker(async () => new Response('not found', { status: 404 }));
    expect((await request(handlers, 'https://app.test/js/missing.js', 'cors')).status).toBe(404);
    expect(request(handlers, 'https://app.test/api/v1/receipts', 'cors')).toBeUndefined();
  });
});
