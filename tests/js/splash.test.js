/* v1.70.66: регрессия «висячая заставка» на входе по билету (Vitest, jsdom).

   Владелец: «не работает после последовательных действий зависает как на
   скрине» — скрин: заставка загрузки (логотип + спиннер) поверх входа по
   билету из мини-приложения MAX. Дефект: ранняя ветка boot() с билетом
   (#/max-login/ с v1.70.0, ?maxlogin= с v1.70.65) не звала showSplash(false),
   и вход с модальным окном ожидания оставались под заставкой z-index 300.

   Тест поднимает НАСТОЯЩИЙ app.js в jsdom: разметка index.html, заглушка
   fetch, билет в адресе — и сверяет, что заставка снята, экран входа и
   модальное окно ожидания видимы. Три сценария: билет в запросе, билет
   во фрагменте, обычный вход без токена. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import * as ui from '../../app/static/js/ui.js';
import { api, getToken, setToken, clearToken } from '../../app/static/js/api.js';

const root = path.resolve(__dirname, '../..');
const appJs = fs.readFileSync(path.join(root, 'app/static/js/app.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(root, 'app/static/index.html'),
  'utf8');

// app.js — ES-модуль: в eval импорты не живут, поэтому имена из import
// выставляем глобально. ui.js настоящий (openModal, esc, toast), прочие
// модули на пути загрузки не нужны — достаточно стабов.
const stub = () => () => {};
Object.assign(globalThis, {
  api, getToken, setToken, clearToken,
  toast: ui.toast, esc: ui.esc, fmtSum: ui.fmtSum, fmtInt: ui.fmtInt,
  fmtDate: ui.fmtDate, statusLabel: ui.statusLabel, roleLabel: ui.roleLabel,
  chip: ui.chip, openModal: ui.openModal, animateNumber: ui.animateNumber,
  injectIcons: stub(),
  barChart: stub(), donutChart: stub(),
  CameraScanner: class {}, decodeImageFile: stub(), offlineQueue: {},
  parseQrClient: stub(),
  packCut: stub(), splitBlocks: stub(), COL_H: 0, columnX: stub(),
  SCALE: 1, RCPT_W: 0,
  osmCardHtml: stub(), osmWire: stub(), osmStop: stub(),
});

const bodyHtml = indexHtml
  .split('<body').slice(1).join('<body')
  .replace(/^[^>]*>/, '')
  .replace(/<\/body>[\s\S]*$/, '');

// контракт api.js: ok, status, headers.get, json()/text()
function json(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (n) => (n === 'content-type' ? 'application/json' : '') },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function stubFetch() {
  const pollCalls = { n: 0 };
  global.fetch = vi.fn((url) => {
    const u = String(url);
    if (u.includes('/api/v1/about')) {
      return Promise.resolve(json({ version: '1.70.66',
        max_bot_username: 'chek.ymaster.bot' }));
    }
    if (u.includes('/api/v1/auth/max/poll')) {
      // первый опрос — «ждём», второй — «билет устарел»: цикл поллинга
      // завершается и не держит событие теста открытым
      pollCalls.n += 1;
      return Promise.resolve(json(pollCalls.n === 1
        ? { status: 'pending' } : { status: 'expired' }));
    }
    if (u.includes('/api/v1/auth/me')) return Promise.resolve(json({}, 401));
    return Promise.resolve(json({}, 404));
  });
}

function browserStubs() {
  // jsdom даёт не всё, что есть в настоящем браузере; app.js рассчитывает
  // на браузер — добавляем минимум, недостающий среде теста
  if (!window.matchMedia) {
    window.matchMedia = () => ({
      matches: false, media: '', onchange: null,
      addListener: () => {}, removeListener: () => {},
      addEventListener: () => {}, removeEventListener: () => {},
      dispatchEvent: () => false,
    });
  }
  if (!window.ResizeObserver) {
    window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
  }
  if (!window.IntersectionObserver) {
    window.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} };
  }
}

function bootApp(address) {
  document.body.innerHTML = bodyHtml;
  localStorage.clear();
  browserStubs();
  stubFetch();
  window.history.replaceState(null, '', address);
  const source = appJs
    .split('\n')
    .filter((l) => !l.startsWith('import '))
    .join('\n');
  (0, eval)(source);          // boot() вызывается в конце модуля
}

const TICKET = 'login_' + 'a'.repeat(40);

describe('заставка на входе по билету (v1.70.66)', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    localStorage.clear();
  });

  it('билет в запросе: заставка снята, вход и окно ожидания видимы', () => {
    bootApp('/?maxlogin=' + TICKET);
    const splash = document.getElementById('boot-splash');
    expect(splash).not.toBeNull();
    expect(splash.style.display).toBe('none');
    const login = document.getElementById('login-screen');
    expect(login.classList.contains('hidden')).toBe(false);
    // модальное окно поллинга билета открыто поверх формы входа
    const modal = document.getElementById('modal-root');
    expect(modal.classList.contains('hidden')).toBe(false);
    expect(modal.textContent).toContain('Вход через MAX');
  });

  it('билет во фрагменте (поток приглашений): заставка снята', () => {
    bootApp('/#/max-login/' + TICKET);
    const splash = document.getElementById('boot-splash');
    expect(splash.style.display).toBe('none');
  });

  it('без билета и токена: заставка снята, форма входа на экране', () => {
    bootApp('/');
    const splash = document.getElementById('boot-splash');
    expect(splash.style.display).toBe('none');
    const login = document.getElementById('login-screen');
    expect(login.classList.contains('hidden')).toBe(false);
  });
});
