/* v1.70.103 (T-74): js/boot.js — решения до первой отрисовки (Vitest, jsdom).
   Гость без флага «заставка гостям» — класс ym-guest (заставки нет, стартовая страница видна сразу); есть токен или
   особый адрес (#/…, ?maxlogin=) — класс ym-hold (стартовая страница не мелькает до решения app.js); якорь
   стартовой страницы (#main, #land-how) особым адресом не считаются; недоступное хранилище — как гость. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

const root = path.resolve(__dirname, '../..');
const bootJs = fs.readFileSync(path.join(root, 'app/static/js/boot.js'), 'utf8');

function run({ token = null, guests = null, url = '/' } = {}) {
  document.documentElement.className = '';
  localStorage.clear();
  if (token !== null) localStorage.setItem('ymaster_check_token', token);
  if (guests !== null) localStorage.setItem('ym-splash-guests', guests);
  window.history.replaceState(null, '', url);
  (0, eval)(bootJs);
  return document.documentElement.classList;
}

describe('boot.js — до первой отрисовки', () => {
  afterEach(() => { vi.restoreAllMocks(); window.history.replaceState(null, '', '/'); });

  it('гость по умолчанию: без заставки, стартовая страница не придержана', () => {
    const cls = run();
    expect(cls.contains('ym-guest')).toBe(true);
    expect(cls.contains('ym-hold')).toBe(false);
  });

  it('администратор включил заставку гостям — гость её видит', () => {
    const cls = run({ guests: '1' });
    expect(cls.contains('ym-guest')).toBe(false);
    expect(cls.contains('ym-hold')).toBe(false);
  });

  it('вошедший: заставка как раньше, стартовая страница придержана', () => {
    const cls = run({ token: 'jwt.token.value' });
    expect(cls.contains('ym-guest')).toBe(false);
    expect(cls.contains('ym-hold')).toBe(true);
  });

  it('особый адрес гостя (#/public, ?maxlogin=) придерживает стартовую страницу; #land-how — нет', () => {
    expect(run({ url: '/#/public' }).contains('ym-hold')).toBe(true);
    expect(run({ url: '/?maxlogin=abc' }).contains('ym-hold')).toBe(true);
    const anchor = run({ url: '/#land-how' });
    expect(anchor.contains('ym-hold')).toBe(false);
    expect(anchor.contains('ym-guest')).toBe(true);
  });

  it('якорь доступности #main остаётся на главной, а не уходит в маршрут', () => {
    const cls = run({ url: '/#main' });
    expect(cls.contains('ym-hold')).toBe(false);
    expect(cls.contains('ym-guest')).toBe(true);
  });

  it('хранилище недоступно — считаем гостем, без ошибок', () => {
    document.documentElement.className = '';
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('SecurityError'); });
    expect(() => (0, eval)(bootJs)).not.toThrow();
    expect(document.documentElement.classList.contains('ym-guest')).toBe(true);
  });
});
