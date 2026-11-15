import { afterEach, describe, expect, it, vi } from 'vitest';
import { createConnectionStatus } from '../../app/static/js/connection_status.js';

function setup() {
  const render = vi.fn();
  const status = createConnectionStatus({ render });
  return { status, render, current: () => render.mock.lastCall[0] };
}

afterEach(() => vi.useRealTimers());

describe('состояние связи с сервером', () => {
  it('не считает доступ к сети браузера подтверждением соединения', () => {
    const { status, current } = setup();
    status.online();
    expect(current().state).toBe('connecting');
    status.wsOpen();
    expect(current().state).toBe('connected');
    expect(current().live).toBe(true);
  });
  it('успешный API работает без WebSocket и явно отмечает отсутствие живых обновлений', () => {
    vi.useFakeTimers();
    const { status, current } = setup();
    status.wsClosed();
    status.apiResult(1, true);
    expect(current()).toMatchObject({ state: 'connected', live: false });
    vi.advanceTimersByTime(120001);
    expect(current().state).toBe('unknown');
  });
  it('офлайн и ошибка запроса красные, восстановление — только по ответу сервера', () => {
    const { status, current } = setup();
    status.apiResult(1, true);
    status.offline();
    expect(current().state).toBe('disconnected');
    status.online();
    expect(current().state).toBe('connecting');
    status.apiResult(2, false);
    expect(current().state).toBe('disconnected');
    status.apiResult(3, true);
    expect(current().state).toBe('connected');
  });
  it('опоздавший отказ старого запроса не отменяет новый успешный ответ', () => {
    const { status, current } = setup();
    status.apiResult(2, true);
    status.apiResult(1, false);
    expect(current().state).toBe('connected');
  });
  it('ошибка API не скрывается открытым WebSocket', () => {
    const { status, current } = setup();
    status.wsOpen();
    status.apiResult(1, false);
    expect(current().state).toBe('disconnected');
    status.apiResult(2, true);
    expect(current().state).toBe('connected');
  });
  it('смена сети отбрасывает завершение запросов начатых ранее', () => {
    const { status, current } = setup();
    status.apiResult(1, true);
    status.offline();
    status.online(3);
    status.apiResult(2, true);
    expect(current().state).toBe('connecting');
    status.apiResult(4, true);
    expect(current().state).toBe('connected');
  });
});
