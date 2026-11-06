/* v1.70.83: экран разбора конфликтов единого реестра (#9).
   v1.70.85: вкладка «Заявки» и блок «Организации» («Единая база», T-34).
   v1.70.86: вкладка «Показ компаниям», значок «🏢 N», отказ перемещения (T-35, Q-24).
   v1.70.88: вкладка «Готовность» единой записи — только числа (фаза 4а, T-37).
   v1.70.111: вкладка «Время чеков» — сдвиг формой правки до 1.70.110, возврат по QR (T-78). */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../app/static/js/api.js';
import {
  COUNT_MODES, COUNT_ROLES, REVIEW_FLAGS, badgeCount, blockedMoveSummary, buildClaimDecisionBody,
  buildResolveBody, buildVisibilityBody, canResolve, claimActions, claimSourceTitle, claimStatusTitle,
  companiesHtml, companyRoleTitle, decodeFlags, eventTitle, orgCountChip, orgCountRow, orgWord,
  readinessCards, readinessRows, setConflictsBadge, validateReason, viewUnifiedReview, visibilityWarning,
  fmtShift, timeConfirmText, timeRestoredText, timeRows, timeSummary, wallTime,
} from '../../app/static/js/unified_review.js';

vi.mock('../../app/static/js/api.js', () => ({ api: { get: vi.fn(), post: vi.fn(), put: vi.fn() } }));

const DOC = {
  id: 'd1', receipt_date: '2026-09-24T12:30:00', total_sum: 777, merchant_name: 'Спорный магазин',
  review_flags: 8, company_name: null, version: 3,
};
const CARD = {
  ...DOC, fn: '9999999999990077', fd: '77', fp: '9990077', items_count: 1, deleted: false, can_resolve: true,
  copies: [
    { kind: 'pool', id: 'p1', company_id: 'c1', company_name: 'Альфа' },
    { kind: 'registry', id: 'r1', company_id: 'c2', company_name: 'Бета' },
  ],
  candidates: [
    { company_id: 'c1', company_name: 'Альфа', sources: ['pool'] },
    { company_id: 'c2', company_name: 'Бета', sources: ['registry'] },
  ],
  events: [{
    id: 'e1', type: 'register', actor_id: null, actor_name: null, from_company_id: null,
    to_company_id: 'c1', to_company_name: 'Альфа', details: { source_kind: 'pool' },
    created_at: '2026-09-24T12:31:00Z',
  }],
};

function mountDom() {
  document.body.innerHTML = `<main id="view"></main>
    <div id="modal-root" class="hidden"><div class="modal-backdrop"></div><div class="modal-slot"></div></div>
    <div id="toast-container"></div>
    <span id="nav-conflicts-badge" class="nav-badge hidden">0</span>`;
  return document.querySelector('#view');
}

function routeGet({ list = [DOC], total = 1, card = CARD } = {}) {
  api.get.mockImplementation(async (path) => {
    if (path === '/api/v1/unified/stats') {
      return { documents: 10, conflicts: total, ownership_conflicts: total, origins: 20 };
    }
    if (path.startsWith('/api/v1/unified/documents?')) return { total, page: 1, per_page: 20, items: list };
    if (path === '/api/v1/unified/documents/d1/review') return card;
    if (path === '/api/v1/companies') {
      return [{ id: 'c1', name: 'Альфа', is_active: true }, { id: 'c3', name: 'Гамма', is_active: true },
        { id: 'c4', name: 'Закрытая', is_active: false }];
    }
    throw new Error(`неожиданный запрос ${path}`);
  });
}

const flush = () => new Promise((resolve) => { setTimeout(resolve, 0); });

async function openFirstCard(view) {
  view.querySelector('.ur-open').click();
  await flush();
  await flush();
  return document.querySelector('.modal-slot');
}

describe('разбор конфликтов: чистые функции', () => {
  it('пометки расшифровываются по битам', () => {
    expect(decodeFlags(8 | 32).map((f) => f.code)).toEqual(['ownership', 'incomplete']);
    expect(decodeFlags(0)).toEqual([]);
    expect(REVIEW_FLAGS.filter((f) => f.blocking).map((f) => f.bit)).toEqual([1, 2, 4, 8, 16, 64]);
  });

  it('решить можно только спор о владельце у живого чека', () => {
    expect(canResolve({ review_flags: 8 })).toBe(true);
    expect(canResolve({ review_flags: 2 })).toBe(false);
    expect(canResolve({ review_flags: 8, deleted: true })).toBe(false);
    expect(canResolve(null)).toBe(false);
  });

  it('причина обязательна и ограничена по длине', () => {
    expect(validateReason('  ab ')).toContain('не короче 3');
    expect(validateReason('По договору')).toBe('');
    expect(validateReason('я'.repeat(501))).toContain('длиннее 500');
  });

  it('тело запроса: версия карточки, пустая компания — null', () => {
    expect(buildResolveBody({ version: 3 }, 'c1', '  причина ')).toEqual(
      { company_id: 'c1', expected_version: 3, reason: 'причина' });
    expect(buildResolveBody({ version: 4 }, '', 'причина').company_id).toBeNull();
  });

  it('события журнала подписаны по-русски', () => {
    expect(eventTitle('resolve_ownership')).toBe('Решение спора о владельце');
    expect(eventTitle('backfill')).toBe('Перенос архива');
    expect(eventTitle('новый_тип')).toBe('новый_тип');
  });

  it('значок в меню скрыт при нуле', () => {
    mountDom();
    const badge = document.querySelector('#nav-conflicts-badge');
    setConflictsBadge(0);
    expect(badge.classList.contains('hidden')).toBe(true);
    setConflictsBadge(1234);
    expect(badge.classList.contains('hidden')).toBe(false);
    expect(badge.textContent.replace(/\s/g, '')).toBe('1234');
  });
});

describe('разбор конфликтов: экран', () => {
  beforeEach(() => {
    api.get.mockReset();
    api.post.mockReset();
  });

  it('список, карточка и решение «закрепить за компанией»', async () => {
    routeGet();
    api.post.mockResolvedValue({ ...CARD, review_flags: 0, company_id: 'c2', version: 4 });
    const view = mountDom();
    await viewUnifiedReview(view);
    expect(view.querySelector('tbody').textContent).toContain('Спорный магазин');
    expect(view.querySelector('tbody .chip').textContent.trim()).toBe('Владелец');
    expect(view.querySelector('#ur-kpi').textContent).toContain('из них спор о владельце');
    expect(document.querySelector('#nav-conflicts-badge').classList.contains('hidden')).toBe(false);
    const slot = await openFirstCard(view);
    expect(slot.querySelector('#ur-copies').textContent.replace(/\s+/g, ' ')).toContain('Чек-Пул: Альфа');
    expect(slot.textContent).toContain('Регистрация копии');
    const options = [...slot.querySelectorAll('#ur-company option')].map((o) => o.textContent);
    expect(options).toEqual(['Альфа', 'Бета', 'Гамма']);
    slot.querySelector('#ur-company').value = 'c2';
    slot.querySelector('#ur-reason').value = 'Оплачен картой Беты';
    slot.querySelector('#ur-resolve').click();
    await flush();
    await flush();
    expect(api.post).toHaveBeenCalledWith('/api/v1/unified/documents/d1/resolve-ownership',
      { company_id: 'c2', expected_version: 3, reason: 'Оплачен картой Беты' });
    expect(document.querySelector('#modal-root').classList.contains('hidden')).toBe(true);
    expect(document.querySelector('#toast-container').textContent).toContain('Решение применено');
  });

  it('без причины запрос не уходит', async () => {
    routeGet();
    const view = mountDom();
    await viewUnifiedReview(view);
    const slot = await openFirstCard(view);
    slot.querySelector('#ur-reason').value = ' ';
    slot.querySelector('#ur-resolve').click();
    await flush();
    expect(api.post).not.toHaveBeenCalled();
    expect(slot.querySelector('#ur-form-error').textContent).toContain('Укажите причину');
  });

  it('«оставить без владельца» отправляет company_id: null', async () => {
    routeGet();
    api.post.mockResolvedValue({ ...CARD, review_flags: 0, version: 4 });
    const view = mountDom();
    await viewUnifiedReview(view);
    const slot = await openFirstCard(view);
    slot.querySelector('#ur-mode-none').checked = true;
    slot.querySelector('#ur-reason').value = 'Чек не относится к компаниям';
    slot.querySelector('#ur-resolve').click();
    await flush();
    expect(api.post.mock.calls[0][1]).toEqual(
      { company_id: null, expected_version: 3, reason: 'Чек не относится к компаниям' });
  });

  it('устаревшая версия: понятная ошибка и кнопка «открыть заново»', async () => {
    routeGet();
    api.post.mockRejectedValue(Object.assign(new Error('Версия чека изменилась — обновите страницу'), { status: 409 }));
    const view = mountDom();
    await viewUnifiedReview(view);
    const slot = await openFirstCard(view);
    slot.querySelector('#ur-reason').value = 'Решение по договору';
    slot.querySelector('#ur-resolve').click();
    await flush();
    expect(slot.querySelector('#ur-form-error').textContent).toContain('Версия чека изменилась');
    expect(slot.querySelector('#ur-reload')).not.toBeNull();
    expect(slot.querySelector('#ur-resolve').disabled).toBe(false);
  });

  it('расхождение данных без спора о владельце — только просмотр', async () => {
    routeGet({ card: { ...CARD, review_flags: 2, can_resolve: false } });
    const view = mountDom();
    await viewUnifiedReview(view);
    const slot = await openFirstCard(view);
    expect(slot.querySelector('#ur-resolve')).toBeNull();
    expect(slot.textContent).toContain('расхождение данных между копиями');
    expect(api.get).not.toHaveBeenCalledWith('/api/v1/companies');
  });

  it('пустой список — понятное сообщение, фильтр уходит в запрос', async () => {
    routeGet({ list: [], total: 0 });
    const view = mountDom();
    await viewUnifiedReview(view);
    expect(view.textContent).toContain('Конфликтов нет');
    view.querySelector('#ur-flag').value = '8';
    view.querySelector('#ur-flag').dispatchEvent(new Event('change'));
    await flush();
    const listCalls = api.get.mock.calls.map((c) => c[0]).filter((p) => p.startsWith('/api/v1/unified/documents?'));
    expect(listCalls.at(-1)).toContain('flag=8');
    expect(listCalls.at(-1)).toContain('conflicts=true');
  });
});


// --------------------------------------------------------------------------
//  v1.70.85: заявки компаний на чужие чеки
// --------------------------------------------------------------------------
const LINKED = {
  document_id: 'd1', version: 5, count: 2,
  companies: [
    { link_id: null, company_id: 'c1', company_name: 'Альфа', role: 'owner', counted: true },
    { link_id: 'l1', company_id: 'c2', company_name: 'Бета', role: 'claimant', status: 'open',
      source: 'scan_qr', attempts: 3, counted: true },
    { link_id: 'l2', company_id: 'c3', company_name: null, company_deleted: true, role: 'claimant',
      status: 'void', source: 'manual', attempts: 1, counted: false },
    { link_id: 'l3', company_id: 'c4', company_name: 'Дельта', role: 'former_owner', status: 'open',
      source: 'move', attempts: 0, counted: false },
  ],
};
const CLAIM = {
  link_id: 'l1', document_id: 'd1', version: 5, company_id: 'c2', company_name: 'Бета',
  company_deleted: false, role: 'claimant', status: 'open', source: 'scan_qr', attempts: 3,
  last_at: '2026-10-30T09:00:00Z', last_user_name: 'Бухгалтер Беты', receipt_date: '2026-09-22T15:56:00',
  total_sum: 100, merchant_name: 'Магазин заявки', owner_company_id: 'c1', owner_company_name: 'Альфа',
  blocking: false, counted: true,
};

function routeClaims({ claims = [CLAIM], linked = LINKED, stats = {} } = {}) {
  api.get.mockImplementation(async (path) => {
    if (path === '/api/v1/unified/stats') {
      return { documents: 10, conflicts: 1, ownership_conflicts: 1, origins: 20, claims_open: claims.length, ...stats };
    }
    if (path.startsWith('/api/v1/unified/documents?')) return { total: 1, page: 1, per_page: 20, items: [DOC] };
    if (path.startsWith('/api/v1/unified/claims?')) return { total: claims.length, page: 1, per_page: 20, items: claims };
    if (path === '/api/v1/unified/documents/d1/companies') return linked;
    if (path === '/api/v1/unified/documents/d1/review') return CARD;
    if (path === '/api/v1/companies') return [{ id: 'c1', name: 'Альфа', is_active: true }];
    throw new Error(`неожиданный запрос ${path}`);
  });
}

describe('заявки компаний: чистые функции', () => {
  it('статусы, источники и роли подписаны по-русски', () => {
    expect(claimStatusTitle('open')).toBe('Ждёт решения');
    expect(claimStatusTitle('void')).toBe('Не учитывается');
    expect(claimSourceTitle('pool_transfer')).toBe('перенос из Чек-Пула');
    expect(companyRoleTitle('former_owner')).toBe('Прежний владелец');
    expect(eventTitle('claim_decline')).toBe('Заявка отклонена');
    expect(claimStatusTitle('новый')).toBe('новый');
  });

  it('значок меню — конфликты плюс заявки, ждущие решения', () => {
    expect(badgeCount({ conflicts: 2, claims_open: 3 })).toBe(5);
    expect(badgeCount({ conflicts: 1 })).toBe(1);
    expect(badgeCount(null)).toBe(0);
  });

  it('решения: переданную не решают, передать нельзя удалённой компании и спорному чеку', () => {
    expect(claimActions({ status: 'open' })).toEqual(['transfer', 'decline', 'void']);
    expect(claimActions({ status: 'declined' })).toEqual(['transfer', 'void']);
    expect(claimActions({ status: 'void', company_deleted: true })).toEqual(['decline']);
    expect(claimActions({ status: 'open', blocking: true })).toEqual(['decline', 'void']);
    expect(claimActions({ status: 'transferred' })).toEqual([]);
  });

  it('тело решения: версия чека и причина без пробелов по краям', () => {
    expect(buildClaimDecisionBody({ version: 5 }, '  оплачен картой ')).toEqual(
      { expected_version: 5, reason: 'оплачен картой' });
  });

  it('блок «Организации»: число, роли, «не в числе» и удалённая компания', () => {
    document.body.innerHTML = `<div id="x">${companiesHtml(LINKED)}</div>`;
    const text = document.querySelector('#x').textContent.replace(/\s+/g, ' ');
    expect(document.querySelector('#ur-companies-count b').textContent).toBe('2');
    expect(text).toContain('Альфа — Владелец');
    expect(text).toContain('Бета — Претендент · Ждёт решения · скан QR · попыток: 3');
    expect(text).toContain('компания удалена — Претендент · Не учитывается');
    expect(text).toContain('Дельта — Прежний владелец · перемещение (не в числе)');
    expect(companiesHtml(null)).toBe('');
  });
});

describe('заявки компаний: экран', () => {
  beforeEach(() => {
    api.get.mockReset();
    api.post.mockReset();
  });

  it('значок и показатель учитывают заявки; вкладка грузится по клику', async () => {
    routeClaims({ claims: [CLAIM, { ...CLAIM, link_id: 'l9', company_id: 'c9', company_name: 'Эпсилон' }] });
    const view = mountDom();
    await viewUnifiedReview(view);
    expect(document.querySelector('#nav-conflicts-badge').textContent.trim()).toBe('3');
    expect(view.querySelector('#ur-kpi').textContent).toContain('заявок ждут решения');
    expect(view.querySelector('#ur-claims-badge').textContent.trim()).toBe('2');
    expect(api.get.mock.calls.some((c) => c[0].startsWith('/api/v1/unified/claims'))).toBe(false);
    view.querySelector('#ur-tab-claims').click();
    await flush();
    expect(view.querySelector('#ur-pane-claims').classList.contains('hidden')).toBe(false);
    expect(view.querySelector('#ur-pane-conflicts').classList.contains('hidden')).toBe(true);
    const text = view.querySelector('#ur-claims-out').textContent;
    expect(text).toContain('Магазин заявки');
    expect(text).toContain('Бета');
    expect(text).toContain('Ждёт решения');
    view.querySelector('#ur-claim-status').value = 'declined';
    view.querySelector('#ur-claim-status').dispatchEvent(new Event('change'));
    await flush();
    const calls = api.get.mock.calls.map((c) => c[0]).filter((p) => p.startsWith('/api/v1/unified/claims?'));
    expect(calls.at(-1)).toContain('status=declined');
  });

  it('решение «отклонить»: причина обязательна, версия уходит в запрос', async () => {
    routeClaims();
    api.post.mockResolvedValue({ ok: true, status: 'declined' });
    const view = mountDom();
    await viewUnifiedReview(view);
    view.querySelector('#ur-tab-claims').click();
    await flush();
    view.querySelector('.ur-claim-open').click();
    await flush();
    await flush();
    const slot = document.querySelector('.modal-slot');
    expect(slot.querySelector('#ur-companies-count').textContent).toContain('2');
    expect([...slot.querySelectorAll('[data-action]')].map((b) => b.dataset.action))
      .toEqual(['transfer', 'decline', 'void']);
    slot.querySelector('#ur-claim-reason').value = ' ';
    slot.querySelector('#ur-claim-decline').click();
    await flush();
    expect(api.post).not.toHaveBeenCalled();
    expect(slot.querySelector('#ur-claim-error').textContent).toContain('Укажите причину');
    slot.querySelector('#ur-claim-reason').value = 'Чек оплачен Альфой';
    slot.querySelector('#ur-claim-decline').click();
    await flush();
    await flush();
    expect(api.post).toHaveBeenCalledWith('/api/v1/unified/links/l1/decline',
      { expected_version: 5, reason: 'Чек оплачен Альфой' });
    expect(document.querySelector('#modal-root').classList.contains('hidden')).toBe(true);
    expect(document.querySelector('#toast-container').textContent).toContain('Заявка отклонена');
  });

  it('ошибка решения остаётся в окне, кнопка снова доступна', async () => {
    routeClaims();
    api.post.mockRejectedValue(Object.assign(new Error('Версия чека изменилась — обновите страницу'), { status: 409 }));
    const view = mountDom();
    await viewUnifiedReview(view);
    view.querySelector('#ur-tab-claims').click();
    await flush();
    view.querySelector('.ur-claim-open').click();
    await flush();
    await flush();
    const slot = document.querySelector('.modal-slot');
    slot.querySelector('#ur-claim-reason').value = 'Передаём по договору';
    slot.querySelector('#ur-claim-transfer').click();
    await flush();
    expect(slot.querySelector('#ur-claim-error').textContent).toContain('Версия чека изменилась');
    expect(slot.querySelector('#ur-claim-transfer').disabled).toBe(false);
  });

  it('карточка спора показывает организации, сбой запроса — без блока', async () => {
    routeClaims();
    const view = mountDom();
    await viewUnifiedReview(view);
    let slot = await openFirstCard(view);
    expect(slot.querySelector('#ur-companies').textContent).toContain('Бета');
    routeGet();
    view.querySelector('.ur-open').click();
    await flush();
    await flush();
    slot = document.querySelector('.modal-slot');
    expect(slot.querySelector('#ur-companies')).toBeNull();
    expect(slot.querySelector('#ur-copies')).not.toBeNull();
  });
});


// --------------------------------------------------------------------------
//  v1.70.86: число организаций для компаний и отказ перемещения (Q-24)
// --------------------------------------------------------------------------
const VISIBILITY = {
  mode: 'off', roles: ['accountant', 'manager'], allowed_roles: ['accountant', 'manager'],
  companies: [
    { id: 'c1', name: 'Альфа', is_active: true, enabled: false },
    { id: 'c2', name: 'Бета', is_active: true, enabled: true },
    { id: 'c3', name: 'Закрытая', is_active: false, enabled: false },
  ],
};

function routeVisibility(payload = VISIBILITY) {
  routeClaims();
  const base = api.get.getMockImplementation();
  api.get.mockImplementation(async (path) => (path === '/api/v1/unified/visibility' ? payload : base(path)));
}

describe('число организаций: чистые функции', () => {
  it('склонение и значок — только от двух организаций', () => {
    expect([1, 2, 5, 11, 21, 22, 25, 111, 112].map(orgWord)).toEqual([
      'организация', 'организации', 'организаций', 'организаций', 'организация', 'организации',
      'организаций', 'организаций', 'организаций']);
    expect(orgCountChip(undefined, false)).toBe('');
    expect(orgCountChip(1, true)).toBe('');
    const chip = orgCountChip(2, false);
    expect(chip).toContain('🏢 2');
    expect(chip).toContain('org-count');
    expect(chip).toContain('включая вашу');
    expect(orgCountChip(3, true)).toContain('Разборе конфликтов');
  });

  it('строка карточки — при любом числе, без поля — ничего', () => {
    expect(orgCountRow(1, false)).toContain('id="rd-org-count">1');
    expect(orgCountRow(0, true)).toContain('id="rd-org-count">0');
    expect(orgCountRow(undefined, true)).toBe('');
    expect(orgCountRow(null, false)).toBe('');
  });

  it('отказ перемещения: чеки на разборе, причины и сколько можно перенести', () => {
    const summary = blockedMoveSummary({
      message: 'Чек требует разбора перед перемещением: 2 из 5 отмеченных…',
      blocked: [{ receipt_id: 'r1', reasons: ['ownership', 'amount'] }, { receipt_id: 'r2', reasons: ['amount'] }],
      movable: 3,
    });
    expect(summary).toEqual({
      blocked: 2, movable: 3, reasons: ['владелец — 1', 'сумма — 2'],
      message: 'Чек требует разбора перед перемещением: 2 из 5 отмеченных…',
    });
    expect(blockedMoveSummary('Компания назначения не найдена')).toBeNull();
    expect(blockedMoveSummary(null)).toBeNull();
    expect(blockedMoveSummary({ blocked: [], movable: -1 }).movable).toBe(0);
  });

  it('тело настроек: режим, только разрешённые роли, список компаний всегда', () => {
    expect(COUNT_MODES.map((m) => m.code)).toEqual(['off', 'all', 'selected']);
    expect(COUNT_ROLES.map((r) => r.code)).toEqual(['accountant', 'manager']);
    expect(buildVisibilityBody('selected', ['accountant', 'user', 'accountant'], ['c1', 'c1']))
      .toEqual({ mode: 'selected', roles: ['accountant'], company_ids: ['c1'] });
    expect(buildVisibilityBody('everyone', null, null)).toEqual({ mode: 'off', roles: [], company_ids: [] });
  });

  it('предупреждение: включённый показ, который никто не увидит', () => {
    expect(visibilityWarning('off', [], [])).toBe('');
    expect(visibilityWarning('all', [], [])).toContain('ни одна роль');
    expect(visibilityWarning('selected', ['manager'], [])).toContain('ни одна компания');
    expect(visibilityWarning('selected', ['manager'], ['c1'])).toBe('');
    expect(visibilityWarning('all', ['accountant'], [])).toBe('');
  });
});

describe('число организаций: вкладка «Показ компаниям»', () => {
  beforeEach(() => {
    api.get.mockReset();
    api.post.mockReset();
    api.put.mockReset();
  });

  it('грузится по клику, предупреждает и сохраняет режим, роли и компании', async () => {
    routeVisibility();
    api.put.mockImplementation(async (_path, body) => ({
      ...VISIBILITY, mode: body.mode, roles: body.roles,
      companies: VISIBILITY.companies.map((c) => ({ ...c, enabled: body.company_ids.includes(c.id) })),
    }));
    const view = mountDom();
    await viewUnifiedReview(view);
    view.querySelector('#ur-tab-visibility').click();
    await flush();
    expect(view.querySelector('#ur-tab-visibility').getAttribute('aria-selected')).toBe('true');
    expect(view.querySelector('#ur-pane-claims').classList.contains('hidden')).toBe(true);
    expect(view.querySelector('#ur-pane-conflicts').classList.contains('hidden')).toBe(true);
    expect(view.querySelector('#ur-vis-mode-off').checked).toBe(true);
    expect(view.querySelector('#ur-vis-companies').textContent).toContain('(в архиве)');
    const warning = view.querySelector('#ur-vis-warning');
    expect(warning.textContent).toBe('');
    view.querySelector('#ur-vis-mode-selected').checked = true;
    view.querySelector('#ur-vis-mode-selected').dispatchEvent(new Event('change'));
    view.querySelector('.ur-vis-company[value="c2"]').checked = false;
    view.querySelector('.ur-vis-company[value="c2"]').dispatchEvent(new Event('change'));
    expect(warning.textContent).toContain('ни одна компания');
    view.querySelector('.ur-vis-company[value="c1"]').checked = true;
    view.querySelector('.ur-vis-company[value="c1"]').dispatchEvent(new Event('change'));
    view.querySelector('#ur-vis-role-manager').checked = false;
    view.querySelector('#ur-vis-role-manager').dispatchEvent(new Event('change'));
    expect(warning.textContent).toBe('');
    view.querySelector('#ur-vis-save').click();
    await flush();
    expect(api.put).toHaveBeenCalledWith('/api/v1/unified/visibility',
      { mode: 'selected', roles: ['accountant'], company_ids: ['c1'] });
    expect(document.querySelector('#toast-container').textContent).toContain('Настройки показа сохранены');
    expect(view.querySelector('#ur-vis-mode-selected').checked).toBe(true);
    expect(view.querySelector('.ur-vis-company[value="c1"]').checked).toBe(true);
  });

  it('ошибка сохранения остаётся на вкладке, кнопка снова доступна', async () => {
    routeVisibility();
    api.put.mockRejectedValue(Object.assign(new Error('Недостаточно прав'), { status: 403 }));
    const view = mountDom();
    await viewUnifiedReview(view);
    view.querySelector('#ur-tab-visibility').click();
    await flush();
    view.querySelector('#ur-vis-save').click();
    await flush();
    expect(view.querySelector('#ur-vis-warning').textContent).toContain('Недостаточно прав');
    expect(view.querySelector('#ur-vis-save').disabled).toBe(false);
  });

  it('«Обновить» на вкладке перечитывает настройки, а не список споров', async () => {
    routeVisibility();
    const view = mountDom();
    await viewUnifiedReview(view);
    view.querySelector('#ur-tab-visibility').click();
    await flush();
    api.get.mockClear();
    view.querySelector('#ur-refresh').click();
    await flush();
    const paths = api.get.mock.calls.map((c) => c[0]);
    expect(paths).toContain('/api/v1/unified/visibility');
    expect(paths.some((p) => p.startsWith('/api/v1/unified/documents?'))).toBe(false);
  });
});

const READINESS = {
  registry: { total: 1200, linked: 900, pending: 300 },
  pool: { total: 40, linked: 40, pending: 0 },
  documents: 910,
  review: { blocking: 3, ownership: 1, verification: 0, incomplete_identity: 7 },
  drift: { registry: 0, pool: 0 },
  ready_percent: 75.8,
};

describe('готовность единой записи (v1.70.88)', () => {
  it('карточки и строки — только числа; пустой ответ даёт нули, а не ошибку', () => {
    const cards = readinessCards(READINESS);
    expect(cards.map((c) => c.sub)).toEqual(['чеков в единой записи', 'чеков реестра ждут регистрации',
      'чеков Чек-Пула ждут регистрации', 'копий расходятся']);
    expect(cards[0].value).toBe('75,8 %');
    expect(cards[1].value.replace(/\s/g, '')).toBe('300');
    const rows = readinessRows(READINESS);
    expect(rows[0][1].replace(/\s/g, '')).toBe('1200/900/300');
    expect(rows.find(([label]) => label.startsWith('из них спор'))[1]).toBe('1');
    expect(readinessCards(undefined).map((c) => c.value)).toEqual(['0 %', '0', '0', '0']);
    expect(readinessRows({})).toHaveLength(7);
    expect(readinessCards({ drift: { registry: 2, pool: 1 } })[3].sub).toContain('сверка сведёт');
  });

  it('вкладка загружает отчёт, «Обновить» перечитывает его, а не список споров', async () => {
    routeClaims();
    const base = api.get.getMockImplementation();
    api.get.mockImplementation(async (path) => (path === '/api/v1/unified/readiness' ? READINESS : base(path)));
    const view = mountDom();
    await viewUnifiedReview(view);
    view.querySelector('#ur-tab-readiness').click();
    await flush();
    expect(view.querySelector('#ur-pane-readiness').classList.contains('hidden')).toBe(false);
    expect(view.querySelector('#ur-tab-readiness').getAttribute('aria-selected')).toBe('true');
    expect(view.querySelector('#ur-ready-out').textContent).toContain('75,8 %');
    expect(view.querySelector('#ur-pane-readiness').textContent).toContain('--soft --dry-run');
    api.get.mockClear();
    view.querySelector('#ur-refresh').click();
    await flush();
    const paths = api.get.mock.calls.map((c) => c[0]);
    expect(paths).toContain('/api/v1/unified/readiness');
    expect(paths.some((p) => p.startsWith('/api/v1/unified/documents?'))).toBe(false);
  });

  it('ошибка отчёта остаётся в панели', async () => {
    routeClaims();
    const base = api.get.getMockImplementation();
    api.get.mockImplementation(async (path) => {
      if (path === '/api/v1/unified/readiness') throw new Error('Только администратор');
      return base(path);
    });
    const view = mountDom();
    await viewUnifiedReview(view);
    view.querySelector('#ur-tab-readiness').click();
    await flush();
    expect(view.querySelector('#ur-ready-out').textContent).toContain('Только администратор');
  });
});

const TIME = {
  checked: 120, shifted: 2, fixable: 1, limit: 500,
  items: [
    { receipt_id: 'r1', company: 'Альфа', fn: '7000000000000001', fd: '3467', fp: '2882574203',
      receipt_date: '2026-09-22T10:56:00', qr_time: '2026-09-22T15:56:00', hours: -5, fixable: true, reason: '' },
    { receipt_id: 'r2', company: 'Бета', fn: '7000000000000002', fd: '12', fp: '777', receipt_date: '2026-09-22T12:56:00',
      qr_time: '2026-09-22T15:56:00', hours: -3, fixable: false, reason: 'ручной ввод: время в QR — введённое' },
  ],
};

describe('время чеков: чистые функции (T-78)', () => {
  it('время — как есть, без пересчёта в пояс браузера', () => {
    expect(wallTime('2026-09-22T10:56:00')).toBe('22.09.2026 10:56');
    expect(wallTime('2026-09-22T23:30:00+05:00')).toBe('22.09.2026 23:30');
    expect(wallTime(null)).toBe('—');
    expect([fmtShift(-5), fmtShift(3), fmtShift(0)]).toEqual(['−5 ч', '+3 ч', '0 ч']);
  });

  it('сводка, строки и тексты', () => {
    expect(timeSummary({ checked: 7, shifted: 0, items: [] })).toBe('Проверено чеков с QR: 7. Сдвинутого времени нет.');
    expect(timeSummary(TIME)).toBe('Проверено чеков с QR: 120. Время сдвинуто у 2, вернуть по QR можно у 1.');
    expect(timeSummary({ ...TIME, shifted: 600 })).toContain('В таблице — первые 2.');
    const rows = timeRows(TIME);
    expect(rows[0]).toEqual({ company: 'Альфа', receipt: '7000000000000001 / 3467 / 2882574203',
      stored: '22.09.2026 10:56', qr: '22.09.2026 15:56', shift: '−5 ч', action: 'вернуть по QR' });
    expect(rows[1].action).toBe('ручной ввод: время в QR — введённое');
    expect(timeConfirmText(1)).toContain('резервная копия базы');
    expect(timeRestoredText({ restored: 1, backup: 'db-manual-1.db' }))
      .toBe('Время возвращено у 1 чеков; резервная копия — db-manual-1.db');
    expect(timeRestoredText({ restored: 2, backup: null })).toBe('Время возвращено у 2 чеков');
  });
});

describe('время чеков: вкладка (T-78)', () => {
  beforeEach(() => {
    api.get.mockReset();
    api.post.mockReset();
  });

  function routeTime(payload = TIME) {
    routeGet();
    const base = api.get.getMockImplementation();
    api.get.mockImplementation(async (path) => (path === '/api/v1/unified/receipt-time' ? payload : base(path)));
  }

  it('список и возврат по QR после подтверждения', async () => {
    routeTime();
    api.post.mockResolvedValue({ restored: 1, skipped: 0, backup: 'db-manual-20261106.db' });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const view = mountDom();
    await viewUnifiedReview(view);
    view.querySelector('#ur-tab-time').click();
    await flush();
    expect(view.querySelector('#ur-tab-time').getAttribute('aria-selected')).toBe('true');
    expect(view.querySelector('#ur-pane-readiness').classList.contains('hidden')).toBe(true);
    expect(view.querySelector('#ur-time-summary').textContent).toContain('Время сдвинуто у 2');
    const cells = [...view.querySelectorAll('#ur-time-out tbody tr')].map((tr) => tr.textContent);
    expect(cells[0]).toContain('22.09.2026 10:56');
    expect(cells[0]).toContain('−5 ч');
    expect(cells[1]).toContain('ручной ввод');
    const button = view.querySelector('#ur-time-restore');
    expect(button.textContent).toContain('Вернуть время из QR (1)');
    button.click();
    await flush();
    expect(confirm).toHaveBeenCalledOnce();
    expect(api.post).toHaveBeenCalledWith('/api/v1/unified/receipt-time/restore', {});
    expect(document.querySelector('#toast-container').textContent)
      .toContain('Время возвращено у 1 чеков; резервная копия — db-manual-20261106.db');
    expect(api.get.mock.calls.filter(([path]) => path === '/api/v1/unified/receipt-time')).toHaveLength(2);
    confirm.mockRestore();
  });

  it('без подтверждения ничего не меняется, без сдвигов кнопки нет', async () => {
    routeTime();
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const view = mountDom();
    await viewUnifiedReview(view);
    view.querySelector('#ur-tab-time').click();
    await flush();
    view.querySelector('#ur-time-restore').click();
    await flush();
    expect(api.post).not.toHaveBeenCalled();
    confirm.mockRestore();
    routeTime({ checked: 5, shifted: 0, fixable: 0, items: [] });
    const again = mountDom();
    await viewUnifiedReview(again);
    again.querySelector('#ur-tab-time').click();
    await flush();
    expect(again.querySelector('#ur-time-summary').textContent).toContain('Сдвинутого времени нет');
    expect(again.querySelector('#ur-time-restore')).toBeNull();
  });
});
