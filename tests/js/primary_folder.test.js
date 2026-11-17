/* v1.70.113 (T-79, #63): «📁 Папка с УПД» — отбор УПД среди файлов папки, кодировка, порции, обход перетащенной
   папки, пропуск уже загруженного по SHA-256, итог по компаниям. API подменён, данные синтетические. */
import { deflateRawSync } from 'node:zlib';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { post: [] };
let known = [];

vi.mock('../../app/static/js/api.js', () => ({
  api: {
    post: vi.fn(async (path, body) => {
      calls.post.push({ path, body });
      if (path === '/api/v1/primary/known') return { known: body.hashes.filter((h) => known.includes(h)) };
      if (path === '/api/v1/bank/closing/settle') {
        return { companies: [{ company_id: 'c-a', company_name: 'Альфа', matched: 1,
          expense: { count: 3, amount: 119600, closed_amount: 106750, percent: 89.3 }, income: { count: 0 } }] };
      }
      const files = body.getAll('files');
      return { created: files.length, duplicates: 0, rejected: 0,
        results: files.map((f) => ({ file: f.name, result: 'created', company_id: 'c-a', company_name: 'Альфа', direction: 'in' })),
        companies: [{ company_id: 'c-a', name: 'Альфа', in: files.length, out: 0, duplicates: 0 }] };
    }),
  },
}));

const mod = await import('../../app/static/js/primary_folder.js');
const enc1251 = (text) => {
  // windows-1251 для кириллицы А–я и Ёё; латиница и знаки — как есть
  const out = [];
  for (const ch of text) {
    const c = ch.codePointAt(0);
    if (c < 128) out.push(c);
    else if (c >= 0x410 && c <= 0x44f) out.push(c - 0x410 + 0xc0);
    else if (c === 0x401) out.push(0xa8);
    else if (c === 0x451) out.push(0xb8);
    else out.push(0x3f);
  }
  return new Uint8Array(out);
};
const upd = (fn = 'СЧФДОП', id = 'ON_NSCHFDOPPR_1') => enc1251(`<?xml version="1.0" encoding="windows-1251"?>
<Файл ИдФайл="${id}" ВерсФорм="5.03"><Документ КНД="1115131" Функция="${fn}" ДатаИнфПр="10.10.2026"></Документ></Файл>`);
/** Настоящий ZIP с deflate: центральный каталог, локальные заголовки и CRC — как у выгрузки СБИС. */
const zipped = (docs) => {
  const enc = new TextEncoder();
  const crc = (bytes) => {
    let c = 0xffffffff;
    for (const b of bytes) {
      c ^= b;
      for (let k = 0; k < 8; k += 1) c = (c >>> 1) ^ (0xedb88320 & -(c & 1));
    }
    return (c ^ 0xffffffff) >>> 0;
  };
  const parts = [];
  const central = [];
  let offset = 0;
  for (const [name, body] of docs) {
    const nameBytes = enc.encode(name);
    const packed = new Uint8Array(deflateRawSync(Buffer.from(body)));
    const local = new Uint8Array(30 + nameBytes.length + packed.length);
    const lv = new DataView(local.buffer);
    lv.setUint32(0, 0x04034b50, true); lv.setUint16(4, 20, true); lv.setUint16(6, 0x0800, true);
    lv.setUint16(8, 8, true); lv.setUint32(14, crc(body), true);
    lv.setUint32(18, packed.length, true); lv.setUint32(22, body.length, true);
    lv.setUint16(26, nameBytes.length, true);
    local.set(nameBytes, 30); local.set(packed, 30 + nameBytes.length);
    const head = new Uint8Array(46 + nameBytes.length);
    const hv = new DataView(head.buffer);
    hv.setUint32(0, 0x02014b50, true); hv.setUint16(4, 20, true); hv.setUint16(6, 20, true);
    hv.setUint16(8, 0x0800, true); hv.setUint16(10, 8, true); hv.setUint32(16, crc(body), true);
    hv.setUint32(20, packed.length, true); hv.setUint32(24, body.length, true);
    hv.setUint16(28, nameBytes.length, true); hv.setUint32(42, offset, true);
    head.set(nameBytes, 46);
    parts.push(local); central.push(head); offset += local.length;
  }
  const cd = new Uint8Array(central.reduce((a, c) => a + c.length, 0));
  let at = 0;
  for (const c of central) { cd.set(c, at); at += c.length; }
  const eocd = new Uint8Array(22);
  const ev = new DataView(eocd.buffer);
  ev.setUint32(0, 0x06054b50, true); ev.setUint16(8, docs.length, true); ev.setUint16(10, docs.length, true);
  ev.setUint32(12, cd.length, true); ev.setUint32(16, offset, true);
  const out = new Uint8Array(offset + cd.length + 22);
  let pos = 0;
  for (const part of [...parts, cd, eocd]) { out.set(part, pos); pos += part.length; }
  return out;
};

const file = (path, bytes) => {
  const f = new File([bytes], path.split('/').pop());
  Object.defineProperty(f, 'webkitRelativePath', { value: path });
  return f;
};

beforeEach(() => { calls.post = []; known = []; });

describe('«Папка с УПД»: отбор файлов', () => {
  it('вид файла — по имени и по началу текста', () => {
    expect(mod.primFolderKind('a/ON_NSCHFDOPPOK_x.xml', '')).toBe('buyer');
    expect(mod.primFolderKind('a/ON_NKORSCHFDOPPR_x.xml', '')).toBe('correction');
    expect(mod.primFolderKind('a/DP_IZVPOL_x.xml', '')).toBe('edo');
    expect(mod.primFolderKind('a/подпись.sig', '')).toBe('notxml');
    expect(mod.primFolderKind('a/пакет.ZIP', '')).toBe('zip');
    expect(mod.primFolderKind('a/doc.xml', '<Файл ИдФайл="Z"><Документ Функция="СЧФДОП">')).toBe('upd');
    expect(mod.primFolderKind('a/doc.xml', '<Файл><Документ Функция="СЧФ">')).toBe('upd');
    expect(mod.primFolderKind('a/doc.xml', '<Файл><Документ Функция="КСЧФ">')).toBe('correction');
    expect(mod.primFolderKind('a/doc.xml', '<Файл ИдФайл="ON_NSCHFDOPPOK_1"><Документ Функция="СЧФДОП">')).toBe('buyer');
    expect(mod.primFolderKind('a/doc.xml', '<Файл ВерсФорм="5.03">')).toBe('maybe');          // решит сервер
    expect(mod.primFolderKind('a/config.xml', '<settings><a/></settings>')).toBe('other');
  });

  it('кодировка — из объявления XML; без объявления — UTF-8', () => {
    expect(mod.primFolderDecode(upd())).toContain('Функция="СЧФДОП"');
    expect(mod.primFolderDecode(new TextEncoder().encode('<Файл Функция="ДОП">'))).toBe('<Файл Функция="ДОП">');
  });

  it('порции: до 20 файлов и 8 МБ, архив — отдельно', () => {
    const many = Array.from({ length: 45 }, (_, i) => ({ name: `${i}.xml`, size: 1000 }));
    expect(mod.primFolderChunks(many).map((c) => c.length)).toEqual([20, 20, 5]);
    const big = [{ name: 'a.xml', size: 5e6 }, { name: 'b.xml', size: 5e6 }, { name: 'c.zip', size: 10 }, { name: 'd.xml', size: 1 }];
    expect(mod.primFolderChunks(big).map((c) => c.map((f) => f.name))).toEqual([['a.xml'], ['b.xml'], ['c.zip'], ['d.xml']]);
  });

  it('итоги порций складываются по компаниям; отказы — с причиной', () => {
    let acc = mod.primFolderMerge(null, { created: 2, duplicates: 1, rejected: 1,
      companies: [{ company_id: 'c-a', name: 'Альфа', in: 2, out: 0, duplicates: 1 }],
      results: [{ file: 'x.xml', result: 'rejected', message: 'не ваши компании' }] });
    acc = mod.primFolderMerge(acc, { created: 1, companies: [{ company_id: 'c-a', name: 'Альфа', in: 0, out: 1, duplicates: 0 }], results: [] });
    expect(acc.created).toBe(3);
    expect(acc.companies['c-a']).toEqual({ company_id: 'c-a', name: 'Альфа', in: 2, out: 1, duplicates: 1 });
    expect(acc.problems).toEqual([{ file: 'x.xml', message: 'не ваши компании' }]);
  });

  it('скрытые и служебные файлы не отправляются', async () => {
    const files = [file('Папка/.скрытый.xml', upd()), file('Папка/.DS_Store', new Uint8Array([1])),
      file('Папка/Thumbs.db', new Uint8Array([2])), file('Папка/ON_NSCHFDOPPR_1.xml', upd())];
    const scan = await mod.primFolderScan(files, () => {});
    expect(scan.counts.hidden).toBe(3);
    expect(scan.send.map((f) => f.name)).toEqual(['ON_NSCHFDOPPR_1.xml']);
    expect(mod.primFolderServicePath('.скрытый.xml')).toBe(true);
    expect(mod.primFolderServicePath('вложенная/.git/config.xml')).toBe(true);
    expect(mod.primFolderServicePath('ON_NSCHFDOPPR_1.xml')).toBe(false);
  });

  it('пустой файл не отправляется — серверу его читать незачем', async () => {
    const scan = await mod.primFolderScan([file('Папка/пустой.xml', new Uint8Array(0)), file('Папка/a.xml', upd())], () => {});
    expect(scan.counts.empty).toBe(1);
    expect(scan.send.map((f) => f.name)).toEqual(['a.xml']);
  });

  it('XML больше 5 МБ остаётся дома: словами, а не отказом сервера', async () => {
    const big = new Uint8Array(6 * 1024 * 1024);
    big.set(upd(), 0);
    const scan = await mod.primFolderScan([file('Папка/огромный.xml', big)], () => {});
    expect(scan.counts.too_big).toBe(1);
    expect(scan.send).toEqual([]);
    expect(scan.notices.join(' ')).toContain('больше 5 МБ не бывает');
  });

  it('архив разбирается на документы и уходит по одному, а не целиком', async () => {
    const archive = zipped([['внутри/ON_NSCHFDOPPR_5.xml', upd('СЧФДОП', 'ON_NSCHFDOPPR_5')],
      ['внутри/ON_NSCHFDOPPR_6.xml', upd('СЧФДОП', 'ON_NSCHFDOPPR_6')],
      ['внутри/подпись.sig', new Uint8Array([1, 2])]]);
    const scan = await mod.primFolderScan([file('Папка/архив.zip', archive)], () => {});
    expect(scan.counts.zip).toBe(1);
    expect(scan.send.map((f) => mod.primFolderPath(f))).toEqual(
      ['Папка/архив.zip/внутри/ON_NSCHFDOPPR_5.xml', 'Папка/архив.zip/внутри/ON_NSCHFDOPPR_6.xml']);
    expect(scan.notices).toEqual([]);
    const doc = new TextDecoder('windows-1251').decode(await mod.primFolderBytes(scan.send[0]));
    expect(doc).toContain('ИдФайл="ON_NSCHFDOPPR_5"');
  });

  it('архив больше 50 документов обрезается словами, а не отказом всей пачки', async () => {
    const docs = Array.from({ length: 60 }, (_, i) => [`папка/ON_NSCHFDOPPR_${i}.xml`, upd('СЧФДОП', `ON_NSCHFDOPPR_${i}`)]);
    const scan = await mod.primFolderScan([file('Папка/много.zip', zipped(docs))], () => {});
    expect(scan.send.length).toBe(50);
    expect(scan.notices.join(' ')).toContain('документов 60, за раз можно 50');
    expect(scan.notices.join(' ')).toContain('остальные 10');
  });

  it('битый архив и архив без XML объясняются, а не молчат', async () => {
    const broken = await mod.primFolderScan([file('Папка/битый.zip', new Uint8Array([0x50, 0x4b, 1, 2, 3]))], () => {});
    expect(broken.send).toEqual([]);
    expect(broken.notices.join(' ')).toContain('архив не читается');
    const empty = await mod.primFolderScan([file('Папка/пустой.zip',
      zipped([['внутри/подпись.sig', new Uint8Array([1])]]))], () => {});
    expect(empty.send).toEqual([]);
    expect(empty.notices.join(' ')).toContain('нет XML-документов');
  });

  it('замечания отбора печатаются в итоге', () => {
    const html = mod.primFolderSummaryHtml({ created: 0, duplicates: 0, rejected: 0, known: 0, sent: 0,
      companies: {}, settle: [], scan: { total: 1, counts: { too_big: 1 }, notices: ['огромный.xml — 12.0 МБ'] } });
    expect(html).toContain('prim-folder-notices');
    expect(html).toContain('огромный.xml — 12.0 МБ');
    expect(html).toContain('XML больше 5 МБ: 1');
  });

  it('перетащенная папка обходится целиком; не папка — null', async () => {
    const fileEntry = (name, body) => ({ isFile: true, isDirectory: false, name, file: (ok) => ok(new File([body], name)) });
    const dirEntry = (name, children) => {
      let given = false;
      return { isFile: false, isDirectory: true, name,
        createReader: () => ({ readEntries: (ok) => { ok(given ? [] : children); given = true; } }) };
    };
    const root = dirEntry('Октябрь', [fileEntry('a.xml', 'A'), dirEntry('S1', [fileEntry('b.xml', 'B')])]);
    const dt = { items: [{ webkitGetAsEntry: () => root }] };
    const files = await mod.primFolderFromDrop(dt);
    expect(files.map((f) => mod.primFolderPath(f))).toEqual(['Октябрь/a.xml', 'Октябрь/S1/b.xml']);
    expect(mod.primFolderFromDrop({ items: [{ webkitGetAsEntry: () => fileEntry('a.xml', 'A') }] })).toBeNull();
  });
});

describe('«Папка с УПД»: загрузка', () => {
  it('пропускает не-УПД и уже загруженное, шлёт порции с route=auto, затем сверка', async () => {
    const files = [file('Окт/S1/ON_NSCHFDOPPR_1.xml', upd()), file('Окт/S1/ON_NSCHFDOPPR_2.xml', upd('ДОП', 'ON_NSCHFDOPPR_2')),
      file('Окт/S1/ON_NSCHFDOPPOK_1.xml', upd('СЧФДОП', 'ON_NSCHFDOPPOK_1')), file('Окт/DP_IZVPOL_1.xml', enc1251('<Файл/>')),
      file('Окт/S1/подпись.sig', new Uint8Array([1, 2])), file('Окт/S1/ON_NSCHFDOPPR_3.xml', upd('СЧФДОП', 'ON_NSCHFDOPPR_3'))];
    known = [await mod.primFolderSha(files[0])];                  // первый файл уже был загружен раньше
    const progress = [];
    const sum = await mod.primFolderUpload(files, { onProgress: (t) => progress.push(t) });
    expect(calls.post.map((c) => c.path)).toEqual(['/api/v1/primary/known', '/api/v1/primary/upload', '/api/v1/bank/closing/settle']);
    const form = calls.post[1].body;
    expect(form.get('route')).toBe('auto');
    expect(form.getAll('files').map((f) => f.name)).toEqual(['Окт/S1/ON_NSCHFDOPPR_2.xml', 'Окт/S1/ON_NSCHFDOPPR_3.xml']);
    expect(calls.post[2].body).toEqual({ company_ids: ['c-a'] });
    expect(sum.known).toBe(1);
    expect(sum.scan.counts).toEqual({ upd: 3, buyer: 1, edo: 1, notxml: 1 });
    expect(progress).toContain('Сверяем с банковской выпиской…');
    const html = mod.primFolderSummaryHtml(sum);
    expect(html).toContain('<b>Папка: файлов 6.</b> УПД загружено: 2,');
    expect(html).toContain('уже были: 1');
    expect(html).toContain('титулы покупателя: 1');
    expect(html).toContain('расходы: закрыто');
    expect(html).toContain('(89,3 %)');
  });
});
