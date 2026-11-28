// ======================================================================
// DocVer (Ямастер Чек) — управление платными подписками, тарифами и счетами (v1.74.0)
// Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
// ======================================================================

import { api } from './api.js';
import { esc, fmtDate, toast, openModal } from './ui.js';

function closeModal() {
  const b = document.querySelector('#modal-root .modal-close');
  if (b) b.click();
}

const MODE_LABELS = {
  free_all: 'Пилотный (бесплатно для всех)',
  soft_notice: 'Мягкий контроль (уведомления без блокировок)',
  strict: 'Боевой платный режим (строгий контроль модулей и квот)',
};

const STATUS_BADGE = {
  active: 'verified',
  partner: 'verified',
  trial: 'new',
  free: 'new',
  grace: 'pending',
  expired: 'failed',
  suspended: 'failed',
};

function kopToRubInput(kop) {
  const n = Math.max(0, Number(kop) || 0);
  return (n % 100 === 0) ? String(n / 100) : (n / 100).toFixed(2);
}

function rubInputToKop(val) {
  const s = String(val ?? '').trim().replace(/\s+/g, '').replace(',', '.');
  if (!s) return 0;
  const num = Number(s);
  if (!Number.isFinite(num) || num < 0) return 0;
  return Math.round(num * 100);
}

function fmtKop(kop) {
  const n = Math.round(Number(kop) || 0);
  const rub = Math.floor(Math.abs(n) / 100);
  const rem = Math.abs(n) % 100;
  const sign = n < 0 ? '−' : '';
  const body = rub.toLocaleString('ru-RU');
  return rem ? `${sign}${body},${String(rem).padStart(2, '0')} ₽` : `${sign}${body} ₽`;
}

async function openInvoicePrintWindow(invoiceId) {
  const win = window.open('', '_blank');
  try {
    const { ticket } = await api.post(`/api/v1/billing/invoices/${encodeURIComponent(invoiceId)}/print-ticket`, {});
    const url = `/api/v1/billing/invoices/${encodeURIComponent(invoiceId)}/print?ticket=${encodeURIComponent(ticket)}`;
    if (win) win.location.href = url;
    else window.location.href = url;
  } catch (err) {
    if (win) win.close();
    toast(err.message, 'err');
  }
}

export function openPaymentRequiredModal(detail = {}) {
  const msg = detail.message || 'Для выполнения операции требуется активная подписка на соответствующий модуль.';
  const { slot } = openModal(`
    <div class="modal-title">💳 Требуется подключение модуля</div>
    <div class="info-callout" style="margin:10px 0">${esc(msg)}</div>
    <p class="form-hint">Все ранее загруженные чеки, УПД и отчёты вашей организации остаются в полной сохранности и доступны для просмотра и печати.</p>
    <div class="modal-actions">
      <button type="button" class="btn" data-bil-close>Закрыть</button>
      <button type="button" class="btn btn-primary" data-bil-go>Перейти к подписке организации</button>
    </div>`);
  const bClose = slot.querySelector('[data-bil-close]');
  if (bClose) bClose.onclick = () => closeModal();
  const bGo = slot.querySelector('[data-bil-go]');
  if (bGo) bGo.onclick = () => {
    closeModal();
    location.hash = detail.upgrade_hash || '#/settings/personal/st-mycompany';
  };
}

// ======================================================================
//  АДМИНСКИЙ БЛОК В «НАСТРОЙКАХ» (#st-billing)
// ======================================================================
export function adminBillingCardShellHTML() {
  return `
    <div class="glass card" id="st-billing" data-sec="personal" style="order:39" data-wide>
      <div class="card-title">💳 Платные подписки, тарифы и прайс-лист
        <span class="form-hint">единый центр коммерции (v1.74.0)</span></div>
      <div id="st-billing-body"><div class="skeleton" style="height:110px"></div></div>
    </div>`;
}

export async function bindAdminBillingCard(container, focusCompanyId = '') {
  const host = container.querySelector('#st-billing-body');
  if (!host) return;

  let activeTab = focusCompanyId ? 'companies' : 'rules';
  let compSearch = '';
  let compStatusFilter = '';
  let data = null;

  const reload = async () => {
    try {
      data = await api.get('/api/v1/billing/admin/overview');
      render();
      if (focusCompanyId) {
        const target = (data.companies || []).find((c) => c.company_id === focusCompanyId);
        focusCompanyId = '';
        if (target) openCompanySubModal(target);
      }
    } catch (err) {
      host.innerHTML = `<div class="form-error">${esc(err.message)}</div>`;
    }
  };

  const openPlanEditorModal = (plan = null) => {
    const p = plan || {
      code: '',
      title: '',
      badge: '',
      description: '',
      modules: ['receipts'],
      monthly_kop: 149000,
      quarterly_kop: 419000,
      yearly_kop: 1490000,
      limits: { users: 10, receipts_monthly: 1000, upd_monthly: 500, counterparties_monthly: 300 },
      active: true,
      sort_order: 50,
    };
    const isEdit = !!plan;
    const mods = data.module_prices || [];
    const { slot } = openModal(`
      <div class="modal-title">${isEdit ? '✏ Настройка тарифа «' + esc(p.title) + '»' : '＋ Новый тарифный план'}</div>
      <div class="form-grid" style="margin:10px 0">
        <label class="field"><span>Код тарифа (латиницей)</span>
          <input id="pl-code" value="${esc(p.code)}" placeholder="business_plus" ${isEdit ? 'readonly' : ''}></label>
        <label class="field"><span>Название тарифа</span>
          <input id="pl-title" value="${esc(p.title)}" placeholder="Бизнес Плюс"></label>
        <label class="field"><span>Метка на витрине</span>
          <input id="pl-badge" value="${esc(p.badge || '')}" placeholder=" например: Популярный"></label>
        <label class="field"><span>Порядок сортировки</span>
          <input id="pl-sort" type="number" min="0" max="999" value="${Number(p.sort_order ?? 50)}"></label>
      </div>
      <label class="field" style="margin-bottom:10px"><span>Краткое описание для клиента</span>
        <input id="pl-desc" value="${esc(p.description || '')}" placeholder="Кому подходит и что включает"></label>
      <div class="bil-box">
        <div class="card-section-title">Включённые модули</div>
        <div class="bil-checks">${mods.map((m) => `
          <label class="check-row">
            <input type="checkbox" data-pl-mod="${esc(m.code)}" ${(p.modules || []).includes(m.code) ? 'checked' : ''} style="width:auto">
            <span>${esc(m.title)}</span>
          </label>`).join('')}
        </div>
      </div>
      <div class="form-grid" style="margin:10px 0">
        <label class="field"><span>Цена за 1 мес., ₽</span>
          <input id="pl-m1" type="number" min="0" step="1" value="${esc(kopToRubInput(p.monthly_kop))}"></label>
        <label class="field"><span>Цена за 3 мес., ₽</span>
          <input id="pl-m3" type="number" min="0" step="1" value="${esc(kopToRubInput(p.quarterly_kop))}"></label>
        <label class="field"><span>Цена за 12 мес., ₽</span>
          <input id="pl-m12" type="number" min="0" step="1" value="${esc(kopToRubInput(p.yearly_kop))}"></label>
      </div>
      <div class="form-grid" style="margin:10px 0">
        <label class="field"><span>Лимит сотрудников (0 = безлимит)</span>
          <input id="pl-lu" type="number" min="0" value="${Number(p.limits?.users || 0)}"></label>
        <label class="field"><span>Чеков в месяц (0 = безлимит)</span>
          <input id="pl-lr" type="number" min="0" value="${Number(p.limits?.receipts_monthly || 0)}"></label>
        <label class="field"><span>УПД в месяц (0 = безлимит)</span>
          <input id="pl-lp" type="number" min="0" value="${Number(p.limits?.upd_monthly || 0)}"></label>
        <label class="field"><span>Контрагентов (0 = безлимит)</span>
          <input id="pl-lc" type="number" min="0" value="${Number(p.limits?.counterparties_monthly || 0)}"></label>
      </div>
      <label class="field" style="margin-bottom:8px"><span>Правило для уже действующих подписчиков этого тарифа</span>
        <select id="pl-policy">
          <option value="grandfather" ${data.config.price_change_policy === 'grandfather' ? 'selected' : ''}>Сохранить прежнюю цену за действующими клиентами (Grandfathering)</option>
          <option value="next_renewal" ${data.config.price_change_policy === 'next_renewal' ? 'selected' : ''}>Перевести на новую цену со следующего продления</option>
        </select>
      </label>
      <label class="check-row" style="margin-bottom:10px">
        <input type="checkbox" id="pl-active" ${p.active !== false ? 'checked' : ''} style="width:auto">
        <span>Тариф активен и отображается клиентам на витрине</span>
      </label>
      <div class="modal-actions">
        <button type="button" class="btn" data-pl-cancel>Отмена</button>
        <button type="button" class="btn btn-primary" id="pl-save">💾 Сохранить тариф</button>
      </div>`);
    slot.querySelector('[data-pl-cancel]').onclick = () => closeModal();
    slot.querySelector('#pl-save').onclick = async () => {
      const selMods = Array.from(slot.querySelectorAll('[data-pl-mod]:checked')).map((el) => el.dataset.plMod);
      const body = {
        code: slot.querySelector('#pl-code').value.trim().toLowerCase(),
        title: slot.querySelector('#pl-title').value.trim(),
        badge: slot.querySelector('#pl-badge').value.trim(),
        description: slot.querySelector('#pl-desc').value.trim(),
        modules: selMods,
        monthly_kop: rubInputToKop(slot.querySelector('#pl-m1').value),
        quarterly_kop: rubInputToKop(slot.querySelector('#pl-m3').value),
        yearly_kop: rubInputToKop(slot.querySelector('#pl-m12').value),
        limits: {
          users: parseInt(slot.querySelector('#pl-lu').value, 10) || 0,
          receipts_monthly: parseInt(slot.querySelector('#pl-lr').value, 10) || 0,
          upd_monthly: parseInt(slot.querySelector('#pl-lp').value, 10) || 0,
          counterparties_monthly: parseInt(slot.querySelector('#pl-lc').value, 10) || 0,
        },
        active: slot.querySelector('#pl-active').checked,
        sort_order: parseInt(slot.querySelector('#pl-sort').value, 10) || 50,
        apply_policy: slot.querySelector('#pl-policy').value,
      };
      try {
        if (isEdit) await api.put(`/api/v1/billing/admin/plans/${encodeURIComponent(p.code)}`, body);
        else await api.post('/api/v1/billing/admin/plans', body);
        toast('Тарифный план сохранён', 'ok', '🏷');
        closeModal();
        await reload();
      } catch (err) {
        toast(err.message, 'err');
      }
    };
  };

  const openCompanySubModal = (row) => {
    const sub = row.subscription || {};
    const plans = data.plans || [];
    const mods = data.module_prices || [];
    const endDateVal = sub.period_end ? String(sub.period_end).slice(0, 10) : '';
    const { slot } = openModal(`
      <div class="modal-title">🏢 Подписка компании «${esc(row.company_name)}»</div>
      <p class="form-hint" style="margin-bottom:10px">${row.inn ? 'ИНН ' + esc(row.inn) + ' · ' : ''}Текущий расход за ${esc(sub.usage?.period || '')}: чеков ${sub.usage?.receipts || 0}, УПД ${sub.usage?.upd || 0}, контрагентов ${sub.usage?.counterparties || 0}</p>
      <div class="form-grid" style="margin:8px 0">
        <label class="field"><span>Статус подписки</span>
          <select id="cs-status">
            <option value="active" ${sub.status === 'active' ? 'selected' : ''}>Активна (оплачена)</option>
            <option value="trial" ${sub.status === 'trial' ? 'selected' : ''}>Пробный период (Trial)</option>
            <option value="partner" ${sub.status === 'partner' ? 'selected' : ''}>Партнёрский / VIP бессрочно</option>
            <option value="free" ${sub.status === 'free' ? 'selected' : ''}>Бесплатный базовый</option>
            <option value="expired" ${sub.status === 'expired' ? 'selected' : ''}>Истекла (только чтение архива)</option>
            <option value="suspended" ${sub.status === 'suspended' ? 'selected' : ''}>Приостановлена</option>
          </select></label>
        <label class="field"><span>Базовый тарифный план</span>
          <select id="cs-plan">
            <option value="">Помодульный набор (без пакета)</option>
            ${plans.map((p) => `<option value="${esc(p.code)}" ${sub.plan_code === p.code ? 'selected' : ''}>${esc(p.title)} (${fmtKop(p.monthly_kop)}/мес)</option>`).join('')}
          </select></label>
        <label class="field"><span>Расчётный период</span>
          <select id="cs-cycle">
            <option value="monthly" ${sub.billing_cycle === 'monthly' ? 'selected' : ''}>1 месяц</option>
            <option value="quarterly" ${sub.billing_cycle === 'quarterly' ? 'selected' : ''}>3 месяца (квартал)</option>
            <option value="yearly" ${sub.billing_cycle === 'yearly' ? 'selected' : ''}>12 месяцев (год)</option>
          </select></label>
        <label class="field"><span>Оплачено до даты</span>
          <input id="cs-end" type="date" value="${esc(endDateVal)}"></label>
      </div>
      <div class="bil-box">
        <div class="card-section-title">Дополнительные модули сверх тарифа (Add-ons)</div>
        <div class="bil-checks">${mods.map((m) => `
          <label class="check-row">
            <input type="checkbox" data-cs-addon="${esc(m.code)}" ${(sub.addon_modules || []).includes(m.code) ? 'checked' : ''} style="width:auto">
            <span>${esc(m.title)} (+${fmtKop(m.monthly_kop)}/мес)</span>
          </label>`).join('')}
        </div>
      </div>
      <div class="form-grid" style="margin:10px 0">
        <label class="field"><span>Индивидуальная скидка, %</span>
          <input id="cs-disc" type="number" min="0" max="100" value="${Number(sub.custom_discount_pct || 0)}"></label>
        <label class="field"><span>Фиксированная цена по договору, ₽ (пусто = авто)</span>
          <input id="cs-price" type="number" min="0" step="1" value="${sub.locked_price_kop ? esc(kopToRubInput(sub.locked_price_kop)) : ''}" placeholder="авторасчёт по прайсу"></label>
        <label class="field"><span>Номер договора</span>
          <input id="cs-cnum" value="${esc(sub.contract_number || '')}" placeholder="Договор №"></label>
        <label class="field"><span>Дата договора</span>
          <input id="cs-cdate" type="date" value="${esc(sub.contract_date || '')}"></label>
      </div>
      <label class="check-row" style="margin-bottom:8px">
        <input type="checkbox" id="cs-lock" ${sub.price_locked !== false ? 'checked' : ''} style="width:auto">
        <span>Зафиксировать цену за клиентом (Grandfathering — не менять при росте прайса)</span>
      </label>
      <label class="field" style="margin-bottom:10px"><span>Служебная заметка администратора</span>
        <input id="cs-note" value="${esc(sub.admin_note || '')}" placeholder="Спецусловия, контакты ЛПР, договорённости"></label>
      <div class="modal-actions">
        <button type="button" class="btn" data-cs-cancel>Отмена</button>
        <button type="button" class="btn btn-primary" id="cs-save">💾 Сохранить подписку</button>
      </div>`);
    slot.querySelector('[data-cs-cancel]').onclick = () => closeModal();
    slot.querySelector('#cs-save').onclick = async () => {
      const priceRaw = slot.querySelector('#cs-price').value.trim();
      const addons = Array.from(slot.querySelectorAll('[data-cs-addon]:checked')).map((el) => el.dataset.csAddon);
      const body = {
        status: slot.querySelector('#cs-status').value,
        plan_code: slot.querySelector('#cs-plan').value,
        billing_cycle: slot.querySelector('#cs-cycle').value,
        addon_modules: addons,
        custom_discount_pct: parseInt(slot.querySelector('#cs-disc').value, 10) || 0,
        locked_price_kop: priceRaw !== '' ? rubInputToKop(priceRaw) : null,
        price_locked: slot.querySelector('#cs-lock').checked,
        period_end: slot.querySelector('#cs-end').value || null,
        contract_number: slot.querySelector('#cs-cnum').value.trim(),
        contract_date: slot.querySelector('#cs-cdate').value.trim(),
        admin_note: slot.querySelector('#cs-note').value.trim(),
      };
      try {
        await api.put(`/api/v1/billing/admin/companies/${encodeURIComponent(row.company_id)}/subscription`, body);
        toast('Подписка компании обновлена', 'ok', '💳');
        closeModal();
        await reload();
      } catch (err) {
        toast(err.message, 'err');
      }
    };
  };

  const openExtendModal = (row) => {
    const { slot } = openModal(`
      <div class="modal-title">⏳ Продлить доступ «${esc(row.company_name)}»</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin:10px 0">
        <button type="button" class="btn btn-sm" data-ext-preset="7">+7 дней</button>
        <button type="button" class="btn btn-sm" data-ext-preset="14">+14 дней</button>
        <button type="button" class="btn btn-sm" data-ext-preset="30">+30 дней (месяц)</button>
        <button type="button" class="btn btn-sm" data-ext-preset="90">+90 дней (квартал)</button>
        <button type="button" class="btn btn-sm" data-ext-preset="365">+365 дней (год)</button>
      </div>
      <div class="form-grid" style="margin:10px 0">
        <label class="field"><span>Добавить дней</span>
          <input id="ext-days" type="number" min="1" max="3650" value="30"></label>
        <label class="field"><span>Или точная дата окончания</span>
          <input id="ext-date" type="date" value=""></label>
      </div>
      <label class="field" style="margin-bottom:10px"><span>Основание продления (сохранится в журнале аудита)</span>
        <input id="ext-reason" placeholder="Оплата по счёту, бонус лояльности, пилот"></label>
      <div class="modal-actions">
        <button type="button" class="btn" data-ext-cancel>Отмена</button>
        <button type="button" class="btn btn-primary" id="ext-save">Продлить подписку</button>
      </div>`);
    slot.querySelectorAll('[data-ext-preset]').forEach((b) => {
      b.onclick = () => {
        slot.querySelector('#ext-days').value = b.dataset.extPreset;
        slot.querySelector('#ext-date').value = '';
      };
    });
    slot.querySelector('[data-ext-cancel]').onclick = () => closeModal();
    slot.querySelector('#ext-save').onclick = async () => {
      try {
        const dtVal = slot.querySelector('#ext-date').value.trim();
        await api.post(`/api/v1/billing/admin/companies/${encodeURIComponent(row.company_id)}/extend`, {
          add_days: parseInt(slot.querySelector('#ext-days').value, 10) || 30,
          target_date: dtVal || null,
          reason: slot.querySelector('#ext-reason').value.trim(),
        });
        toast('Срок подписки продлён', 'ok', '⏳');
        closeModal();
        await reload();
      } catch (err) {
        toast(err.message, 'err');
      }
    };
  };

  const openIssueInvoiceModal = (row) => {
    const sub = row.subscription || {};
    const plans = (data.plans || []).filter((p) => p.active !== false);
    const mods = (data.module_prices || []).filter((m) => m.available !== false);
    const { slot } = openModal(`
      <div class="modal-title">🧾 Выставить счёт для «${esc(row.company_name)}»</div>
      <div class="form-grid" style="margin:10px 0">
        <label class="field"><span>Тарифный план</span>
          <select id="adm-inv-plan">
            <option value="">Помодульный набор</option>
            ${plans.map((p) => `<option value="${esc(p.code)}" ${(sub.plan_code || 'accounting_pro') === p.code ? 'selected' : ''}>${esc(p.title)} (${fmtKop(p.monthly_kop)}/мес)</option>`).join('')}
          </select></label>
        <label class="field"><span>Период оплаты</span>
          <select id="adm-inv-cycle">
            <option value="monthly">1 месяц</option>
            <option value="quarterly">3 месяца</option>
            <option value="yearly" selected>12 месяцев (выгодно)</option>
          </select></label>
      </div>
      <div class="bil-box">
        <div class="card-section-title">Дополнительные модули</div>
        <div class="bil-checks">${mods.map((m) => `
          <label class="check-row">
            <input type="checkbox" data-adm-inv-mod="${esc(m.code)}" ${(sub.addon_modules || []).includes(m.code) ? 'checked' : ''} style="width:auto">
            <span>${esc(m.title)}</span>
          </label>`).join('')}
        </div>
      </div>
      <div class="form-grid" style="margin:10px 0">
        <label class="field"><span>Скидка по счёту, %</span>
          <input id="adm-inv-disc" type="number" min="0" max="100" value="${Number(sub.custom_discount_pct || 0)}"></label>
        <label class="field"><span>Комментарий / договор</span>
          <input id="adm-inv-comm" placeholder="Основание или комментарий"></label>
      </div>
      <div class="modal-actions">
        <button type="button" class="btn" data-adm-inv-cancel>Отмена</button>
        <button type="button" class="btn btn-primary" id="adm-inv-create">🧾 Сформировать и открыть счёт</button>
      </div>`);
    slot.querySelector('[data-adm-inv-cancel]').onclick = () => closeModal();
    slot.querySelector('#adm-inv-create').onclick = async () => {
      try {
        const addons = Array.from(slot.querySelectorAll('[data-adm-inv-mod]:checked')).map((el) => el.dataset.admInvMod);
        const res = await api.post('/api/v1/billing/admin/invoices', {
          company_id: row.company_id,
          plan_code: slot.querySelector('#adm-inv-plan').value,
          addon_modules: addons,
          billing_cycle: slot.querySelector('#adm-inv-cycle').value,
          custom_discount_pct: parseInt(slot.querySelector('#adm-inv-disc').value, 10) || 0,
          comment: slot.querySelector('#adm-inv-comm').value.trim(),
        });
        toast(`Счёт № ${res.invoice.number} выставлен`, 'ok', '🧾');
        closeModal();
        await reload();
        await openInvoicePrintWindow(res.invoice.id);
      } catch (err) {
        toast(err.message, 'err');
      }
    };
  };

  const render = () => {
    const cfg = data.config || {};
    const m = data.metrics || {};
    const mods = data.module_prices || [];
    const plans = data.plans || [];
    const promos = data.promos || [];
    const invoices = data.invoices || [];
    const events = data.events || [];

    const filteredCompanies = (data.companies || []).filter((c) => {
      if (compStatusFilter && c.subscription?.status !== compStatusFilter) return false;
      if (compSearch) {
        const q = compSearch.toLowerCase();
        const hay = `${c.company_name} ${c.full_name} ${c.inn}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    host.innerHTML = `
      <div class="bil-kpi-grid">
        <div class="bil-kpi">
          <div class="bil-kpi-label">MRR (ежемесячная выручка)</div>
          <div class="bil-kpi-val">${esc(m.mrr_formatted || '0 ₽')}</div>
          <div class="form-hint">ARR (годовая): <b>${esc(m.arr_formatted || '0 ₽')}</b></div>
        </div>
        <div class="bil-kpi">
          <div class="bil-kpi-label">Платные подписки</div>
          <div class="bil-kpi-val">${Number(m.active || 0)} <span class="form-hint">из ${Number(m.total_companies || 0)} комп.</span></div>
          <div class="form-hint">Партнёрских: ${Number(m.partner || 0)} · Старая цена: ${Number(m.grandfathered || 0)}</div>
        </div>
        <div class="bil-kpi">
          <div class="bil-kpi-label">Триал и льготный период</div>
          <div class="bil-kpi-val">${Number(m.trial || 0)} / ${Number(m.grace || 0)}</div>
          <div class="form-hint">Истекают ≤ 7 дн.: <b>${Number(m.expiring_7d || 0)}</b> (≤ 30 дн.: ${Number(m.expiring_30d || 0)})</div>
        </div>
        <div class="bil-kpi">
          <div class="bil-kpi-label">Счета на оплату</div>
          <div class="bil-kpi-val">${Number(m.open_invoices || 0)} <span class="form-hint">ждут оплаты</span></div>
          <div class="form-hint">Оплачено счетов: <b>${Number(m.paid_invoices || 0)}</b></div>
        </div>
      </div>

      <div class="bil-subtabs" role="tablist" aria-label="Управление подписками">
        <button type="button" class="bil-subtab ${activeTab === 'rules' ? 'active' : ''}" data-btab="rules">⚙ Режим и правила</button>
        <button type="button" class="bil-subtab ${activeTab === 'catalog' ? 'active' : ''}" data-btab="catalog">🏷 Цены модулей и тарифы</button>
        <button type="button" class="bil-subtab ${activeTab === 'companies' ? 'active' : ''}" data-btab="companies">🏢 Подписки компаний (${(data.companies || []).length})</button>
        <button type="button" class="bil-subtab ${activeTab === 'invoices' ? 'active' : ''}" data-btab="invoices">🧾 Счета, промокоды и аудит${m.open_invoices ? ' (' + m.open_invoices + ')' : ''}</button>
      </div>

      ${activeTab === 'rules' ? `
      <div class="bil-pane">
        <div class="info-callout" style="margin-bottom:12px">
          Текущий режим платформы: <b>${esc(MODE_LABELS[cfg.billing_mode] || cfg.billing_mode)}</b>.
          В пилотном режиме ни один клиент не блокируется, пока вы настраиваете цены и назначаете тарифы.
        </div>
        <div class="form-grid" style="margin-bottom:10px">
          <label class="field"><span>Режим тарификации платформы</span>
            <select id="bc-mode">
              <option value="free_all" ${cfg.billing_mode === 'free_all' ? 'selected' : ''}>Пилотный — бесплатно для всех организаций</option>
              <option value="soft_notice" ${cfg.billing_mode === 'soft_notice' ? 'selected' : ''}>Мягкий контроль — учёт квот и напоминания без блокировок</option>
              <option value="strict" ${cfg.billing_mode === 'strict' ? 'selected' : ''}>Боевой платный режим — контроль модулей и квот (архив доступен всегда)</option>
            </select></label>
          <label class="field"><span>Политика изменения цен по умолчанию</span>
            <select id="bc-policy">
              <option value="grandfather" ${cfg.price_change_policy === 'grandfather' ? 'selected' : ''}>Grandfathering — сохранять прежнюю цену действующим клиентам</option>
              <option value="next_renewal" ${cfg.price_change_policy === 'next_renewal' ? 'selected' : ''}>Применять новую цену со следующего продления</option>
            </select></label>
          <label class="field"><span>Пробный период (Trial), дней</span>
            <input id="bc-trial" type="number" min="0" max="365" value="${Number(cfg.trial_days ?? 14)}"></label>
          <label class="field"><span>Льготный период после окончания (Grace), дней</span>
            <input id="bc-grace" type="number" min="0" max="90" value="${Number(cfg.grace_days ?? 7)}"></label>
          <label class="field"><span>Ориентир скидки за годовую оплату, %</span>
            <input id="bc-adisc" type="number" min="0" max="60" value="${Number(cfg.annual_discount_pct ?? 17)}"></label>
          <label class="field"><span>Ставка НДС в счетах</span>
            <select id="bc-vat">${(data.vat_modes || []).map((v) => `
              <option value="${esc(v.code)}" ${cfg.vat_mode === v.code ? 'selected' : ''}>${esc(v.title)}</option>`).join('')}
            </select></label>
        </div>

        <details class="card-note" style="margin-bottom:12px">
          <summary>Реквизиты поставщика для автоматического выставления счетов</summary>
          <div class="form-grid" style="margin-top:10px">
            <label class="field"><span>Поставщик (Юрлицо / ИП)</span>
              <input id="bc-sname" value="${esc(cfg.seller_name || '')}"></label>
            <label class="field"><span>ИНН поставщика</span>
              <input id="bc-sinn" value="${esc(cfg.seller_inn || '')}"></label>
            <label class="field"><span>КПП</span>
              <input id="bc-skpp" value="${esc(cfg.seller_kpp || '')}"></label>
            <label class="field"><span>Префикс номера счёта</span>
              <input id="bc-sprefix" value="${esc(cfg.invoice_prefix || 'ДВ')}"></label>
            <label class="field"><span>Юридический адрес</span>
              <input id="bc-saddr" value="${esc(cfg.seller_address || '')}"></label>
            <label class="field"><span>Банк получателя</span>
              <input id="bc-sbank" value="${esc(cfg.seller_bank || '')}"></label>
            <label class="field"><span>БИК банка</span>
              <input id="bc-sbik" value="${esc(cfg.seller_bik || '')}"></label>
            <label class="field"><span>Расчётный счёт</span>
              <input id="bc-sacc" value="${esc(cfg.seller_account || '')}"></label>
            <label class="field"><span>Корреспондентский счёт</span>
              <input id="bc-scorr" value="${esc(cfg.seller_corr_account || '')}"></label>
            <label class="field"><span>Почта отдела подписок</span>
              <input id="bc-semail" value="${esc(cfg.support_email || '')}"></label>
          </div>
        </details>

        <button type="button" class="btn btn-primary btn-sm" id="bc-save">💾 Сохранить правила и реквизиты</button>
      </div>` : ''}

      ${activeTab === 'catalog' ? `
      <div class="bil-pane">
        <div class="card-section-title">Поштучные цены модулей (Add-ons)</div>
        <p class="form-hint" style="margin-bottom:8px">Клиент может докупить любой модуль отдельно к базовому тарифу или собрать индивидуальный набор. Модуль «Подготовка договоров» можно включить тумблером, когда он будет готов к продажам.</p>
        <div class="table-scroll" style="margin-bottom:16px">
          <table class="data">
            <thead>
              <tr>
                <th>Модуль</th>
                <th>Цена / мес., ₽</th>
                <th>Цена / год, ₽</th>
                <th>Базовый лимит</th>
                <th>Продажа</th>
                <th>Версия</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              ${mods.map((mod) => `
              <tr data-mod-row="${esc(mod.code)}">
                <td data-label="Модуль"><b>${esc(mod.title)}</b><div class="form-hint">${esc(mod.description)}</div></td>
                <td data-label="Цена / мес., ₽"><input type="number" min="0" step="1" class="bil-num-in" data-mod-m="${esc(mod.code)}" value="${esc(kopToRubInput(mod.monthly_kop))}"></td>
                <td data-label="Цена / год, ₽"><input type="number" min="0" step="1" class="bil-num-in" data-mod-y="${esc(mod.code)}" value="${esc(kopToRubInput(mod.yearly_kop))}"></td>
                <td data-label="Базовый лимит"><input type="number" min="0" step="10" class="bil-num-in" data-mod-q="${esc(mod.code)}" value="${Number(mod.monthly_quota || 0)}"> <span class="form-hint">${esc(mod.quota_unit || '')}</span></td>
                <td data-label="Продажа">
                  <label class="check-row"><input type="checkbox" data-mod-av="${esc(mod.code)}" ${mod.available ? 'checked' : ''} style="width:auto"><span>${mod.available ? 'Открыт' : 'Скрыт'}</span></label>
                </td>
                <td data-label="Версия"><span class="chip new">v${Number(mod.price_version || 1)}</span></td>
                <td data-label="Действие"><button type="button" class="btn btn-sm btn-primary" data-mod-save="${esc(mod.code)}">Сохранить</button></td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>

        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:10px">
          <div class="card-section-title" style="margin:0">Пакетные тарифные планы (Bundles)</div>
          <button type="button" class="btn btn-primary btn-sm" id="bil-plan-add">＋ Создать тарифный план</button>
        </div>
        <div class="bil-plans-grid">
          ${plans.map((p) => `
          <div class="bil-plan-card ${p.active === false ? 'archived-row' : ''}">
            <div class="bil-plan-head">
              <b>${esc(p.title)}</b>
              ${p.badge ? `<span class="chip verified">${esc(p.badge)}</span>` : ''}
              ${p.active === false ? '<span class="chip unknown">архив</span>' : ''}
            </div>
            <div class="bil-plan-price">${fmtKop(p.monthly_kop)} <span class="form-hint">/ мес.</span></div>
            <div class="form-hint">3 мес.: <b>${fmtKop(p.quarterly_kop)}</b> · 12 мес.: <b>${fmtKop(p.yearly_kop)}</b> · прайс v${Number(p.price_version || 1)}</div>
            <p class="form-hint" style="margin:6px 0">${esc(p.description || '')}</p>
            <div class="form-hint">Модулей: <b>${(p.modules || []).length}</b> · Сотр.: ${p.limits?.users || '∞'} · Чеки: ${p.limits?.receipts_monthly || '—'}/мес · УПД: ${p.limits?.upd_monthly || '—'}/мес</div>
            <div style="margin-top:10px;display:flex;gap:6px;flex-wrap:wrap">
              <button type="button" class="btn btn-sm" data-plan-edit="${esc(p.code)}">✏ Изменить цену и состав</button>
            </div>
          </div>`).join('')}
        </div>
      </div>` : ''}

      ${activeTab === 'companies' ? `
      <div class="bil-pane">
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
          <input id="bil-comp-q" placeholder="Поиск компании по названию или ИНН" value="${esc(compSearch)}" style="flex:1;min-width:210px">
          <select id="bil-comp-st" style="max-width:220px">
            <option value="">Все статусы</option>
            <option value="active" ${compStatusFilter === 'active' ? 'selected' : ''}>Активна</option>
            <option value="trial" ${compStatusFilter === 'trial' ? 'selected' : ''}>Пробный период</option>
            <option value="grace" ${compStatusFilter === 'grace' ? 'selected' : ''}>Льготный период</option>
            <option value="expired" ${compStatusFilter === 'expired' ? 'selected' : ''}>Истекла</option>
            <option value="partner" ${compStatusFilter === 'partner' ? 'selected' : ''}>Партнёрский доступ</option>
            <option value="free" ${compStatusFilter === 'free' ? 'selected' : ''}>Бесплатный доступ</option>
          </select>
        </div>
        <div class="table-scroll">
          <table class="data">
            <thead>
              <tr>
                <th>Организация</th>
                <th>Статус</th>
                <th>Тариф и модули</th>
                <th>Стоимость</th>
                <th>Расход за мес.</th>
                <th>Действует до</th>
                <th>Управление</th>
              </tr>
            </thead>
            <tbody>
              ${filteredCompanies.length ? filteredCompanies.map((c) => {
                const s = c.subscription || {};
                const badgeCls = STATUS_BADGE[s.status] || 'unknown';
                return `
                <tr>
                  <td data-label="Организация">
                    <b>${esc(c.company_name)}</b>
                    ${c.inn ? `<div class="form-hint">ИНН ${esc(c.inn)}</div>` : ''}
                  </td>
                  <td data-label="Статус"><span class="chip ${badgeCls}">${esc(s.status_title || s.status)}</span></td>
                  <td data-label="Тариф и модули">
                    <div><b>${esc(s.plan_title || '—')}</b></div>
                    <div class="form-hint">Модулей: ${(s.enabled_modules || []).length}${s.custom_discount_pct ? ' · скидка ' + s.custom_discount_pct + '%' : ''}</div>
                  </td>
                  <td data-label="Стоимость">
                    <div><b>${s.locked_price_kop ? fmtKop(s.locked_price_kop) : '0 ₽'}</b> <span class="form-hint">${esc(s.cycle_title || '')}</span></div>
                    ${s.grandfathered ? '<div class="form-hint">🔒 Старая цена сохранена</div>' : ''}
                  </td>
                  <td data-label="Расход за мес.">
                    <span class="form-hint">Чеки: <b>${s.usage?.receipts || 0}</b>${s.limits?.receipts_monthly ? '/' + s.limits.receipts_monthly : ''} · УПД: <b>${s.usage?.upd || 0}</b>${s.limits?.upd_monthly ? '/' + s.limits.upd_monthly : ''}</span>
                  </td>
                  <td data-label="Действует до">${s.period_end ? fmtDate(s.period_end) : 'бессрочно'}</td>
                  <td data-label="Управление">
                    <div style="display:flex;gap:6px;flex-wrap:wrap">
                      <button type="button" class="btn btn-sm" data-csub-edit="${esc(c.company_id)}">⚙ Тариф</button>
                      <button type="button" class="btn btn-sm" data-csub-ext="${esc(c.company_id)}">⏳ +Дни</button>
                      <button type="button" class="btn btn-sm" data-csub-inv="${esc(c.company_id)}">🧾 Счёт</button>
                    </div>
                  </td>
                </tr>`;
              }).join('') : '<tr><td colspan="7" class="form-hint">Компании по выбранному фильтру не найдены.</td></tr>'}
            </tbody>
          </table>
        </div>
      </div>` : ''}

      ${activeTab === 'invoices' ? `
      <div class="bil-pane">
        <div class="card-section-title">Выставленные счета и заявки клиентов</div>
        <div class="table-scroll" style="margin-bottom:16px">
          <table class="data">
            <thead>
              <tr>
                <th>Счёт №</th>
                <th>Дата</th>
                <th>Покупатель</th>
                <th>Сумма</th>
                <th>Статус</th>
                <th>Действия</th>
              </tr>
            </thead>
            <tbody>
              ${invoices.length ? invoices.map((inv) => `
              <tr>
                <td data-label="Счёт №"><b>${esc(inv.number)}</b><div class="form-hint">${inv.origin === 'client' ? 'заявка клиента' : 'выставлен админом'}</div></td>
                <td data-label="Дата">${fmtDate(inv.created_at)}</td>
                <td data-label="Покупатель"><b>${esc(inv.company_name)}</b>${inv.company_inn ? `<div class="form-hint">ИНН ${esc(inv.company_inn)}</div>` : ''}</td>
                <td data-label="Сумма"><b>${esc(inv.total_formatted)}</b><div class="form-hint">${esc(inv.cycle_title || '')}</div></td>
                <td data-label="Статус">
                  <span class="chip ${inv.status === 'paid' ? 'verified' : (inv.status === 'cancelled' ? 'failed' : 'pending')}">
                    ${inv.status === 'paid' ? 'Оплачен' : (inv.status === 'cancelled' ? 'Отменён' : 'Ждёт оплаты')}
                  </span>
                  ${inv.payment_ref ? `<div class="form-hint">п/п ${esc(inv.payment_ref)}</div>` : ''}
                </td>
                <td data-label="Действия">
                  <div style="display:flex;gap:6px;flex-wrap:wrap">
                    <button type="button" class="btn btn-sm" data-inv-print="${esc(inv.id)}">🖨 Счёт</button>
                    ${inv.status === 'issued' ? `
                      <button type="button" class="btn btn-sm btn-primary" data-inv-pay="${esc(inv.id)}" data-inv-num="${esc(inv.number)}">✓ Оплачен</button>
                      <button type="button" class="btn btn-sm btn-bad" data-inv-cancel="${esc(inv.id)}">✕</button>
                    ` : ''}
                  </div>
                </td>
              </tr>`).join('') : '<tr><td colspan="6" class="form-hint">Выставленных счетов и заявок пока нет.</td></tr>'}
            </tbody>
          </table>
        </div>

        <div class="bil-box" style="margin-bottom:16px">
          <div class="card-section-title">Промокоды и партнёрские купоны</div>
          <div class="form-grid" style="margin-bottom:8px">
            <label class="field"><span>Код (A-Z, 0-9)</span>
              <input id="pr-code" placeholder="WELCOME20"></label>
            <label class="field"><span>Описание акции</span>
              <input id="pr-title" placeholder="Скидка новым клиентам"></label>
            <label class="field"><span>Тип скидки</span>
              <select id="pr-kind">
                <option value="percent">Процент (%)</option>
                <option value="fixed_kop">Фиксированная сумма (₽)</option>
              </select></label>
            <label class="field"><span>Размер скидки (% или ₽)</span>
              <input id="pr-val" type="number" min="0" value="20"></label>
            <label class="field"><span>Бонусные дни к подписке</span>
              <input id="pr-bonus" type="number" min="0" max="365" value="0"></label>
            <label class="field"><span>Лимит активаций (0 = безлимит)</span>
              <input id="pr-max" type="number" min="0" value="0"></label>
          </div>
          <button type="button" class="btn btn-sm btn-primary" id="pr-save">＋ Сохранить промокод</button>
          ${promos.length ? `
          <div class="table-scroll" style="margin-top:10px">
            <table class="data">
              <thead><tr><th>Код</th><th>Условия</th><th>Использований</th><th>Статус</th><th></th></tr></thead>
              <tbody>${promos.map((pr) => `
                <tr>
                  <td data-label="Код"><b>${esc(pr.code)}</b><div class="form-hint">${esc(pr.title)}</div></td>
                  <td data-label="Условия">${pr.discount_kind === 'percent' ? pr.discount_value + '%' : fmtKop(pr.discount_value)}${pr.bonus_trial_days ? ' · +' + pr.bonus_trial_days + ' дн.' : ''}</td>
                  <td data-label="Использований">${pr.used_count}${pr.max_uses ? ' / ' + pr.max_uses : ' (без лимита)'}</td>
                  <td data-label="Статус"><span class="chip ${pr.active ? 'verified' : 'unknown'}">${pr.active ? 'Активен' : 'Отключён'}</span></td>
                  <td data-label="Действие">${pr.active ? `<button type="button" class="btn btn-sm btn-bad" data-pr-off="${esc(pr.code)}">Отключить</button>` : ''}</td>
                </tr>`).join('')}
              </tbody>
            </table>
          </div>` : ''}
        </div>

        <details class="card-note">
          <summary>Журнал аудита изменений цен, тарифов и подписок (${events.length})</summary>
          <div class="table-scroll" style="margin-top:8px">
            <table class="data">
              <thead><tr><th>Время</th><th>Администратор / Пользователь</th><th>Действие</th><th>Объект</th></tr></thead>
              <tbody>${events.length ? events.map((ev) => `
                <tr>
                  <td data-label="Время">${fmtDate(ev.created_at)}</td>
                  <td data-label="Пользователь">${esc(ev.username || 'system')}</td>
                  <td data-label="Действие">${esc(ev.action)}</td>
                  <td data-label="Объект">${esc(ev.entity_id || '—')}</td>
                </tr>`).join('') : '<tr><td colspan="4" class="form-hint">Записей пока нет.</td></tr>'}
              </tbody>
            </table>
          </div>
        </details>
      </div>` : ''}
    `;

    host.querySelectorAll('[data-btab]').forEach((b) => {
      b.onclick = () => {
        activeTab = b.dataset.btab;
        render();
      };
    });

    const btnCfgSave = host.querySelector('#bc-save');
    if (btnCfgSave) btnCfgSave.onclick = async () => {
      btnCfgSave.disabled = true;
      try {
        await api.put('/api/v1/billing/admin/config', {
          billing_mode: host.querySelector('#bc-mode').value,
          price_change_policy: host.querySelector('#bc-policy').value,
          trial_days: parseInt(host.querySelector('#bc-trial').value, 10) || 0,
          grace_days: parseInt(host.querySelector('#bc-grace').value, 10) || 0,
          annual_discount_pct: parseInt(host.querySelector('#bc-adisc').value, 10) || 0,
          vat_mode: host.querySelector('#bc-vat').value,
          seller_name: host.querySelector('#bc-sname').value.trim(),
          seller_inn: host.querySelector('#bc-sinn').value.trim(),
          seller_kpp: host.querySelector('#bc-skpp').value.trim(),
          invoice_prefix: host.querySelector('#bc-sprefix').value.trim(),
          seller_address: host.querySelector('#bc-saddr').value.trim(),
          seller_bank: host.querySelector('#bc-sbank').value.trim(),
          seller_bik: host.querySelector('#bc-sbik').value.trim(),
          seller_account: host.querySelector('#bc-sacc').value.trim(),
          seller_corr_account: host.querySelector('#bc-scorr').value.trim(),
          support_email: host.querySelector('#bc-semail').value.trim(),
        });
        toast('Правила коммерции и реквизиты сохранены', 'ok', '💳');
        await reload();
      } catch (err) {
        toast(err.message, 'err');
      } finally {
        btnCfgSave.disabled = false;
      }
    };

    host.querySelectorAll('[data-mod-save]').forEach((b) => {
      b.onclick = async () => {
        const code = b.dataset.modSave;
        b.disabled = true;
        try {
          await api.put(`/api/v1/billing/admin/modules/${encodeURIComponent(code)}`, {
            monthly_kop: rubInputToKop(host.querySelector(`[data-mod-m="${code}"]`).value),
            yearly_kop: rubInputToKop(host.querySelector(`[data-mod-y="${code}"]`).value),
            monthly_quota: parseInt(host.querySelector(`[data-mod-q="${code}"]`).value, 10) || 0,
            available: host.querySelector(`[data-mod-av="${code}"]`).checked,
            apply_policy: data.config.price_change_policy,
          });
          toast('Цена и настройки модуля обновлены', 'ok', '🏷');
          await reload();
        } catch (err) {
          toast(err.message, 'err');
        } finally {
          b.disabled = false;
        }
      };
    });

    const btnPlanAdd = host.querySelector('#bil-plan-add');
    if (btnPlanAdd) btnPlanAdd.onclick = () => openPlanEditorModal(null);

    host.querySelectorAll('[data-plan-edit]').forEach((b) => {
      b.onclick = () => {
        const plan = plans.find((x) => x.code === b.dataset.planEdit);
        if (plan) openPlanEditorModal(plan);
      };
    });

    const inpCompQ = host.querySelector('#bil-comp-q');
    if (inpCompQ) inpCompQ.oninput = () => {
      compSearch = inpCompQ.value;
      const pos = inpCompQ.selectionStart;
      render();
      const again = host.querySelector('#bil-comp-q');
      if (again) { again.focus(); try { again.setSelectionRange(pos, pos); } catch {} }
    };

    const selCompSt = host.querySelector('#bil-comp-st');
    if (selCompSt) selCompSt.onchange = () => {
      compStatusFilter = selCompSt.value;
      render();
    };

    host.querySelectorAll('[data-csub-edit]').forEach((b) => {
      b.onclick = () => {
        const row = (data.companies || []).find((x) => x.company_id === b.dataset.csubEdit);
        if (row) openCompanySubModal(row);
      };
    });
    host.querySelectorAll('[data-csub-ext]').forEach((b) => {
      b.onclick = () => {
        const row = (data.companies || []).find((x) => x.company_id === b.dataset.csubExt);
        if (row) openExtendModal(row);
      };
    });
    host.querySelectorAll('[data-csub-inv]').forEach((b) => {
      b.onclick = () => {
        const row = (data.companies || []).find((x) => x.company_id === b.dataset.csubInv);
        if (row) openIssueInvoiceModal(row);
      };
    });

    host.querySelectorAll('[data-inv-print]').forEach((b) => {
      b.onclick = () => openInvoicePrintWindow(b.dataset.invPrint);
    });
    host.querySelectorAll('[data-inv-pay]').forEach((b) => {
      b.onclick = () => {
        const invId = b.dataset.invPay;
        const invNum = b.dataset.invNum;
        const { slot } = openModal(`
          <div class="modal-title">✓ Подтвердить оплату счёта № ${esc(invNum)}</div>
          <p class="form-hint" style="margin-bottom:8px">После подтверждения подписка организации автоматически активируется или продлится на оплаченный период.</p>
          <label class="field" style="margin-bottom:10px"><span>Номер платёжного поручения / квитанции</span>
            <input id="pay-ref" placeholder="Платёжное поручение № 145"></label>
          <div class="modal-actions">
            <button type="button" class="btn" data-pay-cancel>Отмена</button>
            <button type="button" class="btn btn-primary" id="pay-ok">Подтвердить и активировать</button>
          </div>`);
        slot.querySelector('[data-pay-cancel]').onclick = () => closeModal();
        slot.querySelector('#pay-ok').onclick = async () => {
          try {
            await api.post(`/api/v1/billing/admin/invoices/${encodeURIComponent(invId)}/mark-paid`, {
              payment_ref: slot.querySelector('#pay-ref').value.trim(),
            });
            toast('Оплата подтверждена, подписка активирована', 'ok', '✓');
            closeModal();
            await reload();
          } catch (err) {
            toast(err.message, 'err');
          }
        };
      };
    });
    host.querySelectorAll('[data-inv-cancel]').forEach((b) => {
      b.onclick = async () => {
        if (!confirm('Отменить выбранный счёт?')) return;
        try {
          await api.post(`/api/v1/billing/admin/invoices/${encodeURIComponent(b.dataset.invCancel)}/cancel`, {});
          toast('Счёт отменён', 'ok');
          await reload();
        } catch (err) {
          toast(err.message, 'err');
        }
      };
    });

    const btnPrSave = host.querySelector('#pr-save');
    if (btnPrSave) btnPrSave.onclick = async () => {
      const kind = host.querySelector('#pr-kind').value;
      const rawVal = host.querySelector('#pr-val').value;
      const discountValue = kind === 'fixed_kop' ? rubInputToKop(rawVal) : (parseInt(rawVal, 10) || 0);
      try {
        await api.post('/api/v1/billing/admin/promos', {
          code: host.querySelector('#pr-code').value.trim().toUpperCase(),
          title: host.querySelector('#pr-title').value.trim(),
          discount_kind: kind,
          discount_value: discountValue,
          bonus_trial_days: parseInt(host.querySelector('#pr-bonus').value, 10) || 0,
          max_uses: parseInt(host.querySelector('#pr-max').value, 10) || 0,
          active: true,
        });
        toast('Промокод сохранён', 'ok', '🎁');
        await reload();
      } catch (err) {
        toast(err.message, 'err');
      }
    };

    host.querySelectorAll('[data-pr-off]').forEach((b) => {
      b.onclick = async () => {
        try {
          await api.del(`/api/v1/billing/admin/promos/${encodeURIComponent(b.dataset.prOff)}`);
          toast('Промокод отключён', 'ok');
          await reload();
        } catch (err) {
          toast(err.message, 'err');
        }
      };
    });
  };

  await reload();
}

// ======================================================================
//  ВИТРИНА И СТАТУС ПОДПИСКИ У БУХГАЛТЕРА И РУКОВОДИТЕЛЯ (#st-mycompany)
// ======================================================================
export async function mountClientBillingBox(hostEl, companyId) {
  if (!hostEl || !companyId) return;
  let box = hostEl.querySelector('#my-billing-box');
  if (!box) {
    box = document.createElement('div');
    box.id = 'my-billing-box';
    box.className = 'bil-client-box';
    hostEl.appendChild(box);
  }
  box.innerHTML = '<div class="form-hint">Загружаем параметры подписки организации…</div>';

  let info = null;
  try {
    info = await api.get(`/api/v1/billing/my?company_id=${encodeURIComponent(companyId)}`);
  } catch {
    box.remove();
    return;
  }

  const sub = info.subscription || {};
  const badgeCls = STATUS_BADGE[sub.status] || 'new';
  const invoices = info.invoices || [];

  const openStorefrontModal = async () => {
    const plans = info.plans || [];
    const mods = info.modules || [];
    const curPlan = sub.plan_code || (plans[0] ? plans[0].code : '');
    const { slot } = openModal(`
      <div class="modal-title">🛒 Тарифы и модули для «${esc(info.company?.name || '')}»</div>
      <div class="form-grid" style="margin:10px 0">
        <label class="field"><span>Готовый тарифный пакет</span>
          <select id="sf-plan">
            <option value="">Без пакета — выбрать только нужные модули</option>
            ${plans.map((p) => `<option value="${esc(p.code)}" ${curPlan === p.code ? 'selected' : ''}>${esc(p.title)}${p.badge ? ' (' + esc(p.badge) + ')' : ''} — ${fmtKop(p.monthly_kop)}/мес</option>`).join('')}
          </select></label>
        <label class="field"><span>Период подписки</span>
          <select id="sf-cycle">
            <option value="monthly">1 месяц</option>
            <option value="quarterly">3 месяца (квартал)</option>
            <option value="yearly" selected>12 месяцев (максимальная выгода)</option>
          </select></label>
      </div>
      <div class="bil-box">
        <div class="card-section-title">Дополнительные или отдельные модули</div>
        <div class="bil-checks">${mods.map((m) => `
          <label class="check-row">
            <input type="checkbox" data-sf-mod="${esc(m.code)}" ${(sub.addon_modules || []).includes(m.code) ? 'checked' : ''} style="width:auto">
            <span><b>${esc(m.title)}</b> — ${fmtKop(m.monthly_kop)}/мес (${fmtKop(m.yearly_kop)}/год)</span>
          </label>`).join('')}
        </div>
        ${(info.coming_soon_modules || []).length ? `
          <div class="form-hint" style="margin-top:6px">Скоро в линейке: ${(info.coming_soon_modules || []).map((m) => esc(m.title)).join(', ')}.</div>
        ` : ''}
      </div>
      <div class="form-grid" style="margin:10px 0">
        <label class="field"><span>Промокод (при наличии)</span>
          <input id="sf-promo" placeholder="ПРОМОКОД"></label>
        <label class="field"><span>Комментарий для бухгалтерии</span>
          <input id="sf-comm" placeholder="Например: нужен договор в ЭДО"></label>
      </div>
      <div class="info-callout" id="sf-quote-box">Рассчитываем стоимость…</div>
      <div class="modal-actions">
        <button type="button" class="btn" data-sf-cancel>Закрыть</button>
        <button type="button" class="btn btn-primary" id="sf-submit">🧾 Выписать счёт на оплату</button>
      </div>`);

    const refreshQuote = async () => {
      const qBox = slot.querySelector('#sf-quote-box');
      const addons = Array.from(slot.querySelectorAll('[data-sf-mod]:checked')).map((el) => el.dataset.sfMod);
      try {
        const q = await api.post('/api/v1/billing/quote', {
          company_id: companyId,
          plan_code: slot.querySelector('#sf-plan').value,
          addon_modules: addons,
          billing_cycle: slot.querySelector('#sf-cycle').value,
          promo_code: slot.querySelector('#sf-promo').value.trim(),
        });
        qBox.innerHTML = `К оплате за <b>${esc(q.cycle_title)}</b>: <b>${esc(q.total_formatted)}</b> (${esc(q.vat_label)})` +
          (q.discount_kop > 0 ? ` · скидка −${fmtKop(q.discount_kop)}` : '') +
          (q.grandfathered ? ' · 🔒 применена ваша фиксированная цена' : '');
      } catch (err) {
        qBox.innerHTML = `<span class="form-error">${esc(err.message)}</span>`;
      }
    };

    slot.querySelector('#sf-plan').onchange = refreshQuote;
    slot.querySelector('#sf-cycle').onchange = refreshQuote;
    slot.querySelector('#sf-promo').onchange = refreshQuote;
    slot.querySelectorAll('[data-sf-mod]').forEach((cb) => { cb.onchange = refreshQuote; });
    slot.querySelector('[data-sf-cancel]').onclick = () => closeModal();
    slot.querySelector('#sf-submit').onclick = async () => {
      const addons = Array.from(slot.querySelectorAll('[data-sf-mod]:checked')).map((el) => el.dataset.sfMod);
      try {
        const res = await api.post('/api/v1/billing/request-invoice', {
          company_id: companyId,
          plan_code: slot.querySelector('#sf-plan').value,
          addon_modules: addons,
          billing_cycle: slot.querySelector('#sf-cycle').value,
          promo_code: slot.querySelector('#sf-promo').value.trim(),
          comment: slot.querySelector('#sf-comm').value.trim(),
        });
        toast(`Счёт № ${res.invoice.number} сформирован`, 'ok', '🧾');
        closeModal();
        await mountClientBillingBox(hostEl, companyId);
        await openInvoicePrintWindow(res.invoice.id);
      } catch (err) {
        toast(err.message, 'err');
      }
    };

    await refreshQuote();
  };

  box.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:8px">
      <div class="card-title" style="font-size:14px;margin:0">💳 Подписка и модули организации</div>
      <span class="chip ${badgeCls}">${esc(sub.status_title || sub.status)}</span>
    </div>
    ${(sub.warnings || []).map((w) => `<div class="info-callout" style="margin-bottom:8px;font-size:12.5px">${esc(w)}</div>`).join('')}
    <dl class="kv" style="font-size:13px;margin-bottom:8px">
      <dt>Текущий тариф</dt><dd><b>${esc(sub.plan_title || 'Базовый доступ')}</b>${sub.grandfathered ? ' <span class="form-hint">(🔒 фиксированная цена)</span>' : ''}</dd>
      <dt>Срок действия</dt><dd>${sub.period_end ? 'до ' + fmtDate(sub.period_end) : 'без ограничения срока'}</dd>
      <dt>Использование за ${esc(sub.usage?.period || 'месяц')}</dt>
      <dd>чеки: <b>${sub.usage?.receipts || 0}</b>${sub.limits?.receipts_monthly ? ' из ' + sub.limits.receipts_monthly : ''} · УПД: <b>${sub.usage?.upd || 0}</b>${sub.limits?.upd_monthly ? ' из ' + sub.limits.upd_monthly : ''} · контрагенты: <b>${sub.usage?.counterparties || 0}</b>${sub.limits?.counterparties_monthly ? ' из ' + sub.limits.counterparties_monthly : ''}</dd>
    </dl>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <button type="button" class="btn btn-primary btn-sm" id="my-bil-open">🛒 Подобрать тариф / выписать счёт</button>
      ${invoices.slice(0, 2).map((inv) => `
        <button type="button" class="btn btn-sm" data-my-inv-print="${esc(inv.id)}">🖨 Счёт № ${esc(inv.number)} (${inv.status === 'paid' ? 'оплачен' : esc(inv.total_formatted)})</button>
      `).join('')}
    </div>
  `;

  const btnOpen = box.querySelector('#my-bil-open');
  if (btnOpen) btnOpen.onclick = openStorefrontModal;
  box.querySelectorAll('[data-my-inv-print]').forEach((b) => {
    b.onclick = () => openInvoicePrintWindow(b.dataset.myInvPrint);
  });
}
