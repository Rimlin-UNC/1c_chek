/* Отдельный read-only контур Supabase. Не трогает рабочую БД приложения. */
import { api } from './api.js';
import { esc, fmtInt, fmtSum } from './ui.js';

const STATUS = { new: 'Новый', verified: 'Проверен', exported: 'Выгружен' };

export function filterSnapshot(snapshot, filters = {}) {
  const rows = Array.isArray(snapshot?.receipts) ? snapshot.receipts : [];
  return rows.filter((r) => (!filters.company || r.company_id === filters.company)
    && (!filters.participant || (r.participant_id || '') === filters.participant)
    && (!filters.status || r.status === filters.status));
}

export function snapshotTotals(rows) {
  return { count: rows.length, sum: rows.reduce((acc, r) => acc + (Number(r.total) || 0), 0) };
}

async function loadSnapshot(config, fetchImpl = fetch) {
  const resp = await fetchImpl(`${config.url}/rest/v1/rpc/${config.rpc}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      apikey: config.publishable_key,
      Authorization: `Bearer ${config.publishable_key}`,
    },
    body: '{}',
  });
  if (!resp.ok) throw new Error(`Supabase ответил ${resp.status}`);
  const data = await resp.json();
  if (!data || data.mode !== 'read_only_demo' || !Array.isArray(data.receipts)) {
    throw new Error('Supabase вернул неожиданный формат демо-снимка');
  }
  return data;
}

// v1.70.87 (#25): alive() — проверка от route(): пока раздел на экране, true;
// после ухода пользователя поздние ответы не перерисовывают открытый раздел.
export async function viewSupabaseDemo(container, alive = () => true) {
  container.innerHTML = '<div class="glass card"><div class="skeleton" style="height:120px"></div></div>';
  let config;
  try { config = await api.get('/api/v1/supabase-demo/config'); }
  catch (e) {
    if (!alive()) return;
    container.innerHTML = `<div class="glass card"><div class="form-error">${esc(e.message)}</div></div>`;
    return;
  }
  if (!alive()) return;
  if (!config.enabled) {
    container.innerHTML = `<div class="glass card"><div class="card-title">Supabase демо</div>
      <p class="form-hint">${esc(config.error || 'Контур выключен. Задайте SUPABASE_DEMO_ENABLED, SUPABASE_URL и SUPABASE_PUBLISHABLE_KEY в .env.')}</p></div>`;
    return;
  }
  const filters = { company: '', participant: '', status: '' };
  let snapshot = null;
  let req = 0;

  async function refresh() {
    const current = ++req;
    const out = container.querySelector('#sb-demo-out');
    if (out) out.innerHTML = '<div class="skeleton" style="height:120px"></div>';
    try {
      const data = await loadSnapshot(config);
      if (current !== req || !container.isConnected || !alive()) return;
      snapshot = data;
      render();
    } catch (e) {
      if (current !== req || !container.isConnected || !alive()) return;
      if (out) out.innerHTML = `<div class="form-error">${esc(e.message)}. Рабочая база приложения не затронута.</div>`;
    }
  }

  function render() {
    const rows = filterSnapshot(snapshot, filters);
    const totals = snapshotTotals(rows);
    const companies = snapshot.companies || [];
    const participants = (snapshot.participants || []).filter((p) => !filters.company || p.company_id === filters.company);
    const itemsByReceipt = new Map();
    (snapshot.items || []).forEach((i) => {
      if (!itemsByReceipt.has(i.receipt_id)) itemsByReceipt.set(i.receipt_id, []);
      itemsByReceipt.get(i.receipt_id).push(i);
    });
    container.innerHTML = `
      <div class="glass card">
        <div class="card-title">☁️ Supabase демо <span class="spacer"></span>
          <span class="chip">read-only</span>
          <button type="button" class="btn btn-sm" id="sb-demo-refresh">Обновить</button></div>
        <p class="form-hint">Синтетические данные читаются из Supabase REST. Это отдельный демонстрационный контур:
          рабочая база, баллы, согласия и переносы приложения не меняются. Publishable key не даёт запись в базовые таблицы.</p>
        <div class="grid kpi-grid">
          <div class="glass kpi"><div class="kpi-value">${fmtInt(totals.count)}</div><div class="kpi-sub">чеков по фильтрам</div></div>
          <div class="glass kpi"><div class="kpi-value">${fmtSum(totals.sum)}</div><div class="kpi-sub">сумма по фильтрам</div></div>
          <div class="glass kpi"><div class="kpi-value">${fmtInt(companies.length)}</div><div class="kpi-sub">демо-компаний</div></div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin:10px 0">
          <label class="field"><span>Компания</span><select id="sb-demo-company"><option value="">Все компании</option>
            ${companies.map((c) => `<option value="${esc(c.id)}" ${filters.company === c.id ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}</select></label>
          <label class="field"><span>Участник</span><select id="sb-demo-participant"><option value="">Все участники</option>
            ${participants.map((p) => `<option value="${esc(p.id)}" ${filters.participant === p.id ? 'selected' : ''}>${esc(p.label)}</option>`).join('')}</select></label>
          <label class="field"><span>Статус</span><select id="sb-demo-status"><option value="">Все статусы</option>
            ${Object.entries(STATUS).map(([k, v]) => `<option value="${k}" ${filters.status === k ? 'selected' : ''}>${v}</option>`).join('')}</select></label>
          <button type="button" class="btn btn-sm" id="sb-demo-reset">Сбросить</button>
        </div>
        <div id="sb-demo-out"></div>
      </div>`;
    const out = container.querySelector('#sb-demo-out');
    out.innerHTML = `<div class="table-wrap"><table class="data"><thead><tr>
      <th>№</th><th>Дата</th><th>Компания</th><th>Участник</th><th>Магазин</th><th>Сумма</th><th>Статус</th><th>Позиции</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td>${esc(r.receipt_no)}</td><td>${esc(r.receipt_date)}</td><td>${esc(r.company_name)}</td>
        <td>${esc(r.participant_label || 'Без участника')}</td><td>${esc(r.merchant_name)}</td><td class="num">${fmtSum(r.total)}</td>
        <td>${esc(STATUS[r.status] || r.status)}</td><td>${esc((itemsByReceipt.get(r.id) || []).map((i) => `${i.name} × ${i.quantity}`).join('; '))}</td></tr>`).join('')
        || '<tr><td colspan="8">Нет чеков по выбранным условиям.</td></tr>'}</tbody></table></div>
      <p class="form-hint">Снимок: ${esc(snapshot.generated_at || '')}. Источник: ${esc(config.url)}.</p>`;
    container.querySelector('#sb-demo-refresh').onclick = refresh;
    container.querySelector('#sb-demo-company').onchange = (e) => { filters.company = e.target.value; filters.participant = ''; render(); };
    container.querySelector('#sb-demo-participant').onchange = (e) => { filters.participant = e.target.value; render(); };
    container.querySelector('#sb-demo-status').onchange = (e) => { filters.status = e.target.value; render(); };
    container.querySelector('#sb-demo-reset').onclick = () => { filters.company = ''; filters.participant = ''; filters.status = ''; render(); };
  }

  container.innerHTML = '<div class="glass card"><div id="sb-demo-out"><div class="skeleton" style="height:120px"></div></div></div>';
  await refresh();
}
