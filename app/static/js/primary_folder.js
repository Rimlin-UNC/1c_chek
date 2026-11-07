/* v1.70.113 (T-79, #63, ADR-43): «📁 Папка с УПД» — пакетная загрузка электронных УПД из папки с подпапками.
   Браузер сам обходит папку (выбор кнопкой или перетаскивание), по первым 8 КБ каждого XML отбирает УПД и
   счета-фактуры продавца по признакам формата ФНС (корень «Файл», атрибут «Функция» СЧФ, СЧФДОП или ДОП) и
   пропускает служебные файлы ЭДО, титулы покупателя, корректировочные документы и чужие XML — со счётчиками.
   Сомнительное (XML формата ФНС без «Функции» в начале файла) отправляется: решит сервер. Затем браузер считает
   SHA-256 и спрашивает сервер, что уже загружено, — повторная загрузка той же папки не гоняет файлы заново, — и
   отправляет остальное порциями с ходом загрузки. Компанию каждого документа сервер определяет по ИНН:
   покупатель — расход, продавец — продажа. В конце — пересчёт сверки с выпиской и итог по компаниям.
   Имена верхнего уровня — с приставкой primFolder (общая область модулей в сборке). */
import { api } from './api.js';
import { esc, fmtSum } from './ui.js';

export const PRIM_FOLDER_KINDS = {
  upd: 'УПД и счета-фактуры', maybe: 'XML формата ФНС — проверит сервер', zip: 'архивы ЭДО',
  buyer: 'титулы покупателя', correction: 'корректировочные документы', edo: 'служебные файлы ЭДО',
  other: 'другие XML', notxml: 'не XML (подписи, PDF и прочее)',
};
export const PRIM_FOLDER_SEND = ['upd', 'maybe', 'zip'];
const PRIM_FOLDER_HEAD = 8192;
const PRIM_FOLDER_CHUNK = 20;
const PRIM_FOLDER_CHUNK_BYTES = 8 * 1024 * 1024;
const PRIM_FOLDER_HASH_BATCH = 500;
const PRIM_FOLDER_FUNCTIONS = ['СЧФ', 'СЧФДОП', 'ДОП'];

/** Путь файла в папке: при выборе кнопкой — webkitRelativePath, при перетаскивании — relativePath. */
export function primFolderPath(file) {
  return (file && (file.webkitRelativePath || file.relativePath || file.name)) || '';
}

/** Первые байты XML → текст: кодировка из объявления <?xml … encoding="…"?> (ФНС — windows-1251), иначе UTF-8. */
export function primFolderDecode(bytes) {
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || []);
  let declaration = '';
  for (let i = 0; i < Math.min(u8.length, 200); i += 1) declaration += String.fromCharCode(u8[i]);
  const m = /encoding\s*=\s*["']([\w.-]+)["']/i.exec(declaration);
  let enc = m ? m[1].toLowerCase() : 'utf-8';
  if (enc === 'cp1251' || enc === 'win-1251') enc = 'windows-1251';
  try {
    return new TextDecoder(enc).decode(u8);
  } catch (e) {
    return new TextDecoder('utf-8').decode(u8);
  }
}

/** Вид файла по пути и началу текста: upd | maybe | zip | buyer | correction | edo | other | notxml. */
export function primFolderKind(path, head) {
  const base = String(path || '').split(/[\\/]/).pop();
  if (/\.zip$/i.test(base)) return 'zip';
  if (!/\.xml$/i.test(base)) return 'notxml';
  const byName = (id) => {
    const up = String(id || '').toUpperCase();
    if (/^ON_N?SCHFDOPPOK|^ON_N?KORSCHFDOPPOK/.test(up)) return 'buyer';
    if (/^ON_N?KORSCHFDOPPR/.test(up)) return 'correction';
    if (/^DP_/.test(up)) return 'edo';
    return '';
  };
  const named = byName(base);
  if (named) return named;
  const text = String(head || '');
  const fromId = byName((/ИдФайл\s*=\s*"([^"]+)"/.exec(text) || [])[1]);
  if (fromId) return fromId;
  if (!/<Файл[\s>]/.test(text)) return 'other';
  const fn = (/Функция\s*=\s*"([^"]*)"/.exec(text) || [])[1];
  if (fn === undefined) return 'maybe';
  if (PRIM_FOLDER_FUNCTIONS.includes(fn)) return 'upd';
  return /^К|ДИС/.test(fn) ? 'correction' : 'other';
}

/** Порции для отправки: до 20 файлов и 8 МБ; архив — всегда отдельно (в нём до 50 документов). */
export function primFolderChunks(files, maxCount = PRIM_FOLDER_CHUNK, maxBytes = PRIM_FOLDER_CHUNK_BYTES) {
  const chunks = [];
  let cur = [];
  let size = 0;
  for (const f of files || []) {
    if (/\.zip$/i.test(primFolderPath(f))) {
      if (cur.length) { chunks.push(cur); cur = []; size = 0; }
      chunks.push([f]);
      continue;
    }
    if (cur.length && (cur.length >= maxCount || size + (f.size || 0) > maxBytes)) {
      chunks.push(cur);
      cur = [];
      size = 0;
    }
    cur.push(f);
    size += f.size || 0;
  }
  if (cur.length) chunks.push(cur);
  return chunks;
}

/** Обход перетащенной папки (DataTransfer): файлы с путями; null — перетащили не папку. */
export function primFolderFromDrop(dt) {
  const entries = [...((dt && dt.items) || [])]
    .map((it) => (it && typeof it.webkitGetAsEntry === 'function' ? it.webkitGetAsEntry() : null)).filter(Boolean);
  if (!entries.some((e) => e.isDirectory)) return null;     // записи берутся сразу — после события их не будет
  const files = [];
  const walk = async (entry, path) => {
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
      try { Object.defineProperty(file, 'relativePath', { value: path + file.name }); } catch (e) { /* только имя */ }
      files.push(file);
      return;
    }
    const reader = entry.createReader();
    for (;;) {
      const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
      if (!batch.length) break;
      for (const child of batch) await walk(child, `${path}${entry.name}/`);
    }
  };
  return (async () => {
    for (const e of entries) await walk(e, '');
    return files;
  })();
}

/** Байты файла или его части: Blob.arrayBuffer, а где его нет (старые браузеры) — FileReader. */
export function primFolderBytes(blob) {
  if (typeof blob.arrayBuffer === 'function') return blob.arrayBuffer();
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(blob);
  });
}

async function primFolderHead(file) {
  return primFolderDecode(new Uint8Array(await primFolderBytes(file.slice(0, PRIM_FOLDER_HEAD))));
}

/** SHA-256 файла (hex); без защищённого соединения (crypto.subtle) — пусто, тогда проверит сам сервер. */
export async function primFolderSha(file) {
  const subtle = globalThis.crypto && globalThis.crypto.subtle;
  if (!subtle) return '';
  const digest = await subtle.digest('SHA-256', await primFolderBytes(file));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

/** Отбор файлов папки: { send: [файлы к отправке], counts: {вид: число}, total }. */
export async function primFolderScan(files, onProgress = () => {}) {
  const counts = {};
  const send = [];
  const list = [...(files || [])];
  for (let i = 0; i < list.length; i += 1) {
    const f = list[i];
    const path = primFolderPath(f);
    const kind = /\.xml$/i.test(path) ? primFolderKind(path, await primFolderHead(f)) : primFolderKind(path, '');
    counts[kind] = (counts[kind] || 0) + 1;
    if (PRIM_FOLDER_SEND.includes(kind)) send.push(f);
    if (i % 50 === 0) onProgress(`Просмотрено файлов: ${i + 1} из ${list.length}`);
  }
  return { send, counts, total: list.length };
}

/** Сложить ответ порции в общий итог. */
export function primFolderMerge(acc, res) {
  const out = acc || { created: 0, duplicates: 0, rejected: 0, companies: {}, problems: [] };
  out.created += res.created || 0;
  out.duplicates += res.duplicates || 0;
  out.rejected += res.rejected || 0;
  for (const c of res.companies || []) {
    const cur = out.companies[c.company_id] || { company_id: c.company_id, name: c.name, in: 0, out: 0, duplicates: 0 };
    cur.in += c.in || 0;
    cur.out += c.out || 0;
    cur.duplicates += c.duplicates || 0;
    out.companies[c.company_id] = cur;
  }
  for (const r of res.results || []) {
    if (r.result === 'rejected' && out.problems.length < 200) out.problems.push({ file: r.file, message: r.message });
  }
  return out;
}

/** Вся загрузка: отбор → что уже есть (SHA-256) → порции → пересчёт сверки. Возвращает итог для экрана. */
export async function primFolderUpload(files, opts = {}) {
  const say = typeof opts.onProgress === 'function' ? opts.onProgress : () => {};
  const scan = await primFolderScan(files, say);
  let send = scan.send;
  let known = 0;
  const xml = send.filter((f) => !/\.zip$/i.test(primFolderPath(f)));
  if (xml.length) {
    say('Проверяем, что уже загружено…');
    const hashes = new Map();
    for (const f of xml) {
      const sha = await primFolderSha(f);
      if (!sha) break;                                      // нет crypto.subtle — проверит сервер при загрузке
      hashes.set(f, sha);
    }
    if (hashes.size === xml.length) {
      const all = [...hashes.values()];
      const seen = new Set();
      for (let i = 0; i < all.length; i += PRIM_FOLDER_HASH_BATCH) {
        const res = await api.post('/api/v1/primary/known', { hashes: all.slice(i, i + PRIM_FOLDER_HASH_BATCH) });
        (res.known || []).forEach((h) => seen.add(h));
      }
      send = send.filter((f) => !seen.has(hashes.get(f)));
      known = xml.length - send.filter((f) => hashes.has(f)).length;
    }
  }
  let total = null;
  const chunks = primFolderChunks(send);
  let done = 0;
  for (const chunk of chunks) {
    const form = new FormData();
    chunk.forEach((f) => form.append('files', f, primFolderPath(f)));
    form.append('route', 'auto');
    say(`Загружаем: ${done} из ${send.length}`);
    total = primFolderMerge(total, await api.post('/api/v1/primary/upload', form));
    done += chunk.length;
  }
  total = total || primFolderMerge(null, {});
  const ids = Object.keys(total.companies);
  let settle = [];
  if (ids.length) {
    say('Сверяем с банковской выпиской…');
    try { settle = (await api.post('/api/v1/bank/closing/settle', { company_ids: ids })).companies || []; } catch (e) { settle = []; }
  }
  return { ...total, scan: { counts: scan.counts, total: scan.total }, known, sent: send.length, settle };
}

function primFolderSide(s, title) {
  if (!s || !s.count) return '';
  return `${esc(title)}: закрыто ${esc(fmtSum(s.closed_amount))} из ${esc(fmtSum(s.amount))} (${esc(String(s.percent).replace('.', ','))} %)`;
}

/** Итог загрузки папки для экрана «Первичка». */
export function primFolderSummaryHtml(sum) {
  const counts = (sum.scan && sum.scan.counts) || {};
  const skipped = Object.entries(counts).filter(([k]) => !PRIM_FOLDER_SEND.includes(k))
    .map(([k, n]) => `${esc(PRIM_FOLDER_KINDS[k] || k)}: ${n}`).join(' · ');
  const companies = Object.values(sum.companies || {});
  const settle = Object.fromEntries((sum.settle || []).map((c) => [c.company_id, c]));
  const rows = companies.map((c) => {
    const st = settle[c.company_id];
    const closing = st ? [primFolderSide(st.expense, 'расходы'), primFolderSide(st.income, 'поступления')]
      .filter(Boolean).join('; ') : '';
    return `<tr><td>${esc(c.name)}</td><td class="num">${c.in}</td><td class="num">${c.out}</td>
      <td class="num">${c.duplicates}</td><td>${closing || '<span class="form-hint">выписки нет</span>'}</td></tr>`;
  }).join('');
  const problems = (sum.problems || []).map((p) => `<li><b>${esc(p.file)}</b> — ${esc(p.message)}</li>`).join('');
  return `<div class="prim-folder-sum">
    <p><b>Папка: файлов ${esc(String((sum.scan && sum.scan.total) || 0))}.</b> УПД загружено: ${sum.created},
      уже были: ${sum.duplicates + (sum.known || 0)}, не принято: ${sum.rejected}.</p>
    ${skipped ? `<p class="form-hint">Пропущены: ${skipped}.</p>` : ''}
    ${rows ? `<div class="table-wrap"><table class="data"><thead><tr><th>Компания</th><th class="num">Расходы</th>
      <th class="num">Продажи</th><th class="num">Уже были</th><th>Сверка с выпиской</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="form-hint">Подробно — «Банк: выписка» → «Закрытие документами».</p>` : ''}
    ${problems ? `<details><summary>Не принятые файлы (${sum.problems.length})</summary><ul class="prim-folder-problems">${problems}</ul></details>` : ''}
  </div>`;
}
