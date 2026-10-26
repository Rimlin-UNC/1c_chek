/* v1.70.60: юнит-тесты транспортного слоя (Vitest, среда jsdom).
   Хранилище токена и форма ошибки API. */
import { beforeEach, describe, expect, it } from 'vitest';
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
