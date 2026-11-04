/* v1.70.98 (T-45): экран «Банк: выписка» — бухгалтер загружает выписку в формате обмена 1С, программа
   находит списания без документов (оплаты поставщикам без УПД, покупки по картам без чека) и чеки, которых
   нет в выписке: сотрудник платил своими — ему нужно вернуть деньги. Платежи без документов сами попадают
   в «Что нужно от клиента»; покупка по карте сотрудника — запрос ему лично. Пришёл документ — запрос
   закрывается сам. Все имена верхнего уровня — с приставкой bank (общая область модулей в сборке). */
import { api } from './api.js';
import { esc, fmtSum, openModal, toast } from './ui.js';

export const BANK_STATUSES = {
  open: ['chip unknown', 'Нет документов'], matched: ['chip verified', 'Документ найден'], ignored: ['chip failed', 'Не нужны'],
};
export const BANK_KINDS = { card: '💳 покупка по карте', transfer: '🏢 оплата поставщику' };
export const BANK_SKIPPED = {
  tax: 'налоги и взносы', salary: 'зарплата', fee: 'комиссии банка', own: 'свои счета', advance: 'под отчёт',
  income: 'поступления', loan: 'кредиты и займы',
};
export const BANK_REFUND = {
  pending: ['chip unknown', 'К возмещению'], paid: ['chip verified', 'Возмещено'], declined: ['chip failed', 'Не возмещать'],
};
const BANK_TABS = [['open', 'Без документов'], ['matched', 'Документ найден'], ['ignored', 'Не нужны'],
  ['refund', 'К возмещению'], ['statements', 'Выписки']];

export function bankDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  return m ? `${m[3]}.${m[2]}.${m[1]}` : '—';
}

function bankChip(map, status) {
  const [cls, label] = map[status] || ['chip unknown', status || '—'];
  return `<span class="${cls}">${esc(label)}</span>`;
}

/** Итог загрузки одной строкой: сколько операций, чему нужны документы, что нашлось. */
export function bankUploadSummary(res) {
  if (!res) return '';
  const skipped = Object.entries(res.kinds || {}).filter(([k, n]) => BANK_SKIPPED[k] && n)
    .map(([k, n]) => `${BANK_SKIPPED[k]} ${n}`);
  const parts = [`операций: ${res.operations}`, `нужны документы: ${res.saved}`, `документ найден: ${res.matched}`,
    `без документов: ${res.open}`];
  if (res.duplicates) parts.push(`уже были в прошлых выписках: ${res.duplicates}`);
  if (skipped.length) parts.push(`не требуют документов: ${skipped.join(', ')}`);
  if (res.requests) parts.push(`в «Что нужно от клиента» добавлено: ${res.requests}`);
  return parts.join(' · ');
}

export function bankOpsHtml(items, status) {
  const list = Array.isArray(items) ? items : [];
  if (!list.length) {
    const empty = { open: 'Платежей без документов нет. Загрузите выписку — программа проверит списания.',
      matched: 'Сопоставленных платежей пока нет.', ignored: 'Отмеченных «документы не нужны» нет.' };
    return `<tr><td colspan="6">${esc(empty[status] || 'Операций нет.')}</td></tr>`;
  }
  return list.map((op) => {
    const act = (action, label) => `<button type="button" class="btn btn-sm bank-act" data-id="${esc(op.id)}" data-action="${action}">${label}</button>`;
    const actions = op.status === 'open' ? act('ignore', 'Документы не нужны')
      : act('open', op.status === 'matched' ? '↺ Отменить сопоставление' : '↺ Вернуть в работу');
    const what = op.kind === 'card'
      ? `${BANK_KINDS.card}${op.card_tail ? ` *${esc(op.card_tail)}` : ''}${op.card_owner ? `<div class="form-hint">карта: ${esc(op.card_owner)}</div>` : ''}`
      : `${BANK_KINDS.transfer}${op.number ? `<div class="form-hint">платёж № ${esc(op.number)}</div>` : ''}`;
    const who = `${esc(op.counterparty_name || '—')}${op.counterparty_inn ? `<div class="form-hint">ИНН ${esc(op.counterparty_inn)}</div>` : ''}`;
    const note = op.match_note ? `<div class="form-hint">${esc(op.match_note)}${op.decided_by ? ` — ${esc(op.decided_by)}` : ''}</div>` : '';
    return `<tr data-id="${esc(op.id)}"><td>${esc(bankDate(op.date))}</td><td>${what}</td><td>${who}</td>
      <td class="bank-purpose">${esc(op.purpose || '—')}</td><td class="num dreq-sum">${fmtSum(op.amount)}</td>
      <td>${bankChip(BANK_STATUSES, op.status)}${note}<div class="dreq-actions">${actions}</div></td></tr>`;
  }).join('');
}

export function bankRefundHtml(data) {
  const groups = data && Array.isArray(data.employees) ? data.employees : [];
  if (!data || !data.covered) return '<p class="form-hint">Загрузите выписку — программа покажет чеки, которых в ней нет: их сотрудник оплатил сам.</p>';
  if (!groups.length) return '<p class="form-hint">Все чеки за период выписки нашлись в ней — возмещать нечего.</p>';
  return groups.map((g) => {
    const rows = g.items.map((it) => {
      const act = (status, label) => `<button type="button" class="btn btn-sm bank-refund" data-id="${esc(it.receipt_id)}" data-status="${status}">${label}</button>`;
      const actions = it.status === 'pending' ? `${act('paid', '✓ Возмещено')} ${act('declined', 'Не возмещать')}` : act('pending', '↺ Вернуть');
      return `<tr><td>${esc(bankDate(it.date))}</td><td>${esc(it.merchant || '—')}${it.payment ? `<div class="form-hint">${esc(it.payment)}</div>` : ''}</td>
        <td class="num dreq-sum">${fmtSum(it.amount)}${it.amount !== it.total_sum ? `<div class="form-hint">чек ${fmtSum(it.total_sum)}</div>` : ''}</td>
        <td>${bankChip(BANK_REFUND, it.status)}${it.note ? `<div class="form-hint">${esc(it.note)}</div>` : ''}<div class="dreq-actions">${actions}</div></td></tr>`;
    }).join('');
    return `<div class="bank-group"><h4>${esc(g.name)} <span class="form-hint">к возмещению: <b>${fmtSum(g.pending_total)}</b>${g.paid_total ? ` · возмещено: ${fmtSum(g.paid_total)}` : ''}</span></h4>
      <div class="table-wrap"><table class="data"><thead><tr><th>Дата</th><th>Продавец</th><th class="num">К учёту</th><th>Решение</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
  }).join('');
}

export function bankStatementsHtml(list) {
  const items = Array.isArray(list) ? list : [];
  if (!items.length) return '<p class="form-hint">Выписок пока нет.</p>';
  const rows = items.map((s) => `<tr><td>${esc(bankDate(s.date_from))} — ${esc(bankDate(s.date_to))}</td>
    <td>${s.account_tail ? `счёт …${esc(s.account_tail)}` : '—'}</td>
    <td class="num">${esc(s.operations)}</td><td class="num">${esc(s.saved)}</td>
    <td>${esc(s.uploaded_by || '—')}<div class="form-hint">${esc(bankDate(s.created_at))}</div></td>
    <td><button type="button" class="btn btn-sm bank-del" data-id="${esc(s.id)}">Удалить</button></td></tr>`).join('');
  return `<div class="table-wrap"><table class="data"><thead><tr><th>Период</th><th>Счёт</th><th class="num">Операций</th>
    <th class="num">Нужны документы</th><th>Загрузил</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

export function bankSettingsHtml(settings, staff) {
  const s = settings || {};
  const people = Array.isArray(staff) ? staff : [];
  const cards = (Array.isArray(s.cards) ? s.cards : []).concat([{ tail: '', user_id: '' }, { tail: '', user_id: '' }]);
  const options = (sel) => `<option value="">— не указан —</option>${people.map((p) => `<option value="${esc(p.id)}" ${p.id === sel ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}`;
  const rows = cards.map((c, i) => `<div class="dreq-grid">
      <label class="field"><span>Карта — последние 4 цифры</span><input id="bank-s-tail-${i}" maxlength="4" inputmode="numeric" value="${esc(c.tail || '')}"></label>
      <label class="field"><span>Сотрудник</span><select id="bank-s-user-${i}">${options(c.user_id)}</select></label></div>`).join('');
  return `<h3 class="modal-title">⚙ Карты и срок хранения</h3>
    <p class="form-hint">Укажите корпоративные карты сотрудников: покупка по карте без чека станет запросом лично держателю карты.</p>
    ${rows}
    <label class="field"><span>Хранить операции выписок, дней (от 30 до 3650; по умолчанию 1095 — три года)</span>
      <input id="bank-s-days" type="number" min="30" max="3650" value="${esc(s.retention_days || 1095)}"></label>
    <div id="bank-s-error" class="form-error dreq-error" aria-live="polite"></div>
    <div class="modal-actions"><button type="button" class="btn btn-primary" id="bank-s-save">Сохранить</button></div>
    <input type="hidden" id="bank-s-count" value="${cards.length}">`;
}

/** Тело сохранения настроек; ошибка ввода — { error }. */
export function bankSettingsBody(get, companyId) {
  const count = Number(get('bank-s-count').value) || 0;
  const cards = [];
  for (let i = 0; i < count; i += 1) {
    const tail = (get(`bank-s-tail-${i}`).value || '').replace(/\D/g, '');
    const user = get(`bank-s-user-${i}`).value || '';
    if (!tail && !user) continue;
    if (tail.length !== 4) return { error: 'Карта — ровно 4 последние цифры номера' };
    cards.push({ tail, user_id: user });
  }
  const days = Number(get('bank-s-days').value);
  if (!Number.isInteger(days) || days < 30 || days > 3650) return { error: 'Срок хранения — от 30 до 3650 дней' };
  return { ...(companyId ? { company_id: companyId } : {}), retention_days: days, cards };
}

function bankDownload(blob, filename) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
}

export async function viewBank(container, opts = {}) {
  const alive = typeof opts.alive === 'function' ? opts.alive : () => true;
  const companies = (Array.isArray(opts.companies) ? opts.companies : []).filter((c) => c.is_active !== false);
  const filterCompany = opts.companyFilter && opts.companyFilter !== 'all' ? opts.companyFilter : '';
  const state = { company: opts.isAdmin ? filterCompany : '', tab: 'open', data: null };
  let req = 0;
  container.innerHTML = `
    <div class="glass card">
      <div class="card-title">🏦 Банк: выписка</div>
      <p class="form-hint">Загрузите выписку в формате обмена с 1С (в интернет-банке — «Выгрузка в 1С», файл kl_to_1c.txt).
        Программа найдёт списания без документов: оплаты поставщикам без УПД, накладной или акта и покупки по картам без чека —
        они сами попадут в «Что нужно от клиента». А чеки, которых нет в выписке, сотрудник оплатил сам — это «к возмещению».
        Хранятся только списания, которым нужны документы; налоги, зарплата и комиссии лишь считаются.</p>
      <div class="dreq-controls">
        ${opts.isAdmin ? `<label class="field"><span>Компания</span><select id="bank-company">
          <option value="">Выберите компанию</option>
          ${companies.map((c) => `<option value="${esc(c.id)}" ${state.company === c.id ? 'selected' : ''}>${esc(c.short_name || c.name)}</option>`).join('')}
        </select></label>` : ''}
      </div>
      <div class="dreq-tools">
        <button type="button" class="btn btn-primary btn-sm" id="bank-upload">⬆ Загрузить выписку</button>
        <input type="file" id="bank-file" accept=".txt,text/plain" hidden>
        <button type="button" class="btn btn-sm" id="bank-rematch">🔄 Сопоставить заново</button>
        <button type="button" class="btn btn-sm" id="bank-settings">⚙ Карты и срок хранения</button>
      </div>
      <div id="bank-result" class="form-hint" aria-live="polite"></div>
    </div>
    <div class="glass card">
      <div class="card-title">Платежи и чеки</div>
      <p class="form-hint" id="bank-kpi" aria-live="polite"></p>
      <div class="segmented bank-tabs" id="bank-tabs">${BANK_TABS.map(([id, label]) => `<button type="button" id="bank-tab-${id}" data-tab="${id}" class="${id === 'open' ? 'active' : ''}">${label}</button>`).join('')}</div>
      <div id="bank-out"><div class="skeleton" style="height:120px"></div></div>
    </div>`;
  const $ = (sel) => container.querySelector(sel);
  const query = (extra = {}) => {
    const q = new URLSearchParams();
    if (state.company) q.set('company_id', state.company);
    Object.entries(extra).forEach(([k, v]) => q.set(k, v));
    return q;
  };

  function kpi(data) {
    const t = data.totals || {};
    const r = data.reimburse || {};
    const open = t.open || { count: 0, amount: 0 };
    $('#bank-kpi').textContent = `без документов: ${open.count} на ${fmtSum(open.amount)} · найдено: ${(t.matched || {}).count || 0}`
      + ` · к возмещению: ${fmtSum(r.pending_total || 0)} (${r.pending_count || 0})`;
  }

  async function showRefund() {
    $('#bank-out').innerHTML = '<div class="skeleton" style="height:80px"></div>';
    try {
      const data = await api.get(`/api/v1/bank/reimburse?${query()}`);
      if (!container.isConnected || state.tab !== 'refund') return;
      const csv = data.covered && data.employees.length
        ? '<div class="dreq-tools"><button type="button" class="btn btn-sm" id="bank-csv">⬇ Таблица для авансового отчёта (CSV)</button></div>' : '';
      $('#bank-out').innerHTML = `<p class="form-hint">Чеки за период выписок, которых в выписках нет: сотрудник оплатил их сам.
        Последние ${esc(data.cover_tail_days)} дня выписки ждут следующую — списание по карте приходит позже покупки.</p>${csv}${bankRefundHtml(data)}`;
      $('#bank-out').querySelectorAll('.bank-refund').forEach((b) => { b.onclick = () => decide(b.dataset.id, b.dataset.status); });
      const btn = $('#bank-csv');
      if (btn) {
        btn.onclick = async () => {
          try {
            const { blob, filename } = await api.download(`/api/v1/bank/reimburse.csv?${query()}`);
            bankDownload(blob, filename);
          } catch (e) { toast(e.message, 'err'); }
        };
      }
    } catch (e) {
      if (container.isConnected) $('#bank-out').innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }

  function render(data) {
    state.data = data;
    kpi(data);
    BANK_TABS.forEach(([id]) => { const b = $(`#bank-tab-${id}`); if (b) b.classList.toggle('active', id === state.tab); });
    if (state.tab === 'refund') { showRefund(); return; }
    if (state.tab === 'statements') {
      $('#bank-out').innerHTML = bankStatementsHtml(data.statements);
      $('#bank-out').querySelectorAll('.bank-del').forEach((b) => { b.onclick = () => removeStatement(b.dataset.id); });
      return;
    }
    $('#bank-out').innerHTML = `<div class="table-wrap"><table class="data"><thead><tr><th>Дата</th><th>Операция</th><th>Получатель</th>
      <th>Назначение</th><th class="num">Сумма</th><th>Документы</th></tr></thead><tbody>${bankOpsHtml(data.operations, state.tab)}</tbody></table></div>`;
    $('#bank-out').querySelectorAll('.bank-act').forEach((b) => { b.onclick = () => operation(b.dataset.id, b.dataset.action); });
  }

  async function load() {
    if (opts.isAdmin && !state.company) {
      $('#bank-out').innerHTML = '<p class="form-hint">Выберите компанию — у каждой свои счета и выписки.</p>';
      $('#bank-kpi').textContent = '';
      return;
    }
    const current = ++req;
    const status = ['open', 'matched', 'ignored'].includes(state.tab) ? state.tab : 'open';
    try {
      const data = await api.get(`/api/v1/bank?${query({ status })}`);
      if (current !== req || !alive() || !container.isConnected) return;
      render(data);
    } catch (e) {
      if (current === req && container.isConnected) $('#bank-out').innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }

  async function upload(file) {
    if (!file) return;
    if (opts.isAdmin && !state.company) { toast('Сначала выберите компанию', 'err'); return; }
    const form = new FormData();
    form.append('file', file);
    if (state.company) form.append('company_id', state.company);
    $('#bank-result').textContent = 'Разбираем выписку…';
    try {
      const res = await api.post('/api/v1/bank/statements', form);
      if (!container.isConnected) return;
      $('#bank-result').textContent = `Выписка ${bankDate(res.statement.date_from)} — ${bankDate(res.statement.date_to)}: ${bankUploadSummary(res)}`;
      toast(res.open ? `Без документов: ${res.open}` : 'Документы найдены для всех платежей', 'ok');
      state.tab = 'open';
      load();
    } catch (e) {
      if (container.isConnected) $('#bank-result').textContent = '';
      toast(e.message, 'err');
    }
  }

  async function operation(id, action) {
    let note = '';
    if (action === 'ignore') {
      note = window.prompt('Почему документы не нужны? Например: возврат займа, депозит (попадёт в журнал)', '') ?? null;
      if (note === null) return;
    }
    try {
      await api.post(`/api/v1/bank/operations/${encodeURIComponent(id)}`, { action, note });
      load();
    } catch (e) { toast(e.message, 'err'); }
  }

  async function decide(id, status) {
    let note = '';
    if (status === 'declined') {
      note = window.prompt('Почему не возмещать? Например: оплачено из подотчётных', '') ?? null;
      if (note === null) return;
    }
    try {
      await api.post(`/api/v1/bank/reimburse/${encodeURIComponent(id)}`, { ...(state.company ? { company_id: state.company } : {}), status, note });
      showRefund();
      load();
    } catch (e) { toast(e.message, 'err'); }
  }

  async function removeStatement(id) {
    if (!window.confirm('Удалить выписку? Её операции и открытые запросы по ним будут сняты (запросы — с пометкой в журнале).')) return;
    try {
      const res = await api.del(`/api/v1/bank/statements/${encodeURIComponent(id)}`);
      toast(`Выписка удалена, операций: ${res.deleted}`, 'ok');
      load();
    } catch (e) { toast(e.message, 'err'); }
  }

  async function rematch() {
    if (opts.isAdmin && !state.company) { toast('Сначала выберите компанию', 'err'); return; }
    try {
      const res = await api.post(`/api/v1/bank/match?${query()}`, {});
      toast(res.matched ? `Найдено документов: ${res.matched}` : 'Новых совпадений нет', 'ok');
      load();
    } catch (e) { toast(e.message, 'err'); }
  }

  function settings() {
    const data = state.data;
    if (!data) return;
    const { slot, close } = openModal(bankSettingsHtml(data.settings, data.staff));
    if (!slot) return;
    const get = (id) => slot.querySelector(`#${id}`);
    get('bank-s-save').onclick = async () => {
      const payload = bankSettingsBody(get, state.company);
      if (payload.error) { get('bank-s-error').textContent = payload.error; return; }
      try {
        await api.put('/api/v1/bank/settings', payload);
        toast('Сохранено', 'ok');
        if (typeof close === 'function') close();
        load();
      } catch (e) { get('bank-s-error').textContent = e.message; }
    };
  }

  const company = $('#bank-company');
  if (company) company.onchange = (e) => { state.company = e.target.value; load(); };
  const fileInput = $('#bank-file');
  $('#bank-upload').onclick = () => fileInput.click();
  fileInput.onchange = () => { const f = fileInput.files && fileInput.files[0]; fileInput.value = ''; upload(f); };
  $('#bank-rematch').onclick = () => rematch();
  $('#bank-settings').onclick = () => settings();
  BANK_TABS.forEach(([id]) => {
    const b = $(`#bank-tab-${id}`);
    if (b) b.onclick = () => { state.tab = id; if (state.data && (id === 'refund' || id === 'statements')) render(state.data); else load(); };
  });
  await load();
}
