// ======================================================================
// Ямастер Чек — контрагенты (T-80, #65, v1.71.0, ADR-48).
// Машина предлагает — человек подтверждает: кандидаты из УПД и выписки
// остаются proposals, а блоки копируются только после проверки глазами.
// ======================================================================
import { api } from './api.js';
import { esc } from './ui.js';

export const COUNTERPARTY_BLOCKS = [
  'Реквизиты стороны', 'Преамбула договора', 'Для платёжного поручения',
];

export function counterpartyCreateBody(form) {
  return {
    inn: String(form.inn || '').trim(),
    name: String(form.name || '').trim(),
    kpp: String(form.kpp || '').trim(),
    company_id: form.companyId || undefined,
  };
}

export function counterpartyAccountBody(form) {
  return {
    account: String(form.account || '').trim(),
    bik: String(form.bik || '').trim(),
    company_id: form.companyId || undefined,
  };
}

export function counterpartyPatchBody(form) {
  return {
    signatory_post: String(form.signatoryPost || '').trim(),
    signatory_name: String(form.signatoryName || '').trim(),
    signatory_basis: String(form.signatoryBasis || '').trim(),
    genitive: String(form.genitive || '').trim(),
    company_id: form.companyId || undefined,
  };
}

export function counterpartyCardHtml(card) {
  const risk = card.risk || { level: 'none', reasons: [] };
  const reasons = (risk.reasons || []).map(r => `<li>${esc(r)}</li>`).join('');
  const blocks = COUNTERPARTY_BLOCKS.map((name) => {
    const text = (card.blocks || {})[name] || '';
    return `<section class="cp-block">
      <h3>${esc(name)}</h3>
      <pre>${esc(text)}</pre>
      <button type="button" class="btn btn-sm" data-copy="${esc(name)}">Скопировать</button>
    </section>`;
  }).join('');
  return `<div class="cp-card" data-id="${esc(card.id || '')}">
    <div class="cp-risk cp-risk-${esc(risk.level || 'none')}">
      <strong>Проверка надёжности: ${esc(risk.level || 'нет данных')}</strong>
      <ul>${reasons || '<li>Данных ЕГРЮЛ пока нет</li>'}</ul>
    </div>
    <div class="cp-grid">
      <div><b>${esc(card.name_full || '')}</b><br>ИНН ${esc(card.inn || '—')} · КПП ${esc(card.kpp || '—')}</div>
      <div>${esc(card.address || '')}</div>
    </div>
    <div class="cp-aliases">${(card.aliases || []).map(a =>
      `<span class="chip">${esc(a.name || '')}</span>`).join('')}</div>
    ${blocks}
    <p class="cp-note">Проверьте реквизиты и падеж перед вставкой: машина готовит черновик.</p>
    <p class="cp-note">проверьте перед вставкой</p>
  </div>`;
}

export async function loadCounterparties(companyId, search = '') {
  const params = new URLSearchParams();
  if (companyId) params.set('company_id', companyId);
  if (search) params.set('search', search);
  return api.get(`/api/v1/counterparties?${params.toString()}`);
}

export async function loadCandidates(companyId) {
  const params = new URLSearchParams();
  if (companyId) params.set('company_id', companyId);
  return api.get(`/api/v1/counterparties/candidates?${params.toString()}`);
}

export async function createCounterparty(body) {
  return api.post('/api/v1/counterparties', body);
}

export async function openCounterparty(cardId, companyId) {
  const params = new URLSearchParams();
  if (companyId) params.set('company_id', companyId);
  return api.get(`/api/v1/counterparties/${encodeURIComponent(cardId)}?${params.toString()}`);
}

export async function patchCounterparty(cardId, body) {
  return api.patch(`/api/v1/counterparties/${encodeURIComponent(cardId)}`, body);
}

export async function verifyCounterparty(cardId, body) {
  return api.post(`/api/v1/counterparties/${encodeURIComponent(cardId)}/verify`, body);
}

export async function addCounterpartyAccount(cardId, body) {
  return api.post(`/api/v1/counterparties/${encodeURIComponent(cardId)}/accounts`, body);
}

export async function mergeCounterparty(cardId, body) {
  return api.post(`/api/v1/counterparties/${encodeURIComponent(cardId)}/merge`, body);
}
