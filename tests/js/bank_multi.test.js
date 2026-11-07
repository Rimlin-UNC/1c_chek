/* v1.70.102 (ADR-32): несколько банков по API — поля под банк (ключ API, токены, сертификат клиента),
   список банков компании, переключение без перезагрузки, тело запроса, проверка ключа и отключение банка.
   API подменён, данные синтетические. */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { get: [], post: [], put: [], del: [] };
const banks = [
  { id: 'tochka', title: 'Точка Банк', methods: ['oauth', 'jwt'], cert: '', how: 'Интеграции и API', docs: 'https://t' },
  { id: 'tbank', title: 'Т-Банк', methods: ['token'], cert: '', how: 'Т-Бизнес → «Интеграции» → «Выпуск токена»', docs: 'https://tb' },
  { id: 'sber', title: 'Сбербанк', methods: ['refresh'], cert: 'required', how: 'Личный кабинет Sber API', docs: 'https://s' },
  { id: 'alfa', title: 'Альфа-Банк', methods: ['token'], cert: 'optional', how: 'Альфа-Бизнес Онлайн', docs: 'https://a' },
  { id: 'modul', title: 'Модульбанк', methods: ['token'], cert: '', how: 'Интеграция с банком', docs: 'https://m' },
];
const fileBanks = [{ title: 'ВТБ', note: 'по договору' }, { title: 'Газпромбанк', note: 'открытого API нет' }];
const conn = (bank, extra = {}) => ({
  banks, file_banks: fileBanks, methods: [], server_app: false, redirect_uri: 'https://chek.example/api/v1/bank/tochka/callback',
  bank, bank_title: banks.find((b) => b.id === bank).title, method: banks.find((b) => b.id === bank).methods[0],
  status: 'none', client_id: '', secret_set: false, jwt_set: false, token_set: false, refresh_set: false, cert_set: false,
  cert: {}, accounts: [], start_date: '2026-10-01', synced_until: null, last_sync_at: null, last_error: '', last_result: {},
  pending: 0, awaiting: false, consent_expires_at: null, connections: [], ...extra,
});
const tbankActive = conn('tbank', {
  status: 'active', token_set: true, accounts: [{ tail: '0001', currency: 'RUB', loaded: true }], synced_until: '2026-11-04',
  connections: [{ bank: 'tbank', bank_title: 'Т-Банк', status: 'active', accounts: ['0001'], synced_until: '2026-11-04' },
    { bank: 'sber', bank_title: 'Сбербанк', status: 'reauth', accounts: ['0002'], synced_until: null }],
  last_result: { ok: true, operations: 4, saved: 3, matched: 2, open: 1 },
});
let byBank = {};

vi.mock('../../app/static/js/api.js', () => ({
  api: {
    get: vi.fn(async (path) => {
      calls.get.push(path);
      const m = /bank=(\w+)/.exec(path);
      return byBank[m ? m[1] : '_'] || conn(m ? m[1] : 'tochka');
    }),
    put: vi.fn(async (path, body) => { calls.put.push({ path, body }); return conn(body.bank, { status: 'new', token_set: true }); }),
    post: vi.fn(async (path, body) => {
      calls.post.push({ path, body });
      if (path.endsWith('/check')) return { ...tbankActive, bank: body.bank, bank_title: 'Т-Банк' };
      if (path.endsWith('/sync')) return { result: tbankActive.last_result, connection: tbankActive };
      return {};
    }),
    del: vi.fn(async (path) => { calls.del.push(path); return { removed: true }; }),
  },
}));

const bank = await import('../../app/static/js/bank.js');
const { bankConnBody, bankConnectPanel, bankConnHtml, bankFileBanksHtml, bankListHtml, bankMeta, bankStatementSource } = bank;
const flush = () => new Promise((r) => setTimeout(r, 0));
const hidden = (box, id) => box.querySelector(id).classList.contains('hidden');

beforeEach(() => {
  Object.keys(calls).forEach((k) => { calls[k] = []; });
  byBank = {};
  document.body.innerHTML = '<div id="toast-container"></div><div id="box"></div>';
});

describe('несколько банков: разметка под банк', () => {
  it('Т-Банк: только ключ API, подсказка где взять, без входа через Точку и без сертификата', () => {
    const box = document.getElementById('box');
    box.innerHTML = bankConnHtml(conn('tbank'));
    expect(box.querySelector('#bankc-bank').value).toBe('tbank');
    expect([...box.querySelector('#bankc-method').options].map((o) => o.value)).toEqual(['token']);
    expect(hidden(box, '#bankc-token-box')).toBe(false);
    expect(hidden(box, '#bankc-oauth')).toBe(true);
    expect(hidden(box, '#bankc-auth')).toBe(true);
    expect(hidden(box, '#bankc-jwtcheck')).toBe(false);
    expect(box.querySelector('#bankc-cert-box')).toBeNull();
    expect(box.textContent).toContain('Выпуск токена');
    expect(box.querySelector('.bank-conn-uri')).not.toBeNull();             // разметка Точки скрыта, но не мешает
  });

  it('Сбербанк: client_id, secret, refresh_token и обязательный сертификат', () => {
    const box = document.getElementById('box');
    box.innerHTML = bankConnHtml(conn('sber', { cert_set: true, cert: { until: '2027-08-12', fingerprint: 'AB12' } }));
    expect(hidden(box, '#bankc-oauth')).toBe(false);
    expect(hidden(box, '#bankc-refresh-box')).toBe(false);
    expect(hidden(box, '#bankc-token-box')).toBe(true);
    expect(box.querySelector('#bankc-cert-box').textContent).toContain('обязательно');
    expect(box.querySelector('#bankc-cert').getAttribute('placeholder')).toContain('до 12.08.2027');
    expect(box.querySelector('#bankc-cert-file').getAttribute('accept')).toContain('.p12');
  });

  it('Альфа-Банк: сертификат — если банк его выдал; подключён — срок сертификата в фактах', () => {
    const html = bankConnHtml(conn('alfa', { status: 'active', cert_set: true, cert: { until: '2027-01-31' },
      accounts: [{ tail: '0003', currency: 'RUB', loaded: true }] }));
    expect(html).toContain('если банк его выдал');
    expect(html).toContain('сертификат до 31.01.2027');
    expect(html).toContain('Альфа-Банк · счета:');
  });

  it('список банков компании и другие банки — файлом; всё экранируется', () => {
    const list = bankListHtml([{ bank: 'tbank', bank_title: 'Т-Банк', status: 'active', accounts: ['0001'], synced_until: '2026-11-04' },
      { bank: 'sber', bank_title: '<b>Сбер</b>', status: 'reauth', accounts: [] }], 'tbank');
    expect(list).toContain('выписка по 04.11.2026');
    expect(list).toContain('&lt;b&gt;Сбер&lt;/b&gt;');
    expect(list).toContain('data-bank="sber"');
    expect(list).not.toContain('data-bank="tbank"');                        // открытый банк — без кнопки
    expect(bankListHtml([], 'tbank')).toBe('');
    const files = bankFileBanksHtml(fileBanks);
    expect(files).toContain('ВТБ');
    expect(files).not.toContain('<details open');
    expect(bankMeta({}, 'x').methods).toEqual(['oauth', 'jwt']);           // старый ответ сервера — как у Точки
    expect(bankStatementSource({ source: 'tbank', uploaded_by: '' })).toBe('Т-Банк, API, сама');
    expect(bankStatementSource({ source: 'sber', uploaded_by: 'Бухгалтер' })).toBe('Сбербанк, API · Бухгалтер');
  });
});

describe('несколько банков: тело запроса', () => {
  const form = (data) => {
    document.getElementById('box').innerHTML = bankConnHtml(data);
    return (id) => document.getElementById(id);
  };

  it('ключ API: пробелы и переносы убираются, короткий ключ — ошибка', () => {
    const get = form(conn('tbank'));
    get('bankc-token').value = ' t.abc\ndef-123 ';
    expect(bankConnBody(get, 'c1')).toEqual({ bank: 'tbank', method: 'token', company_id: 'c1', start_date: '2026-10-01',
      token: 't.abcdef-123' });
    get('bankc-token').value = 'abc';
    expect(bankConnBody(get, '').error).toContain('короткий');
  });

  it('Сбер: токены и сертификат PEM, файл .p12 и пароль; не PEM — ошибка', () => {
    const get = form(conn('sber'));
    get('bankc-client').value = 'sberapp01';
    get('bankc-secret').value = 'sber-secret';
    get('bankc-refresh').value = 'r-123456789';
    get('bankc-cert').value = '-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----';
    get('bankc-cert-pass').value = 'p';
    expect(bankConnBody(get, '')).toMatchObject({ bank: 'sber', method: 'refresh', client_id: 'sberapp01',
      client_secret: 'sber-secret', refresh_token: 'r-123456789', cert_password: 'p' });
    get('bankc-cert').value = '';
    get('bankc-cert-file').dataset.p12 = 'QUJD';
    expect(bankConnBody(get, '')).toMatchObject({ cert_p12: 'QUJD', cert_password: 'p' });
    get('bankc-cert').value = 'не сертификат';
    expect(bankConnBody(get, '').error).toContain('BEGIN CERTIFICATE');
  });
});

describe('несколько банков: панель в работе', () => {
  it('выбор банка перерисовывает настройки; «Проверить» — /check с банком; «Отключить» — с банком', async () => {
    const box = document.getElementById('box');
    await bankConnectPanel(box, 'c1');
    expect(calls.get).toEqual(['/api/v1/bank/connection?company_id=c1']);
    const select = box.querySelector('#bankc-bank');
    select.value = 'tbank';
    select.dispatchEvent(new Event('change'));
    await flush(); await flush();
    expect(calls.get.at(-1)).toBe('/api/v1/bank/connection?company_id=c1&bank=tbank');
    expect(hidden(box, '#bankc-token-box')).toBe(false);
    box.querySelector('#bankc-token').value = 'synthetic-key-123';
    box.querySelector('#bankc-jwtcheck').click();
    await flush(); await flush(); await flush();
    expect(calls.put[0].body).toMatchObject({ bank: 'tbank', method: 'token', token: 'synthetic-key-123', company_id: 'c1' });
    expect(calls.post.at(-1)).toEqual({ path: '/api/v1/bank/connection/check', body: { company_id: 'c1', bank: 'tbank' } });
    expect(box.textContent).toContain('подключён');
    expect(document.getElementById('toast-container').textContent).toContain('Т-Банк подключён');
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(true);
    box.querySelector('#bankc-del').click();
    await flush(); await flush();
    expect(calls.del).toEqual(['/api/v1/bank/connection?company_id=c1&bank=tbank']);
    confirm.mockRestore();
  });

  it('список: «Открыть» другой банк; «Загрузить сейчас» — с банком', async () => {
    byBank._ = tbankActive;
    byBank.sber = conn('sber', { status: 'reauth', connections: tbankActive.connections });
    const box = document.getElementById('box');
    await bankConnectPanel(box, '');
    expect(box.querySelectorAll('.bank-conn-row').length).toBe(2);
    box.querySelector('#bankc-sync').click();
    await flush(); await flush();
    expect(calls.post.at(-1)).toEqual({ path: '/api/v1/bank/connection/sync', body: { bank: 'tbank' } });
    box.querySelector('.bank-conn-open[data-bank="sber"]').click();
    await flush(); await flush();
    expect(calls.get.at(-1)).toBe('/api/v1/bank/connection?bank=sber');
    expect(box.querySelector('#bankc-bank').value).toBe('sber');
    expect(box.textContent).toContain('нужно подключить заново');
  });
});
