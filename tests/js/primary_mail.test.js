/* v1.70.104 (T-63, ADR-34): первичка из почты — панель ящика (поставщики почты, поля, тело запроса, «Сохранить и
   проверить», «Загрузить сейчас», «Отключить») и карточка «Из почты» (черновики на сверку, журнал вложений).
   API подменён, адреса и документы синтетические. */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { get: [], post: [], put: [], del: [] };
const base = { company_id: 'c1', company: 'Ромашка', configured: false, drafts: 0, check_every_min: 15, host: '', port: 993,
  security: 'ssl', username: '', password_set: false, folder: 'INBOX', senders: '', start_date: null, status: '',
  status_title: 'не подключён', last_check_at: null, last_ok_at: null, last_error: '', last_result: {} };
const active = { ...base, configured: true, host: 'imap.mail.ru', username: 'docs@client.ru', password_set: true,
  status: 'active', status_title: 'подключён', start_date: '2026-10-06', last_check_at: '2026-11-05T09:30:00',
  drafts: 2, last_result: { ok: true, messages: 3, created: 2, drafts: 1, duplicates: 1 } };
let state = base;

const draftRec = { item_id: 'i1', source: 'pdf', file_name: 'УПД УТ-215.pdf', company_id: 'c1', sender: 'docs@supplier.ru',
  subject: 'Документы', message: '', media_type: 'application/pdf', file_url: '/api/v1/primary/inbox/i1/file',
  draft: { function: 'СЧФДОП', number: 'УТ-215', date: '2026-10-14', correction_number: '', correction_date: null, currency: '643',
    seller: { name: 'ООО «СтройБаза»', inn: '5001007329', kpp: '500101001' },
    buyer: { name: 'ООО «Ромашка-Строй»', inn: '7704123450', kpp: '770401001' },
    totals: { without_vat: 5000, vat: 1100, with_vat: 6100 }, lines: [] },
  found: ['number', 'date', 'seller_name', 'seller_inn', 'seller_kpp', 'buyer_name', 'buyer_inn', 'buyer_kpp', 'totals'],
  uncertain: [], issues: [], duplicate: null, hints: {}, ocr: null };
const inboxData = { drafts: [{ id: 'i1', file_name: 'УПД УТ-215.pdf', sender: 'docs@supplier.ru', subject: 'Документы',
  received_at: '2026-11-04T07:15:00', result: 'draft', draft: { number: 'УТ-215', date: '2026-10-14', seller: 'ООО «СтройБаза»' } }],
recent: [], week: {} };

vi.mock('../../app/static/js/api.js', () => ({
  api: {
    download: vi.fn(async (path) => { calls.get.push(`download ${path}`); return { blob: new Blob(['%PDF-1.4']), filename: 'x.pdf' }; }),
    get: vi.fn(async (path) => {
      calls.get.push(path);
      if (path.startsWith('/api/v1/primary/inbox/i1')) return JSON.parse(JSON.stringify(draftRec));
      if (path.startsWith('/api/v1/primary/inbox')) return inboxData;
      if (path === '/api/v1/primary/ocr') return { ready: true, engine: 'tesserocr', missing: [] };
      if (path.startsWith('/api/v1/primary?')) return { items: [], total: 0, summary: { ok: 0, warnings: 0, errors: 0 } };
      return state;
    }),
    put: vi.fn(async (path, body) => { calls.put.push({ path, body }); state = { ...active, status: 'new' }; return state; }),
    post: vi.fn(async (path, body) => {
      calls.post.push({ path, body });
      if (path.endsWith('/check')) { state = active; return { result: { ok: true, messages_in_folder: 12 }, mailbox: active }; }
      if (path.endsWith('/sync')) return { result: { ok: true, messages: 1, created: 1, drafts: 0 }, mailbox: active };
      if (path.endsWith('/i1/confirm')) return { result: 'created', document: { kind: 'УПД', doc_number: 'УТ-215', doc_date: '2026-10-14' } };
      return {};
    }),
    del: vi.fn(async (path) => { calls.del.push(path); state = base; return { removed: true }; }),
  },
}));

const mail = await import('../../app/static/js/primary_mail.js');
const { viewPrimary } = await import('../../app/static/js/primary.js');
const { MAIL_PROVIDERS, mailBody, mailConnectPanel, mailInboxHtml, mailPanelHtml, mailProviderOf, mailResultSummary } = mail;
const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  Object.keys(calls).forEach((k) => { calls[k] = []; });
  state = base;
  document.body.innerHTML = '<div id="toast-container"></div><div id="box"></div>';
});

describe('панель «Почта для документов»', () => {
  it('не подключён: Яндекс по умолчанию, подсказка про пароль приложения, только «Подключить и проверить»', () => {
    const box = document.getElementById('box');
    box.innerHTML = mailPanelHtml(base);
    expect(box.querySelector('#mailc-provider').value).toBe('yandex');
    expect(box.querySelector('#mailc-host').value).toBe('imap.yandex.ru');
    expect(box.querySelector('#mailc-how').textContent).toContain('Пароли приложений');
    expect(box.querySelector('#mailc-save').textContent).toBe('Подключить и проверить');
    expect(box.querySelector('#mailc-sync')).toBeNull();
    expect(box.querySelector('#mailc-del')).toBeNull();
    expect(box.querySelector('details').open).toBe(true);
    expect(box.textContent).toContain('только на чтение');
  });

  it('подключён: поставщик по адресу сервера, факты проверки, итог загрузки, настройки свёрнуты', () => {
    const box = document.getElementById('box');
    box.innerHTML = mailPanelHtml(active);
    expect(box.querySelector('#mailc-provider').value).toBe('mailru');
    expect(box.querySelector('#mailc-pass').placeholder).toBe('задан — пусто, не менять');
    expect(box.querySelector('.chip').textContent).toBe('подключён');
    expect(box.textContent).toContain('проверка каждые 15 минут');
    expect(box.textContent).toContain('ждут сверки: 2');
    expect(box.textContent).toContain('Последняя загрузка: писем: 3 · в реестр: 2 · на сверку: 1 · дублей: 1');
    expect(box.querySelector('details').open).toBe(false);
    expect(box.querySelector('#mailc-sync')).not.toBeNull();
  });

  it('поставщик почты и тело запроса: порт по защите, пустая папка — INBOX, компания администратора', () => {
    expect(mailProviderOf('IMAP.RAMBLER.RU')).toBe('rambler');
    expect(mailProviderOf('mail.corp.ru')).toBe('custom');
    expect(MAIL_PROVIDERS.map((p) => p.id)).toEqual(['yandex', 'mailru', 'rambler', 'custom']);
    const box = document.getElementById('box');
    box.innerHTML = mailPanelHtml(base);
    const get = (id) => box.querySelector(`#${id}`);
    expect(mailBody(get, '').error).toContain('логин');
    get('mailc-user').value = ' docs@client.ru ';
    get('mailc-security').value = 'starttls';
    get('mailc-folder').value = ' ';
    get('mailc-senders').value = '@supplier.ru';
    expect(mailBody(get, 'c9')).toEqual({ host: 'imap.yandex.ru', port: 143, security: 'starttls', username: 'docs@client.ru',
      password: '', folder: 'INBOX', senders: '@supplier.ru', start_date: null, company_id: 'c9' });
    get('mailc-host').value = '';
    expect(mailBody(get, '').error).toContain('IMAP-сервер');
  });

  it('итог проверки одной строкой', () => {
    expect(mailResultSummary({ messages: 0 })).toBe('писем: 0');
    expect(mailResultSummary({ messages: 4, created: 1, skipped: 2, foreign: 1, left: 3 }))
      .toBe('писем: 4 · в реестр: 1 · не УПД: 2 · от других отправителей: 1 · ещё писем в очереди: 3');
  });

  it('«Подключить и проверить»: PUT, затем вход в ящик; «Загрузить сейчас»; «Отключить»', async () => {
    const box = document.getElementById('box');
    await mailConnectPanel(box, 'c1');
    expect(calls.get).toEqual(['/api/v1/primary/mailbox?company_id=c1']);
    box.querySelector('#mailc-provider').value = 'mailru';
    box.querySelector('#mailc-provider').onchange({ target: box.querySelector('#mailc-provider') });
    expect(box.querySelector('#mailc-host').value).toBe('imap.mail.ru');
    box.querySelector('#mailc-user').value = 'docs@client.ru';
    box.querySelector('#mailc-pass').value = 'app-pass';
    await box.querySelector('#mailc-save').onclick();
    await flush();
    expect(calls.put[0].body).toMatchObject({ company_id: 'c1', host: 'imap.mail.ru', password: 'app-pass' });
    expect(calls.post[0]).toEqual({ path: '/api/v1/primary/mailbox/check', body: { company_id: 'c1' } });
    expect(box.querySelector('.chip').textContent).toBe('подключён');
    const synced = vi.fn();
    box.innerHTML = '';
    await mailConnectPanel(box, '', { onSynced: synced });
    await box.querySelector('#mailc-sync').onclick();
    expect(calls.post.at(-1)).toEqual({ path: '/api/v1/primary/mailbox/sync', body: {} });
    expect(synced).toHaveBeenCalledWith({ ok: true, messages: 1, created: 1, drafts: 0 });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    await box.querySelector('#mailc-del').onclick();
    await flush();
    expect(calls.del).toEqual(['/api/v1/primary/mailbox']);
    expect(box.querySelector('#mailc-save').textContent).toBe('Подключить и проверить');
    confirm.mockRestore();
  });
});

describe('карточка «Из почты»', () => {
  const data = {
    drafts: [
      { id: 'i1', company: 'Ромашка', file_name: 'УПД УТ-215.pdf', sender: 'docs@supplier.ru', subject: 'Документы',
        received_at: '2026-11-04T07:15:00', result: 'draft', draft: { number: 'УТ-215', date: '2026-10-14', seller: 'ООО «СтройБаза»' } },
      { id: 'i2', company: 'Ромашка', file_name: 'скан.jpg', sender: '', subject: '', created_at: '2026-11-05T08:00:00',
        result: 'queued', draft: null },
    ],
    recent: [{ id: 'i3', file_name: 'upd.xml', sender: 'docs@supplier.ru', result: 'created', result_title: 'в реестре',
      message: '', created_at: '2026-11-04T07:16:00' }],
    week: { created: 1, confirmed: 1, duplicate: 2 },
  };

  it('черновики: что распознано, от кого, «Сверить» — только у готовых; журнал и итоги недели', () => {
    const box = document.getElementById('box');
    box.innerHTML = mailInboxHtml(data, { isAdmin: true });
    const rows = box.querySelectorAll('.mail-draft');
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain('№ УТ-215 от 14.10.2026 ООО «СтройБаза»');
    expect(rows[0].textContent).toContain('docs@supplier.ru · «Документы»');
    expect(rows[0].textContent).toContain('Ромашка');
    expect(rows[1].textContent).toContain('распознаётся…');
    expect(box.querySelectorAll('.mail-review')).toHaveLength(1);
    expect(box.querySelectorAll('.mail-dismiss')).toHaveLength(2);
    expect(box.textContent).toContain('Ждут сверки: 2');
    expect(box.textContent).toContain('За неделю: в реестр — 2, дублей — 2, не принято — 0, не УПД — 0.');
    expect(box.querySelector('tbody').textContent).toContain('в реестре');
  });

  it('пусто: понятные подсказки, компания видна только администратору', () => {
    const box = document.getElementById('box');
    box.innerHTML = mailInboxHtml({ drafts: [], recent: [], week: {} });
    expect(box.textContent).toContain('Из почты ещё ничего не приходило.');
    expect(box.textContent).toContain('Черновиков на сверку нет');
    expect(box.querySelector('table')).toBeNull();
    box.innerHTML = mailInboxHtml(data, { isAdmin: false });
    expect(box.querySelector('.mail-draft').textContent).not.toContain('Ромашка');
  });
});

describe('сверка черновика из почты на экране «Первичка»', () => {
  it('окно сверки: от кого, «Открыть файл», «Не нужен»; сохранение — в /inbox/{id}/confirm с компанией черновика', async () => {
    document.body.innerHTML = '<div id="toast-container"></div><div id="modal-root" class="hidden"><div class="modal-backdrop"></div>'
      + '<div class="modal-slot"></div></div><div id="box"></div>';
    URL.createObjectURL = vi.fn(() => 'blob:mail-1');
    URL.revokeObjectURL = vi.fn();
    const box = document.getElementById('box');
    await viewPrimary(box, { isAdmin: false });
    for (let i = 0; i < 6; i += 1) await flush();
    expect(calls.get).toContain('/api/v1/primary/inbox');
    expect(box.querySelector('#prim-mail').textContent).toContain('Ждут сверки: 1');
    box.querySelector('.mail-review').onclick();
    for (let i = 0; i < 6; i += 1) await flush();
    const slot = document.querySelector('.modal-slot');
    expect(calls.get).toContain('download /api/v1/primary/inbox/i1/file');
    expect(slot.textContent).toContain('📬 Из почты: docs@supplier.ru, «Документы»');
    expect(slot.querySelector('#prim-open-file').getAttribute('href')).toBe('blob:mail-1');
    expect(slot.querySelector('#prim-skip').textContent).toBe('Не нужен');
    await slot.querySelector('#prim-save').onclick();
    const saved = calls.post.find((c) => c.path === '/api/v1/primary/inbox/i1/confirm');
    expect(saved.body.company_id).toBe('c1');
    expect(saved.body.draft.number).toBe('УТ-215');
    expect(calls.post.some((c) => c.path === '/api/v1/primary/confirm')).toBe(false);
    for (let i = 0; i < 4; i += 1) await flush();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mail-1');
  });
});
