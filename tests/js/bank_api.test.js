/* v1.70.99 (ADR-29): панель «Банк по API» — разметка, проверка ввода, сценарии в jsdom (API подменён),
   уведомление о возврате из Точки; эскалация в настройках «Что нужно от клиента». Данные синтетические. */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { get: [], post: [], put: [], del: [] };
const base = {
  banks: [{ id: 'tochka', title: 'Точка Банк' }], methods: [{ id: 'oauth', title: 'OAuth 2.0' }, { id: 'jwt', title: 'JWT-ключ' }],
  server_app: false, redirect_uri: 'https://chek.example/api/v1/bank/tochka/callback', company_inn: '7701234567',
  bank: 'tochka', bank_title: 'Точка Банк', method: 'oauth', status: 'none', client_id: '', secret_set: false, jwt_set: false,
  accounts: [], start_date: '2026-10-01', synced_until: null, last_sync_at: null, last_ok_at: null, last_error: '',
  last_result: {}, pending: 0, awaiting: false, consent_expires_at: null,
};
const active = {
  ...base, status: 'active', client_id: 'app-1', secret_set: true, synced_until: '2026-11-04', last_sync_at: '2026-11-04T09:05:00',
  consent_expires_at: '2027-11-04T09:00:00',
  accounts: [{ tail: '0001', currency: 'RUB', loaded: true }, { tail: '0840', currency: 'USD', loaded: false }],
  last_result: { ok: true, operations: 9, saved: 3, matched: 2, open: 1, requests: 1, duplicates: 0, pending: 0 },
};
let state = base;

vi.mock('../../app/static/js/api.js', () => ({
  api: {
    get: vi.fn(async (path) => { calls.get.push(path); return state; }),
    put: vi.fn(async (path, body) => { calls.put.push({ path, body }); state = { ...state, status: 'new', secret_set: true }; return state; }),
    post: vi.fn(async (path, body) => {
      calls.post.push({ path, body });
      if (path.endsWith('/authorize')) return { url: 'https://enter.tochka.com/connect/authorize?state=x' };
      if (path.endsWith('/sync')) return { result: active.last_result, connection: active };
      if (path.endsWith('/jwt')) { state = { ...active, method: 'jwt', jwt_set: true }; return state; }
      return {};
    }),
    del: vi.fn(async (path) => { calls.del.push(path); state = base; return { removed: true }; }),
  },
}));

const bank = await import('../../app/static/js/bank.js');
const { bankConnBody, bankConnectPanel, bankConnHtml, bankReturnNotice, bankStatementSource, bankSyncSummary, bankWhen } = bank;
const { dreqSettingsBody } = await import('../../app/static/js/docreq.js');
const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  Object.keys(calls).forEach((k) => { calls[k] = []; });
  state = base;
  document.body.innerHTML = '<div id="toast-container"></div><div id="box"></div>';
});

describe('банк по API: разметка', () => {
  it('не подключён: настройки открыты, адрес возврата виден, секрета нет', () => {
    document.getElementById('box').innerHTML = bankConnHtml(base);
    const box = document.getElementById('box');
    expect(box.querySelector('details').open).toBe(true);
    expect(box.textContent).toContain('не подключён');
    expect(box.querySelector('.bank-conn-uri').textContent).toBe(base.redirect_uri);
    expect(box.querySelector('#bankc-auth').classList.contains('hidden')).toBe(false);
    expect(box.querySelector('#bankc-jwtcheck').classList.contains('hidden')).toBe(true);
    expect(box.querySelector('#bankc-sync')).toBeNull();
    expect(box.querySelector('#bankc-del')).toBeNull();
    expect(box.querySelector('#bankc-secret').value).toBe('');
  });

  it('подключён: счета, валютный не загружается, итог последней загрузки, кнопки', () => {
    const html = bankConnHtml(active);
    expect(html).toContain('*0001 RUB');
    expect(html).toContain('*0840 USD — не загружается');
    expect(html).toContain('выписка загружена по 04.11.2026');
    expect(html).toContain('доступ до 04.11.2027');
    expect(html).toContain('нужны документы: 3');
    expect(html).toContain('Подключить заново');
    expect(html).toContain('id="bankc-sync"');
    expect(html).toContain('id="bankc-del"');
    expect(html).toContain('задан — пусто, не менять');
    expect(html).not.toContain('<details open');
  });

  it('ошибки и ожидание экранируются, неизвестный статус — «не подключён»', () => {
    const html = bankConnHtml({ ...base, status: 'weird', last_error: 'Банк <b>не</b> ответил', awaiting: true, pending: 1 });
    expect(html).toContain('Банк &lt;b&gt;не&lt;/b&gt; ответил');
    expect(html).toContain('ссылка действует 15 минут');
    expect(html).toContain('Банк готовит выписку');
    expect(html).toContain('не подключён');
  });

  it('итоги, время и источник выписки', () => {
    expect(bankSyncSummary({ ok: true, operations: 5, saved: 2, matched: 1, open: 1, duplicates: 1, pending: 1 }))
      .toBe('операций: 5 · нужны документы: 2 · документ найден: 1 · без документов: 1 · уже были в загруженных файлах: 1'
        + ' · банк ещё готовит выписку — заберём через несколько минут');
    expect(bankSyncSummary({ ok: false, error: 'Банк недоступен' })).toBe('Банк недоступен');
    expect(bankWhen('')).toBe('—');
    expect(bankWhen('2026-11-04T09:05:00')).toMatch(/^\d\d\.\d\d\.2026 \d\d:\d\d$/);
    expect(bankStatementSource({ source: 'tochka', uploaded_by: '' })).toBe('Точка, API, сама');
    expect(bankStatementSource({ source: 'tochka', uploaded_by: 'Бухгалтер' })).toBe('Точка, API · Бухгалтер');
    expect(bankStatementSource({ source: 'file', uploaded_by: 'Бухгалтер' })).toBe('Бухгалтер');
    expect(bankStatementSource({})).toBe('файл');
  });
});

describe('банк по API: проверка ввода', () => {
  const form = (data) => {
    document.getElementById('box').innerHTML = bankConnHtml(data);
    return (id) => document.getElementById(id);
  };

  it('OAuth: client_id и секрет без пробелов, компания администратора', () => {
    const get = form(base);
    get('bankc-client').value = ' app-1 ';
    get('bankc-secret').value = 's3cret';
    expect(bankConnBody(get, 'c1')).toEqual({ bank: 'tochka', method: 'oauth', company_id: 'c1', start_date: '2026-10-01',
      client_id: 'app-1', client_secret: 's3cret' });
    get('bankc-secret').value = 'с пробелом';
    expect(bankConnBody(get, '').error).toContain('без пробелов');
  });

  it('JWT: три части через точку, пробелы и переносы убираются', () => {
    const get = form({ ...base, method: 'jwt' });
    expect(get('bankc-jwt-box').classList.contains('hidden')).toBe(false);
    get('bankc-jwt').value = 'aaa.bbb\n.ccc';
    expect(bankConnBody(get, '')).toMatchObject({ method: 'jwt', jwt: 'aaa.bbb.ccc' });
    get('bankc-jwt').value = 'not-a-jwt';
    expect(bankConnBody(get, '').error).toContain('три части');
  });
});

describe('банк по API: панель в работе', () => {
  it('сохранить → войти в Точку: сначала PUT, затем адрес входа', async () => {
    const assign = vi.fn();
    const original = window.location;
    Object.defineProperty(window, 'location', { configurable: true, value: { ...original, assign } });
    try {
      const box = document.getElementById('box');
      await bankConnectPanel(box, 'c1');
      expect(calls.get).toEqual(['/api/v1/bank/connection?company_id=c1']);
      box.querySelector('#bankc-client').value = 'app-1';
      box.querySelector('#bankc-secret').value = 's3cret';
      box.querySelector('#bankc-auth').click();
      await flush(); await flush();
      expect(calls.put[0].body).toMatchObject({ company_id: 'c1', client_id: 'app-1', client_secret: 's3cret' });
      expect(calls.post[0]).toEqual({ path: '/api/v1/bank/connection/authorize', body: { company_id: 'c1' } });
      expect(assign).toHaveBeenCalledWith('https://enter.tochka.com/connect/authorize?state=x');
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: original });
    }
  });

  it('смена способа переключает поля и кнопки без перерисовки', async () => {
    const box = document.getElementById('box');
    await bankConnectPanel(box, '');
    const method = box.querySelector('#bankc-method');
    method.value = 'jwt';
    method.dispatchEvent(new Event('change'));
    const hidden = (id) => box.querySelector(id).classList.contains('hidden');
    expect(hidden('#bankc-oauth')).toBe(true);
    expect(hidden('#bankc-jwt-box')).toBe(false);
    expect(hidden('#bankc-auth')).toBe(true);
    expect(hidden('#bankc-jwtcheck')).toBe(false);
    box.querySelector('#bankc-jwt').value = 'aaa.bbb.ccc';
    box.querySelector('#bankc-jwtcheck').click();
    await flush(); await flush();
    expect(calls.put[0].body).toMatchObject({ method: 'jwt', jwt: 'aaa.bbb.ccc' });
    expect(calls.post.map((c) => c.path)).toEqual(['/api/v1/bank/connection/jwt']);
    expect(box.textContent).toContain('подключён');
  });

  it('«Загрузить сейчас» обновляет панель и список операций; «Отключить» спрашивает подтверждение', async () => {
    state = active;
    const onSynced = vi.fn();
    const box = document.getElementById('box');
    await bankConnectPanel(box, 'c1', { onSynced });
    box.querySelector('#bankc-sync').click();
    await flush(); await flush();
    expect(calls.post.at(-1)).toEqual({ path: '/api/v1/bank/connection/sync', body: { company_id: 'c1' } });
    expect(onSynced).toHaveBeenCalledWith(active.last_result);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    box.querySelector('#bankc-del').click();
    await flush();
    expect(calls.del).toEqual([]);
    box.querySelector('#bankc-del').click();
    await flush(); await flush();
    expect(calls.del).toEqual(['/api/v1/bank/connection?company_id=c1']);
    confirm.mockRestore();
  });
});

describe('возврат из Точки', () => {
  it('подключено: уведомление, компания, адрес без параметров', () => {
    window.history.replaceState(null, '', '/?bank=connected&company=c1#/bank');
    expect(bankReturnNotice()).toEqual({ status: 'connected', company: 'c1', reason: '' });
    expect(window.location.search).toBe('');
    expect(window.location.hash).toBe('#/bank');
    expect(document.getElementById('toast-container').textContent).toContain('Точка подключена');
  });

  it('ошибка: причина в уведомлении (экранирована); чужие параметры — не наши', () => {
    window.history.replaceState(null, '', '/?bank=error&reason=%3Cb%3E%D0%9D%D0%B5%D1%82%3C%2Fb%3E#/bank');
    expect(bankReturnNotice()).toMatchObject({ status: 'error', reason: '<b>Нет</b>' });
    expect(document.getElementById('toast-container').innerHTML).toContain('&lt;b&gt;Нет&lt;/b&gt;');
    window.history.replaceState(null, '', '/?bank=hack#/dashboard');
    expect(bankReturnNotice()).toBeNull();
  });
});

describe('«Что нужно от клиента»: эскалация руководителю (v1.70.99)', () => {
  const settingsForm = (value) => {
    const fields = {
      'dreq-s-days': { value: '3, 1, 0' }, 'dreq-s-enabled': { checked: true }, 'dreq-s-auto': { checked: true },
      'dreq-s-day': { value: '5' }, 'dreq-s-hour': { value: '18:00' }, 'dreq-s-every': { value: '2' }, 'dreq-s-times': { value: '3' },
      'dreq-s-email': { value: '' }, 'dreq-s-escalate': value === undefined ? null : { value },
    };
    return (id) => (id in fields ? fields[id] : { checked: false, value: '' });
  };

  it('0–30 дней, по умолчанию 3; вне диапазона — ошибка', () => {
    expect(dreqSettingsBody(settingsForm('5'), 'c1')).toMatchObject({ escalate_days: 5, company_id: 'c1' });
    expect(dreqSettingsBody(settingsForm('0'), '')).toMatchObject({ escalate_days: 0 });
    expect(dreqSettingsBody(settingsForm(undefined), '')).toMatchObject({ escalate_days: 3 });
    expect(dreqSettingsBody(settingsForm('31'), '').error).toContain('0–30');
    expect(dreqSettingsBody(settingsForm('2.5'), '').error).toContain('0–30');
  });
});
