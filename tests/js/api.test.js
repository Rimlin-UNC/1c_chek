/* v1.70.60: юнит-тесты транспортного слоя (Vitest, среда jsdom).
   Хранилище токена и форма ошибки API. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, clearToken, getToken, setToken } from '../../app/static/js/api.js';

describe('токен сессии', () => {
  beforeEach(() => clearToken());

  it('без токена — пустая строка', () => {
    expect(getToken()).toBe('');
  });
  it('круговой путь: запись — чтение — сброс', () => {
    setToken('abc123');
    expect(getToken()).toBe('abc123');
    clearToken();
    expect(getToken()).toBe('');
  });
  it('токен живёт в localStorage кабинета', () => {
    setToken('xyz');
    expect(localStorage.getItem('ymaster_check_token')).toBe('xyz');
  });
});

describe('ApiError', () => {
  it('несёт статус, текст и тело ответа', () => {
    const e = new ApiError(401, 'не принят', { detail: 'x' });
    expect(e).toBeInstanceOf(Error);
    expect(e.status).toBe(401);
    expect(e.message).toBe('не принят');
    expect(e.data).toEqual({ detail: 'x' });
  });
  it('статус 0 — сеть недоступна', () => {
    expect(new ApiError(0, 'сеть').status).toBe(0);
  });
});

describe('фиксированный контекст отложенного запроса', () => {
  it('403 клиента не сбрасывает выбор и не повторяет запись в собственной компании', async () => {
    const { api, setClient, getClient } = await import('../../app/static/js/api.js');
    setClient('current-client');
    const original = globalThis.fetch;
    const calls = [];
    globalThis.fetch = async (path, init) => {
      calls.push({ path, headers: init.headers });
      return new Response(JSON.stringify({ detail: 'Нет доступа к компании клиента' }), {
        status: 403, headers: { 'Content-Type': 'application/json' },
      });
    };
    try {
      await expect(api.post('/api/v1/receipts/scan/offline', {}, {
        clientId: 'original-client', noClientFallback: true, retries: 1,
      })).rejects.toMatchObject({ status: 403 });
      expect(calls).toHaveLength(1);
      expect(calls[0].headers['X-Ym-Company']).toBe('original-client');
      expect(getClient()).toBe('current-client');
    } finally { globalThis.fetch = original; setClient(''); }
  });
  it('пустой зафиксированный клиент не заменяется текущим', async () => {
    const { api, setClient } = await import('../../app/static/js/api.js');
    setClient('current-client');
    const original = globalThis.fetch;
    let headers;
    globalThis.fetch = async (_path, init) => {
      headers = init.headers;
      return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
    };
    try {
      await api.post('/api/v1/receipts/scan/offline', {}, { clientId: null, noClientFallback: true, retries: 1 });
      expect(headers['X-Ym-Company']).toBeUndefined();
    } finally { globalThis.fetch = original; setClient(''); }
  });
});

describe('индикатор связи использует уже выполняемые запросы', () => {
  it('сообщает об успехе и сетевой ошибке без дополнительных запросов', async () => {
    const { api } = await import('../../app/static/js/api.js');
    const original = globalThis.fetch;
    const events = [];
    const listener = e => events.push(e.detail);
    window.addEventListener('ymaster:api-connection', listener);
    try {
      globalThis.fetch = vi.fn().mockResolvedValueOnce(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
        .mockRejectedValueOnce(new Error('offline'));
      await api.get('/api/v1/test', { retries: 1 });
      await expect(api.get('/api/v1/test', { retries: 1 })).rejects.toMatchObject({ status: 0 });
      expect(globalThis.fetch).toHaveBeenCalledTimes(2);
      expect(events.map(e => e.ok)).toEqual([true, false]);
      expect(events[1].id).toBeGreaterThan(events[0].id);
    } finally { globalThis.fetch = original; window.removeEventListener('ymaster:api-connection', listener); }
  });
  it('ошибка внешнего сервиса 502 не означает потерю связи с приложением', async () => {
    const { api } = await import('../../app/static/js/api.js');
    const original = globalThis.fetch;
    const listener = vi.fn();
    window.addEventListener('ymaster:api-connection', listener);
    try {
      globalThis.fetch = vi.fn().mockResolvedValue(new Response('{"detail":"Нет связи с GitHub"}', { status: 502, headers: { 'Content-Type': 'application/json' } }));
      await expect(api.get('/api/v1/test', { retries: 1 })).rejects.toMatchObject({ status: 502 });
      expect(listener.mock.calls.map(([event]) => event.detail.ok)).not.toContain(false);
    } finally { globalThis.fetch = original; window.removeEventListener('ymaster:api-connection', listener); }
  });
  it('отменённый запрос не означает потерю связи', async () => {
    const { api } = await import('../../app/static/js/api.js');
    const original = globalThis.fetch;
    const listener = vi.fn();
    window.addEventListener('ymaster:api-connection', listener);
    const controller = new AbortController();
    controller.abort();
    try {
      globalThis.fetch = vi.fn().mockRejectedValue(new Error('abort'));
      await expect(api.get('/api/v1/test', { retries: 1, signal: controller.signal })).rejects.toMatchObject({ status: 0 });
      expect(listener).not.toHaveBeenCalled();
    } finally { globalThis.fetch = original; window.removeEventListener('ymaster:api-connection', listener); }
  });
});

describe('конкурирующие сеансы', () => {
  it('поздний 401 старого токена не удаляет токен нового пользователя', async () => {
    const { api } = await import('../../app/static/js/api.js');
    setToken('old-token');
    const original = globalThis.fetch;
    let resolveFetch;
    globalThis.fetch = () => new Promise(resolve => { resolveFetch = resolve; });
    try {
      const pending = api.get('/api/v1/auth/me', { retries: 1 });
      setToken('new-token');
      resolveFetch(new Response('{}', { status: 401 }));
      await expect(pending).rejects.toMatchObject({ status: 401 });
      expect(getToken()).toBe('new-token');
    } finally { globalThis.fetch = original; clearToken(); }
  });
});
