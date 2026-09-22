// ======================================================================
// Ямастер Чек — API-клиент
// Разработчик и владелец идеи: ООО «Ямастер»
// Сайт: https://ymaster.ru | E-mail: info@ymaster.ru
// ======================================================================

const TOKEN_KEY = 'ymaster_check_token';

export function getToken() { return localStorage.getItem(TOKEN_KEY) || ''; }
export function setToken(t) { localStorage.setItem(TOKEN_KEY, t); }
export function clearToken() { localStorage.removeItem(TOKEN_KEY); }

export class ApiError extends Error {
  constructor(status, message, data) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

async function request(method, path, body, options = {}) {
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
    throw new ApiError(0, 'Нет связи с сервером. Проверьте подключение — сканы сохраняются локально.', null);
  }
  if (resp.status === 401 && !path.includes('/auth/')) {
    clearToken();
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

export const api = {
  get: (p) => request('GET', p),
  post: (p, b) => request('POST', p, b),
  put: (p, b) => request('PUT', p, b),
  patch: (p, b) => request('PATCH', p, b),
  del: (p) => request('DELETE', p),
  download: async (p, b) => {
    const headers = { 'Authorization': 'Bearer ' + getToken(), 'Content-Type': 'application/json' };
    const resp = await fetch(p, { method: 'POST', headers, body: JSON.stringify(b || {}) });
    if (!resp.ok) {
      let msg = 'Ошибка ' + resp.status;
      try { const j = await resp.json(); msg = j.detail || msg; } catch {}
      throw new ApiError(resp.status, msg);
    }
    const blob = await resp.blob();
    const cd = resp.headers.get('content-disposition') || '';
    const m = cd.match(/filename="?([^";]+)"?/);
    return { blob, filename: m ? m[1] : 'export.json' };
  },
};
