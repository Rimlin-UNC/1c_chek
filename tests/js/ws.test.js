/* v1.72.1 (T-91): живые обновления идут на активного клиента бухгалтера. Окно проверяет клиентскую часть
   app.js — адрес сокета с ?company=, переключение клиента сообщением без переподключения, отказ сервера.
   Как и другие оконные тесты, нужные функции вырезаются из исходника: импорт app.js тянет загрузку приложения. */
import { describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';

const source = readFileSync('app/static/js/app.js', 'utf8');

function pick(start, end) {
  const a = source.indexOf(start);
  const b = source.indexOf(end, a + start.length);
  if (a < 0 || b < 0) throw new Error(`не найден фрагмент: ${start}`);
  return source.slice(a, b);
}

// connectWS + wsSwitchClient + scheduleReconnect собираются вместе: они делят state и reconnect-таймер
const wsCode = pick('function connectWS()', '\nfunction handleWsEvent(').replace('export function', 'function');
const build = () => new Function(
  'WebSocket', 'location', 'state', 'getClient', 'getToken', 'toast', 'handleWsEvent', 'connectionStatus', 'getApiRequestId',
  `const WS_BACKOFF = [2, 5, 10, 20, 60, 120, 300], WS_STALE_MS = 70000, WS_CONNECT_MS = 20000; ${wsCode}; return { connectWS, wsSwitchClient, stopWs };`,
);

function harness({ role = 'accountant', client = '' } = {}) {
  const state = { me: { role }, ws: null, wsClientSent: '' };
  const sent = [];
  class FakeWS {
    constructor(url) { this.url = url; this.readyState = 1; this.sent = []; state.ws = this; }
    send(data) { this.sent.push(data); sent.push(data); }
    close() { this.readyState = 3; }
  }
  const toast = vi.fn();
  const factory = build();
  const app = factory(FakeWS, { protocol: 'https:', host: 'check.example' }, state, () => client, () => 'tok',
    toast, vi.fn(), { wsOpen: vi.fn(), wsClosed: vi.fn(), online: vi.fn(), offline: vi.fn(), update: vi.fn() }, () => 0);
  return { app, state, sent, toast, setClient: (value) => { client = value; } };
}

describe('живые обновления по активному клиенту (T-91)', () => {
  it('без активного клиента адрес сокета прежний — без ?company', () => {
    const { app, state } = harness({ client: '' });
    app.connectWS();
    expect(state.ws.url).toBe('wss://check.example/ws/status');
  });

  it('активный клиент бухгалтера уходит в ?company= (события компании клиента)', () => {
    const { app, state } = harness({ client: 'c-a' });
    app.connectWS();
    expect(state.ws.url).toBe('wss://check.example/ws/status?company=c-a');
  });

  it('администратор своего активного клиента не передаёт — он видит всё', () => {
    const { app, state } = harness({ role: 'admin', client: 'c-a' });
    app.connectWS();
    expect(state.ws.url).toBe('wss://check.example/ws/status');
  });

  it('переключение клиента — сообщением type=client, сокет тот же', () => {
    const { app, state, sent } = harness({ client: 'c-a' });
    app.connectWS();
    const socket = state.ws;
    expect(app.wsSwitchClient('c-b')).toBe(true);
    expect(JSON.parse(sent[0])).toEqual({ type: 'client', company: 'c-b' });
    expect(state.ws).toBe(socket);
  });

  it('смена клиента во время подключения применяется после открытия', () => {
    vi.useFakeTimers();
    try {
      const { app, state, sent, setClient } = harness({ client: 'c-a' });
      app.connectWS();
      state.ws.readyState = 0;
      setClient('c-b');
      expect(app.wsSwitchClient('c-b')).toBe(false);
      state.ws.readyState = 1;
      state.wsOk = true;
      state.ws.onopen();
      expect(JSON.parse(sent[0])).toEqual({ type: 'client', company: 'c-b' });
    } finally { vi.useRealTimers(); }
  });

  it('предупреждение при подключении показывается пользователю', () => {
    const { app, state, toast } = harness({ client: 'c-a' });
    app.connectWS();
    state.ws.onmessage({ data: JSON.stringify({ type: 'connected', payload: { warning: 'Нет доступа' } }) });
    expect(toast).toHaveBeenCalledWith('Нет доступа', 'err', 'Живые обновления');
  });

  it('повторное открытие не создаёт второй сокет и не отправляет клиентские ping', () => {
    vi.useFakeTimers();
    try {
      const { app, state, sent } = harness({ client: '' });
      app.connectWS();
      const socket = state.ws;
      socket.onopen();
      app.connectWS();
      expect(state.ws).toBe(socket);
      vi.advanceTimersByTime(30000);
      expect(sent).toEqual([]);
      app.stopWs();
    } finally { vi.useRealTimers(); }
  });

  it('зависшее подключение закрывается и не накапливает сокеты', () => {
    vi.useFakeTimers();
    try {
      const { app, state } = harness({ client: '' });
      app.connectWS();
      const old = state.ws;
      old.readyState = 0;
      vi.advanceTimersByTime(20000);
      expect(old.readyState).toBe(3);
      expect(state.ws).toBeNull();
      expect(state.wsAttempts).toBe(1);
      app.stopWs();
    } finally { vi.useRealTimers(); }
  });

  it('нет ответов heartbeat: полуоткрытый сокет заменяется с задержкой', () => {
    vi.useFakeTimers();
    try {
      const { app, state } = harness({ client: '' });
      app.connectWS();
      const old = state.ws;
      old.onopen();
      vi.advanceTimersByTime(70000);
      expect(old.readyState).toBe(3);
      expect(state.ws).toBeNull();
      expect(state.wsAttempts).toBe(1);
      app.stopWs();
    } finally { vi.useRealTimers(); }
  });

  it('старый закрывшийся сокет не сбрасывает новый', () => {
    const { app, state } = harness({ client: '' });
    app.connectWS();
    const old = state.ws;
    app.stopWs();
    app.connectWS();
    const current = state.ws;
    old.onclose();
    expect(state.ws).toBe(current);
    expect(state.wsAttempts).toBeUndefined();
    app.stopWs();
  });

  it('без открытого сокета переключение молча не делается', () => {
    const { app } = harness({ client: 'c-a' });
    expect(app.wsSwitchClient('c-b')).toBe(false);
  });
});
