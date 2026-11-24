import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';

const source = readFileSync('app/static/js/app.js', 'utf8');
const start = source.indexOf('function aoReportFilename(');
const end = source.indexOf('// --- v1.14.0: CSV', start);
// Новая функция выбора «Сохранить PDF / Распечатать» использует helpers из
// ao_folder.js; в этом юнит-стенде подменяем их минимальными реализациями.
const makePreview = new Function('esc', '$', 'aoCsvDownload', 'ao1Json', 'downloadBlob',
  'toast', 'aoMultiPrint', 'ao1Print', 'printReceiptsPDF', 'openAO1Modal',
  'aoFolderDisplay', 'aoJsonDownload', 'aoSaveFromButton', '__choiceFn',
  'aoFolderSegments', 'aoDownloadsPath', 'aoSafeSegment', 'aoPdfZipReadme',
  'aoAsciiFilename', 'aoFlatPrefix', 'aoSaveFile', 'aoDownloadBlob', 'aoBlobBytes',
  'aoPdfTarget', 'api', 'aoSavePdf',
  `${source.slice(start, end)}; aoPrintChoice = __choiceFn; return aoShowPreview;`);

function setup(rows, byAssignee = [{ name: 'Иван', sum: 10, count: rows.length }]) {
  document.body.innerHTML = '<div id="modal-root"><div id="slot"></div></div>';
  const printReceipt = vi.fn(); const standard = vi.fn(); const multi = vi.fn(); const toast = vi.fn();
  const csv = vi.fn(); const json = vi.fn(); const saveFromButton = vi.fn();
  // В рабочем приложении aoPrintChoice показывает выбор «Сохранить / Печать»;
  // в юнит-тесте сразу выполняем прежний путь печати, чтобы сохранить его контракт.
  const printChoice = (_slot, _rep, _meta, doPrint) => doPrint();
  const preview = makePreview(x => String(x ?? ''), () => document.getElementById('modal-root'),
    csv, vi.fn(), vi.fn(), toast, multi, standard, printReceipt, vi.fn(),
    () => 'Загрузки/АО/ООО «Ямастер»', json, saveFromButton, printChoice,
    () => ['АО', 'ООО «Ямастер»'], () => 'Загрузки/АО/ООО «Ямастер»',
    x => String(x), () => 'PDF readme', x => String(x), () => 'AO',
    vi.fn(), vi.fn(), vi.fn(), vi.fn(), { download: vi.fn() }, vi.fn());
  const rep = { rows, by_assignee: byAssignee, period: { from: '2026-10-01', to: '2026-10-31' },
    total: { count: rows.length, sum: 10 }, invalid_count: 0, truncated: false };
  const slot = document.getElementById('slot');
  preview(slot, rep, { 'ao-num': 'АО-2026-10-46', 'ao-date': '2026-10-31' });
  return { slot, rep, printReceipt, standard, multi, toast, csv, json, saveFromButton };
}
const row = (id, assignee = 'Иван', operation = 1, personal_sum = 0) => ({ id, assignee, operation, personal_sum });

describe('выбор печати авансового отчёта', () => {
  it('по умолчанию только АО-1; приложения — по выбору', () => {
    const x = setup([row('a'), row('b')]);
    x.slot.querySelector('#ao-print').click();
    expect(x.standard).toHaveBeenCalledOnce();
    expect(x.printReceipt).not.toHaveBeenCalled();
    x.slot.querySelector('#ao-include').value = 'receipts';
    x.slot.querySelector('#ao-print').click();
    expect(x.printReceipt.mock.calls[0][0]).toEqual(['a', 'b']);
    x.printReceipt.mock.calls[0][1](3);
    expect(x.standard.mock.calls[1][2].attachmentPages).toBe(3);
    expect(x.standard.mock.calls[1][3]).toBe(true);
  });
  it('порядок приложений соответствует сгруппированной ведомости', () => {
    const x = setup([row('b', 'Б'), row('a', 'А')], [{ name: 'А', sum: 5, count: 1 }, { name: 'Б', sum: 5, count: 1 }]);
    x.slot.querySelector('#ao-include').value = 'receipts';
    x.slot.querySelector('#ao-print').click();
    expect(x.printReceipt.mock.calls[0][0]).toEqual(['a', 'b']);
  });
  it('не печатает возвраты и личные расходы как обычные расходы', () => {
    for (const r of [row('a', 'Иван', 2), row('a', 'Иван', 1, 10), row('a', 'Иван', null)]) {
      const x = setup([r]); x.slot.querySelector('#ao-print').click();
      expect(x.standard).not.toHaveBeenCalled(); expect(x.toast).toHaveBeenCalled();
    }
  });
  it('не смешивает организации в одном отчёте', () => {
    const a = { ...row('a'), company_id: 'one' }, b = { ...row('b'), company_id: 'two' };
    const x = setup([a, b]); x.slot.querySelector('#ao-print').click();
    expect(x.standard).not.toHaveBeenCalled(); expect(x.toast).toHaveBeenCalled();
  });
  it('не создаёт комплект по обрезанному отчёту', () => {
    const x = setup([row('a')]); x.rep.truncated = true;
    x.slot.querySelector('#ao-include').value = 'receipts';
    x.slot.querySelector('#ao-print').click();
    expect(x.printReceipt).not.toHaveBeenCalled(); expect(x.toast).toHaveBeenCalled();
  });
});


describe('папка авансового отчёта в «Загрузках»', () => {
  it('показывает путь «АО/компания» и сохраняет комплект по кнопке', () => {
    const x = setup([row('a')]);
    expect(x.slot.innerHTML).toContain('Загрузки/АО/ООО «Ямастер»');
    expect(x.slot.querySelector('#ao-folder-status')).not.toBeNull();
    x.slot.querySelector('#ao-folder').click();
    expect(x.saveFromButton).toHaveBeenCalledWith(
      x.rep, expect.objectContaining({ 'ao-num': 'АО-2026-10-46' }));
  });
  it('CSV и JSON для 1С получают параметры отчёта для имени файла', () => {
    const x = setup([row('a')]);
    x.slot.querySelector('#ao-csv').click();
    x.slot.querySelector('#ao-json').click();
    expect(x.csv).toHaveBeenCalledWith(x.rep, expect.objectContaining({ 'ao-num': 'АО-2026-10-46' }));
    expect(x.json).toHaveBeenCalledWith(x.rep, expect.objectContaining({ 'ao-num': 'АО-2026-10-46' }));
  });
});


describe('настоящий PDF вместо только окна печати', () => {
  const startBody = source.indexOf('function aoPdfRequestBody');
  const endBody = source.indexOf('async function aoPdfTarget', startBody);
  const requestBody = new Function(`${source.slice(startBody, endBody)}; return aoPdfRequestBody;`)();

  it('кнопка «Печать / PDF» предлагает сохранить файл или распечатать бланк', () => {
    expect(source).toContain("slot.querySelector('#ao-print').onclick = () => aoPrintChoice(");
    expect(source).toContain('id="ao-pdf-save"');
    expect(source).toContain('id="ao-pdf-print"');
    expect(source).toContain('💾 Сохранить PDF');
    expect(source).toContain("'/api/v1/receipts/advance-report/pdf'");
  });

  it('запрос PDF несёт те же чеки, номер и состав, что видит бухгалтер', () => {
    const rep = { period: { from: '2026-11-01', to: '2026-11-23' }, company: { id: 'c1' },
      rows: [{ id: 'r1' }, { id: 'r2' }], by_assignee: [] };
    const meta = { 'ao-num': 'АО-2026-11-01', 'ao-date': '2026-11-23' };
    expect(requestBody(rep, meta, true)).toEqual({
      date_from: '2026-11-01', date_to: '2026-11-23', company_id: 'c1',
      receipt_ids: ['r1', 'r2'], only_valid: false,
      report_number: 'АО-2026-11-01', report_date: '2026-11-23',
      meta, include_receipts: true,
    });
  });
});


describe('имя PDF отчёта', () => {
  const extract = source.slice(source.indexOf('function aoReportFilename('), source.indexOf('// --- v1.14.0: предпросмотр'));
  const { filename, printTitle } = new Function(`${extract}; return { filename: aoReportFilename, printTitle: printWithTitle };`)();
  it('включает номер, сокращённое название, дату и сумму в рублях и копейках', () => {
    const rep = { total: { sum: 56755.34 }, requisites: { 'Организация': { 'НаименованиеСокращенное': 'ООО «Ямастер»' } } };
    expect(filename(rep, { 'ao-num': 'АО-2026-10-46', 'ao-date': '2026-10-31' }))
      .toBe('АО-2026-10-46-ООО «Ямастер»-2026-10-31-56755-34');
  });
  it('удаляет запрещённые символы файлов и восстанавливает заголовок после печати', () => {
    expect(filename({ total: { sum: 10 }, company: { name: 'ООО А/Б:*' } }, { 'ao-num': 'АО-1', 'ao-date': '2026-10-31' }))
      .toBe('АО-1-ООО А Б-2026-10-31-10-00');
    document.title = 'Приложение'; const original = window.print;
    window.print = vi.fn(() => expect(document.title).toBe('Номер отчёта'));
    printTitle('Номер отчёта'); window.dispatchEvent(new Event('afterprint'));
    expect(document.title).toBe('Приложение'); window.print = original;
  });
});
