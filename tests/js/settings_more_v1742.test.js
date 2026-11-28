import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { JSDOM } from 'jsdom';
import { filterSettingsCards } from '../../app/static/js/settings_layout.js';

const ROOT = resolve(import.meta.dirname, '..', '..');
const APP_JS = readFileSync(resolve(ROOT, 'app/static/js/app.js'), 'utf-8');

describe('v1.74.2 — filterSettingsCards (чистый поиск карточек настроек)', () => {
  const catalog = [
    { id: 'st-backups', sec: 'data', secTitle: 'Данные и восстановление', label: 'Резервные копии', hint: 'архивы базы и автокопии' },
    { id: 'wa-card', sec: 'personal', secTitle: 'Личное и доступ', label: 'Быстрый вход', hint: 'отпечаток / лицо / PIN устройства' },
    { id: 'st-billing', sec: 'personal', secTitle: 'Личное и доступ', label: 'Платные подписки и тарифы', hint: 'MRR, ARR, счета и промокоды' },
    { id: 'st-1c', sec: 'integr', secTitle: 'Чек-Пул и интеграции', label: 'Интеграция с 1С', hint: 'токен выгрузки и правила сопоставления' },
    { id: 'st-maint', sec: 'device', secTitle: 'Устройство и служебное', label: 'Обслуживание и сброс кэша', hint: 'перезапуск Service Worker и очистка' },
  ];

  it('пустой запрос возвращает копию всего каталога', () => {
    const res = filterSettingsCards(catalog, '   ');
    expect(res).toHaveLength(catalog.length);
    expect(res).not.toBe(catalog);
  });

  it('находит карточку без учёта регистра и буквы ё/е', () => {
    const res = filterSettingsCards(catalog, 'РЕЗЕРВНЫЕ');
    expect(res.map((x) => x.id)).toEqual(['st-backups']);
    const yo = filterSettingsCards([
      { id: 'a', label: 'Учёт и расчёты', hint: 'описание', secTitle: 'Данные' },
    ], 'учет расчеты');
    expect(yo.map((x) => x.id)).toEqual(['a']);
  });

  it('поддерживает многословный поиск по заголовку, подсказке и разделу', () => {
    expect(filterSettingsCards(catalog, 'токен 1с').map((x) => x.id)).toEqual(['st-1c']);
    expect(filterSettingsCards(catalog, 'личное отпечаток').map((x) => x.id)).toEqual(['wa-card']);
  });

  it('ставит совпадение в заголовке выше совпадения в подсказке', () => {
    const items = [
      { id: 'hint-match', label: 'Параметры обмена', hint: 'быстрый вход по ключу', secTitle: 'Интеграции' },
      { id: 'title-match', label: 'Быстрый вход', hint: 'ключ безопасности', secTitle: 'Личное' },
    ];
    expect(filterSettingsCards(items, 'быстрый вход').map((x) => x.id)).toEqual([
      'title-match',
      'hint-match',
    ]);
  });

  it('безопасно обрабатывает не-массив и пустые элементы', () => {
    expect(filterSettingsCards(null, 'тест')).toEqual([]);
    expect(filterSettingsCards([null, undefined, catalog[0]], 'копии').map((x) => x.id)).toEqual(['st-backups']);
  });
});

describe('v1.74.2 — DOM-структура #settings-nav и setSettingsCardCollapsed', () => {
  it('держит .stg-tabs-tools вне .stg-tabs-row[role="tablist"], чтобы меню не обрезалось на телефоне', () => {
    const navMatch = APP_JS.match(/<nav class="settings-nav settings-tabs" id="settings-nav"[\s\S]*?<\/nav>/);
    expect(navMatch).toBeTruthy();
    const html = navMatch[0]
      .replace(/\$\{[\s\S]*?\}/g, '<button type="button" role="tab" class="stg-tab" data-sec="data">Данные</button>');
    const dom = new JSDOM(`<!doctype html><body>${html}</body>`);
    const doc = dom.window.document;
    const bar = doc.querySelector('.stg-tabs-bar');
    const row = doc.querySelector('.stg-tabs-row[role="tablist"]');
    const tools = doc.querySelector('.stg-tabs-tools');
    const moreBtn = doc.querySelector('#snav-more');
    const moreMenu = doc.querySelector('#snav-more-menu');

    expect(bar).toBeTruthy();
    expect(row).toBeTruthy();
    expect(tools).toBeTruthy();
    expect(row.contains(tools)).toBe(false);
    expect(bar.contains(row)).toBe(true);
    expect(bar.contains(tools)).toBe(true);
    expect(tools.contains(moreBtn)).toBe(true);
    expect(tools.contains(moreMenu)).toBe(true);
    expect(moreMenu.getAttribute('role')).toBe('menu');

    const menuIds = Array.from(moreMenu.querySelectorAll('[role="menuitem"]')).map((el) => el.id);
    expect(menuIds).toEqual([
      'snav-search',
      'snav-toc',
      'snav-all',
      'snav-expand',
      'snav-collapse',
      'snav-copy-link',
    ]);
  });

  it('setSettingsCardCollapsed синхронизирует .collapsed, aria-expanded и localStorage["ymaster-fold"]', () => {
    const fnMatch = APP_JS.match(/function foldKey\(card\)[\s\S]*?function makeSettingsCollapsible\(container\)[\s\S]*?\n\}/);
    expect(fnMatch).toBeTruthy();
    const dom = new JSDOM(`<!doctype html><body>
      <div class="settings-grid">
        <div class="glass card" id="c1"><div class="card-title">🔑 Быстрый вход</div><p>Тело 1</p></div>
        <div class="glass card" id="c2"><div class="card-title">🎨 Оформление</div><p>Тело 2</p></div>
      </div>
    </body>`, { url: 'https://chek.ymaster.ru/' });
    const win = dom.window;
    const runner = new win.Function(
      'document',
      'localStorage',
      `${fnMatch[0]}; return { makeSettingsCollapsible, setSettingsCardCollapsed, foldState };`,
    );
    const { makeSettingsCollapsible, setSettingsCardCollapsed, foldState } = runner(win.document, win.localStorage);
    const grid = win.document.querySelector('.settings-grid');
    makeSettingsCollapsible(grid);

    const c1 = win.document.getElementById('c1');
    const c2 = win.document.getElementById('c2');
    expect(c1.classList.contains('foldable')).toBe(true);
    expect(c1.classList.contains('collapsed')).toBe(false);

    setSettingsCardCollapsed(c1, true);
    setSettingsCardCollapsed(c2, true);
    expect(c1.classList.contains('collapsed')).toBe(true);
    expect(c1.querySelector('.card-title').getAttribute('aria-expanded')).toBe('false');
    expect(Object.values(foldState()).every((v) => v === true)).toBe(true);

    setSettingsCardCollapsed(c1, false);
    expect(c1.classList.contains('collapsed')).toBe(false);
    expect(c1.querySelector('.card-title').getAttribute('aria-expanded')).toBe('true');
  });
});
