/* v1.70.83: экран разбора конфликтов единого реестра (только администратор).
   Список спорных чеков, карточка с копиями по компаниям и журналом, решение
   спора о владельце через resolve-ownership — с обязательной причиной и
   проверкой версии (два администратора не перепишут решение друг друга).
   v1.70.85 («Единая база», фазы 1–2): вкладка «Заявки» — компании, которые
   пытались учесть чужой чек; число организаций и их список в карточке.
   Решения: передать чек, отклонить, не учитывать — тоже с причиной и версией. */
import { api } from './api.js';
import { esc, fmtDate, fmtInt, fmtSum, openModal, toast } from './ui.js';

// Биты совпадают с app/unified/models.py — замок в tests/test_unified_review.py.
export const REVIEW_FLAGS = [
  { bit: 1, code: 'date', title: 'Дата', blocking: true,
    hint: 'копии чека расходятся по дате и времени' },
  { bit: 2, code: 'amount', title: 'Сумма', blocking: true,
    hint: 'копии чека расходятся по сумме' },
  { bit: 4, code: 'operation', title: 'Операция', blocking: true,
    hint: 'в копиях разный тип операции (приход или возврат)' },
  { bit: 8, code: 'ownership', title: 'Владелец', blocking: true,
    hint: 'копии чека у разных компаний — чек скрыт от компаний до решения администратора' },
  { bit: 16, code: 'items', title: 'Позиции', blocking: true,
    hint: 'списки позиций в копиях различаются' },
  { bit: 32, code: 'incomplete', title: 'Неполная тройка', blocking: false,
    hint: 'нет ФН, ФД или ФП — чек не склеивается с другими (пометка, не конфликт)' },
  { bit: 64, code: 'merchant', title: 'Продавец', blocking: true,
    hint: 'копии расходятся по продавцу' },
  { bit: 128, code: 'verification', title: 'Проверка ФНС', blocking: false,
    hint: 'копии расходятся по подтверждению ФНС (пометка, не конфликт)' },
];
export const OWNERSHIP_BIT = 8;
export const REASON_MIN = 3;
export const REASON_MAX = 500;

const SOURCE_TITLES = { registry: 'реестр', pool: 'Чек-Пул' };
const EVENT_TITLES = {
  register: 'Регистрация копии',
  backfill: 'Перенос архива',
  sync: 'Синхронизация правки',
  claim: 'Закрепление за компанией',
  release: 'Возврат в Чек-Пул',
  move: 'Перенос между компаниями',
  resolve_ownership: 'Решение спора о владельце',
  detach_registry: 'Удаление копии из реестра',
  reconcile: 'Сверка после отката',
  company_wipe: 'Удаление компании',
  claim_transfer: 'Чек передан по заявке',
  claim_decline: 'Заявка отклонена',
  claim_void: 'Заявка не учитывается',
};

// v1.70.85: заявки компаний (app/unified/links.py — коды совпадают).
export const CLAIM_STATUSES = [
  { code: 'open', title: 'Ждёт решения' },
  { code: 'declined', title: 'Отклонена' },
  { code: 'void', title: 'Не учитывается' },
  { code: 'transferred', title: 'Чек передан' },
];
const CLAIM_SOURCE_TITLES = {
  scan_qr: 'скан QR', scan_image: 'фото чека', scan_pdf: 'PDF-чек', manual: 'ручной ввод',
  pool_transfer: 'перенос из Чек-Пула', archive_twin: 'копия чека в архиве', move: 'перемещение',
};
const ROLE_TITLES = { owner: 'Владелец', claimant: 'Претендент', former_owner: 'Прежний владелец' };
export const CLAIM_ACTIONS = {
  transfer: { title: 'Передать чек', done: 'Чек передан компании', status: 'transferred' },
  decline: { title: 'Отклонить', done: 'Заявка отклонена', status: 'declined' },
  void: { title: 'Не учитывать', done: 'Заявка больше не учитывается', status: 'void' },
};

/** Пометки документа по битовой маске review_flags. */
export function decodeFlags(mask) {
  const m = Number(mask) || 0;
  return REVIEW_FLAGS.filter((f) => (m & f.bit) !== 0);
}

/** Спор о владельце можно решить: пометка есть и документ не удалён. */
export function canResolve(doc) {
  return !!doc && !doc.deleted && ((Number(doc.review_flags) || 0) & OWNERSHIP_BIT) !== 0;
}

/** Причина решения обязательна: '' — всё в порядке, иначе текст ошибки. */
export function validateReason(text) {
  const t = String(text || '').trim();
  if (t.length < REASON_MIN) return `Укажите причину решения — не короче ${REASON_MIN} символов`;
  if (t.length > REASON_MAX) return `Причина длиннее ${REASON_MAX} символов`;
  return '';
}

/** Тело POST …/resolve-ownership: пустая компания — «оставить без владельца». */
export function buildResolveBody(doc, companyId, reason) {
  return {
    company_id: companyId || null,
    expected_version: doc.version,
    reason: String(reason || '').trim(),
  };
}

export function eventTitle(type) {
  return EVENT_TITLES[type] || type || '—';
}

export function sourceTitle(kind) {
  return SOURCE_TITLES[kind] || kind || '—';
}

export function claimStatusTitle(code) {
  const found = CLAIM_STATUSES.find((s) => s.code === code);
  return found ? found.title : (code || '—');
}

export function claimSourceTitle(code) {
  return CLAIM_SOURCE_TITLES[code] || code || '—';
}

export function companyRoleTitle(role) {
  return ROLE_TITLES[role] || role || '—';
}

/** Значок в меню: конфликты и заявки, ждущие решения. */
export function badgeCount(stats) {
  const s = stats || {};
  return (Number(s.conflicts) || 0) + (Number(s.claims_open) || 0);
}

/** Доступные решения: переданную заявку не решают повторно; передать нельзя
    удалённой компании и чеку, который требует разбора (вкладка «Споры»). */
export function claimActions(item) {
  if (!item || item.status === 'transferred') return [];
  return Object.keys(CLAIM_ACTIONS).filter((action) => (action === 'transfer'
    ? !item.company_deleted && !item.blocking
    : item.status !== CLAIM_ACTIONS[action].status));
}

/** Тело POST …/links/{id}/{действие}: версия чека из строки списка и причина. */
export function buildClaimDecisionBody(item, reason) {
  return { expected_version: item.version, reason: String(reason || '').trim() };
}

/** Блок «Организации»: сколько компаний учитывают чек и какие (только администратор). */
export function companiesHtml(data) {
  if (!data || !Array.isArray(data.companies)) return '';
  const rows = data.companies.map((c) => {
    const name = c.company_deleted ? 'компания удалена' : (c.company_name || c.company_id);
    const parts = [companyRoleTitle(c.role)];
    if (c.role === 'claimant') parts.push(claimStatusTitle(c.status));
    if (c.source && c.role !== 'owner') parts.push(claimSourceTitle(c.source));
    if (c.attempts) parts.push(`попыток: ${fmtInt(c.attempts)}`);
    return `<li>${esc(name)} — ${esc(parts.join(' · '))}${c.counted ? '' : ' <span class="form-hint">(не в числе)</span>'}</li>`;
  }).join('');
  return `<div class="card-title" style="margin-top:8px">Организации</div>
    <p class="form-hint" id="ur-companies-count">Учитывают чек: <b>${fmtInt(data.count)}</b> — владелец и
      компании с заявками «ждёт решения» и «отклонена». Сотрудники компаний этого числа не видят.</p>
    <ul id="ur-companies">${rows || '<li>компаний нет</li>'}</ul>`;
}

/** Счётчик конфликтов в меню: ноль скрывает значок. */
export function setConflictsBadge(count, root = document) {
  const badge = root.getElementById ? root.getElementById('nav-conflicts-badge') : null;
  if (!badge) return;
  const n = Number(count) || 0;
  badge.textContent = fmtInt(n);
  badge.classList.toggle('hidden', n === 0);
  badge.classList.toggle('accent', n > 0);
}

function flagChips(mask) {
  return decodeFlags(mask).map((f) => `<span class="chip ${f.blocking ? 'failed' : 'pending'}"
    title="${esc(f.hint)}">${esc(f.title)}</span>`).join(' ');
}

function companyMove(e) {
  if (!e.from_company_id && !e.to_company_id) return '—';
  const from = e.from_company_id ? (e.from_company_name || 'компания удалена') : 'без владельца';
  const to = e.to_company_id ? (e.to_company_name || 'компания удалена') : 'без владельца';
  return `${esc(from)} → ${esc(to)}`;
}

function eventDetails(e) {
  const d = e.details || {};
  if (d.reason) return `Причина: ${esc(d.reason)}`;
  if (d.source_kind) return `Копия: ${esc(sourceTitle(d.source_kind))}`;
  return '';
}

function cardHtml(doc, companies, linked) {
  const flags = decodeFlags(doc.review_flags);
  const candidateIds = new Set((doc.candidates || []).map((c) => c.company_id));
  const others = (companies || []).filter((c) => c && c.is_active !== false && !candidateIds.has(c.id));
  const copies = (doc.copies || []).map((c) => `<li>${esc(sourceTitle(c.kind))}:
    ${c.company_id ? esc(c.company_name || 'компания удалена') : 'без компании'}</li>`).join('');
  const journal = (doc.events || []).map((e) => `<tr><td>${esc(fmtDate(e.created_at))}</td>
    <td>${esc(eventTitle(e.type))}</td><td>${esc(e.actor_name || (e.actor_id ? 'удалённый сотрудник' : 'система'))}</td>
    <td>${companyMove(e)}</td><td>${eventDetails(e)}</td></tr>`).join('');
  let action;
  if (doc.deleted) {
    action = '<p class="form-hint">Чек удалён: все копии убраны, разбирать нечего.</p>';
  } else if (canResolve(doc)) {
    const candidates = doc.candidates || [];
    action = `<div class="card-title" style="margin-top:12px">Решение</div>
      <label class="field"><input type="radio" name="ur-mode" id="ur-mode-company" value="company"
        ${candidates.length || others.length ? 'checked' : 'disabled'}> Закрепить чек за компанией</label>
      <label class="field"><span>Компания</span><select id="ur-company">
        ${candidates.length ? `<optgroup label="Компании с копией чека">${candidates.map((c) =>
          `<option value="${esc(c.company_id)}">${esc(c.company_name || c.company_id)}</option>`).join('')}</optgroup>` : ''}
        ${others.length ? `<optgroup label="Другие компании">${others.map((c) =>
          `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</optgroup>` : ''}
      </select></label>
      <label class="field"><input type="radio" name="ur-mode" id="ur-mode-none" value="none"
        ${candidates.length || others.length ? '' : 'checked'}> Оставить без владельца — чек не получит ни одна компания</label>
      <label class="field"><span>Причина решения (обязательно, попадёт в журнал)</span>
        <textarea id="ur-reason" rows="3" maxlength="${REASON_MAX}"
          placeholder="Например: чек оплачен картой компании по договору № 15"></textarea></label>
      <div class="form-error" id="ur-form-error" role="alert"></div>
      <button type="button" class="btn btn-primary" id="ur-resolve">Применить решение</button>`;
  } else {
    action = `<p class="form-hint">Здесь расхождение данных между копиями, а не спор о владельце. Действия для
      такого разбора на этом экране пока нет: чек виден компании-владельцу, но не переносится
      между компаниями.</p>`;
  }
  return `<div class="card-title">Чек от ${esc(fmtDate(doc.receipt_date))} · ${fmtSum(doc.total_sum)}</div>
    <p class="form-hint">${esc(doc.merchant_name || 'Продавец не указан')}${doc.merchant_inn ? ` · ИНН ${esc(doc.merchant_inn)}` : ''}
      · ФН ${esc(doc.fn || '—')} · ФД ${esc(doc.fd || '—')} · ФП ${esc(doc.fp || '—')} · позиций: ${fmtInt(doc.items_count)}
      · версия ${fmtInt(doc.version)}</p>
    <div class="card-title" style="margin-top:8px">Причины</div>
    <ul>${flags.map((f) => `<li><b>${esc(f.title)}</b> — ${esc(f.hint)}</li>`).join('') || '<li>пометок нет</li>'}</ul>
    <div class="card-title" style="margin-top:8px">Копии чека</div>
    <ul id="ur-copies">${copies || '<li>живых копий нет</li>'}</ul>
    ${companiesHtml(linked)}
    <div class="card-title" style="margin-top:8px">Журнал</div>
    <div class="table-wrap"><table class="data"><thead><tr><th>Когда</th><th>Событие</th><th>Кто</th>
      <th>Владелец</th><th>Подробности</th></tr></thead><tbody>${journal || '<tr><td colspan="5">Записей нет.</td></tr>'}</tbody></table></div>
    ${action}`;
}

export async function viewUnifiedReview(container) {
  const state = { page: 1, perPage: 20, flag: 0, tab: 'conflicts', claimStatus: 'open', claimPage: 1 };
  let req = 0;
  let claimReq = 0;
  container.innerHTML = `
    <div class="glass card">
      <div class="card-title">⚖️ Разбор конфликтов единого реестра <span class="spacer"></span>
        <button type="button" class="btn btn-sm" id="ur-refresh">Обновить</button></div>
      <p class="form-hint">Конфликт — один фискальный чек, копии которого расходятся. Спор о владельце скрывает чек
        от компаний, пока администратор не решит, чей он. Расхождения данных (дата, сумма, позиции и другие)
        запрещают перенос чека между компаниями. Каждое решение записывается в журнал чека.</p>
      <div class="grid kpi-grid" id="ur-kpi"></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0" role="tablist">
        <button type="button" class="btn btn-sm btn-primary" id="ur-tab-conflicts" role="tab"
          aria-selected="true">Споры</button>
        <button type="button" class="btn btn-sm" id="ur-tab-claims" role="tab" aria-selected="false">Заявки
          <span class="nav-badge hidden" id="ur-claims-badge">0</span></button>
      </div>
      <div id="ur-pane-conflicts">
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin:10px 0">
          <label class="field"><span>Причина</span><select id="ur-flag">
            <option value="0">Все конфликты</option>
            ${REVIEW_FLAGS.filter((f) => f.blocking).map((f) => `<option value="${f.bit}">${esc(f.title)}</option>`).join('')}
          </select></label>
        </div>
        <div id="ur-out"><div class="skeleton" style="height:120px"></div></div>
      </div>
      <div id="ur-pane-claims" class="hidden">
        <p class="form-hint">Заявка — компания пыталась учесть чек, который уже принадлежит другой: скан, фото,
          PDF, ручной ввод или перенос из Чек-Пула получили отказ. Сотрудник видит только «Заявка передана
          администратору»; число организаций и их список — только здесь. «Отклонить» оставляет компанию в числе
          организаций, «Не учитывать» — убирает (ошибочный скан, тест).</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin:10px 0">
          <label class="field"><span>Статус</span><select id="ur-claim-status">
            ${CLAIM_STATUSES.map((st) => `<option value="${st.code}">${esc(st.title)}</option>`).join('')}
            <option value="all">Все заявки</option>
          </select></label>
        </div>
        <div id="ur-claims-out"></div>
      </div>
    </div>`;
  const out = container.querySelector('#ur-out');
  const claimsOut = container.querySelector('#ur-claims-out');

  async function loadStats() {
    const box = container.querySelector('#ur-kpi');
    try {
      const s = await api.get('/api/v1/unified/stats');
      if (!container.isConnected) return;
      box.innerHTML = `
        <div class="glass kpi"><div class="kpi-value">${fmtInt(s.conflicts)}</div><div class="kpi-sub">конфликтов</div></div>
        <div class="glass kpi"><div class="kpi-value">${fmtInt(s.ownership_conflicts)}</div><div class="kpi-sub">из них спор о владельце</div></div>
        <div class="glass kpi"><div class="kpi-value">${fmtInt(s.claims_open)}</div><div class="kpi-sub">заявок ждут решения</div></div>
        <div class="glass kpi"><div class="kpi-value">${fmtInt(s.documents)}</div><div class="kpi-sub">чеков в едином реестре</div></div>`;
      setConflictsBadge(badgeCount(s));
      const claimsBadge = container.querySelector('#ur-claims-badge');
      const open = Number(s.claims_open) || 0;
      claimsBadge.textContent = fmtInt(open);
      claimsBadge.classList.toggle('hidden', open === 0);
    } catch (e) {
      box.innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }

  async function loadList() {
    const current = ++req;
    out.innerHTML = '<div class="skeleton" style="height:120px"></div>';
    const params = new URLSearchParams({ conflicts: 'true', page: String(state.page), per_page: String(state.perPage) });
    if (state.flag) params.set('flag', String(state.flag));
    try {
      const data = await api.get(`/api/v1/unified/documents?${params}`);
      if (current !== req || !container.isConnected) return;
      renderList(data);
    } catch (e) {
      if (current !== req || !container.isConnected) return;
      out.innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }

  function renderList(data) {
    const items = Array.isArray(data.items) ? data.items : [];
    const total = Number(data.total) || 0;
    const pages = Math.max(1, Math.ceil(total / state.perPage));
    out.innerHTML = `<div class="table-wrap"><table class="data"><thead><tr>
      <th>Дата</th><th>Продавец</th><th class="num">Сумма</th><th>Причины</th><th>Владелец</th><th></th></tr></thead><tbody>
      ${items.map((d) => `<tr><td>${esc(fmtDate(d.receipt_date))}</td>
        <td>${esc(d.merchant_name || '—')}</td><td class="num">${fmtSum(d.total_sum)}</td>
        <td>${flagChips(d.review_flags)}</td><td>${esc(d.company_name || 'не назначен')}</td>
        <td><button type="button" class="btn btn-sm ur-open" data-id="${esc(d.id)}">Открыть</button></td></tr>`).join('')
        || '<tr><td colspan="6">Конфликтов нет — разбирать нечего.</td></tr>'}</tbody></table></div>
      <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
        <button type="button" class="btn btn-sm" id="ur-prev" ${state.page <= 1 ? 'disabled' : ''}>← Назад</button>
        <span class="form-hint">Страница ${fmtInt(state.page)} из ${fmtInt(pages)} · всего ${fmtInt(total)}</span>
        <button type="button" class="btn btn-sm" id="ur-next" ${state.page >= pages ? 'disabled' : ''}>Вперёд →</button>
      </div>`;
    out.querySelectorAll('.ur-open').forEach((b) => { b.onclick = () => openCard(b.dataset.id); });
    out.querySelector('#ur-prev').onclick = () => { state.page -= 1; loadList(); };
    out.querySelector('#ur-next').onclick = () => { state.page += 1; loadList(); };
  }

  async function openCard(id) {
    let doc;
    try {
      doc = await api.get(`/api/v1/unified/documents/${encodeURIComponent(id)}/review`);
    } catch (e) {
      toast(e.message, 'err');
      return;
    }
    let companies = [];
    if (canResolve(doc)) {
      try { companies = await api.get('/api/v1/companies'); } catch { companies = []; }
    }
    let linked = null;
    try {
      linked = await api.get(`/api/v1/unified/documents/${encodeURIComponent(doc.id)}/companies`);
    } catch { linked = null; }
    const modal = openModal(cardHtml(doc, Array.isArray(companies) ? companies : [], linked));
    const btn = modal.slot.querySelector('#ur-resolve');
    if (!btn) return;
    const errBox = modal.slot.querySelector('#ur-form-error');
    btn.onclick = async () => {
      errBox.textContent = '';
      const mode = (modal.slot.querySelector('input[name="ur-mode"]:checked') || {}).value;
      const companyId = mode === 'company' ? modal.slot.querySelector('#ur-company').value : '';
      if (mode === 'company' && !companyId) { errBox.textContent = 'Выберите компанию'; return; }
      const reason = modal.slot.querySelector('#ur-reason').value;
      const problem = validateReason(reason);
      if (problem) { errBox.textContent = problem; return; }
      btn.disabled = true;
      try {
        await api.post(`/api/v1/unified/documents/${encodeURIComponent(doc.id)}/resolve-ownership`,
          buildResolveBody(doc, companyId, reason));
        modal.close();
        toast(companyId ? 'Чек закреплён за компанией' : 'Чек оставлен без владельца', 'ok', 'Решение применено. ');
        await Promise.all([loadStats(), loadList()]);
      } catch (e) {
        btn.disabled = false;
        errBox.textContent = e.message;
        if (e.status === 409) {
          const again = document.createElement('button');
          again.type = 'button';
          again.className = 'btn btn-sm';
          again.id = 'ur-reload';
          again.textContent = 'Открыть карточку заново';
          again.onclick = () => { modal.close(); openCard(doc.id); };
          errBox.append(' ', again);
        }
      }
    };
  }

  async function loadClaims() {
    const current = ++claimReq;
    claimsOut.innerHTML = '<div class="skeleton" style="height:120px"></div>';
    const params = new URLSearchParams({ status: state.claimStatus, page: String(state.claimPage),
      per_page: String(state.perPage) });
    try {
      const data = await api.get(`/api/v1/unified/claims?${params}`);
      if (current !== claimReq || !container.isConnected) return;
      renderClaims(data);
    } catch (e) {
      if (current !== claimReq || !container.isConnected) return;
      claimsOut.innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }

  function renderClaims(data) {
    const items = Array.isArray(data.items) ? data.items : [];
    const total = Number(data.total) || 0;
    const pages = Math.max(1, Math.ceil(total / state.perPage));
    claimsOut.innerHTML = `<div class="table-wrap"><table class="data"><thead><tr>
      <th>Дата чека</th><th>Продавец</th><th class="num">Сумма</th><th>Владелец</th><th>Претендент</th>
      <th>Источник</th><th class="num">Попыток</th><th>Последняя попытка</th><th>Статус</th><th></th></tr></thead><tbody>
      ${items.map((c, i) => `<tr><td>${esc(fmtDate(c.receipt_date))}</td>
        <td>${esc(c.merchant_name || '—')}</td><td class="num">${fmtSum(c.total_sum)}</td>
        <td>${esc(c.owner_company_name || 'не назначен')}</td>
        <td>${esc(c.company_deleted ? 'компания удалена' : (c.company_name || '—'))}</td>
        <td>${esc(claimSourceTitle(c.source))}</td><td class="num">${fmtInt(c.attempts)}</td>
        <td>${esc(fmtDate(c.last_at))}${c.last_user_name ? ` · ${esc(c.last_user_name)}` : ''}</td>
        <td>${esc(claimStatusTitle(c.status))}</td>
        <td><button type="button" class="btn btn-sm ur-claim-open" data-i="${i}">Решить</button></td></tr>`).join('')
        || '<tr><td colspan="10">Заявок нет.</td></tr>'}</tbody></table></div>
      <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
        <button type="button" class="btn btn-sm" id="ur-claims-prev" ${state.claimPage <= 1 ? 'disabled' : ''}>← Назад</button>
        <span class="form-hint">Страница ${fmtInt(state.claimPage)} из ${fmtInt(pages)} · всего ${fmtInt(total)}</span>
        <button type="button" class="btn btn-sm" id="ur-claims-next" ${state.claimPage >= pages ? 'disabled' : ''}>Вперёд →</button>
      </div>`;
    claimsOut.querySelectorAll('.ur-claim-open').forEach((b) => {
      b.onclick = () => openClaim(items[Number(b.dataset.i)]);
    });
    claimsOut.querySelector('#ur-claims-prev').onclick = () => { state.claimPage -= 1; loadClaims(); };
    claimsOut.querySelector('#ur-claims-next').onclick = () => { state.claimPage += 1; loadClaims(); };
  }

  async function openClaim(item) {
    let linked = null;
    try {
      linked = await api.get(`/api/v1/unified/documents/${encodeURIComponent(item.document_id)}/companies`);
    } catch { linked = null; }
    const buttons = claimActions(item).map((action) => `<button type="button"
      class="btn ${action === 'transfer' ? 'btn-primary' : ''}" id="ur-claim-${action}"
      data-action="${action}">${esc(CLAIM_ACTIONS[action].title)}</button>`).join(' ');
    const modal = openModal(`<div class="card-title">Заявка: ${esc(item.company_deleted ? 'компания удалена' : (item.company_name || '—'))}</div>
      <p class="form-hint">Чек от ${esc(fmtDate(item.receipt_date))} · ${fmtSum(item.total_sum)} ·
        ${esc(item.merchant_name || 'продавец не указан')} · владелец: ${esc(item.owner_company_name || 'не назначен')}</p>
      <p class="form-hint">Источник: ${esc(claimSourceTitle(item.source))} · попыток: ${fmtInt(item.attempts)} ·
        статус: ${esc(claimStatusTitle(item.status))}</p>
      ${item.blocking ? `<p class="form-hint">Чек требует разбора (вкладка «Споры») — передать его пока нельзя.</p>` : ''}
      ${companiesHtml(linked)}
      ${buttons ? `<label class="field"><span>Причина решения (обязательно, попадёт в журнал)</span>
        <textarea id="ur-claim-reason" rows="3" maxlength="${REASON_MAX}"
          placeholder="Например: чек оплачен картой этой компании"></textarea></label>
      <div class="form-error" id="ur-claim-error" role="alert"></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">${buttons}</div>`
    : `<p class="form-hint">${item.status === 'transferred' ? 'Чек уже передан по этой заявке.'
      : 'Решений по заявке нет: компания удалена или чек требует разбора.'}</p>`}`);
    const errBox = modal.slot.querySelector('#ur-claim-error');
    modal.slot.querySelectorAll('[data-action]').forEach((btn) => {
      btn.onclick = async () => {
        errBox.textContent = '';
        const reason = modal.slot.querySelector('#ur-claim-reason').value;
        const problem = validateReason(reason);
        if (problem) { errBox.textContent = problem; return; }
        const { action } = btn.dataset;
        btn.disabled = true;
        try {
          await api.post(`/api/v1/unified/links/${encodeURIComponent(item.link_id)}/${action}`,
            buildClaimDecisionBody(item, reason));
          modal.close();
          toast(CLAIM_ACTIONS[action].done, 'ok', 'Решение применено. ');
          await Promise.all([loadStats(), loadClaims()]);
        } catch (e) {
          btn.disabled = false;
          errBox.textContent = e.message;
        }
      };
    });
  }

  function showTab(tab) {
    state.tab = tab;
    const claims = tab === 'claims';
    container.querySelector('#ur-pane-conflicts').classList.toggle('hidden', claims);
    container.querySelector('#ur-pane-claims').classList.toggle('hidden', !claims);
    const tabs = { conflicts: container.querySelector('#ur-tab-conflicts'), claims: container.querySelector('#ur-tab-claims') };
    Object.entries(tabs).forEach(([key, el]) => {
      el.classList.toggle('btn-primary', key === tab);
      el.setAttribute('aria-selected', String(key === tab));
    });
    if (claims) loadClaims();
  }

  container.querySelector('#ur-tab-conflicts').onclick = () => showTab('conflicts');
  container.querySelector('#ur-tab-claims').onclick = () => showTab('claims');
  container.querySelector('#ur-claim-status').onchange = (e) => {
    state.claimStatus = e.target.value || 'open';
    state.claimPage = 1;
    loadClaims();
  };
  container.querySelector('#ur-refresh').onclick = () => {
    loadStats();
    if (state.tab === 'claims') loadClaims(); else loadList();
  };
  container.querySelector('#ur-flag').onchange = (e) => {
    state.flag = Number(e.target.value) || 0;
    state.page = 1;
    loadList();
  };
  await Promise.all([loadStats(), loadList()]);
}
