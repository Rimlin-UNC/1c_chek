/* v1.70.69: вход через Telegram на экране входа (Vitest, jsdom).

   Владелец: «Регистрация через Telegram «как у Макса» — следующий выпуск».
   Проверяется настоящий app.js в jsdom (приём файла splash.test.js):
   кнопка «Войти или зарегистрироваться через Telegram» запрашивает код,
   открывает окно ожидания с кодом и кнопкой «Открыть Telegram», опрос
   доводит вход до сессии — окно закрывается, экран входа сменяется
   приложением. Сеть — заглушка fetch, ссылка открывается только кнопкой. */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import * as ui from '../../app/static/js/ui.js';
import { api, getToken, setToken, clearToken } from '../../app/static/js/api.js';

const root = path.resolve(__dirname, '../..');
const appJs = fs.readFileSync(path.join(root, 'app/static/js/app.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(root, 'app/static/index.html'),
  'utf8');

const stub = () => () => {};
Object.assign(globalThis, {
  api, getToken, setToken, clearToken,
  toast: ui.toast, esc: ui.esc, fmtSum: ui.fmtSum, fmtInt: ui.fmtInt,
  fmtDate: ui.fmtDate, statusLabel: ui.statusLabel, roleLabel: ui.roleLabel,
  chip: ui.chip, openModal: ui.openModal, animateNumber: ui.animateNumber,
  injectIcons: stub(),
  barChart: stub(), donutChart: stub(),
  CameraScanner: class {}, decodeImageFile: stub(),
  offlineQueue: { size: () => 0, all: () => [], shift: () => {},
    push: () => 0 },
  parseQrClient: stub(),
  packCut: stub(), splitBlocks: stub(), COL_H: 0, columnX: stub(),
  SCALE: 1, RCPT_W: 0,
  osmCardHtml: stub(), osmWire: stub(), osmStop: stub(),
});

const bodyHtml = indexHtml
  .split('<body').slice(1).join('<body')
  .replace(/^[^>]*>/, '')
  .replace(/<\/body>[\s\S]*$/, '');

function json(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (n) => (n === 'content-type' ? 'application/json' : '') },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

const CODE = 'tglogin_' + 'B'.repeat(32);
const USER = { id: 'u-1', username: 'tg_egor', full_name: 'Егор Тестов',
  role: 'user' };

function stubFetch(state) {
  global.fetch = vi.fn((url) => {
    const u = String(url);
    if (u.includes('/api/v1/about')) {
      return Promise.resolve(json({ version: '1.70.69',
        max_bot_username: 'chek.ymaster.bot' }));
    }
    if (u.includes('/api/v1/auth/telegram/start')) {
      state.started += 1;
      return Promise.resolve(json({
        code: CODE, expires_in: 600,
        tg_link: 'https://t.me/chek_ymaster_bot?start=' + CODE }));
    }
    if (u.includes('/api/v1/auth/telegram/poll')) {
      state.polls += 1;
      if (state.polls === 1) return Promise.resolve(json({ status: 'pending' }));
      return Promise.resolve(json({ status: 'ok', access_token: 'tok-1',
        token_type: 'bearer', user: USER }));
    }
    if (u.includes('/api/v1/auth/me')) return Promise.resolve(json(USER));
    return Promise.resolve(json({}, 404));
  });
}

function browserStubs() {
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
    window.IntersectionObserver = class {
      observe() {} unobserve() {} disconnect() {};
    };
  }
}

function bootApp(state) {
  document.body.innerHTML = bodyHtml;
  localStorage.clear();
  browserStubs();
  stubFetch(state);
  window.history.replaceState(null, '', '/');
  const source = appJs.split('\n').filter((l) => !l.startsWith('import '))
    .join('\n');
  (0, eval)(source);
}

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

describe('вход через Telegram (v1.70.69)', () => {
  beforeEach(() => {
    document.body.innerHTML = '';
    localStorage.clear();
    clearToken();
  });

  it('кнопка на экране входа есть и подписана понятно', () => {
    bootApp({ started: 0, polls: 0 });
    const btn = document.getElementById('login-telegram');
    expect(btn).not.toBeNull();
    expect(btn.textContent).toContain('Telegram');
    expect(btn.textContent.toLowerCase()).toContain('зарегистрироваться');
  });

  it('клик: код в окне, ссылка t.me — только кнопкой, вход после подтверждения',
    async () => {
      const state = { started: 0, polls: 0 };
      const openSpy = vi.fn();
      window.open = openSpy;
      bootApp(state);
      document.getElementById('login-telegram').click();
      await wait(60);

      expect(state.started).toBe(1);                 // код запрошен
      const modal = document.getElementById('modal-root');
      expect(modal.classList.contains('hidden')).toBe(false);
      expect(modal.textContent).toContain('Вход через Telegram');
      const field = document.getElementById('tgl-code');
      expect(field).not.toBeNull();
      expect(field.value).toBe(CODE);                // код виден человеку
      expect(openSpy).not.toHaveBeenCalled();        // сам ничего не открыл

      const open = document.getElementById('tgl-open');
      expect(open).not.toBeNull();
      open.click();
      expect(openSpy).toHaveBeenCalledTimes(1);      // только по нажатию
      expect(openSpy.mock.calls[0][0])
        .toBe('https://t.me/chek_ymaster_bot?start=' + CODE);

      await wait(5400);                              // второй опрос — «ok»
      expect(state.polls).toBeGreaterThanOrEqual(2);
      expect(getToken()).toBe('tok-1');              // сессия сайта выдана
      // окно входа закрылось (следом приложение показывает «Что нового»)
      expect(document.getElementById('tgl-code')).toBeNull();
      expect(document.getElementById('tgl-open')).toBeNull();
      expect(document.getElementById('login-screen').classList.contains('hidden'))
        .toBe(true);
      expect(document.getElementById('app-shell').classList.contains('hidden'))
        .toBe(false);
    }, 20000);

  it('без имени бота окно без кнопки: остаётся код и подсказка', async () => {
    const state = { started: 0, polls: 0 };
    bootApp(state);
    // второй запрос кода — уже без ссылки (бот ещё не опрошен)
    global.fetch = vi.fn((url) => {
      const u = String(url);
      if (u.includes('/api/v1/about')) {
        return Promise.resolve(json({ version: '1.70.69' }));
      }
      if (u.includes('/api/v1/auth/telegram/start')) {
        return Promise.resolve(json({ code: CODE, expires_in: 600 }));
      }
      if (u.includes('/api/v1/auth/telegram/poll')) {
        return Promise.resolve(json({ status: 'expired' }));
      }
      if (u.includes('/api/v1/auth/me')) return Promise.resolve(json({}, 401));
      return Promise.resolve(json({}, 404));
    });
    document.getElementById('login-telegram').click();
    await wait(60);
    const modal = document.getElementById('modal-root');
    expect(modal.textContent).toContain('Вход через Telegram');
    expect(document.getElementById('tgl-code').value).toBe(CODE);
    expect(document.getElementById('tgl-open')).toBeNull();
  });
});
