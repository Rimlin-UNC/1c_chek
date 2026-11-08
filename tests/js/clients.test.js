/* v1.70.100 (T-50, T-68): экран «Клиенты» — карточки компаний по убыванию внимания, срок сдачи, переход в
   компанию, переключатель клиента бухгалтера, назначение клиентов администратором и пакет за месяц.
   API подменён, данные синтетические. */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { get: [], put: [], post: [], download: [] };
let clientState = '';
const overview = {
  period: '2026-10', period_title: 'Октябрь 2026',
  periods: [{ period: '2026-10', title: 'Октябрь 2026' }, { period: '2026-11', title: 'Ноябрь 2026' }],
  totals: { companies: 2, bad: 1, warn: 0, ok: 1, overdue: 3, open: 3, escalated: 1, fns_bad: 1, upd_errors: 1,
    bank_open: 2, to_export: 4, upd_to_export: 1 },
  companies: [
    { id: 'c-a', name: 'ООО «Альфа»', inn: '7701000001', home: false, level: 'bad', score: 21,
      receipts: { count: 12, sum: 45000, attention: 2, fns_bad: 1, policy: 0, to_export: 4 },
      primary: { errors: 1, to_export: 1 },
      docreq: { open: 3, overdue: 3, escalated: 1, deadline: '2026-11-05T18:00:00+03:00', days_left: -2 },
      bank: { open: 2, status: 'active', last_sync_at: '2026-11-04T07:05:00Z', last_error: '' } },
    { id: 'c-b', name: 'ООО «Бета»', inn: '7701000002', home: true, level: 'ok', score: 0,
      receipts: { count: 0, sum: 0, attention: 0, fns_bad: 0, policy: 0, to_export: 0 },
      primary: { errors: 0, to_export: 0 },
      docreq: { open: 0, overdue: 0, escalated: 0, deadline: '2026-11-05T18:00:00+03:00', days_left: 4 },
      bank: { open: 0, status: 'none', last_sync_at: '', last_error: '' } },
  ],
};

vi.mock('../../app/static/js/api.js', () => ({
  api: {
    get: vi.fn(async (path) => {
      calls.get.push(path);
      if (path.startsWith('/api/v1/clients/access/')) return { home_company_id: 'c-b', company_ids: ['c-a'] };
      return overview;
    }),
    put: vi.fn(async (path, body) => { calls.put.push({ path, body }); return { company_ids: body.company_ids, added: 1, removed: 0 }; }),
    post: vi.fn(async (path, body) => {
      calls.post.push({ path, body });
      if (path === '/api/v1/companies') return { id: 'c-new', name: body.name, inn: body.inn || '', is_active: true };
      if (path === '/api/v1/companies/lookup-checko') return { name_full: 'ООО «РОМАШКА»', status: 'Действующая' };
      return {};
    }),
    download: vi.fn(async (path) => { calls.download.push(path); return { blob: new Blob(['zip']), filename: 'ymaster-package-7701000001-2026-10.zip' }; }),
  },
  setClient: vi.fn((id) => { clientState = id; }),
}));

const mod = await import('../../app/static/js/clients.js');

beforeEach(() => {
  calls.get = []; calls.put = []; calls.post = []; calls.download = [];
  clientState = '';
  document.body.innerHTML = '<div id="toast-container"></div><div id="modal-root" class="modal-root hidden"><div class="modal-backdrop"></div><div class="modal-slot"></div></div><div id="app"></div>';
  URL.createObjectURL = vi.fn(() => 'blob:x');
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});   // скачивание — без навигации jsdom
});

describe('«Клиенты»: подписи и карточки', () => {
  it('склонение дней и срок сдачи', () => {
    expect([1, 2, 5, 11, 21, 22].map(mod.clientsDays)).toEqual(['день', 'дня', 'дней', 'дней', 'день', 'дня']);
    expect(mod.clientsDeadline({ deadline: '2026-11-05T18:00:00+03:00', days_left: 3, open: 1 }))
      .toEqual({ text: 'через 3 дня', when: '5 ноября, 18:00', cls: '' });
    expect(mod.clientsDeadline({ deadline: '2026-11-05T18:00:00+03:00', days_left: 0, open: 1 }).text).toBe('осталось меньше суток');
    expect(mod.clientsDeadline({ deadline: '2026-11-05T18:00:00+03:00', days_left: -2, open: 3 }))
      .toEqual({ text: 'просрочено на 2 дня', when: '5 ноября, 18:00', cls: 'cl-late' });
    expect(mod.clientsDeadline({ deadline: '2026-11-05T18:00:00+03:00', days_left: -2, open: 0 }).text)
      .toBe('срок прошёл — всё сдано');
    expect(mod.clientsDeadline({ deadline: '2026-11-05T18:00:00+03:00', days_left: 0, open: 0 }))
      .toEqual({ text: 'открытых запросов нет', when: '5 ноября, 18:00', cls: 'cl-done' });
  });

  it('карточка: статус, цифры с тоном и переходы в разделы этой компании', () => {
    const html = mod.clientsCardHtml(overview.companies[0]);
    expect(html).toContain('ООО «Альфа»');
    expect(html).toContain('<span class="chip failed">Нужны действия</span>');
    expect(html).toContain('просрочено на 2 дня');
    for (const view of ['docreq', 'receipts', 'primary', 'bank']) expect(html).toContain(`data-open="${view}"`);
    expect(html).toContain('<b>3</b> просрочено');
    expect(html).toContain('<b>1</b> ошибки ФНС');
    expect(html).toContain('банк подключён, загрузка');
    expect(html).toContain('data-pack="c-a"');
    const calm = mod.clientsCardHtml(overview.companies[1]);
    expect(calm).toContain('<span class="chip">своя</span>');
    expect(calm).toContain('всё есть');
    expect(calm).toContain('банк по API не подключён');
    expect(calm).not.toContain('cl-tile cl-bad');
  });

  it('итоги и фильтр списка по статусу и поиску', () => {
    const totals = mod.clientsTotalsHtml(overview.totals);
    expect(totals).toContain('Просрочено запросов');
    expect(totals).toContain('нужны действия: 1 · вопросы: 0 · в порядке: 1');
    expect(mod.clientsListHtml(overview.companies, 'bad')).toContain('ООО «Альфа»');
    expect(mod.clientsListHtml(overview.companies, 'bad')).not.toContain('ООО «Бета»');
    expect(mod.clientsListHtml(overview.companies, 'all', '7701000002')).toContain('ООО «Бета»');
    expect(mod.clientsListHtml(overview.companies, 'warn')).toContain('Под фильтр не попала');
    expect(mod.clientsListHtml([], 'all')).toContain('«Пользователи» → бухгалтер → «🏢 Клиенты»');
  });

  it('месяцы для выбора: отчётный — первым', () => {
    const list = mod.clientsPeriods(new Date(2026, 10, 5));
    expect(list.slice(0, 3).map((p) => p.period)).toEqual(['2026-10', '2026-11', '2026-09']);
    expect(list[0].title).toBe('Октябрь 2026');
    expect(mod.clientsPeriods(new Date(2026, 0, 10))[0].period).toBe('2025-12');
  });
});

describe('«Клиенты»: сценарии', () => {
  it('экран: загрузка, фильтр, переход в компанию и пакет за месяц', async () => {
    const box = document.getElementById('app');
    const open = vi.fn();
    await mod.viewClients(box, { open });
    expect(calls.get).toEqual(['/api/v1/clients']);
    expect(box.querySelectorAll('.cl-card')).toHaveLength(2);
    box.querySelector('.cl-filter button[data-filter="bad"]').click();
    expect(box.querySelectorAll('.cl-card')).toHaveLength(1);
    box.querySelector('.cl-card [data-open="bank"]').click();
    expect(open).toHaveBeenCalledWith('c-a', 'bank');
    box.querySelector('[data-pack="c-a"]').click();
    await new Promise((r) => setTimeout(r, 0));
    expect(calls.download).toEqual(['/api/v1/export/package?period=2026-10&company_id=c-a']);
    const select = box.querySelector('#cl-period');
    select.value = '2026-11';
    select.dispatchEvent(new Event('change'));
    await new Promise((r) => setTimeout(r, 0));
    expect(calls.get.at(-1)).toBe('/api/v1/clients?period=2026-11');
  });

  it('переключатель клиента: виден при двух компаниях, своя — без заголовка', () => {
    const sel = document.createElement('select');
    sel.className = 'hidden';
    const onSwitch = vi.fn();
    mod.clientsSwitcher(sel, { role: 'accountant', company_id: 'c-b', clients: [{ id: 'c-b', name: 'Бета', home: true }] }, onSwitch);
    expect(sel.classList.contains('hidden')).toBe(true);
    const me = { role: 'accountant', company_id: 'c-b',
      clients: [{ id: 'c-b', name: 'Бета', home: true }, { id: 'c-a', name: 'Альфа', home: false }] };
    mod.clientsSwitcher(sel, me, onSwitch);
    expect(sel.classList.contains('hidden')).toBe(false);
    expect(sel.options[0].textContent).toBe('Бета · своя');
    sel.value = 'c-a';
    sel.dispatchEvent(new Event('change'));
    expect(clientState).toBe('c-a');
    expect(onSwitch).toHaveBeenCalledWith('c-a');
    sel.value = 'c-b';
    sel.dispatchEvent(new Event('change'));
    expect(clientState).toBe('');                          // своя компания — заголовок не нужен
    mod.clientsSwitcher(sel, { role: 'admin', clients: me.clients }, onSwitch);
    expect(sel.classList.contains('hidden')).toBe(true);
  });

  it('администратор назначает клиентов: отмеченные уходят в PUT, своя компания бухгалтера не предлагается', async () => {
    const companies = [{ id: 'c-a', name: 'Альфа', inn: '1', is_active: true }, { id: 'c-b', name: 'Бета', is_active: true },
      { id: 'c-c', name: 'Гамма', inn: '3', is_active: true }, { id: 'c-d', name: 'Дельта', is_active: false }];
    await mod.clientsAccessDialog({ id: 'u-1', name: 'Бухгалтер' }, companies);
    const slot = document.querySelector('.modal-slot');
    const boxes = [...slot.querySelectorAll('#cla-list input')];
    expect(boxes.map((b) => b.value)).toEqual(['c-a', 'c-c']);
    expect(boxes[0].checked).toBe(true);
    expect(slot.querySelector('#cla-count').textContent).toBe('Выбрано клиентов: 1');
    boxes[1].checked = true;
    slot.querySelector('#cla-list').dispatchEvent(new Event('change'));
    expect(slot.querySelector('#cla-count').textContent).toBe('Выбрано клиентов: 2');
    slot.querySelector('#cla-save').click();
    await new Promise((r) => setTimeout(r, 0));
    expect(calls.put).toEqual([{ path: '/api/v1/clients/access/u-1', body: { company_ids: ['c-a', 'c-c'] } }]);
  });

  it('карточка пакета на экране выгрузки: администратору без компании — подсказка и неактивная кнопка', () => {
    expect(mod.clientsPackageHtml({ isAdmin: true, companyId: '' })).toContain('id="pkg-download" disabled');
    const html = mod.clientsPackageHtml({ isAdmin: false });
    expect(html).toContain('📦 Пакет для других учётных систем');
    expect(html).not.toContain('disabled');
  });
});

// v1.71.1 (T-84): бухгалтер заводит клиента сам — кнопка на экране и окно создания
describe('«Клиенты»: создание клиента (T-84)', () => {
  it('сообщения об отказе — словами и с подсказкой', () => {
    expect(mod.clientsCreateError(409, 'Похожая компания уже есть: «ООО Альфа» — это дубли'))
      .toBe('Похожая компания уже есть: «ООО Альфа» — это дубли. Откройте её из списка — дубль заводить не нужно.');
    expect(mod.clientsCreateError(409, 'У бухгалтера уже 500 клиентов — предел 500. Снимите лишних.'))
      .toContain('предел 500');
    expect(mod.clientsCreateError(403, 'Недостаточно прав')).toContain('бухгалтер и администратор');
    expect(mod.clientsCreateError(0, '')).toBe('Не удалось создать клиента');
    expect(mod.clientsCreateButtonHtml()).toContain('id="cl-new"');
    expect(mod.CLIENTS_MAX).toBe(500);
  });

  it('окно создания: ИНН → ЕГРЮЛ заполняет название, сохранение уходит в POST и обновляет список', async () => {
    const done = vi.fn();
    mod.clientsCreateDialog({ onCreated: done });
    const slot = document.querySelector('.modal-slot');
    expect(slot.textContent).toContain('Новый клиент');
    expect(slot.querySelector('#clc-inn')).toBeTruthy();

    slot.querySelector('#clc-inn').value = '7707083893';
    slot.querySelector('#clc-checko').click();
    await new Promise((r) => setTimeout(r, 0));
    expect(calls.post[0]).toEqual({ path: '/api/v1/companies/lookup-checko', body: { inn: '7707083893' } });
    expect(slot.querySelector('#clc-name').value).toBe('ООО «РОМАШКА»');

    slot.querySelector('#clc-note').value = 'договор № 12';
    slot.querySelector('#clc-save').click();
    await new Promise((r) => setTimeout(r, 0));
    expect(calls.post[1]).toEqual({
      path: '/api/v1/companies',
      body: { name: 'ООО «РОМАШКА»', inn: '7707083893', note: 'договор № 12' },
    });
    expect(done).toHaveBeenCalledWith({ id: 'c-new', name: 'ООО «РОМАШКА»', inn: '7707083893', is_active: true });
  });

  it('окно создания: без названия запрос не уходит', async () => {
    mod.clientsCreateDialog();
    const slot = document.querySelector('.modal-slot');
    slot.querySelector('#clc-save').click();
    await new Promise((r) => setTimeout(r, 0));
    expect(calls.post).toEqual([]);
  });

  it('экран: кнопка «＋ Новый клиент» есть в шапке и открывает окно', async () => {
    const box = document.getElementById('app');
    await mod.viewClients(box);
    const btn = box.querySelector('#cl-new');
    expect(btn).toBeTruthy();
    expect(btn.textContent).toContain('Новый клиент');
    btn.click();
    expect(document.querySelector('.modal-slot').textContent).toContain('Новый клиент');
  });

  it('после создания клиента экран сообщает наружу — шапка перерисовывает переключатель', async () => {
    const box = document.getElementById('app');
    box.innerHTML = '';
    const onCreated = vi.fn();
    await mod.viewClients(box, { onCreated });
    box.querySelector('#cl-new').click();
    const slot = document.querySelector('.modal-slot');
    slot.querySelector('#clc-name').value = 'ООО «Новая волна»';
    slot.querySelector('#clc-save').click();
    await new Promise((r) => setTimeout(r, 0));
    expect(onCreated).toHaveBeenCalledWith({ id: 'c-new', name: 'ООО «Новая волна»', inn: '', is_active: true });
  });

  it('пустой список приглашает завести клиента, а не только идти к администратору', () => {
    const html = mod.clientsListHtml([], 'all');
    expect(html).toContain('＋ Новый клиент');
    expect(html).toContain('«Пользователи» → бухгалтер → «🏢 Клиенты»');
  });
});
