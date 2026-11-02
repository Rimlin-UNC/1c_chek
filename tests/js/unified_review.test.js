/* v1.70.83: экран разбора конфликтов единого реестра (#9). */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../app/static/js/api.js';
import {
  REVIEW_FLAGS, buildResolveBody, canResolve, decodeFlags, eventTitle, setConflictsBadge,
  validateReason, viewUnifiedReview,
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
