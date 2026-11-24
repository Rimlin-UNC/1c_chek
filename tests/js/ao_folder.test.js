import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  AO_ROOT_FOLDER, aoSafeSegment, aoShortOpf, aoCompanyFolderName, aoFolderSegments,
  aoFolderPath, aoDownloadsPath, aoFlatPrefix, aoAsciiFilename, aoZipFileName, aoBundleFiles,
  aoUtf8, aoCrc32, aoBuildZip, aoZipReadme, aoPdfZipReadme, aoPrintableDocument,
  aoBlobBytes, aoEntryBytes, aoFileBytes, aoCanPickFolder, aoFolderHandleForSave,
  aoFileHandleForSave, aoForgetFolder, aoWriteBundle, aoWriteFile, aoSaveBundle, aoSaveFile,
} from '../../app/static/js/ao_folder.js';

const decode = (u8) => (typeof TextDecoder !== 'undefined'
  ? new TextDecoder().decode(u8) : Buffer.from(u8).toString('utf8'));

async function blobBytes(blob) {
  if (typeof blob.arrayBuffer === 'function') return new Uint8Array(await blob.arrayBuffer());
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsArrayBuffer(blob);
  });
}

/** Разбор центрального каталога ZIP: имена, данные, CRC и флаги. */
function zipEntries(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let eocd = -1;
  for (let i = bytes.length - 22; i >= 0; i--) {
    if (view.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  expect(eocd).toBeGreaterThanOrEqual(0);
  const count = view.getUint16(eocd + 10, true);
  let off = view.getUint32(eocd + 16, true);
  const out = [];
  for (let i = 0; i < count; i++) {
    expect(view.getUint32(off, true)).toBe(0x02014b50);
    const nameLen = view.getUint16(off + 28, true);
    const extraLen = view.getUint16(off + 30, true);
    const commentLen = view.getUint16(off + 32, true);
    const size = view.getUint32(off + 24, true);
    const crc = view.getUint32(off + 16, true);
    const flags = view.getUint16(off + 8, true);
    const localOff = view.getUint32(off + 42, true);
    const name = decode(bytes.slice(off + 46, off + 46 + nameLen));
    expect(view.getUint32(localOff, true)).toBe(0x04034b50);
    const lNameLen = view.getUint16(localOff + 26, true);
    const lExtraLen = view.getUint16(localOff + 28, true);
    const start = localOff + 30 + lNameLen + lExtraLen;
    out.push({ name, flags, crc, data: bytes.slice(start, start + size) });
    off += 46 + nameLen + extraLen + commentLen;
  }
  return out;
}

function fakeDir(name = 'Загрузки') {
  const dirs = {};
  const files = {};
  const handle = {
    name,
    queryPermission: vi.fn(async () => 'granted'),
    requestPermission: vi.fn(async () => 'granted'),
    getDirectoryHandle: vi.fn(async (child) => {
      dirs[child] = dirs[child] || fakeDir(child);
      return dirs[child];
    }),
    getFileHandle: vi.fn(async (file) => {
      const writable = { write: vi.fn(async () => {}), close: vi.fn(async () => {}) };
      files[file] = files[file] || { createWritable: vi.fn(async () => writable) };
      return files[file];
    }),
  };
  return Object.assign(handle, { dirs, files });
}

const rep = () => ({
  requisites: { 'Организация': { 'НаименованиеСокращенное': 'ООО «Ямастер»' } },
  company: { name: 'Общество с ограниченной ответственностью «Ямастер»', inn: '7801234564' },
});

const anchorClick = HTMLAnchorElement.prototype.click;

beforeEach(async () => {
  await aoForgetFolder();
  document.body.innerHTML = '';
  URL.createObjectURL = vi.fn(() => 'blob:ao-test');
  URL.revokeObjectURL = vi.fn();
  HTMLAnchorElement.prototype.click = vi.fn();   // jsdom не умеет переходы по blob:
});

afterEach(() => {
  delete window.showDirectoryPicker;
  delete window.showSaveFilePicker;
  try { delete window.isSecureContext; } catch { /* свойство стенда может быть не настраиваемым */ }
  HTMLAnchorElement.prototype.click = anchorClick;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('имя папки компании', () => {
  it('сокращённое название из карточки компании становится «ООО «…»»', () => {
    expect(aoCompanyFolderName(rep())).toBe('ООО «Ямастер»');
    expect(aoFolderSegments(rep())).toEqual([AO_ROOT_FOLDER, 'ООО «Ямастер»']);
  });

  it('прямые кавычки превращаются в ёлочки, а полное название — в ОПФ', () => {
    expect(aoSafeSegment('ООО "Ромашка/Логистика"')).toBe('ООО «Ромашка Логистика»');
    expect(aoShortOpf('Общество с ограниченной ответственностью «Василёк»')).toBe('ООО «Василёк»');
    expect(aoCompanyFolderName({ company: { name: 'Общество с ограниченной ответственностью "Василёк"' } }))
      .toBe('ООО «Василёк»');
  });

  it('название без ОПФ дополняется «ООО «…»», ИП остаётся ИП', () => {
    expect(aoCompanyFolderName({ company: { name: 'Ромашка' } })).toBe('ООО «Ромашка»');
    expect(aoCompanyFolderName({ company: { name: 'ИП Иванов И.И.' } })).toBe('ИП Иванов И.И');
    expect(aoCompanyFolderName({}, { 'ao-org': 'ООО «Заря»' })).toBe('ООО «Заря»');
    expect(aoCompanyFolderName({})).toBe('ООО «Организация»');
  });

  it('не даёт выйти из «Загрузок»: разделители пути и «..» убираются', () => {
    const name = aoSafeSegment('../../../АО:Вася*?|.\\');
    expect(name).not.toMatch(/[\\/:*?"<>|]/);
    expect(name).not.toContain('..');
    expect(name.startsWith('.')).toBe(false);
    expect(aoSafeSegment('')).toBe('Организация');
    expect(aoSafeSegment('x'.repeat(300)).length).toBeLessThanOrEqual(90);
    expect(aoSafeSegment('CON')).toBe('_CON');
    expect(aoSafeSegment('com9.txt')).toBe('_com9.txt');
    expect(aoSafeSegment('COM10')).toBe('COM10');
  });

  it('путь, префикс и имя архива', () => {
    const segments = aoFolderSegments(rep());
    expect(aoFolderPath(segments)).toBe('АО/ООО «Ямастер»');
    expect(aoDownloadsPath(segments, 'Загрузки')).toBe('Загрузки/АО/ООО «Ямастер»');
    expect(aoDownloadsPath(segments, 'Downloads')).toBe('Загрузки/АО/ООО «Ямастер»');
    expect(aoDownloadsPath(segments, 'Мои документы')).toBe('Мои документы/АО/ООО «Ямастер»');
    expect(aoFlatPrefix(segments)).toBe('АО_ООО «Ямастер»');
    // a.download с кириллицей Chromium превращает в «download» — имя архива ASCII.
    expect(aoZipFileName(segments, 'АО-2026-10-46')).toBe('AO_OOO_Yamaster_AO-2026-10-46.zip');
    expect(aoAsciiFilename('АО/ООО «Ямастер».zip')).toBe('AO_OOO_Yamaster.zip');
    expect(aoAsciiFilename('')).toBe('otchet');
  });
});

describe('комплект файлов и ZIP-запасной путь', () => {
  const files = aoBundleFiles({ base: 'АО-2026-10-46-ООО «Ямастер»',
    csvText: '\ufeff№;Сумма\r\n1;10,00', jsonText: '{"АвансовыйОтчет":{}}',
    htmlText: '<!doctype html><title>АО</title>' });
  const segments = ['АО', 'ООО «Ямастер»'];

  it('в комплекте печатная форма, CSV с BOM и JSON для 1С', () => {
    expect(files.map(f => f.name)).toEqual([
      'АО-2026-10-46-ООО «Ямастер».html',
      'АО-2026-10-46-ООО «Ямастер».csv',
      'АО-2026-10-46-ООО «Ямастер».json',
    ]);
    expect(files[1].text.startsWith('\ufeff')).toBe(true);
    expect(files[0].type).toContain('text/html');
  });

  it('ZIP содержит папки «АО» и компании, кириллицу в UTF-8 и верные CRC', async () => {
    const zip = aoBuildZip(files, segments, aoZipReadme(segments, 'АО-2026-10-46'));
    expect(zip.type).toBe('application/zip');
    const entries = zipEntries(await blobBytes(zip));
    expect(entries.map(e => e.name)).toEqual([
      'ПРОЧТИТЕ.txt', 'АО/', 'АО/ООО «Ямастер»/',
      'АО/ООО «Ямастер»/АО-2026-10-46-ООО «Ямастер».html',
      'АО/ООО «Ямастер»/АО-2026-10-46-ООО «Ямастер».csv',
      'АО/ООО «Ямастер»/АО-2026-10-46-ООО «Ямастер».json',
    ]);
    for (const e of entries) {
      expect(e.flags & 0x0800).toBe(0x0800);      // имена в UTF-8
      expect(e.crc).toBe(aoCrc32(e.data));
    }
    expect(decode(entries[0].data)).toContain('Загрузки/АО/ООО «Ямастер»');
    expect(entries[4].data[0]).toBe(0xef);       // BOM у CSV
    expect(decode(entries[3].data)).toContain('<!doctype html>');
    expect(decode(entries[5].data)).toContain('АвансовыйОтчет');
  });

  it('UTF-8 и CRC считаются по байтам, а не по символам', () => {
    const bytes = aoUtf8('ООО «Ямастер» ₽');
    expect(bytes[0]).toBe(0xd0);
    expect(aoCrc32(aoUtf8('123456789'))).toBe(0xcbf43926);
    expect(aoCrc32(new Uint8Array(0))).toBe(0);
  });

  it('печатная форма — отдельный HTML с путём папки и стилями печати', () => {
    const html = aoPrintableDocument({ title: 'АО <1>', folder: 'Загрузки/АО/ООО «Ямастер»',
      formHtml: '<div class="print-page">Форма</div>', cssText: '.print-page { color: #000; }' });
    expect(html).toContain('charset="utf-8"');
    expect(html).toContain('Загрузки/АО/ООО «Ямастер»');
    expect(html).toContain('class="print-ao1"');
    expect(html).toContain('@media print');
    expect(html).toContain('<div class="print-page">Форма</div>');
    expect(html).toContain('<title>АО &lt;1&gt;</title>');   // заголовок экранирован
  });
});

describe('сохранение на диск (File System Access)', () => {
  it('создаёт «АО» и папку компании, затем пишет все три файла', async () => {
    const root = fakeDir('Загрузки');
    const files = aoBundleFiles({ base: 'АО-1', csvText: 'a', jsonText: 'b', htmlText: 'c' });
    const dir = await aoWriteBundle(root, ['АО', 'ООО «Ямастер»'], files);
    expect(root.getDirectoryHandle).toHaveBeenCalledWith('АО', { create: true });
    expect(root.dirs['АО'].getDirectoryHandle).toHaveBeenCalledWith('ООО «Ямастер»', { create: true });
    expect(dir.getFileHandle.mock.calls.map(c => c[0])).toEqual(['АО-1.html', 'АО-1.csv', 'АО-1.json']);
    expect(dir.files['АО-1.csv'].createWritable).toHaveBeenCalledOnce();
  });

  it('выбор папки один раз: picker, отмена и браузеры без API', async () => {
    const root = fakeDir('Загрузки');
    window.showDirectoryPicker = vi.fn(async () => root);
    expect(aoCanPickFolder()).toBe(true);
    expect(await aoFolderHandleForSave()).toBe(root);
    expect(window.showDirectoryPicker).toHaveBeenCalledWith(expect.objectContaining({
      id: 'ymaster-ao-downloads', startIn: 'downloads', mode: 'readwrite',
    }));
    await aoForgetFolder();
    window.showDirectoryPicker = vi.fn(async () => { const e = new Error('отмена'); e.name = 'AbortError'; throw e; });
    expect(await aoFolderHandleForSave()).toBe('cancelled');
    await aoForgetFolder();
    delete window.showDirectoryPicker;
    expect(aoCanPickFolder()).toBe(false);
    expect(await aoFolderHandleForSave()).toBe(null);
  });

  it('неклонируемый handle в старом API не ломает запись в выбранную папку', async () => {
    const root = fakeDir('Загрузки');
    window.showDirectoryPicker = vi.fn(async () => root);
    const request = (result) => {
      const req = { result };
      queueMicrotask(() => req.onsuccess?.());
      return req;
    };
    const store = {
      get: () => request(null),
      put: () => { throw new Error('DataCloneError'); },
      delete: () => request(null),
    };
    const tx = { objectStore: () => store, onabort: null, onerror: null, oncomplete: null };
    const db = {
      objectStoreNames: { contains: () => true },
      createObjectStore: () => {},
      transaction: () => tx,
    };
    vi.stubGlobal('indexedDB', {
      open: () => {
        const req = { result: db };
        queueMicrotask(() => req.onsuccess?.());
        return req;
      },
    });
    expect(await aoFolderHandleForSave()).toBe(root);
    expect(window.showDirectoryPicker).toHaveBeenCalledOnce();
  });

  it('с выбранной папкой сохраняет в неё, без папки — отдаёт ZIP', async () => {
    const files = aoBundleFiles({ base: 'АО-1', csvText: 'a', jsonText: 'b', htmlText: 'c' });
    const segments = ['АО', 'ООО «Ямастер»'];
    const root = fakeDir('Загрузки');
    const saved = await aoSaveBundle({ files, segments, base: 'АО-1', handle: root });
    expect(saved).toEqual({ mode: 'folder', path: 'Загрузки/АО/ООО «Ямастер»' });
    expect(root.dirs['АО'].dirs['ООО «Ямастер»'].files['АО-1.html']).toBeTruthy();

    const zipped = await aoSaveBundle({ files, segments, base: 'АО-1', handle: null });
    expect(zipped.mode).toBe('zip');
    expect(zipped.filename).toBe('AO_OOO_Yamaster_AO-1.zip');
    expect(/[^\x00-\x7F]/.test(zipped.filename)).toBe(false);
    expect(zipped.path).toBe('Загрузки/АО/ООО «Ямастер»');
    expect(document.querySelector('a')).toBeNull();   // временная ссылка удалена
    expect(URL.createObjectURL).toHaveBeenCalledOnce();
    expect(await aoSaveBundle({ files, segments, base: 'АО-1', handle: 'cancelled' }))
      .toEqual({ mode: 'cancelled' });
  });
});


describe('v1.73.8: настоящий PDF — бинарные байты, а не UTF-8 от строки', () => {
  const segments = ['АО', 'ООО «Ямастер»'];
  const pdf = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x34, 0x00, 0xff]);

  it('aoBlobBytes/aoEntryBytes/aoFileBytes сохраняют нули и 0xff', async () => {
    const blob = { arrayBuffer: async () => pdf.buffer.slice(0, pdf.length) };
    expect(Array.from(await aoBlobBytes(blob))).toEqual(Array.from(pdf));
    expect(Array.from(aoEntryBytes({ bytes: pdf }))).toEqual(Array.from(pdf));
    expect(Array.from(await aoFileBytes({ bytes: pdf }))).toEqual(Array.from(pdf));
    expect(Array.from(await aoFileBytes({ blob }))).toEqual(Array.from(pdf));
    expect(Array.from(aoEntryBytes({ text: 'АО' }))).toEqual(Array.from(aoUtf8('АО')));
  });

  it('ZIP содержит PDF побайтово и пояснение про выделяемый текст', async () => {
    const zip = aoBuildZip([{ name: 'АО-1.pdf', bytes: pdf }], segments,
      aoPdfZipReadme(segments, 'АО-1', 'АО-1.pdf'));
    const entries = zipEntries(await blobBytes(zip));
    const entry = entries.find(e => e.name.endsWith('.pdf'));
    expect(entry).toBeTruthy();
    expect(Array.from(entry.data)).toEqual(Array.from(pdf));
    expect(entry.crc).toBe(aoCrc32(pdf));
    expect(decode(entries[0].data)).toContain('Загрузки/АО/ООО «Ямастер»/АО-1.pdf');
    expect(decode(entries[0].data)).toContain('выделяемый');
  });

  it('пишет один PDF в выбранную папку и в выбранный файл', async () => {
    const root = fakeDir('Загрузки');
    const saved = await aoSaveFile({
      file: { name: 'АО-1.pdf', bytes: pdf, type: 'application/pdf' },
      segments, base: 'АО-1', target: { kind: 'folder', handle: root },
    });
    expect(saved.mode).toBe('folder');
    expect(saved.path).toBe('Загрузки/АО/ООО «Ямастер»/АО-1.pdf');
    const dir = root.dirs['АО'].dirs['ООО «Ямастер»'];
    const writable = await dir.files['АО-1.pdf'].createWritable();
    expect(Array.from(writable.write.mock.calls[0][0])).toEqual(Array.from(pdf));

    const written = [];
    const fileHandle = {
      name: 'AO-1.pdf',
      createWritable: vi.fn(async () => ({
        write: vi.fn(async (data) => written.push(data)), close: vi.fn(async () => {}),
      })),
    };
    await aoWriteFile(fileHandle, segments, { name: 'АО-1.pdf', bytes: pdf });
    expect(Array.from(written[0])).toEqual(Array.from(pdf));
  });

  it('без системного выбора папки предлагает сохранить один .pdf', async () => {
    const fileHandle = { name: 'AO-1.pdf' };
    window.showSaveFilePicker = vi.fn(async () => fileHandle);
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true, writable: true });
    expect(aoCanPickFolder()).toBe(false);
    expect(await aoFileHandleForSave('АО «Ямастер».pdf')).toBe(fileHandle);
    const opts = window.showSaveFilePicker.mock.calls[0][0];
    expect(opts).toMatchObject({ id: 'ymaster-ao-pdf', startIn: 'downloads' });
    expect(opts.suggestedName).toMatch(/^[A-Za-z0-9._-]+\.pdf$/);
  });

  it('ZIP-фолбэк сообщает точный путь и имя PDF', async () => {
    let zipBlob = null;
    URL.createObjectURL = vi.fn((blob) => { zipBlob = blob; return 'blob:ao-test'; });
    const saved = await aoSaveFile({
      file: { name: 'АО-1.pdf', bytes: pdf, type: 'application/pdf' },
      segments, base: 'АО-1', target: null,
    });
    expect(saved.mode).toBe('zip');
    expect(saved.file).toBe('АО-1.pdf');
    expect(saved.filename).toMatch(/\.zip$/);
    expect(saved.path).toBe('Загрузки/АО/ООО «Ямастер»/АО-1.pdf');
    const entries = zipEntries(await blobBytes(zipBlob));
    expect(entries.some(e => e.name === 'АО/ООО «Ямастер»/АО-1.pdf')).toBe(true);
  });
});
