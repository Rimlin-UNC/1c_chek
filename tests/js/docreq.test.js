// «Что нужно от клиента» (T-58, v1.70.97): разметка запросов, журнала и расписания; экран в jsdom —
// поиск недостающего, отметка «получено», настройки, «Напомнить сейчас» с предпросмотром. API подменён.
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { get: [], post: [], put: [] };
const overview = {
  company: { id: 'c1', name: 'ООО «Ромашка»' }, period: '2026-10', period_title: 'октябрь 2026',
  deadline: '2026-11-05T18:00:00+03:00', deadline_human: '5 ноября 2026, 18:00', overdue: false,
  settings: { enabled: true, auto_detect: true, deadline_day: 5, x_hour: '18:00', remind_days: [3, 1, 0], repeat_every: 2,
    repeat_max: 3, channels: ['telegram', 'email'], roles: ['manager'], contact_email: 'buh@client.example' },
  channels: { telegram: { ready: true, hint: '' }, max: { ready: false, hint: 'бот MAX не включён в настройках' },
    email: { ready: true, hint: '' } },
  items: [
    { id: 'r1', kind: 'expected', title: 'Документ от ООО «Арендодатель» за октябрь 2026', counterparty_inn: '5001007329',
      amount: 51000, assignee: '', status: 'open', close_note: '' },
    { id: 'r2', kind: 'receipt', title: 'Чек на <1 250,00 ₽>', amount: 1250, assignee: 'Сотрудник Тестов', status: 'received',
      close_note: 'Закрыт сам: чек подтверждён ФНС' },
  ],
  summary: { open: 1, received: 1, cancelled: 0 },
  schedule: [{ slot: 'D-3', human: '2 ноября 2026, 10:00', sent: true }, { slot: 'D-1', human: '4 ноября 2026, 10:00', sent: false }],
  discipline: { received: 4, on_time: 3, share: 75 },
};
const journal = { items: [
  { id: 'e1', kind: 'reminder', slot: 'D-3', channel: 'telegram', recipient: 'Руководитель (boss)', ok: true,
    at: '2026-11-02T07:00:00', text: 'Срок сдачи — 5 ноября' },
  { id: 'e2', kind: 'reminder', slot: 'manual', channel: 'email', recipient: 'buh@client.example', ok: false,
    at: '2026-11-03T09:00:00', text: 'Не доставлено (TimeoutError).' },
  { id: 'e3', kind: 'created', slot: '', channel: '', recipient: '', actor: 'программа', ok: true, at: '2026-11-01T06:00:00',
    text: 'ожидается по истории: Документ…' },
] };
const preview = { warning: 'Общие запросы (2) сейчас некому отправить: …', recipients: [
  { recipient: 'Руководитель (boss)', items: 1, channels: ['telegram'], text: 'ООО «Ромашка»: для закрытия месяца…' },
  { recipient: 'buh@client.example', items: 1, channels: [], text: '…' },
] };

vi.mock('../../app/static/js/api.js', () => ({
  api: {
    get: vi.fn(async (path) => {
      calls.get.push(path);
      if (path.includes('/journal')) return journal;
      if (path.includes('/preview')) return preview;
      return overview;
    }),
    post: vi.fn(async (path, body) => {
      calls.post.push({ path, body });
      if (path.endsWith('/detect')) return { created: 2, closed: 0, overview };
      if (path.endsWith('/remind')) return { sent: 1, failed: 0, note: '' };
      return { ok: true };
    }),
    put: vi.fn(async (path, body) => { calls.put.push({ path, body }); return { settings: overview.settings }; }),
  },
}));

const mod = await import('../../app/static/js/docreq.js');
const { dreqDisciplineText, dreqJournalHtml, dreqParseDays, dreqRowsHtml, dreqScheduleHtml, dreqTime, viewDocRequests } = mod;
const flush = () => new Promise((r) => setTimeout(r, 0));

describe('что нужно от клиента: разметка', () => {
  it('строки запросов: вид, ИНН, сумма, действия по статусу, экранирование', () => {
    const html = dreqRowsHtml(overview.items);
    expect(html).toContain('📅 ожидается по истории · ИНН 5001007329');
    expect(html).toContain('data-id="r1" data-status="received"');
    expect(html).toContain('data-id="r2" data-status="open"');               // полученный можно вернуть
    expect(html).toContain('Чек на &lt;1 250,00 ₽&gt;');
    expect(html).toContain('Закрыт сам: чек подтверждён ФНС');
    expect(dreqRowsHtml([])).toContain('Найти недостающее');
  });

  it('журнал: событие, метка напоминания, канал, неудача', () => {
    const html = dreqJournalHtml(journal.items);
    expect(html).toContain('Напоминание (D-3)');
    expect(html).toContain('Напоминание (вручную)');
    expect(html).toContain('class="dreq-failed"');
    expect(html).toContain('Запрос создан');
    expect(html).toContain('программа');
    expect(dreqJournalHtml([])).toContain('Журнал пуст');
  });

  it('расписание, дисциплина, дни до срока, время журнала', () => {
    expect(dreqScheduleHtml(overview.schedule, true)).toContain('✓ 2 ноября 2026, 10:00 · 4 ноября 2026, 10:00');
    expect(dreqScheduleHtml(overview.schedule, false)).toContain('Автонапоминания выключены');
    expect(dreqDisciplineText(overview.discipline)).toBe('В срок за три месяца: 75 % (3 из 4)');
    expect(dreqDisciplineText({ share: null })).toBe('');
    expect(dreqParseDays('3, 1, 0')).toEqual([3, 1, 0]);
    expect(dreqParseDays('0 2 2')).toEqual([2, 0]);
    expect(dreqParseDays('11')).toBeNull();
    expect(dreqParseDays('1,2,3,4,5,6')).toBeNull();
    expect(dreqTime('2026-11-02T07:00:00')).toBe(new Date('2026-11-02T07:00:00Z').toLocaleString('ru-RU',
      { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' }));
    expect(dreqTime(null)).toBe('—');
  });
});

describe('что нужно от клиента: экран', () => {
  beforeEach(() => {
    calls.get.length = 0; calls.post.length = 0; calls.put.length = 0;
    document.body.innerHTML = `<div id="toast-container"></div><div id="view"></div>
      <div id="modal-root" class="hidden"><div class="modal-backdrop"></div><div class="modal-slot"></div></div>`;
  });

  it('бухгалтер: срок, расписание, дисциплина, запросы и журнал', async () => {
    const view = document.getElementById('view');
    await viewDocRequests(view, { isAdmin: false });
    await flush();
    expect(calls.get[0]).toBe('/api/v1/doc-requests?');
    expect(view.querySelector('#dreq-company')).toBeNull();
    expect(view.querySelector('#dreq-head').innerHTML).toContain('5 ноября 2026, 18:00');
    expect(view.querySelector('#dreq-head').innerHTML).toContain('В срок за три месяца: 75 %');
    expect(view.querySelector('#dreq-kpi').textContent).toBe('Ждём: 1 · получено: 1 · отменено: 0');
    expect(view.querySelector('#dreq-period').value).toBe('2026-10');
    expect(calls.get[1]).toBe('/api/v1/doc-requests/journal?period=2026-10');
    expect(view.querySelector('#dreq-journal').innerHTML).toContain('buh@client.example');
  });

  it('администратор без компании — подсказка, без запросов; с компанией — company_id', async () => {
    const view = document.getElementById('view');
    await viewDocRequests(view, { isAdmin: true, companies: [{ id: 'c1', name: 'Ромашка' }] });
    expect(calls.get).toEqual([]);
    expect(view.querySelector('#dreq-out').innerHTML).toContain('Выберите компанию');
    const sel = view.querySelector('#dreq-company');
    sel.value = 'c1';
    sel.onchange({ target: sel });
    await flush();
    expect(calls.get[0]).toBe('/api/v1/doc-requests?company_id=c1');
  });

  it('найти недостающее, «получено», добавить запрос', async () => {
    const view = document.getElementById('view');
    await viewDocRequests(view, {});
    await flush();
    await view.querySelector('#dreq-detect').onclick();
    await flush();
    expect(calls.post[0]).toEqual({ path: '/api/v1/doc-requests/detect', body: { period: '2026-10' } });
    await view.querySelector('.dreq-act[data-id="r1"][data-status="received"]').onclick();
    expect(calls.post[1]).toEqual({ path: '/api/v1/doc-requests/r1/status', body: { status: 'received', note: '' } });
    view.querySelector('#dreq-add').onclick();
    const slot = document.querySelector('.modal-slot');
    slot.querySelector('#dreq-a-title').value = 'Акт сверки';
    slot.querySelector('#dreq-a-amount').value = '1 500,50';
    await slot.querySelector('#dreq-a-save').onclick();
    expect(calls.post.at(-1)).toEqual({ path: '/api/v1/doc-requests', body: { period: '2026-10', title: 'Акт сверки',
      counterparty_name: '', counterparty_inn: '', amount: 1500.5 } });
  });

  it('настройки: каналы с подсказкой, проверка дней, сохранение', async () => {
    const view = document.getElementById('view');
    await viewDocRequests(view, {});
    await flush();
    view.querySelector('#dreq-settings').onclick();
    const slot = document.querySelector('.modal-slot');
    expect(slot.innerHTML).toContain('бот MAX не включён в настройках');
    expect(slot.querySelector('#dreq-s-ch-telegram').checked).toBe(true);
    expect(slot.querySelector('#dreq-s-ch-max').checked).toBe(false);
    slot.querySelector('#dreq-s-days').value = '12';
    await slot.querySelector('#dreq-s-save').onclick();
    expect(slot.querySelector('#dreq-s-error').textContent).toContain('от 0 до 10');
    expect(calls.put).toEqual([]);
    slot.querySelector('#dreq-s-days').value = '2, 0';
    slot.querySelector('#dreq-s-role-user').checked = true;
    await slot.querySelector('#dreq-s-save').onclick();
    expect(calls.put[0].path).toBe('/api/v1/doc-requests/settings');
    expect(calls.put[0].body).toEqual({ enabled: true, auto_detect: true, deadline_day: 5, x_hour: '18:00', remind_days: [2, 0],
      repeat_every: 2, repeat_max: 3, channels: ['telegram', 'email'], roles: ['manager', 'user'],
      contact_email: 'buh@client.example', escalate_days: 3 });        // v1.70.99: эскалация, по умолчанию 3 дня
  });

  it('«Напомнить сейчас»: сначала кому и что уйдёт, потом отправка', async () => {
    const view = document.getElementById('view');
    await viewDocRequests(view, {});
    await flush();
    await view.querySelector('#dreq-remind').onclick();
    expect(calls.get.at(-1)).toBe('/api/v1/doc-requests/preview?period=2026-10');
    const slot = document.querySelector('.modal-slot');
    expect(slot.innerHTML).toContain('каналы: Telegram');
    expect(slot.innerHTML).toContain('нет подключённых каналов');
    expect(slot.innerHTML).toContain('Общие запросы (2) сейчас некому отправить');
    expect(calls.post).toEqual([]);
    await slot.querySelector('#dreq-r-send').onclick();
    expect(calls.post[0]).toEqual({ path: '/api/v1/doc-requests/remind', body: { period: '2026-10' } });
  });
});
