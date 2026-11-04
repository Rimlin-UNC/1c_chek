/* v1.70.94 (T-53): экран «Первичка» — электронные УПД и счета-фактуры (форматы ФНС 5.01–5.03).
   Загрузка XML или ZIP-архива оператора ЭДО, проверка (реквизиты, ИНН и КПП, арифметика строк
   и итогов, ставка НДС на переходе к 22 %, формат 5.03 с 01.04.2026), реестр с фильтрами и
   карточка документа. Доступ — бухгалтер и администратор; каждый видит только свою компанию. */
import { api } from './api.js';
import { esc, fmtSum, openModal, toast } from './ui.js';

export const PRIMARY_STATUSES = [
  { code: 'ok', title: 'Без замечаний', chip: 'verified' },
  { code: 'warnings', title: 'Есть замечания', chip: 'verifying' },
  { code: 'errors', title: 'Ошибки', chip: 'failed' },
];
const PRIM_LEVEL = {
  error: { icon: '⛔', title: 'Ошибка' },
  warning: { icon: '⚠️', title: 'Замечание' },
  info: { icon: 'ℹ️', title: 'Сведения' },
};
const PRIM_RESULT = { created: 'загружен', duplicate: 'уже был загружен', rejected: 'не принят' };
const PRIM_VAT = { none: 'без НДС', agent: 'налоговый агент' };

export function primStatusChip(status) {
  const st = PRIMARY_STATUSES.find((s) => s.code === status) || { title: status || '—', chip: 'unknown' };
  return `<span class="chip ${st.chip}"><span class="dot"></span>${esc(st.title)}</span>`;
}

export function primVatLabel(rate) {
  if (!rate) return '—';
  if (PRIM_VAT[rate]) return PRIM_VAT[rate];
  return rate.includes('/') ? rate : `${rate} %`;
}

export function primDay(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  return m ? `${m[3]}.${m[2]}.${m[1]}` : '—';
}

const primMoney = (v) => (v === null || v === undefined ? '—' : fmtSum(v));
const primNum = (v) => (v === null || v === undefined ? '—' : new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 }).format(v));

export function primUploadSummary(res) {
  const parts = [];
  if (res.created) parts.push(`загружено ${res.created}`);
  if (res.duplicates) parts.push(`уже были ${res.duplicates}`);
  if (res.rejected) parts.push(`не принято ${res.rejected}`);
  return parts.length ? parts.join(', ') : 'нет документов';
}

export function primIssuesHtml(issues) {
  const list = Array.isArray(issues) ? issues : [];
  if (!list.length) return '<p class="form-hint">Замечаний нет — документ прошёл все проверки.</p>';
  return `<ul class="prim-issues">${list.map((i) => {
    const lv = PRIM_LEVEL[i.level] || PRIM_LEVEL.info;
    return `<li data-level="${esc(i.level)}"><span aria-hidden="true">${lv.icon}</span>
      <span class="visually-hidden">${esc(lv.title)}:</span> ${esc(i.message)}</li>`;
  }).join('')}</ul>`;
}

export function primLinesHtml(lines) {
  const list = Array.isArray(lines) ? lines : [];
  if (!list.length) return '<p class="form-hint">В документе нет строк.</p>';
  return `<div class="table-wrap"><table class="data prim-lines"><thead><tr><th>№</th><th>Наименование</th>
    <th class="num">Кол-во</th><th class="num">Цена</th><th class="num">Без НДС</th><th>Ставка</th>
    <th class="num">НДС</th><th class="num">С НДС</th></tr></thead><tbody>
    ${list.map((ln) => `<tr><td>${esc(String(ln.position))}</td>
      <td>${esc(ln.name || '—')}${ln.item_code ? `<div class="form-hint">код ${esc(ln.item_code)}</div>` : ''}</td>
      <td class="num">${primNum(ln.quantity)}${ln.unit_name ? ' ' + esc(ln.unit_name) : ''}</td>
      <td class="num">${primMoney(ln.price)}</td><td class="num">${primMoney(ln.sum_without_vat)}</td>
      <td>${esc(primVatLabel(ln.vat_rate))}</td><td class="num">${primMoney(ln.vat_sum)}</td>
      <td class="num">${primMoney(ln.sum_with_vat)}</td></tr>`).join('')}</tbody></table></div>`;
}

function primParty(p) {
  if (!p || !p.name) return '—';
  const req = [p.inn && `ИНН ${p.inn}`, p.kpp && `КПП ${p.kpp}`].filter(Boolean).join(', ');
  return `${esc(p.name)}${req ? `<div class="form-hint">${esc(req)}</div>` : ''}`;
}

function primTitle(d) {
  const corr = d.correction_number ? ` · исправление № ${d.correction_number} от ${primDay(d.correction_date)}` : '';
  return `${d.kind} № ${d.doc_number || 'без номера'} от ${primDay(d.doc_date)}${corr}`;
}

export function primRowsHtml(items) {
  const list = Array.isArray(items) ? items : [];
  if (!list.length) {
    return '<tr><td colspan="7">Документов пока нет. Загрузите XML или ZIP из системы ЭДО — проверка займёт секунды.</td></tr>';
  }
  return list.map((d) => `<tr data-id="${esc(d.id)}"><td>${esc(primDay(d.doc_date))}</td>
    <td>${esc(d.doc_number || '—')}${d.correction_number ? `<div class="form-hint">испр. № ${esc(d.correction_number)}</div>` : ''}</td>
    <td>${primParty(d.seller)}</td><td class="num">${primMoney(d.total_with_vat)}</td><td class="num">${primMoney(d.vat_total)}</td>
    <td>${primStatusChip(d.status)}${d.errors || d.warnings
      ? `<div class="form-hint">${[d.errors && `ошибок ${d.errors}`, d.warnings && `замечаний ${d.warnings}`].filter(Boolean).join(', ')}</div>`
      : ''}</td>
    <td><button type="button" class="btn btn-sm prim-open" data-id="${esc(d.id)}">Открыть</button></td></tr>`).join('');
}

function primResultsHtml(res) {
  const rows = (res.results || []).map((r) => {
    const doc = r.document;
    const what = doc ? `${esc(primTitle(doc))} — ${primStatusChip(doc.status)}` : esc(r.message || '');
    const note = r.result === 'created' ? '' : `<div class="form-hint">${esc(r.message || '')}</div>`;
    return `<li><b>${esc(r.file)}</b>: ${esc(PRIM_RESULT[r.result] || r.result)}<div>${what}</div>${doc ? note : ''}</li>`;
  }).join('');
  return `<div class="form-hint">Итог загрузки: ${esc(primUploadSummary(res))}.</div><ul class="prim-results">${rows}</ul>`;
}

export async function viewPrimary(container, opts = {}) {
  const alive = typeof opts.alive === 'function' ? opts.alive : () => true;
  const companies = (Array.isArray(opts.companies) ? opts.companies : []).filter((c) => c.is_active !== false);
  const filterCompany = opts.companyFilter && opts.companyFilter !== 'all' ? opts.companyFilter : '';
  const state = { status: '', q: '', from: '', to: '', offset: 0, limit: 50 };
  let req = 0;
  container.innerHTML = `
    <div class="glass card">
      <div class="card-title">📑 Первичка: электронные УПД и счета-фактуры</div>
      <p class="form-hint">Загрузите файлы из системы ЭДО (Диадок, СБИС, Такском и другие): XML или ZIP-архив.
        Программа прочитает реквизиты и строки и проверит то, на чём чаще всего теряют вычет: обязательные
        реквизиты, ИНН и КПП, арифметику, ставку НДС (с 2026 года — 22 %) и формат 5.03 (с 01.04.2026 —
        единственный в ЭДО). Подписи и печатные формы из архива пропускаются, коды маркировки не сохраняются.</p>
      ${opts.isAdmin ? `<label class="field prim-company"><span>Компания</span><select id="prim-company">
        <option value="">Определить по ИНН покупателя</option>
        ${companies.map((c) => `<option value="${esc(c.id)}" ${filterCompany === c.id ? 'selected' : ''}>${esc(c.short_name || c.name)}</option>`).join('')}
      </select></label>` : ''}
      <div class="dropzone" id="prim-drop" role="button" tabindex="0" aria-label="Выбрать файлы УПД">
        <span class="big-ico" aria-hidden="true">📥</span>Перетащите сюда XML или ZIP или нажмите, чтобы выбрать
      </div>
      <input type="file" id="prim-file" accept=".xml,.zip" multiple class="hidden">
      <div id="prim-results" aria-live="polite"></div>
    </div>
    <div class="glass card">
      <div class="card-title">Реестр документов <span class="spacer"></span>
        <button type="button" class="btn btn-sm" id="prim-refresh">Обновить</button></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin:6px 0 10px">
        <label class="field"><span>Проверка</span><select id="prim-status"><option value="">Все</option>
          ${PRIMARY_STATUSES.map((s) => `<option value="${s.code}">${esc(s.title)}</option>`).join('')}</select></label>
        <label class="field"><span>Поиск</span><input id="prim-q" type="search" maxlength="100"
          placeholder="номер, продавец, ИНН"></label>
        <label class="field"><span>С даты</span><input id="prim-from" type="date"></label>
        <label class="field"><span>По дату</span><input id="prim-to" type="date"></label>
      </div>
      <div id="prim-kpi" class="form-hint"></div>
      <div id="prim-out"><div class="skeleton" style="height:120px"></div></div>
    </div>`;
  const $ = (sel) => container.querySelector(sel);
  const out = $('#prim-out');

  async function load() {
    const current = ++req;
    const params = new URLSearchParams({ limit: String(state.limit), offset: String(state.offset) });
    if (state.status) params.set('status', state.status);
    if (state.q) params.set('q', state.q);
    if (state.from) params.set('date_from', state.from);
    if (state.to) params.set('date_to', state.to);
    if (opts.isAdmin && filterCompany) params.set('company_id', filterCompany);
    try {
      const data = await api.get(`/api/v1/primary?${params}`);
      if (current !== req || !alive() || !container.isConnected) return;
      const s = data.summary || {};
      $('#prim-kpi').textContent = `Без замечаний: ${s.ok || 0} · с замечаниями: ${s.warnings || 0} · с ошибками: ${s.errors || 0}`;
      const total = Number(data.total) || 0;
      out.innerHTML = `<div class="table-wrap"><table class="data"><thead><tr><th>Дата</th><th>Номер</th>
        <th>Продавец</th><th class="num">Сумма с НДС</th><th class="num">НДС</th><th>Проверка</th><th></th></tr></thead>
        <tbody>${primRowsHtml(data.items)}</tbody></table></div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
          <button type="button" class="btn btn-sm" id="prim-prev" ${state.offset <= 0 ? 'disabled' : ''}>← Назад</button>
          <span class="form-hint">Показано ${Math.min(total, state.offset + 1)}–${Math.min(total, state.offset + state.limit)} из ${total}</span>
          <button type="button" class="btn btn-sm" id="prim-next" ${state.offset + state.limit >= total ? 'disabled' : ''}>Вперёд →</button>
        </div>`;
      out.querySelectorAll('.prim-open').forEach((b) => { b.onclick = () => openCard(b.dataset.id); });
      $('#prim-prev').onclick = () => { state.offset = Math.max(0, state.offset - state.limit); load(); };
      $('#prim-next').onclick = () => { state.offset += state.limit; load(); };
    } catch (e) {
      if (current !== req || !container.isConnected) return;
      out.innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }

  async function openCard(id) {
    let d;
    try { d = await api.get(`/api/v1/primary/${encodeURIComponent(id)}`); }
    catch (e) { toast(e.message, 'err'); return; }
    const { slot, close } = openModal(`
      <h3 class="modal-title">${esc(primTitle(d))}</h3>
      <p class="form-hint">Формат ${esc(d.format_version || '—')} · функция ${esc(d.function || '—')} · файл ${esc(d.file_name || '—')}</p>
      <div class="grid cards-2-even"><div><b>Продавец</b><div>${primParty(d.seller)}</div></div>
        <div><b>Покупатель</b><div>${primParty(d.buyer)}</div></div></div>
      <p>Без НДС: <b>${primMoney(d.total_without_vat)}</b> · НДС: <b>${primMoney(d.vat_total)}</b> ·
        с НДС: <b>${primMoney(d.total_with_vat)}</b> ${primStatusChip(d.status)}</p>
      <h4>Проверка</h4>${primIssuesHtml(d.issues)}
      <h4>Строки</h4>${primLinesHtml(d.lines)}
      <div class="modal-actions"><button type="button" class="btn btn-danger" id="prim-del">Удалить документ</button></div>`);
    if (slot) slot.classList.add('prim-modal');   // шире обычного окна: строки документа — 8 колонок
    const del = document.getElementById('prim-del');
    if (del) {
      del.onclick = async () => {
        if (!window.confirm('Удалить документ из реестра? Файл в системе ЭДО не затрагивается.')) return;
        try {
          await api.del(`/api/v1/primary/${encodeURIComponent(id)}`);
          toast('Документ удалён', 'ok');
          if (typeof close === 'function') close();
          load();
        } catch (e) { toast(e.message, 'err'); }
      };
    }
  }

  async function upload(files) {
    const list = [...(files || [])];
    if (!list.length) return;
    const form = new FormData();
    list.forEach((f) => form.append('files', f));
    const sel = $('#prim-company');
    if (sel && sel.value) form.append('company_id', sel.value);
    const box = $('#prim-results');
    box.innerHTML = '<div class="skeleton" style="height:60px"></div>';
    try {
      const res = await api.post('/api/v1/primary/upload', form);
      if (!container.isConnected) return;
      box.innerHTML = primResultsHtml(res);
      toast(`Первичка: ${primUploadSummary(res)}`, res.rejected && !res.created ? 'err' : 'ok');
      state.offset = 0;
      load();
    } catch (e) {
      box.innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }

  const drop = $('#prim-drop');
  const input = $('#prim-file');
  drop.onclick = () => input.click();
  drop.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } };
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('dragover'); };
  drop.ondragleave = () => drop.classList.remove('dragover');
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove('dragover'); upload(e.dataTransfer && e.dataTransfer.files); };
  input.onchange = () => { upload(input.files); input.value = ''; };
  $('#prim-refresh').onclick = () => load();
  $('#prim-status').onchange = (e) => { state.status = e.target.value; state.offset = 0; load(); };
  let timer = null;
  $('#prim-q').oninput = (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.q = e.target.value.trim(); state.offset = 0; load(); }, 300);
  };
  $('#prim-from').onchange = (e) => { state.from = e.target.value; state.offset = 0; load(); };
  $('#prim-to').onchange = (e) => { state.to = e.target.value; state.offset = 0; load(); };
  await load();
}
