// ======================================================================
// Ямастер Чек — UI-хелперы: тосты, модалки, форматирование
// Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
// ======================================================================

// --- Тосты -------------------------------------------------------------
export function toast(message, kind = 'ok', title = '') {
  const box = document.getElementById('toast-container');
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  const ico = { ok: '✓', err: '✕', warn: '⚠', info: 'ℹ' }[kind] || 'ℹ';
  el.innerHTML = `<div class="t-ico">${ico}</div><div>${title ? `<b>${esc(title)}</b>` : ''}${esc(message)}</div>`;
  box.appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 260);
  }, kind === 'err' ? 6000 : 3800);
}

// --- Экранирование --------------------------------------------------------
export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// --- Форматирование --------------------------------------------------------
export function fmtSum(v) {
  return new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    .format(v || 0) + ' ₽';
}
export function fmtInt(v) { return new Intl.NumberFormat('ru-RU').format(v || 0); }
export function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: '2-digit',
    hour: '2-digit', minute: '2-digit' });
}

const STATUS_LABELS = {
  new: 'Новый', verifying: 'Проверяется', verified: 'Проверен', failed: 'Отклонён ФНС',
  valid: 'Действителен', invalid: 'Недействителен', not_found: 'Не найден в ФНС',
  unknown: 'Не проверен', exported: 'Выгружен в 1С',
};
export function statusLabel(s) { return STATUS_LABELS[s] || s || '—'; }

export const ROLE_LABELS = {
  admin: 'Администратор', accountant: 'Бухгалтер', user: 'Пользователь',
};
export function roleLabel(r) { return ROLE_LABELS[r] || r || '—'; }

export function chip(status, extra = '') {
  return `<span class="chip ${esc(status)} ${extra}"><span class="dot"></span>${esc(statusLabel(status))}</span>`;
}

// --- Модалки ------------------------------------------------------------------
const modalRoot = () => document.getElementById('modal-root');
export function openModal(html, { onClose } = {}) {
  const root = modalRoot();
  root.classList.remove('hidden');
  const slot = root.querySelector('.modal-slot');
  slot.className = 'modal-slot glass';
  slot.innerHTML = `<button class="btn-icon modal-close" title="Закрыть">✕</button>` + html;
  const close = () => { root.classList.add('hidden'); slot.innerHTML = ''; onClose && onClose(); };
  root.querySelector('.modal-backdrop').onclick = close;
  slot.querySelector('.modal-close').onclick = close;
  document.addEventListener('keydown', function onesc(e) {
    if (e.key === 'Escape') { close(); document.removeEventListener('keydown', onesc); }
  });
  return { slot, close };
}

// --- Анимированный счётчик --------------------------------------------------------
export function animateNumber(el, target, formatter = fmtInt, duration = 700) {
  if (!el) return;
  const start = performance.now();
  const from = 0;
  function frame(t) {
    const p = Math.min(1, (t - start) / duration);
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = formatter(from + (target - from) * eased);
    if (p < 1) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
}

// --- Debounce ------------------------------------------------------------------------
export function debounce(fn, ms = 350) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
