// ======================================================================
// Ямастер Чек — API-клиент
// Разработчик и владелец идеи: ООО «Ямастер»
// Сайт: https://ymaster.ru | E-mail: info@ymaster.ru
//
// Надёжность: сетевые сбои (просыпающийся прокси/офлайн) автоматически
// повторяются с нарастающей паузой; токен НЕ стирается при сбоях сети —
// только при настоящем 401 от сервера.
// ======================================================================

const TOKEN_KEY = 'ymaster_check_token';

export function getToken() { return localStorage.getItem(TOKEN_KEY) || ''; }
export function setToken(t) { localStorage.setItem(TOKEN_KEY, t); }
export function clearToken() { localStorage.removeItem(TOKEN_KEY); }

export class ApiError extends Error {
  constructor(status, message, data) {
    super(message);
    this.status = status;         // 0 = сеть недоступна
    this.data = data;
  }
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function once(method, path, body, options = {}) {
  const headers = {};
  const token = getToken();
  if (token) headers['Authorization'] = 'Bearer ' + token;
  let payload;
  if (body instanceof FormData) {
    payload = body;
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(path, { method, headers, body: payload, signal: options.signal });
  } catch (e) {
    // Сеть недоступна (прокси просыпается, офлайн и т.п.)
    throw new ApiError(0, 'Нет связи с сервером', null);
  }
  if (resp.status === 401 && !path.includes('/auth/login') && !path.includes('/auth/register')) {
    clearToken();
    // best-effort очистка серверной cookie сессии
    try { fetch('/api/v1/auth/logout', { method: 'POST' }); } catch {}
    window.dispatchEvent(new CustomEvent('ymaster:logout'));
    throw new ApiError(401, 'Сессия истекла, войдите заново');
  }
  if (!resp.ok) {
    let msg = 'Ошибка ' + resp.status;
    let data = null;
    try {
      data = await resp.json();
      msg = typeof data.detail === 'string' ? data.detail
        : (data.detail?.message || JSON.stringify(data.detail || data));
    } catch { /* не JSON */ }
    throw new ApiError(resp.status, msg, data);
  }
  const ct = resp.headers.get('content-type') || '';
  return ct.includes('json') ? resp.json() : resp.text();
}

/** Запрос с автоматическим повтором при сбоях сети (status 0). */
async function request(method, path, body, options = {}) {
  const attempts = options.retries !== undefined ? options.retries : 3;
  let lastErr;
  for (let i = 0; i < attempts; i++) {
    try {
      return await once(method, path, body, options);
    } catch (e) {
      lastErr = e;
      // Повторяем только сетевые сбои и 502/503/504 (просыпающийся сервер)
      const retryable = e.status === 0 || e.status === 502 || e.status === 503 || e.status === 504;
      if (!retryable || i === attempts - 1) throw e;
      await sleep(350 * (i + 1));
    }
  }
  throw lastErr;
}

export const api = {
  get: (p, o) => request('GET', p, undefined, o),
  post: (p, b, o) => request('POST', p, b, o),
  put: (p, b, o) => request('PUT', p, b, o),
  patch: (p, b, o) => request('PATCH', p, b, o),
  del: (p, o) => request('DELETE', p, undefined, o),
  download: async (p, b) => {
    const headers = { 'Authorization': 'Bearer ' + getToken(), 'Content-Type': 'application/json' };
    const resp = await fetch(p, { method: 'POST', headers, body: JSON.stringify(b || {}) });
    if (!resp.ok) {
      let msg = 'Ошибка ' + resp.status;
      try { const j = await resp.json(); msg = typeof j.detail === 'string' ? j.detail : msg; } catch {}
      throw new ApiError(resp.status, msg);
    }
    const blob = await resp.blob();
    const cd = resp.headers.get('content-disposition') || '';
    const m = cd.match(/filename="?([^";]+)"?/);
    return { blob, filename: m ? m[1] : 'export.json' };
  },
};
