/* v1.73.0 (T-109, ADR-60): «📥 Приёмная папка» — итог обхода, режим, время и разметка журнала.
   API подменён, данные синтетические; проверяются чистые функции экрана. */
import { describe, expect, it, vi } from 'vitest';

const mod = await import('../../app/static/js/primary_inbox.js');

describe('inboxSummary', () => {
  it('собирает итог словами и не печатает нули', () => {
    const text = mod.inboxSummary({ files: 12, ok: 9, duplicate: 2, rejected: 1 });
    expect(text).toBe('файлов: 12 · в реестр: 9 · уже было: 2 · не принято: 1');
  });

  it('пустой обход — только число файлов', () => {
    expect(mod.inboxSummary({ files: 0 })).toBe('файлов: 0');
  });

  it('сообщает о недописанных файлах и остатке на следующий раз', () => {
    const text = mod.inboxSummary({ files: 5, ok: 1, waiting: 1, left: 3 });
    expect(text).toContain('дописываются: 1');
    expect(text).toContain('осталось на следующий раз: 3');
  });

  it('терпит пустой ответ', () => {
    expect(mod.inboxSummary(null)).toBe('файлов: 0');
  });
});

describe('inboxModeText', () => {
  it('без пути — просит настроить', () => {
    expect(mod.inboxModeText({ path: '' })).toBe('папка не задана');
  });

  it('фон включён — называет интервал', () => {
    expect(mod.inboxModeText({ path: '/inbox', enabled: true, interval_minutes: 15 }))
      .toBe('фон: каждые 15 мин');
  });

  it('фон выключен — честно говорит, что только по кнопке', () => {
    expect(mod.inboxModeText({ path: '/inbox', enabled: false })).toContain('только по кнопке');
  });

  it('выключенный на сервере обход важнее настройки', () => {
    expect(mod.inboxModeText({ path: '/inbox', enabled: true, off: true }))
      .toBe('фоновый обход выключен на сервере');
  });
});

describe('inboxWhen', () => {
  it('пусто — «ещё не обходили»', () => {
    expect(mod.inboxWhen('')).toBe('ещё не обходили');
  });

  it('момент UTC показывается как дата и время', () => {
    expect(mod.inboxWhen('2026-11-16T11:05:00Z')).toMatch(/16\.11 \d\d:\d\d/);
  });

  it('мусор не ломает экран', () => {
    expect(mod.inboxWhen('не дата')).toBe('—');
  });
});

describe('inboxErrorText', () => {
  it('слова сервера важнее кода', () => {
    const err = new Error('Каталог не найден: /srv/inbox');
    expect(mod.inboxErrorText(err)).toBe('Каталог не найден: /srv/inbox');
  });

  it('без слов — объясняет код 403', () => {
    const err = new Error('Ошибка');
    err.status = 403;
    expect(mod.inboxErrorText(err)).toBe('Путь вне разрешённых каталогов сервера');
  });
});

describe('inboxPanelHtml', () => {
  const state = {
    path: '/srv/inbox', enabled: true, interval_minutes: 10, roots: ['/srv/inbox'], problems: [],
    counters: { total: 3, ok: 2, duplicate: 1, rejected: 0 }, last_run_at: '2026-11-16T11:05:00Z',
  };

  it('бухгалтеру не показывает поля настройки пути', () => {
    const html = mod.inboxPanelHtml(state, { items: [] }, { isAdmin: false });
    expect(html).not.toContain('inbox-path');
    expect(html).toContain('inbox-run');
    expect(html).toContain('забрать новые файлы в свои компании');
  });

  it('администратору показывает путь, интервал и включение', () => {
    const html = mod.inboxPanelHtml(state, { items: [] }, { isAdmin: true });
    expect(html).toContain('id="inbox-path"');
    expect(html).toContain('id="inbox-interval"');
    expect(html).toContain('id="inbox-enabled"');
  });

  it('проблемы настройки видны словами', () => {
    const html = mod.inboxPanelHtml({ ...state, problems: ['Каталог не найден: /srv/inbox'] }, {}, { isAdmin: true });
    expect(html).toContain('Каталог не найден');
  });

  it('итог последнего обхода написан словами, а не нулями', () => {
    const withResult = { ...state, last_result: { files: 12, ok: 9, duplicate: 2, rejected: 1, later: 3 } };
    expect(mod.inboxLastRun(withResult))
      .toBe('файлов: 12 · в реестр: 9 · уже было: 2 · не принято: 1 · дописываются: 3');
    expect(mod.inboxPanelHtml(withResult, {}, { isAdmin: false })).toContain('файлов: 12');
    expect(mod.inboxLastRun({ ...state, last_result: {} })).toContain('Файлов на разбор не было');
  });

  it('ошибка последнего обхода не молчит', () => {
    const html = mod.inboxPanelHtml({ ...state, last_error: 'Нет прав на запись: /srv/inbox' }, {}, { isAdmin: true });
    expect(html).toContain('Последний обход не удался');
    expect(html).toContain('Нет прав на запись');
    expect(mod.inboxPanelHtml(state, {}, { isAdmin: true })).not.toContain('Последний обход не удался');
  });

  it('журнал печатает файл, итог и причину', () => {
    const html = mod.inboxPanelHtml(state, { items: [{ file: 'сбис/УПД.xml', status: 'rejected',
      message: 'Ни покупатель, ни продавец — не ваши компании', created_at: '2026-11-16T11:05:00Z' }] },
    { isAdmin: false });
    expect(html).toContain('сбис/УПД.xml');
    expect(html).toContain('не принят');
    expect(html).toContain('не ваши компании');
  });

  it('пустой журнал объясняет, что программа ещё не брала файлы', () => {
    expect(mod.inboxPanelHtml(state, {}, { isAdmin: false })).toContain('Журнал пуст');
  });

  it('экранирует имена файлов из папки', () => {
    const html = mod.inboxPanelHtml(state, { items: [{ file: '<img src=x onerror=alert(1)>.xml',
      status: 'ok', message: '', created_at: '' }] }, { isAdmin: false });
    expect(html).not.toContain('<img src=x');
    expect(html).toContain('&lt;img');
  });
});
