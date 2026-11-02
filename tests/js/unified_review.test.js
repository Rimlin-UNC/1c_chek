/* v1.70.83: экран разбора конфликтов единого реестра (#9).
   v1.70.85: вкладка «Заявки» и блок «Организации» («Единая база», T-34). */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../app/static/js/api.js';
import {
  REVIEW_FLAGS, badgeCount, buildClaimDecisionBody, buildResolveBody, canResolve, claimActions,
  claimSourceTitle, claimStatusTitle, companiesHtml, companyRoleTitle, decodeFlags, eventTitle,
  setConflictsBadge, validateReason, viewUnifiedReview,
} from '../../app/static/js/unified_review.js';

vi.mock('../../app/static/js/api.js', () => ({ api: { get: vi.fn(), post: vi.fn() } }));

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
