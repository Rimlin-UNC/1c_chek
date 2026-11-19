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
  hidden: 'скрытые и служебные файлы', empty: 'пустые файлы', too_big: 'XML больше 5 МБ',
};
export const PRIM_FOLDER_SEND = ['upd', 'maybe', 'zip'];
const PRIM_FOLDER_HEAD = 8192;
const PRIM_FOLDER_CHUNK = 20;
const PRIM_FOLDER_CHUNK_BYTES = 8 * 1024 * 1024;
const PRIM_FOLDER_HASH_BATCH = 500;
const PRIM_FOLDER_FUNCTIONS = ['СЧФ', 'СЧФДОП', 'ДОП'];
const PRIM_FOLDER_XML_MAX = 5 * 1024 * 1024;          // app/primary/upd.py: MAX_BYTES — больше не один документ
const PRIM_FOLDER_ZIP_MAX = 20 * 1024 * 1024;         // router.py: MAX_ZIP_BYTES
const PRIM_FOLDER_ZIP_DOCS = 50;                      // service.py: MAX_FILES — предел документов из архива
const PRIM_FOLDER_SERVICE = new Set(['.ds_store', 'thumbs.db', 'desktop.ini']);

/** Путь файла в папке: при выборе кнопкой — webkitRelativePath, при перетаскивании — relativePath. */
export function primFolderPath(file) {
  return (file && (file.webkitRelativePath || file.relativePath || file.name)) || '';
}

/** Первые байты XML → текст: кодировка из объявления <?xml … encoding="…"?> (ФНС — windows-1251), иначе UTF-8. */
export function primFolderDecode(bytes) {
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes || []);
  // v1.73.4: BOM важнее объявления — выгрузки ЭДО бывают в UTF-16 (тогда «<Файл» и «ИдФайл»
  // в тексте не находятся, и УПД пропадал из папки как «другой XML»).
  if (u8.length >= 2 && u8[0] === 0xff && u8[1] === 0xfe) {
    try { return new TextDecoder('utf-16le').decode(u8); } catch { /* ниже общий путь */ }
  }
  if (u8.length >= 2 && u8[0] === 0xfe && u8[1] === 0xff) {
    try { return new TextDecoder('utf-16be').decode(u8); } catch { /* ниже общий путь */ }
  }
  let declaration = '';
  for (let i = 0; i < Math.min(u8.length, 200); i += 1) declaration += String.fromCharCode(u8[i]);
  const m = /encoding\s*=\s*["']([\w.-]+)["']/i.exec(declaration);
  if (m) {
    let enc = m[1].toLowerCase();
    if (enc === 'cp1251' || enc === 'win-1251') enc = 'windows-1251';
    try {
      return new TextDecoder(enc).decode(u8);
    } catch {
      /* кодировка из объявления неизвестна браузеру — ниже общий путь */
    }
  }
  // v1.73.4: без объявления кодировки XML ФНС почти всегда windows-1251. Раньше такой файл читался
  // как UTF-8 с «кракозябрами», атрибут «ИдФайл» не находился, и УПД пропадал из папки как «другой XML».
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(u8);
  } catch {
    try {
      return new TextDecoder('windows-1251').decode(u8);
    } catch {
      return new TextDecoder('utf-8').decode(u8);
    }
  }
}

/** Идентификатор документа ФНС — УПД/счёт-фактура продавца: «ON_NSCHFDOPPR_…» (ИдФайл или имя файла).
    Именно этот признак владелец назвал надёжным: значение ИдФайл, начинающееся с ON_NSCHFDOPPR, — это и есть УПД.
    Маркер ищется в любом месте строки: выгрузки ЭДО зовут файлы и «ON_NSCHFDOPPR_…», и
    «1_ON_NSCHFDOPPR_.xml» — с приставкой оператора, номера загрузки или папки. */
export const PRIM_FOLDER_UPD_MARK = 'ON_NSCHFDOPPR';

/** Атрибут XML из первых байтов: терпит и «ИдФайл="…"», и «ИдФайл='…'», и пробелы вокруг «=». */
export function primFolderAttr(text, name) {
  const re = new RegExp(`${name}\\s*=\\s*["']([^"']+)["']`);
  const found = re.exec(String(text || ''));
  return found ? found[1] : '';
}

/** Вид файла по пути и началу текста: upd | maybe | zip | buyer | correction | edo | other | notxml. */
export function primFolderKind(path, head) {
  const base = String(path || '').split(/[\\/]/).pop();
  if (/\.zip$/i.test(base)) return 'zip';
  if (!/\.xml$/i.test(base)) return 'notxml';
  // v1.73.4: служебные и чужие документы узнаются по началу идентификатора, УПД — положительным
  // признаком «ON_NSCHFDOPPR» (в ИдФайл или в имени файла), а не только по атрибуту «Функция»:
  // без «Функции» в первых 8 КБ файл уходил в «maybe», а при неверной кодировке — и вовсе в «other».
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
  const fileId = primFolderAttr(text, 'ИдФайл');
  const fromId = byName(fileId);
  if (fromId) return fromId;
  const upperId = String(fileId || base).toUpperCase();
  if (upperId.includes(PRIM_FOLDER_UPD_MARK)) return 'upd';
  if (!/<Файл[\s>]/.test(text)) return 'other';
  const fn = primFolderAttr(text, 'Функция');
  if (!fn) return 'maybe';
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
      try { Object.defineProperty(file, 'relativePath', { value: path + file.name }); } catch { /* только имя */ }
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
export async function primFolderBytes(blob) {
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

/** Имя файла внутри архива: флаг UTF-8 (бит 11) важнее правила XML — иначе кириллица превращается в кашу. */
export function primFolderEntryName(nameBytes, view, headerAt) {
  const flags = view ? view.getUint16(headerAt + 8, true) : 0;
  if (flags & 0x0800) {
    try { return new TextDecoder('utf-8').decode(nameBytes); } catch { /* ниже общий путь */ }
  }
  const latin = new TextDecoder('utf-8').decode(nameBytes);
  if (!latin.includes('\ufffd')) return latin;                       // уже валидный UTF-8
  try { return new TextDecoder('windows-1251').decode(nameBytes); } catch {  return latin; }
}

/** Минимальный читатель ZIP на клиенте: центральный каталог → имена .xml и их смещения.
    Полноценная распаковка здесь не нужна: нужен только перечень документов, чтобы отправить их по одному
    (сервер иначе отказывает всей пачке, если в архиве больше 50 документов, — см. service.unpack).
    Метод «сохранённый» (stored) отдаём как есть, сжатые — через DecompressionStream, если он есть в браузере. */
async function primFolderZipCentral(file) {
  const bytes = new Uint8Array(await primFolderBytes(file));
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  // конец центрального каталога: сигнатура 0x06054b50, ищем с конца (комментарий до 64 КБ)
  let eocd = -1;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65558); i -= 1) {
    if (view.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) return null;                                  // не архив или обрезан
  const total = view.getUint16(eocd + 10, true);
  let offset = view.getUint32(eocd + 16, true);
  const entries = [];
  for (let n = 0; n < total && offset + 46 <= bytes.length; n += 1) {
    if (view.getUint32(offset, true) !== 0x02014b50) return null;
    const method = view.getUint16(offset + 10, true);
    const compSize = view.getUint32(offset + 20, true);
    const rawSize = view.getUint32(offset + 24, true);
    const nameLen = view.getUint16(offset + 28, true);
    const extraLen = view.getUint16(offset + 30, true);
    const commentLen = view.getUint16(offset + 32, true);
    const localOffset = view.getUint32(offset + 42, true);
    const name = primFolderEntryName(bytes.subarray(offset + 46, offset + 46 + nameLen), view, offset);
    offset += 46 + nameLen + extraLen + commentLen;
    if (!name || name.endsWith('/') || name.startsWith('__MACOSX/')) continue;
    if (!/\.xml$/i.test(name)) continue;
    if (rawSize > PRIM_FOLDER_XML_MAX) continue;              // такой документ сервер всё равно отклонит
    entries.push({ name, method, compSize, rawSize, localOffset });
  }
  return entries;
}

/** Документы архива как отдельные файлы для отправки; null — архив не читается. */
export async function primFolderZipList(file) {
  const entries = await primFolderZipCentral(file);
  if (!entries) return null;
  const out = [];
  for (const entry of entries) {
    const doc = await primFolderZipDoc(file, entry);
    if (doc && typeof doc === 'string') return { error: doc, docs: out };   // не молчим: причина словами
    if (doc) out.push({ file: doc, path: `${primFolderPath(file)}/${entry.name}`, name: entry.name });
  }
  return { docs: out };
}

/** Deflate без zlib-заголовка (то, что лежит в ZIP) → ArrayBuffer. */
async function primFolderInflate(bytes) {
  const source = new Response(bytes).body;                     // поток из байтов: Blob.stream() есть не везде
  const stream = source.pipeThrough(new DecompressionStream('deflate-raw'));
  return new Response(stream).arrayBuffer();
}

/** Один документ из архива: смещение локального заголовка → байты (stored или deflate). */
async function primFolderZipDoc(file, entry) {
  const slice = file.slice(0, entry.localOffset + 30);
  const head = new Uint8Array(await primFolderBytes(slice));
  const view = new DataView(head.buffer, head.byteOffset, head.byteLength);
  if (view.getUint32(entry.localOffset, true) !== 0x04034b50) return null;
  const nameLen = view.getUint16(entry.localOffset + 26, true);
  const extraLen = view.getUint16(entry.localOffset + 28, true);
  const start = entry.localOffset + 30 + nameLen + extraLen;
  const raw = file.slice(start, start + entry.compSize);
  let body;
  if (entry.method === 0) {
    body = await primFolderBytes(raw);
  } else if (entry.method === 8 && typeof DecompressionStream === 'function') {
    try {
      body = await primFolderInflate(await primFolderBytes(raw));
    } catch (e) {
      return 'распаковка не удалась: ' + (e && e.message ? e.message : 'браузер не смог распаковать');
    }
  } else {
    return 'метод сжатия архива браузеру неизвестен';        // например, дефлект 64 или шифрование
  }
  const name = `${entry.name}`.split('/').pop();
  return new File([body], name, { type: 'application/xml' });
}

/** Скрытый или служебный файл: «.скрытый.xml», «.DS_Store», «Thumbs.db», «~$документ.docx».
    Такие файлы не документы ЭДО: серверная приёмная папка их тоже пропускает (app/primary/inbox.py). */
export function primFolderServicePath(path) {
  const parts = String(path || '').split(/[\\/]/).filter(Boolean).map((x) => x.toLowerCase());
  if (!parts.length) return true;
  const base = parts[parts.length - 1];
  if (base.startsWith('~$') || PRIM_FOLDER_SERVICE.has(base)) return true;
  // скрытый файл или файл внутри скрытого каталога (.git, .DS_Store, .Trash): такой путь — не документ ЭДО
  return parts.some((part) => part.startsWith('.'));
}

/** Отбор файлов папки: { send: [файлы к отправке], counts: {вид: число}, total, notices: [замечания] }.
    Возвращает ровно то, что имеет смысл показать: крупный XML и архивы с лишними документами не отправляются
    целиком, а объясняются словами — иначе браузер зря читает десятки мегабайт и получает отказ сервера. */
export async function primFolderScan(files, onProgress = () => {}) {
  const counts = {};
  const send = [];
  const notices = [];
  const note = (text) => { if (!notices.includes(text)) notices.push(text); };
  const count = (kind) => { counts[kind] = (counts[kind] || 0) + 1; };
  const list = [...(files || [])];
  for (let i = 0; i < list.length; i += 1) {
    const f = list[i];
    const path = primFolderPath(f);
    const base = path.split(/[\\/]/).pop();
    const size = Number(f.size) || 0;
    if (primFolderServicePath(path)) { count('hidden'); continue; }
    if (!size) { count('empty'); continue; }
    if (/\.xml$/i.test(base) && size > PRIM_FOLDER_XML_MAX) {
      count('too_big');
      note(`${base} — ${(size / 1048576).toFixed(1)} МБ: документ ЭДО больше 5 МБ не бывает, файл не отправлен`);
      continue;
    }
    const kind = /\.xml$/i.test(base) ? primFolderKind(path, await primFolderHead(f)) : primFolderKind(path, '');
    if (kind === 'zip') {
      const inside = await primFolderZipList(f);
      count('zip');
      if (size > PRIM_FOLDER_ZIP_MAX) {
        note(`${base} — ${(size / 1048576).toFixed(1)} МБ: архив больше 20 МБ не принимается, файл не отправлен`);
      } else if (!inside) {
        note(`${base}: архив не читается (повреждён или обрезан) — файл не отправлен`);
      } else if (inside.error) {
        note(`${base}: ${inside.error} — документы архива не отправлены`);
      } else if (!inside.docs.length) {
        note(`${base}: в архиве нет XML-документов — файл не отправлен`);
      } else {
        const docs = inside.docs.slice(0, PRIM_FOLDER_ZIP_DOCS);
        if (inside.docs.length > docs.length) {
          note(`${base}: документов ${inside.docs.length}, за раз можно ${PRIM_FOLDER_ZIP_DOCS} — верхние ${docs.length} `
            + `уйдут из этого архива, остальные ${inside.docs.length - docs.length} перенесите следующей папкой`);
        }
        docs.forEach((doc) => {                             // имя — с путём архива, иначе в отчёте только «ON_…xml»
          try { Object.defineProperty(doc.file, 'relativePath', { value: doc.path }); } catch { /* имя как есть */ }
          send.push(doc.file);
        });
      }
      if (i % 50 === 0) onProgress(`Просмотрено файлов: ${i + 1} из ${list.length}`);
      continue;
    }
    count(kind);
    if (PRIM_FOLDER_SEND.includes(kind)) send.push(f);
    if (i % 50 === 0) onProgress(`Просмотрено файлов: ${i + 1} из ${list.length}`);
  }
  return { send, counts, total: list.length, notices };
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
  let send = scan.send;                                      // документы архивов уже распакованы в отдельные File
  let known = 0;
  const xml = send;
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
    try { settle = (await api.post('/api/v1/bank/closing/settle', { company_ids: ids })).companies || []; } catch { settle = []; }
  }
  return { ...total, scan: { counts: scan.counts, total: scan.total, notices: scan.notices },
    known, sent: send.length, settle };
}

function primFolderSide(s, title) {
  if (!s || !s.count) return '';
  return `${esc(title)}: закрыто ${esc(fmtSum(s.closed_amount))} из ${esc(fmtSum(s.amount))} (${esc(String(s.percent).replace('.', ','))} %)`;
}

/** Итог загрузки папки для экрана «Первичка». */
export function primFolderSummaryHtml(sum) {
  const counts = (sum.scan && sum.scan.counts) || {};
  const notices = (sum.scan && sum.scan.notices) || [];
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
    ${notices ? `<ul class="prim-folder-notices">${notices.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>` : ''}
    ${rows ? `<div class="table-wrap"><table class="data"><thead><tr><th>Компания</th><th class="num">Расходы</th>
      <th class="num">Продажи</th><th class="num">Уже были</th><th>Сверка с выпиской</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="form-hint">Подробно — «Банк: выписка» → «Закрытие документами».</p>` : ''}
    ${problems ? `<details><summary>Не принятые файлы (${sum.problems.length})</summary><ul class="prim-folder-problems">${problems}</ul></details>` : ''}
  </div>`;
}
