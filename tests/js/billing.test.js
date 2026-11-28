/* v1.74.0: интерфейс корпоративных платных подписок — блок администратора #st-billing,
   витрина организации #my-billing-box и диалог при HTTP 402 Payment Required. */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { get: [], post: [], put: [], del: [] };

const sampleOverview = {
  config: {
    billing_mode: 'free_all',
    trial_days: 14,
    grace_days: 7,
    annual_discount_pct: 17,
    vat_mode: 'none',
    price_change_policy: 'grandfather',
    seller_name: 'ООО «Ямастер»',
    seller_inn: '7707083893',
    seller_kpp: '770701001',
    seller_bank: 'АО «ТБанк»',
    seller_bik: '044525974',
    seller_account: '40702810000000000001',
    seller_corr_account: '30101810145250000974',
    invoice_prefix: 'YM',
    support_email: 'chek@ymaster.ru',
  },
  vat_modes: [
    { code: 'none', title: 'Без НДС (УСН)', rate: 0 },
    { code: 'vat5', title: 'В т.ч. НДС 5%', rate: 5 },
    { code: 'vat20', title: 'В т.ч. НДС 20%', rate: 20 },
  ],
  metrics: {
    mrr_kop: 349000,
    mrr_formatted: '3 490 ₽',
    arr_kop: 4188000,
    arr_formatted: '41 880 ₽',
    total_companies: 2,
    active: 1,
    trial: 1,
    grace: 0,
    expired: 0,
    partner: 0,
    free: 0,
    suspended: 0,
    expiring_7d: 0,
    expiring_30d: 1,
    grandfathered: 1,
    open_invoices: 1,
    paid_invoices: 2,
  },
  module_prices: [
    { code: 'receipts', title: 'Чеки и авансовые отчёты', description: 'ККТ и АО-1', monthly_kop: 149000, yearly_kop: 1490000, monthly_quota: 500, available: true, price_version: 1 },
    { code: 'upd', title: 'Первичка (УПД) и сверка с банком', description: 'УПД и банк', monthly_kop: 199000, yearly_kop: 1990000, monthly_quota: 200, available: true, price_version: 1 },
    { code: 'contracts', title: 'Подготовка договоров', description: 'Конструктор договоров', monthly_kop: 129000, yearly_kop: 1290000, monthly_quota: 100, available: false, price_version: 1 },
  ],
  plans: [
    {
      code: 'accounting_pro',
      title: 'Бухгалтерия ПРО',
      badge: 'Популярный',
      description: 'Чеки, УПД, контрагенты',
      modules: ['receipts', 'upd', 'counterparties'],
      monthly_kop: 349000,
      quarterly_kop: 989000,
      yearly_kop: 3490000,
      limits: { users: 25, receipts_monthly: 1500, upd_monthly: 500, counterparties_monthly: 200 },
      active: true,
      sort_order: 30,
      price_version: 2,
    },
  ],
  companies: [
    {
      company_id: 'comp-1',
      company_name: 'ООО Ромашка',
      inn: '7701234567',
      is_active: true,
      subscription: {
        status: 'active',
        status_title: 'Активна',
        plan_code: 'accounting_pro',
        plan_title: 'Бухгалтерия ПРО',
        billing_cycle: 'monthly',
        enabled_modules: ['receipts', 'upd', 'counterparties'],
        addon_modules: [],
        locked_price_kop: 299000,
        price_locked: true,
        grandfathered: true,
        custom_discount_pct: 0,
        period_end: '2026-12-27T00:00:00Z',
        days_left: 30,
        usage: { period: '2026-11', receipts: 42, upd: 12, counterparties: 5 },
        limits: { receipts_monthly: 1500, upd_monthly: 500, counterparties_monthly: 200 },
      },
    },
  ],
  promos: [
    { code: 'WELCOME15', title: 'Старт', discount_kind: 'percent', discount_value: 15, bonus_trial_days: 7, used_count: 1, max_uses: 10, valid_until: '2027-01-31', active: true },
  ],
  invoices: [
    { id: 'inv-1', number: 'YM-202611-001', company_id: 'comp-1', buyer_name: 'ООО Ромашка', buyer_inn: '7701234567', cycle_title: '12 мес.', total_kop: 3490000, vat_label: 'Без НДС', status: 'issued', created_at: '2026-11-27T10:00:00Z' },
  ],
  events: [
    { id: 1, created_at: '2026-11-27T10:05:00Z', username: 'admin', action: 'billing_plan_saved', entity_id: 'accounting_pro', details: '{"monthly_kop":349000}' },
  ],
};

const sampleClientView = {
  company: { id: 'comp-1', name: 'ООО Ромашка', full_name: 'Общество Ромашка', inn: '7701234567' },
  billing_mode: 'strict',
  vat_mode: 'none',
  vat_label: 'Без НДС (УСН)',
  support_email: 'chek@ymaster.ru',
  subscription: sampleOverview.companies[0].subscription,
  plans: sampleOverview.plans,
  modules: sampleOverview.module_prices.filter((m) => m.available),
  coming_soon_modules: sampleOverview.module_prices.filter((m) => !m.available),
  invoices: sampleOverview.invoices,
};

vi.mock('../../app/static/js/api.js', () => ({
  api: {
    get: vi.fn(async (path) => {
      calls.get.push(path);
      if (path === '/api/v1/billing/admin/overview') return structuredClone(sampleOverview);
      if (path.startsWith('/api/v1/billing/my')) return structuredClone(sampleClientView);
      return {};
    }),
    post: vi.fn(async (path, body) => {
      calls.post.push({ path, body });
      if (path === '/api/v1/billing/quote') {
        return { cycle_title: '12 мес.', total_formatted: '34 900 ₽', vat_label: 'Без НДС (УСН)', discount_kop: 523500, grandfathered: true };
      }
      if (path === '/api/v1/billing/request-invoice') {
        return { ok: true, invoice: { id: 'inv-2', number: 'YM-202611-002' } };
      }
      if (path.endsWith('/print-ticket')) return { ticket: 'tk-123' };
      return { ok: true };
    }),
    put: vi.fn(async (path, body) => {
      calls.put.push({ path, body });
      return { ok: true };
    }),
    del: vi.fn(async (path) => {
      calls.del.push(path);
      return { ok: true };
    }),
  },
}));

describe('Корпоративные платные подписки и прайс-лист (billing.js v1.74.0)', () => {
  beforeEach(() => {
    calls.get.length = 0;
    calls.post.length = 0;
    calls.put.length = 0;
    calls.del.length = 0;
    document.body.innerHTML = `
      <div id="app"></div>
      <div id="modal-root" class="hidden"><div class="modal-backdrop"></div><div class="modal-slot"></div></div>
      <div id="toast-container"></div>
    `;
    window.open = vi.fn(() => ({ location: { href: '' }, close: vi.fn() }));
  });

  it('рендерит блок администратора #st-billing, переключает 4 вкладки и сохраняет цену модуля в копейках', async () => {
    const { adminBillingCardShellHTML, bindAdminBillingCard } = await import('../../app/static/js/billing.js');
    const app = document.getElementById('app');
    app.innerHTML = adminBillingCardShellHTML();

    await bindAdminBillingCard(app);
    expect(app.textContent).toContain('3 490 ₽');
    expect(app.textContent).toContain('41 880 ₽');

    // Переключаемся на вкладку «Цены модулей и тарифы»
    app.querySelector('[data-btab="catalog"]').click();
    expect(app.textContent).toContain('Чеки и авансовые отчёты');
    expect(app.textContent).toContain('Бухгалтерия ПРО');

    // Изменяем месячную цену модуля receipts на 1790 ₽ -> уходит 179000 копеек
    const mIn = app.querySelector('[data-mod-m="receipts"]');
    mIn.value = '1790';
    app.querySelector('[data-mod-save="receipts"]').click();
    await new Promise((r) => setTimeout(r, 20));

    const modPut = calls.put.find((c) => c.path === '/api/v1/billing/admin/modules/receipts');
    expect(modPut).toBeTruthy();
    expect(modPut.body.monthly_kop).toBe(179000);

    // Вкладка «Подписки компаний» показывает Grandfathering и открывает карточку подписки
    app.querySelector('[data-btab="companies"]').click();
    expect(app.textContent).toContain('ООО Ромашка');
    expect(app.textContent).toContain('Старая цена сохранена');
    app.querySelector('[data-csub-edit="comp-1"]').click();
    expect(document.querySelector('.modal-slot').textContent).toContain('Подписка компании «ООО Ромашка»');
  });

  it('рендерит блок подписки бухгалтера #my-billing-box, считает /quote и выписывает счёт', async () => {
    const { mountClientBillingBox, openPaymentRequiredModal } = await import('../../app/static/js/billing.js');
    const host = document.getElementById('app');

    await mountClientBillingBox(host, 'comp-1');
    expect(host.textContent).toContain('Бухгалтерия ПРО');
    expect(host.textContent).toContain('чеки: 42 из 1500');

    host.querySelector('#my-bil-open').click();
    await new Promise((r) => setTimeout(r, 20));
    const modal = document.querySelector('.modal-slot');
    expect(modal.textContent).toContain('34 900 ₽');

    modal.querySelector('#sf-submit').click();
    await new Promise((r) => setTimeout(r, 25));
    expect(calls.post.some((c) => c.path === '/api/v1/billing/request-invoice')).toBe(true);
    expect(window.open).toHaveBeenCalled();

    // Проверка модального окна при HTTP 402
    openPaymentRequiredModal({ message: 'Исчерпан лимит чеков', module: 'receipts' });
    expect(document.querySelector('.modal-slot').textContent).toContain('Исчерпан лимит чеков');
  });
});
