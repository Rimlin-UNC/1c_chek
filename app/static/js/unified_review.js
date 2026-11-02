/* v1.70.83: экран разбора конфликтов единого реестра (только администратор).
   Список спорных чеков, карточка с копиями по компаниям и журналом, решение
   спора о владельце через resolve-ownership — с обязательной причиной и
   проверкой версии (два администратора не перепишут решение друг друга). */
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

function cardHtml(doc, companies) {
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
    <div class="card-title" style="margin-top:8px">Журнал</div>
    <div class="table-wrap"><table class="data"><thead><tr><th>Когда</th><th>Событие</th><th>Кто</th>
      <th>Владелец</th><th>Подробности</th></tr></thead><tbody>${journal || '<tr><td colspan="5">Записей нет.</td></tr>'}</tbody></table></div>
    ${action}`;
}

export async function viewUnifiedReview(container) {
  const state = { page: 1, perPage: 20, flag: 0 };
  let req = 0;
  container.innerHTML = `
    <div class="glass card">
      <div class="card-title">⚖️ Разбор конфликтов единого реестра <span class="spacer"></span>
        <button type="button" class="btn btn-sm" id="ur-refresh">Обновить</button></div>
      <p class="form-hint">Конфликт — один фискальный чек, копии которого расходятся. Спор о владельце скрывает чек
        от компаний, пока администратор не решит, чей он. Расхождения данных (дата, сумма, позиции и другие)
        запрещают перенос чека между компаниями. Каждое решение записывается в журнал чека.</p>
      <div class="grid kpi-grid" id="ur-kpi"></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin:10px 0">
        <label class="field"><span>Причина</span><select id="ur-flag">
          <option value="0">Все конфликты</option>
          ${REVIEW_FLAGS.filter((f) => f.blocking).map((f) => `<option value="${f.bit}">${esc(f.title)}</option>`).join('')}
        </select></label>
      </div>
      <div id="ur-out"><div class="skeleton" style="height:120px"></div></div>
    </div>`;
  const out = container.querySelector('#ur-out');

  async function loadStats() {
    const box = container.querySelector('#ur-kpi');
    try {
      const s = await api.get('/api/v1/unified/stats');
      if (!container.isConnected) return;
      box.innerHTML = `
        <div class="glass kpi"><div class="kpi-value">${fmtInt(s.conflicts)}</div><div class="kpi-sub">конфликтов</div></div>
        <div class="glass kpi"><div class="kpi-value">${fmtInt(s.ownership_conflicts)}</div><div class="kpi-sub">из них спор о владельце</div></div>
        <div class="glass kpi"><div class="kpi-value">${fmtInt(s.documents)}</div><div class="kpi-sub">чеков в едином реестре</div></div>`;
      setConflictsBadge(s.conflicts);
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
    const modal = openModal(cardHtml(doc, Array.isArray(companies) ? companies : []));
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

  container.querySelector('#ur-refresh').onclick = () => { loadStats(); loadList(); };
  container.querySelector('#ur-flag').onchange = (e) => {
    state.flag = Number(e.target.value) || 0;
    state.page = 1;
    loadList();
  };
  await Promise.all([loadStats(), loadList()]);
}
