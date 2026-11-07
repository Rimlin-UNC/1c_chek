/* v1.70.112 (T-46, ADR-24, ADR-42): экран «Согласования» — карточки, маршрут из трёх шагов, журнал с ПЭП,
   текст подтверждения, лист согласования и сценарии: подпись сотрудника, решение руководителя, настройки
   администратора. API подменён, данные синтетические. */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { get: [], post: [], put: [] };
const HASH = 'ab'.repeat(32);
const base = {
  id: 'ap-1', number: 'АО-2026-11-03', company_id: 'c-a', employee_id: 'u-emp', employee_name: 'Иванов И. И.',
  date_from: '2026-10-01', date_to: '2026-10-31', purpose: 'Материалы для объекта', receipts_count: 2, total: 1900,
  status: 'draft', status_title: 'ждёт подписи сотрудника', stage: 'employee', stage_title: 'сотрудник',
  deadline_at: null, minutes_left: null, doc_hash: HASH,
};
const snapshot = {
  total: 1900, items: [
    { id: 'r1', date: '2026-10-05T12:00', merchant: 'Стройбаза 5', category: 'Материалы', amount: 1200, fns: 'valid', flags: '', return: false },
    { id: 'r2', date: '2026-10-07T12:00', merchant: 'Стройбаза 7', category: 'Материалы', amount: 700, fns: 'not_found', flags: 'limit_check', return: false },
  ],
};
const created = { seq: 1, stage: 'employee', stage_title: 'сотрудник', action: 'created', pep: false, actor_name: 'Иванов И. И.',
  actor_role: 'user', comment: 'АО-2026-11-03: чеков 2', doc_hash: HASH, ip: '10.0.0.5', created_at: '2026-11-02T09:00:00Z' };
let detail;

vi.mock('../../app/static/js/api.js', () => ({
  api: {
    get: vi.fn(async (path) => {
      calls.get.push(path);
      if (path.startsWith('/api/v1/approvals/settings')) {
        return { company: { id: 'c-a', name: 'Альфа' }, can_edit: path.includes('company_id=c-a'),
          settings: { enabled: true, timeout_hours: 72, manager_limit: 0, accountant_limit: 0, channels: ['telegram', 'max'] },
          channels: { telegram: { ready: true, hint: '' }, max: { ready: false, hint: 'бот MAX не подключён' } } };
      }
      if (path.startsWith('/api/v1/approvals/ap-')) return detail;
      return { items: [{ ...base }], counts: { todo: 1, mine: 1 }, enabled: true };
    }),
    post: vi.fn(async (path, body) => { calls.post.push({ path, body }); return { ...detail, status: 'pending', stage: 'manager', can: {} }; }),
    put: vi.fn(async (path, body) => { calls.put.push({ path, body }); return { settings: body }; }),
  },
}));

const mod = await import('../../app/static/js/approvals.js');
const tick = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  calls.get = []; calls.post = []; calls.put = [];
  detail = { ...base, snapshot, changes: [], events: [created], approvers: [], timeout_hours: 72,
    integrity: { content: true, journal: { ok: true, broken_at: 0 } },
    can: { sign: true, decide: false, cancel: true, resubmit: false } };
  document.body.innerHTML = '<div id="toast-container"></div><div id="modal-root" class="modal-root hidden"><div class="modal-backdrop"></div><div class="modal-slot"></div></div><div id="app"></div>';
});

describe('«Согласования»: подписи', () => {
  it('даты, остаток до автосогласования и склонение чеков', () => {
    expect(mod.approvalsDay('2026-10-01')).toBe('01.10.2026');
    expect(mod.approvalsLeft(72 * 60)).toBe('до автосогласования 3 д');
    expect(mod.approvalsLeft(2 * 1440 + 5 * 60 + 7)).toBe('до автосогласования 2 д 5 ч');
    expect(mod.approvalsLeft(190)).toBe('до автосогласования 3 ч 10 мин');
    expect(mod.approvalsLeft(45)).toBe('до автосогласования 45 мин');
    expect(mod.approvalsLeft(0)).toBe('срок вышел — этап согласуется автоматически');
    expect(mod.approvalsLeft(null)).toBe('');
    expect([1, 2, 5, 11, 21, 104].map(mod.approvalsReceipts)).toEqual(['1 чек', '2 чека', '5 чеков', '11 чеков', '21 чек', '104 чека']);
    expect(mod.approvalsStatusChip('approved')).toBe('<span class="chip verified">согласован</span>');
    expect(mod.approvalsStatusChip('rejected')).toBe('<span class="chip failed">отклонён</span>');
  });

  it('маршрут по журналу: подпись, автосогласование, пропуск, отказ и текущий шаг', () => {
    const ev = (stage, action, actor_name = '') => ({ stage, action, actor_name });
    const pending = { status: 'pending', stage: 'accountant' };
    const route = mod.approvalsRoute(pending, [ev('employee', 'created', 'Иванов'), ev('employee', 'signed', 'Иванов'),
      ev('manager', 'auto_approved')]);
    expect(route.employee).toEqual({ state: 'done', who: 'Иванов', note: '' });
    expect(route.manager).toEqual({ state: 'auto', who: '', note: 'без ответа — согласовано' });
    expect(route.accountant.state).toBe('current');
    const skipped = mod.approvalsRoute(pending, [ev('employee', 'signed', 'Петров'), ev('manager', 'skipped')]);
    expect(skipped.manager).toEqual({ state: 'skipped', who: '', note: 'этап пропущен' });
    const threshold = mod.approvalsRoute({ status: 'approved', stage: '' }, [ev('manager', 'threshold_approved')]);
    expect(threshold.manager.note).toBe('по порогу');
    const rejected = mod.approvalsRoute({ status: 'rejected', stage: 'manager' }, [ev('manager', 'rejected', 'Сидоров')]);
    expect(rejected.manager).toEqual({ state: 'rejected', who: 'Сидоров', note: '' });
    expect(rejected.accountant.state).toBe('wait');
    expect(mod.approvalsRouteHtml(pending, [ev('employee', 'signed', 'Иванов')])).toContain('ap-step ap-current');
  });

  it('журнал: у подписи — отметка ПЭП, адрес и начало хеша; событие программы — без ПЭП; HTML экранируется', () => {
    const pep = mod.approvalsEventHtml({ ...created, action: 'signed', pep: true, actor_name: '<b>Иванов</b>' });
    expect(pep).toContain('>ПЭП</span>');
    expect(pep).toContain('адрес 10.0.0.5');
    expect(pep).toContain(`содержимое ${HASH.slice(0, 12)}…`);
    expect(pep).toContain('&lt;b&gt;Иванов&lt;/b&gt; — подписал');
    expect(pep).not.toContain('<b>Иванов</b>');
    const auto = mod.approvalsEventHtml({ ...created, action: 'auto_approved', pep: false, actor_name: '', stage_title: 'руководитель',
      comment: 'нет ответа 72 ч — этап считается согласованным (ADR-24)' });
    expect(auto).toContain('Программа — согласовано автоматически');
    expect(auto).toContain('нет ответа 72 ч');
    expect(auto).not.toContain('ПЭП');
  });

  it('текст подтверждения: сотрудник подписывает отчёт, согласующий — своё решение', () => {
    const sign = mod.approvalsConfirmText(base, 'Иванов И. И.', 'sign');
    expect(sign).toMatch(/^Я, Иванов И\. И\., подписываю простой электронной подписью авансовый отчёт АО-2026-11-03 /);
    expect(sign).toContain('01.10.2026–31.10.2026, 2 чека, к учёту 1');
    expect(sign).toContain(`SHA-256 ${HASH.slice(0, 16)}…`);
    const decide = mod.approvalsConfirmText(base, 'Петров П. П.', 'decide');
    expect(decide).toContain('Я, Петров П. П., подтверждаю своё решение по простой электронной подписью авансовому отчёту');
  });

  it('лист согласования: реквизиты, хеш и решения этапов без служебных уведомлений', () => {
    const sheet = mod.approvalsSheetHtml({ ...detail, status: 'approved', snapshot: { ...snapshot, company: { name: 'ООО «Альфа»', inn: '7701000001' } },
      events: [created, { ...created, seq: 2, action: 'signed', pep: true }, { ...created, seq: 3, action: 'notified', comment: 'отправлено' },
        { ...created, seq: 4, stage: 'manager', stage_title: 'руководитель', action: 'auto_approved', actor_name: '', ip: '' }] });
    expect(sheet).toContain('Лист согласования авансового отчёта АО-2026-11-03');
    expect(sheet).toContain('ООО «Альфа», ИНН 7701000001');
    expect(sheet).toContain(`<code>${HASH}</code>`);
    expect(sheet).toContain('подписал (ПЭП)');
    expect(sheet).toContain('согласовано автоматически');
    expect(sheet).not.toContain('уведомления');
    expect(sheet).toContain('63-ФЗ, ст. 9');
  });

  it('позиции: проверка ФНС и пометка политики расходов', () => {
    const html = mod.approvalsItemsHtml(snapshot);
    expect(html).toContain('<span class="chip valid">ФНС: подтверждён</span>');
    expect(html).toContain('<span class="chip not_found">ФНС: не найден</span> <span class="chip failed">политика</span>');
    expect(html).toContain('05.10.2026 12:00');
  });
});

describe('«Согласования»: сценарии', () => {
  it('сотрудник: список, карточка, подпись только после отметки подтверждения', async () => {
    const box = document.getElementById('app');
    await mod.viewApprovals(box, { me: { id: 'u-emp', role: 'user', full_name: 'Иванов И. И.' } });
    expect(calls.get).toEqual(['/api/v1/approvals?box=todo', '/api/v1/approvals/settings']);
    expect(box.querySelector('#ap-new')).not.toBeNull();
    expect(box.querySelector('.ap-boxes [data-box="todo"] .badge').textContent).toBe('1');
    box.querySelector('.ap-card').click();
    await tick();
    expect(calls.get.at(-1)).toBe('/api/v1/approvals/ap-1');
    const slot = document.querySelector('.modal-slot');
    expect(slot.textContent).toContain('Журнал решений');
    expect(slot.textContent).toContain('Содержимое и журнал целы');
    const btn = slot.querySelector('#ap-do-sign');
    expect(btn.disabled).toBe(true);
    const agree = slot.querySelector('#ap-agree');
    agree.checked = true;
    agree.dispatchEvent(new Event('change'));
    expect(btn.disabled).toBe(false);
    btn.click();
    await tick();
    expect(calls.post).toEqual([{ path: '/api/v1/approvals/ap-1/sign', body: { doc_hash: HASH, confirm: true } }]);
  });

  it('руководитель: отказ уходит с причиной, предупреждения о правке чеков и о целостности видны', async () => {
    detail = { ...detail, status: 'pending', stage: 'manager', minutes_left: 300, approvers: ['Петров П. П.'],
      can: { sign: false, decide: true, cancel: false, resubmit: false },
      changes: [{ id: 'r2', kind: 'changed', merchant: 'Стройбаза 7', was: 700, now: 900, fns_was: 'valid', fns_now: 'valid' }],
      integrity: { content: true, journal: { ok: false, broken_at: 2 } } };
    await mod.approvalsOpen('ap-1', { id: 'u-boss', role: 'manager', full_name: 'Петров П. П.' }, () => {});
    const slot = document.querySelector('.modal-slot');
    expect(slot.textContent).toContain('После формирования изменены чеки (1)');
    expect(slot.textContent).toContain('Журнал нарушен с записи № 2');
    expect(slot.textContent).toContain('до автосогласования 5 ч · решают: Петров П. П.');
    expect(slot.querySelector('#ap-do-sign')).toBeNull();
    const agree = slot.querySelector('#ap-agree');
    agree.checked = true;
    agree.dispatchEvent(new Event('change'));
    slot.querySelector('#ap-comment').value = 'Нет чека на доставку';
    slot.querySelector('#ap-do-reject').click();
    await tick();
    expect(calls.post).toEqual([{ path: '/api/v1/approvals/ap-1/decision',
      body: { decision: 'reject', doc_hash: HASH, confirm: true, comment: 'Нет чека на доставку' } }]);
  });

  it('администратор: выбор компании, настройки с каналами и порогами', async () => {
    const box = document.getElementById('app');
    await mod.viewApprovals(box, { me: { id: 'u-adm', role: 'admin' }, companies: [{ id: 'c-a', name: 'Альфа' }] });
    expect(calls.get).toEqual(['/api/v1/approvals?box=todo']);            // компания не выбрана — настроек нет
    const select = box.querySelector('#ap-company');
    select.value = 'c-a';
    select.dispatchEvent(new Event('change'));
    await tick();
    expect(calls.get.slice(-2)).toEqual(['/api/v1/approvals?box=todo&company_id=c-a', '/api/v1/approvals/settings?company_id=c-a']);
    expect(box.querySelector('#ap-new')).toBeNull();                       // администратор отчёт не подаёт
    const form = box.querySelector('#ap-settings');
    expect(form.textContent).toContain('бот MAX не подключён');
    form.querySelector('#ap-mlimit').value = '5000';
    form.querySelector('.ap-ch[value="max"]').checked = false;
    form.dispatchEvent(new Event('submit', { cancelable: true }));
    await tick();
    expect(calls.put).toEqual([{ path: '/api/v1/approvals/settings', body: { company_id: 'c-a', enabled: true,
      timeout_hours: 72, manager_limit: 5000, accountant_limit: 0, channels: ['telegram'] } }]);
  });
});
