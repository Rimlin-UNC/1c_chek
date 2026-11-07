/* v1.70.113 (T-79, #63): «📁 Папка с УПД» — отбор УПД среди файлов папки, кодировка, порции, обход перетащенной
   папки, пропуск уже загруженного по SHA-256, итог по компаниям. API подменён, данные синтетические. */
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
