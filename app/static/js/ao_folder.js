/* ======================================================================
   Ямастер Чек — v1.73.7 → v1.73.8: папка авансовых отчётов и настоящий PDF.
   ООО «Ямастер» | ymaster.ru | chek@ymaster.ru

   Требование владельца: при сборке авансового отчёта на телефоне и на ПК
   создавать «Загрузки/АО/<сокращённое название компании>» и складывать
   туда отчёт. Браузер НЕ умеет создавать папки через <a download>:
   «/» в имени файла превращается в «_» (правило WHATWG, см. MDN).
   Поэтому два честных пути:

   1) File System Access API (Chrome/Edge на ПК, Chrome 132+ и Samsung
      Internet на Android): человек один раз выбирает папку «Загрузки»,
      программа сама создаёт «АО/<ООО «Компания»>» и пишет файлы.
      Разрешение и выбранная папка запоминаются в IndexedDB.

   2) Остальные браузеры (Firefox, Safari, iOS): скачивается ZIP, внутри
      которого УЖЕ лежит «АО/<ООО «Компания»>/…». Распаковка в «Загрузки»
      создаёт ровно ту структуру, которую просил владелец.

   v1.73.8: тот же модуль умеет писать бинарный PDF (bytes, а не UTF-8 строки),
   предлагать showSaveFilePicker и упаковывать готовый .pdf в ZIP с папками.

   Модуль без зависимостей: чистые функции имени/архива тестируются в
   Vitest, а работа с диском и ZIP проверяется и в Node, и в браузере.
   ====================================================================== */

export const AO_ROOT_FOLDER = 'АО';
export const AO_FOLDER_DB = 'ymaster-check-ao';
export const AO_FOLDER_STORE = 'handles';
export const AO_FOLDER_KEY = 'downloads';
const AO_MAX_SEGMENT = 90;

// Сокращение ОПФ — тот же список, что в app.js (shortOpfName), для случаев,
// когда в карточке компании заполнено только полное наименование.
const AO_OPF_SHORT = [
  [/^общество с ограниченной ответственностью\s+/i, 'ООО '],
  [/^публичное акционерное общество\s+/i, 'ПАО '],
  [/^непубличное акционерное общество\s+/i, 'НАО '],
  [/^акционерное общество\s+/i, 'АО '],
  [/^общество с дополнительной ответственностью\s+/i, 'ОДО '],
  [/^индивидуальный предприниматель\s+/i, 'ИП '],
  [/^крестьянское \(фермерское\) хозяйство\s+/i, 'КФХ '],
  [/^самозанят(?:ый|ая)(?:\s+гражданин(?:ка)?)?\s+/i, 'Самозанятый '],
  [/^плательщик налога на профессиональный доход\s+/i, 'Самозанятый '],
];
// \b в JS считает границей только латиницу — для кириллицы нужен явный хвост.
const AO_LEGAL_FORM = /^(ООО|ОАО|АО|ПАО|НАО|ЗАО|ИП|ОДО|КФХ|Самозанятый|АНО|НКО|ТСЖ|ФГУП|МУП|ГУП)(?=\s|[«"»']|$)/i;
const AO_CORPORATE = /^(ООО|ОАО|АО|ПАО|НАО|ЗАО|ОДО|КФХ)\s+(.+)$/i;
// Имена устройств Windows запрещены и с расширением: CON.txt тоже нельзя.
const AO_WINDOWS_DEVICE = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i;
// Chromium отбрасывает a.download с кириллицей и сохраняет файл как «download».
// Имя скачиваемого файла поэтому транслитерируем, а кириллицу оставляем
// ВНУТРИ ZIP — там это настоящие папки «АО» и «ООО «…»».
const AO_TRANSLIT = {
  'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
  'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
  'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
  'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
  'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
  'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'E',
  'Ж': 'Zh', 'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M',
  'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U',
  'Ф': 'F', 'Х': 'Kh', 'Ц': 'Ts', 'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Sch',
  'Ъ': '', 'Ы': 'Y', 'Ь': '', 'Э': 'E', 'Ю': 'Yu', 'Я': 'Ya', '№': 'N',
};

export function aoShortOpf(name) {
  const n = String(name || '').trim();
  if (!n) return '';
  for (const [re, short] of AO_OPF_SHORT) {
    if (re.test(n)) return (short + n.replace(re, '')).trim();
  }
  return n;
}

function aoCleanSegment(value) {
  let q = 0;
  let text = String(value ?? '').replace(/"/g, () => (q++ % 2 === 0 ? '«' : '»'));
  // eslint-disable-next-line no-control-regex
  text = text.replace(/[\\/\u0000-\u001f<>:|?*]/g, ' ');
  text = text.replace(/\s+/g, ' ').trim().replace(/^[. ]+/, '').replace(/[. ]+$/g, '').trim();
  if (text.length > AO_MAX_SEGMENT) text = text.slice(0, AO_MAX_SEGMENT).replace(/[. ]+$/g, '');
  if (AO_WINDOWS_DEVICE.test(text)) text = `_${text}`;
  return text === '..' ? '' : text;
}

/** Имя папки/файла без разделителей пути и запрещённых символов Windows. */
export function aoSafeSegment(value, fallback = 'Организация') {
  return aoCleanSegment(value) || aoCleanSegment(fallback) || 'Организация';
}

/** ASCII-имя для a.download: Chromium иначе теряет кириллицу («download»). */
export function aoAsciiFilename(value, fallback = 'otchet') {
  const raw = String(value ?? '');
  const dot = raw.lastIndexOf('.');
  const hasExt = dot > 0 && dot < raw.length - 1;
  const stem = hasExt ? raw.slice(0, dot) : raw;
  const ext = hasExt ? raw.slice(dot + 1).replace(/[^A-Za-z0-9]/g, '').toLowerCase() : '';
  let latin = '';
  for (const ch of stem) latin += (AO_TRANSLIT[ch] ?? (/[A-Za-z0-9._-]/.test(ch) ? ch : '_'));
  // eslint-disable-next-line no-control-regex
  let safe = latin.replace(/[\\/\u0000-\u001f<>:|?*"']/g, '_')
    .replace(/[^A-Za-z0-9._-]+/g, '_').replace(/_+/g, '_')
    .replace(/^[_.-]+|[_.-]+$/g, '').slice(0, 120).replace(/[_.-]+$/g, '');
  if (!safe) safe = String(fallback).replace(/[^A-Za-z0-9._-]+/g, '_') || 'otchet';
  return ext ? `${safe}.${ext}` : safe;
}

/** Сокращённое название компании для папки: «ООО «Ромашка»». */
export function aoCompanyFolderName(rep, meta = {}) {
  const org = (rep && rep.requisites && rep.requisites['Организация']) || {};
  const company = (rep && rep.company) || {};
  let name = org['НаименованиеСокращенное'] || company.short_name || company.name
    || (meta && meta['ao-org']) || '';
  name = aoSafeSegment(aoShortOpf(name));
  if (!AO_LEGAL_FORM.test(name)) name = `ООО «${name}»`;
  const corporate = AO_CORPORATE.exec(name);
  if (corporate && !/[«']/.test(corporate[2])) name = `${corporate[1]} «${corporate[2]}»`;
  return aoSafeSegment(name);
}

export function aoFolderSegments(rep, meta = {}) {
  return [AO_ROOT_FOLDER, aoCompanyFolderName(rep, meta)];
}

/** Относительный путь внутри «Загрузок»: АО/ООО «Ромашка». */
export function aoFolderPath(segments) {
  return (segments || []).map(s => aoSafeSegment(s)).join('/');
}

/** Путь для человека: «Загрузки/АО/…» или «<выбранная папка>/АО/…». */
export function aoDownloadsPath(segments, handleName = '') {
  const name = String(handleName || '').trim();
  const isDownloads = /^(downloads?|загрузки|загрузка|download)$/i.test(name);
  const root = !name || isDownloads ? 'Загрузки' : name;
  return `${root}/${aoFolderPath(segments)}`;
}

/** Плоские скачивания без папок несут её название в префиксе. */
export function aoFlatPrefix(segments) {
  return (segments || []).map(s => aoSafeSegment(s)).join('_');
}

export function aoZipFileName(segments, base) {
  return aoAsciiFilename(`${aoFlatPrefix(segments)}_${base}.zip`);
}

/** Три файла комплекта: печатная форма, таблица для Excel, структура для 1С. */
export function aoBundleFiles({ base, csvText, jsonText, htmlText }) {
  const stem = aoSafeSegment(base, 'avansovy-otchet');
  return [
    { name: `${stem}.html`, text: String(htmlText || ''), type: 'text/html;charset=utf-8' },
    { name: `${stem}.csv`, text: String(csvText || ''), type: 'text/csv;charset=utf-8' },
    { name: `${stem}.json`, text: String(jsonText || ''), type: 'application/json;charset=utf-8' },
  ];
}

/* ---------------------------------------------------------------- UTF-8 ----
   Свой кодировщик: в офлайн-PWA и в DOM-стенде TextEncoder может отсутствовать,
   а кириллица в именах ZIP обязана быть настоящим UTF-8 (флаг 0x0800). */
export function aoUtf8(text) {
  const out = [];
  for (const ch of String(text ?? '')) {
    let cp = ch.codePointAt(0);
    if (cp > 0xffff) {
      cp -= 0x10000;
      out.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 63),
        0x80 | ((cp >> 6) & 63), 0x80 | (cp & 63));
    } else if (cp < 0x80) {
      out.push(cp);
    } else if (cp < 0x800) {
      out.push(0xc0 | (cp >> 6), 0x80 | (cp & 63));
    } else {
      out.push(0xe0 | (cp >> 12), 0x80 | ((cp >> 6) & 63), 0x80 | (cp & 63));
    }
  }
  return new Uint8Array(out);
}

export function aoBlobFromText(text, type = 'application/octet-stream') {
  return new Blob([aoUtf8(text)], { type });
}

/** v1.73.8: PDF — бинарный файл. Берём настоящие байты, а не UTF-8 от строки. */
export async function aoBlobBytes(blob) {
  if (!blob) return new Uint8Array(0);
  if (typeof blob.arrayBuffer === 'function') return new Uint8Array(await blob.arrayBuffer());
  if (typeof FileReader !== 'undefined') {
    return new Promise((resolve) => {
      try {
        const reader = new FileReader();
        if (typeof reader.readAsArrayBuffer !== 'function') return resolve(new Uint8Array(0));
        reader.onload = () => resolve(new Uint8Array(reader.result || 0));
        reader.onerror = () => resolve(new Uint8Array(0));
        reader.readAsArrayBuffer(blob);
      } catch { resolve(new Uint8Array(0)); }
    });
  }
  return new Uint8Array(0);
}

/** Байты записи ZIP/FSA: `bytes` для PDF, `text` для HTML/CSV/JSON. */
export function aoEntryBytes(file) {
  const raw = file && file.bytes;
  if (raw) {
    if (typeof Uint8Array !== 'undefined' && raw instanceof Uint8Array) return raw;
    if (typeof ArrayBuffer !== 'undefined' && raw instanceof ArrayBuffer) return new Uint8Array(raw);
    if (Array.isArray(raw)) return new Uint8Array(raw);
  }
  return aoUtf8(file && file.text != null ? file.text : '');
}

/** Для записи на диск: сначала явные bytes, иначе Blob из ответа сервера. */
export async function aoFileBytes(file) {
  const direct = aoEntryBytes(file);
  if (direct.length || !file || !file.blob) return direct;
  return aoBlobBytes(file.blob);
}

let AO_CRC_TABLE = null;
export function aoCrc32(bytes) {
  if (!AO_CRC_TABLE) {
    const table = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
      table[i] = c >>> 0;
    }
    AO_CRC_TABLE = table;
  }
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) crc = AO_CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

function aoDosDateTime(date) {
  const year = Math.min(2107, Math.max(1980, date.getFullYear()));
  return [
    (date.getHours() << 11) | (date.getMinutes() << 5) | (date.getSeconds() >> 1),
    ((year - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate(),
  ];
}

/** ZIP (метод «без сжатия») с готовой структурой «АО/<компания>/…». */
export function aoBuildZip(files, segments, readmeText = '') {
  const safeSegments = (segments || []).map(s => aoSafeSegment(s));
  let prefix = '';
  const dirs = [];
  for (const seg of safeSegments) {
    prefix += `${seg}/`;
    dirs.push(prefix);
  }
  const entries = [];
  if (readmeText) entries.push({ name: 'ПРОЧТИТЕ.txt', bytes: aoUtf8(readmeText) });
  for (const dir of dirs) entries.push({ name: dir, bytes: new Uint8Array(0) });
  for (const file of files || []) {
    entries.push({ name: prefix + aoSafeSegment(file.name), bytes: aoEntryBytes(file) });
  }
  const [dosTime, dosDate] = aoDosDateTime(new Date());
  const chunks = [];
  const central = [];
  let offset = 0;
  for (const entry of entries) {
    const nameBytes = aoUtf8(entry.name);
    const data = entry.bytes;
    const crc = aoCrc32(data);
    const local = new Uint8Array(30 + nameBytes.length);
    const lv = new DataView(local.buffer);
    lv.setUint32(0, 0x04034b50, true);
    lv.setUint16(4, 20, true);       // версия формата
    lv.setUint16(6, 0x0800, true);   // имена в UTF-8
    lv.setUint16(8, 0, true);        // без сжатия
    lv.setUint16(10, dosTime, true);
    lv.setUint16(12, dosDate, true);
    lv.setUint32(14, crc, true);
    lv.setUint32(18, data.length, true);
    lv.setUint32(22, data.length, true);
    lv.setUint16(26, nameBytes.length, true);
    lv.setUint16(28, 0, true);
    local.set(nameBytes, 30);
    chunks.push(local, data);

    const cd = new Uint8Array(46 + nameBytes.length);
    const cv = new DataView(cd.buffer);
    cv.setUint32(0, 0x02014b50, true);
    cv.setUint16(4, 20, true);
    cv.setUint16(6, 20, true);
    cv.setUint16(8, 0x0800, true);
    cv.setUint16(10, 0, true);
    cv.setUint16(12, dosTime, true);
    cv.setUint16(14, dosDate, true);
    cv.setUint32(16, crc, true);
    cv.setUint32(20, data.length, true);
    cv.setUint32(24, data.length, true);
    cv.setUint16(28, nameBytes.length, true);
    cv.setUint16(30, 0, true);
    cv.setUint16(32, 0, true);
    cv.setUint16(34, 0, true);
    cv.setUint16(36, 0, true);
    cv.setUint32(38, entry.name.endsWith('/') ? 0x10 : 0, true);
    cv.setUint32(42, offset, true);
    cd.set(nameBytes, 46);
    central.push(cd);
    offset += local.length + data.length;
  }
  const centralSize = central.reduce((n, c) => n + c.length, 0);
  const eocd = new Uint8Array(22);
  const ev = new DataView(eocd.buffer);
  ev.setUint32(0, 0x06054b50, true);
  ev.setUint16(8, entries.length, true);
  ev.setUint16(10, entries.length, true);
  ev.setUint32(12, centralSize, true);
  ev.setUint32(16, offset, true);
  ev.setUint16(20, 0, true);
  return new Blob([...chunks, ...central, eocd], { type: 'application/zip' });
}

export function aoZipReadme(segments, base) {
  return [
    'Ямастер Чек — авансовый отчёт',
    '',
    `Отчёт: ${base}`,
    '',
    'Чтобы получить нужную структуру папок, распакуйте архив прямо в «Загрузки»',
    '(«Извлечь здесь» / «Распаковать в текущую папку»). Появится:',
    '',
    `  Загрузки/${aoFolderPath(segments)}/`,
    '',
    'Внутри: печатная форма HTML, CSV для Excel и JSON для 1С. Настоящий PDF',
    'с выделяемым текстом создаётся кнопкой «💾 Сохранить PDF» в предпросмотре,',
    'а в Chrome и Edge программа сама кладёт его в эту папку — один раз выберите «Загрузки».',
    '',
  ].join('\r\n');
}

/** v1.73.8: пояснение к ZIP, внутри которого лежит именно готовый PDF. */
export function aoPdfZipReadme(segments, base, filename) {
  return [
    'Ямастер Чек — авансовый отчёт (PDF)',
    '',
    `Отчёт: ${base}`,
    `Файл: ${filename}`,
    '',
    'Чтобы получить нужную структуру папок, распакуйте архив прямо в «Загрузки»',
    '(«Извлечь здесь» / «Распаковать в текущую папку»). Появится:',
    '',
    `  Загрузки/${aoFolderPath(segments)}/${filename}`,
    '',
    'PDF содержит настоящий выделяемый и копируемый текст, а не картинку страницы.',
    'Откройте файл и нажмите «Печать», если нужен бумажный экземпляр:',
    'меню браузера для создания PDF больше не требуется.',
    '',
  ].join('\r\n');
}

export function aoPrintableDocument({ title, folder, formHtml, cssText = '' }) {
  const escape = v => String(v ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fallback = `
:root { --text: #111; --text-dim: #555; --border: #999; }
body { color: #111; font: 12px/1.4 Arial, sans-serif; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #888; padding: 2mm; font-size: 11px; }
.ao-title, .ao1-title { text-align: center; font-size: 16px; }
.print-page { width: 210mm; min-height: 296mm; background: #fff; }
`;
  const own = `
#print-root { position: static; left: 0; width: auto; margin: 0 auto; }
body { background: #f3f4f6; }
.ao-folder-banner { box-sizing: border-box; max-width: 210mm; margin: 12px auto; padding: 10px 14px;
  font: 14px/1.5 system-ui, Arial, sans-serif; background: #fff; border: 1px solid #ddd; border-radius: 8px; }
@media print {
  @page { size: A4 portrait; margin: 10mm 15mm 10mm 20mm; }
  body { margin: 0; background: #fff; }
  body > *:not(#print-root) { display: none !important; }
  .ao-folder-banner { display: none !important; }
  #print-root { width: auto; }
}
`;
  return `<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>${escape(title)}</title>
<style>${cssText || fallback}\n${own}</style></head>
<body class="print-ao1">
<div class="ao-folder-banner">📁 Авансовый отчёт сохранён в <b>${escape(folder)}</b>.
Этот файл — печатная форма HTML. Настоящий PDF с выделяемым текстом создаётся кнопкой
«💾 Сохранить PDF» в предпросмотре отчёта; рядом лежат CSV для Excel и JSON для 1С.</div>
<div id="print-root">${formHtml}</div>
</body></html>`;
}

/* ------------------------------------------------------- доступ к папке ---- */
let aoSessionHandle = null;

export function aoCanPickFolder() {
  return typeof window !== 'undefined'
    && typeof window.showDirectoryPicker === 'function'
    && window.isSecureContext !== false;
}

function aoIdb() {
  return new Promise((resolve) => {
    const idb = globalThis.indexedDB;
    if (!idb) return resolve(null);
    let req;
    try { req = idb.open(AO_FOLDER_DB, 1); } catch { return resolve(null); }
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(AO_FOLDER_STORE)) db.createObjectStore(AO_FOLDER_STORE);
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => resolve(null);
    req.onblocked = () => resolve(null);
  });
}

async function aoIdbValue(mode, value) {
  const db = await aoIdb();
  if (!db) return null;
  return new Promise((resolve) => {
    let tx;
    try { tx = db.transaction(AO_FOLDER_STORE, mode); } catch { return resolve(null); }
    const store = tx.objectStore(AO_FOLDER_STORE);
    let req;
    try {
      // FileSystemDirectoryHandle клонируется, а объект из старого API/стенда — нет:
      // DataCloneError не должен ронять сохранение, просто не запоминаем папку.
      req = mode === 'readonly' ? store.get(AO_FOLDER_KEY) : store.put(value, AO_FOLDER_KEY);
    } catch { return resolve(null); }
    req.onsuccess = () => resolve(req.result ?? null);
    req.onerror = () => resolve(null);
    tx.onabort = () => resolve(null);
    tx.onerror = () => resolve(null);
  });
}

async function aoDeleteStoredHandle() {
  const db = await aoIdb();
  if (!db) return;
  await new Promise((resolve) => {
    let tx;
    try { tx = db.transaction(AO_FOLDER_STORE, 'readwrite'); } catch { return resolve(); }
    tx.objectStore(AO_FOLDER_STORE).delete(AO_FOLDER_KEY);
    tx.oncomplete = () => resolve();
    tx.onerror = () => resolve();
    tx.onabort = () => resolve();
  });
}

async function aoHasPermission(handle, interactive = false) {
  try {
    if (typeof handle.queryPermission !== 'function') return true;  // старый API или стенд
    if ((await handle.queryPermission({ mode: 'readwrite' })) === 'granted') return true;
    if (interactive && typeof handle.requestPermission === 'function') {
      return (await handle.requestPermission({ mode: 'readwrite' })) === 'granted';
    }
  } catch { /* нет разрешения — попросим выбрать папку заново */ }
  return false;
}

async function aoPickFolder() {
  if (!aoCanPickFolder()) return null;
  let handle;
  try {
    try {
      handle = await window.showDirectoryPicker({
        id: 'ymaster-ao-downloads', startIn: 'downloads', mode: 'readwrite',
      });
    } catch (err) {
      // Старый Chrome не знает startIn — повторяем без подсказки каталога.
      if (!err || err.name !== 'TypeError') throw err;
      handle = await window.showDirectoryPicker({ id: 'ymaster-ao-downloads', mode: 'readwrite' });
    }
    aoSessionHandle = handle;
    await aoIdbValue('readwrite', handle);
    return handle;
  } catch (err) {
    if (err && err.name === 'AbortError') return 'cancelled';
    return null;  // нет разрешения, небезопасный контекст — отдадим ZIP
  }
}

/** Вызвать СРАЗУ в обработчике клика: выбор папки требует жеста пользователя.
    Возвращает handle, null (нет API → ZIP) или 'cancelled' (человек отменил). */
export async function aoFolderHandleForSave() {
  if (aoSessionHandle && await aoHasPermission(aoSessionHandle, false)) return aoSessionHandle;
  if (!globalThis.indexedDB) return aoPickFolder();
  const stored = await aoIdbValue('readonly');
  if (stored && await aoHasPermission(stored, true)) {
    aoSessionHandle = stored;
    return stored;
  }
  if (stored) await aoDeleteStoredHandle();
  return aoPickFolder();
}

/** v1.73.8: если выбора ПАПКИ нет, просим сохранить один настоящий `.pdf`.
    Вызывать тоже сразу в клике: `showSaveFilePicker` требует жеста пользователя. */
export async function aoFileHandleForSave(filename) {
  if (typeof window === 'undefined'
      || typeof window.showSaveFilePicker !== 'function'
      || window.isSecureContext === false) return null;
  const suggestedName = aoAsciiFilename(filename, 'avansovy-otchet.pdf');
  const opts = {
    id: 'ymaster-ao-pdf', suggestedName,
    types: [{ description: 'PDF', accept: { 'application/pdf': ['.pdf'] } }],
  };
  try {
    try {
      return await window.showSaveFilePicker({ ...opts, startIn: 'downloads' });
    } catch (err) {
      if (!err || err.name !== 'TypeError') throw err;
      return await window.showSaveFilePicker(opts);
    }
  } catch (err) {
    if (err && err.name === 'AbortError') return 'cancelled';
    return null;
  }
}

/** Забыть выбранную папку (пригодится, если человек сменил каталог загрузок). */
export async function aoForgetFolder() {
  aoSessionHandle = null;
  await aoDeleteStoredHandle();
}

async function aoOpenFolder(handle, segments) {
  let dir = handle;
  for (const segment of segments || []) {
    dir = await dir.getDirectoryHandle(aoSafeSegment(segment), { create: true });
  }
  return dir;
}

async function aoWriteToDir(dir, file) {
  const bytes = await aoFileBytes(file);
  const fh = await dir.getFileHandle(aoSafeSegment(file.name), { create: true });
  const writable = await fh.createWritable();
  await writable.write(bytes);
  await writable.close();
}

async function aoWriteToHandle(fileHandle, file) {
  const bytes = await aoFileBytes(file);
  const writable = await fileHandle.createWritable();
  await writable.write(bytes);
  await writable.close();
}

/** v1.73.8: записать ОДИН файл: в папку «АО/<компания>» или в выбранный файл. */
export async function aoWriteFile(handle, segments, file) {
  if (handle && typeof handle.createWritable === 'function'
      && typeof handle.getDirectoryHandle !== 'function') {
    await aoWriteToHandle(handle, file);
    return handle;
  }
  const dir = await aoOpenFolder(handle, segments);
  await aoWriteToDir(dir, file);
  return dir;
}

/** Создать «АО/<компания>» в выбранной папке и записать файлы отчёта. */
export async function aoWriteBundle(handle, segments, files) {
  const dir = await aoOpenFolder(handle, segments);
  for (const file of files || []) await aoWriteToDir(dir, file);
  return dir;
}

export function aoDownloadBlob(blob, filename) {
  const a = document.createElement('a');
  const url = URL.createObjectURL(blob);
  a.href = url;
  const safeName = aoAsciiFilename(filename);
  a.download = safeName;
  if (document.body && typeof document.body.appendChild === 'function') document.body.appendChild(a);
  a.click();
  if (typeof a.remove === 'function') a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 3000);
  return safeName;
}

/** Сохранить комплект: папка в «Загрузках» или ZIP с готовой структурой. */
export async function aoSaveBundle({ files, segments, base, handle }) {
  if (handle === 'cancelled') return { mode: 'cancelled' };
  if (handle) {
    await aoWriteBundle(handle, segments, files);
    return { mode: 'folder', path: aoDownloadsPath(segments, handle.name) };
  }
  const filename = aoZipFileName(segments, base);
  const zip = aoBuildZip(files, segments, aoZipReadme(segments, base));
  aoDownloadBlob(zip, filename);
  return { mode: 'zip', filename, path: aoDownloadsPath(segments) };
}

/** v1.73.8: сохранить один бинарный PDF.
    target: {kind:'folder', handle} | {kind:'file', handle} | 'cancelled' | null.
    null → ZIP с готовой структурой «АО/<компания>/<отчёт>.pdf». */
export async function aoSaveFile({ file, segments, base, target, readme }) {
  if (target === 'cancelled') return { mode: 'cancelled' };
  const safeName = aoSafeSegment(file && file.name, 'avansovy-otchet.pdf');
  const bytes = await aoFileBytes(file);
  const normalized = { name: safeName, bytes, type: (file && file.type) || 'application/pdf' };
  if (target && target.kind === 'file' && target.handle) {
    await aoWriteToHandle(target.handle, normalized);
    return { mode: 'file', filename: target.handle.name || safeName,
             path: target.handle.name || safeName };
  }
  const dirHandle = target && (target.handle || target);
  if (dirHandle && typeof dirHandle.getDirectoryHandle === 'function') {
    await aoWriteFile(dirHandle, segments, normalized);
    return { mode: 'folder', filename: safeName,
             path: `${aoDownloadsPath(segments, dirHandle.name)}/${safeName}` };
  }
  const filename = aoZipFileName(segments, base);
  const zip = aoBuildZip([normalized], segments,
    readme || aoPdfZipReadme(segments, base, safeName));
  aoDownloadBlob(zip, filename);
  return { mode: 'zip', filename, file: safeName,
           path: `${aoDownloadsPath(segments)}/${safeName}` };
}
