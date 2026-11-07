/* v1.70.101 (T-69): понимание чеков — читаемые позиции, чип категории с причиной, выбор категории бухгалтером
   (запоминается для компании), сводка по категориям, подсказка статьи, память товаров и профиль компании.
   API подменён, данные синтетические. */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { get: [], put: [], del: [] };
const catalog = {
  categories: [
    { code: 'build_base', title: 'Стройматериалы', group: 'Строительные материалы и инструмент', icon: '🧱' },
    { code: 'build_finish', title: 'Отделочные материалы', group: 'Строительные материалы и инструмент', icon: '🎨' },
    { code: 'food', title: 'Продукты', group: 'Продукты питания', icon: '🛒' },
    { code: 'other', title: 'Прочее', group: 'Прочее', icon: '❔' },
  ],
};

vi.mock('../../app/static/js/api.js', () => ({
  api: {
    get: vi.fn(async (path) => { calls.get.push(path); return catalog; }),
    put: vi.fn(async (path, body) => {
      calls.put.push({ path, body });
      return { message: '«Смесь Абвгд» — Отделочные материалы; запомнено для компании',
        item: { category: body.category, category_title: 'Отделочные материалы', category_icon: '🎨',
          category_source: 'manual', category_source_title: 'исправлено вручную', category_certain: true } };
    }),
    del: vi.fn(async (path) => { calls.del.push(path); return { message: 'Забыто' }; }),
  },
}));
vi.mock('../../app/static/js/ui.js', () => ({
  esc: (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'),
  fmtSum: (n) => `${Number(n || 0).toFixed(2)} ₽`,
  toast: vi.fn(),
}));

const mod = await import('../../app/static/js/goods.js');

const items = [
  { id: 'i1', name: 'ЦЕМЕНТ М500 Д0 50КГ', display_name: 'Цемент М500 Д0, 50 кг', pack: '50 кг', quantity: 2,
    price: 600, total: 1200, category: 'build_base', category_title: 'Стройматериалы', category_icon: '🧱',
    category_source: 'dictionary', category_source_title: 'словарь', category_note: 'по слову «цемент»',
    category_certain: true },
  { id: 'i2', name: 'Смесь Абвгд 25кг', display_name: 'Смесь Абвгд, 25 кг', pack: '25 кг', quantity: 1,
    price: 500, total: 500, category: 'build_base', category_title: 'Стройматериалы', category_icon: '🧱',
    category_source: 'context', category_source_title: 'по чеку и продавцу',
    category_note: 'по другим товарам чека («Строительные материалы и инструмент»)', category_certain: false },
  { id: 'i3', name: '<script>alert(1)</script>', display_name: '<script>alert(1)</script>', quantity: 1, price: 1,
    total: 1, category: 'other', category_title: 'Прочее', category_icon: '❔', category_source: 'none' },
];
const goods = {
  categories: [{ category: 'build_base', title: 'Стройматериалы', icon: '🧱', count: 2, sum: 1700 }],
  article_hint: { article: 'Материалы', share: 0.99, reason: 'по товарам чека: 99 % суммы, профиль «Стройка и ремонт»' },
};

beforeEach(() => { calls.get.length = 0; calls.put.length = 0; calls.del.length = 0; });

describe('позиции чека', () => {
  it('читаемое название, исходная строка по наведению, фасовка и сводка по категориям', () => {
    const html = mod.goodsItemsHTML(items, { editable: false, goods });
    expect(html).toContain('Цемент М500 Д0, 50 кг');
    expect(html).toContain('title="В чеке: ЦЕМЕНТ М500 Д0 50КГ"');
    expect(html).toContain('<span class="gd-pack">50 кг</span>');
    expect(html).toContain('🧱 Стройматериалы · 2 · 1700.00 ₽');
    expect(html).not.toContain('<script>');                    // внешние строки экранируются
    expect(html).not.toContain('gd-edit-btn');                 // сотрудник категорию не меняет
  });

  it('догадка помечена «?» и объясняет причину', () => {
    const chip = mod.goodsChipHTML(items[1], false);
    expect(chip).toContain('gd-unsure');
    expect(chip).toContain('Стройматериалы ?');
    expect(chip).toContain('по чеку и продавцу: по другим товарам чека');
  });

  it('бухгалтер выбирает категорию — запрос с «запомнить», чип обновлён', async () => {
    document.body.innerHTML = `<div id="root">${mod.goodsItemsHTML(items, { editable: true, goods })}</div>`;
    const root = document.getElementById('root');
    const saved = vi.fn();
    mod.goodsBindItems(root, items, { onSaved: saved });
    root.querySelector('[data-gd-item="i2"]').click();
    await vi.waitFor(() => expect(root.querySelector('.gd-pick')).not.toBeNull());
    const sel = root.querySelector('.gd-pick');
    expect(sel.querySelectorAll('optgroup').length).toBe(3);
    sel.value = 'build_finish';
    sel.onchange();
    await vi.waitFor(() => expect(saved).toHaveBeenCalled());
    expect(calls.put[0]).toEqual({ path: '/api/v1/goods/items/i2', body: { category: 'build_finish', remember: true } });
    expect(root.querySelector('[data-gd-item="i2"]').textContent).toContain('Отделочные материалы');
    expect(calls.get).toEqual(['/api/v1/goods/catalog']);
  });

  it('подсказка статьи — только с кнопкой у бухгалтера', () => {
    expect(mod.goodsArticleHintHTML(goods, true)).toContain('data-article="Материалы"');
    expect(mod.goodsArticleHintHTML(goods, false)).not.toContain('<button');
    expect(mod.goodsArticleHintHTML({ categories: [] }, true)).toBe('');
  });
});

describe('память товаров и профиль', () => {
  it('список исправлений и «Забыть»', async () => {
    const rows = [{ id: 'm1', name_key: 'смесь абвгд', sample_name: 'Смесь Абвгд, 25 кг', category_title: 'Отделочные материалы',
      icon: '🎨', hits: 3 }];
    document.body.innerHTML = `<div id="mem">${mod.goodsMemoryHTML(rows, true)}</div>`;
    const changed = vi.fn();
    mod.goodsBindMemory(document.getElementById('mem'), { companyId: 'c-1', onChange: changed });
    expect(document.body.innerHTML).toContain('ключ: смесь абвгд');
    document.querySelector('.gd-forget').click();
    await vi.waitFor(() => expect(changed).toHaveBeenCalled());
    expect(calls.del).toEqual(['/api/v1/goods/memory/m1?company_id=c-1']);
    expect(mod.goodsMemoryHTML([], true)).toContain('Пока пусто');
  });

  it('профиль компании: подсказка меняется вместе с выбором, бухгалтеру — только чтение', () => {
    const pol = { goods_profile: 'general', goods_own: true, goods_profiles: [
      { code: 'general', title: 'Общий', hint: 'как раньше' },
      { code: 'catering', title: 'Общепит', hint: 'продукты — сырьё кухни' }] };
    document.body.innerHTML = `<div id="pol">${mod.goodsProfileHTML(pol, true)}</div>`;
    mod.goodsBindProfile(document.getElementById('pol'), pol);
    const sel = document.getElementById('pol-profile');
    sel.value = 'catering';
    sel.dispatchEvent(new Event('change'));
    expect(document.getElementById('pol-profile-hint').textContent).toBe('продукты — сырьё кухни');
    expect(document.getElementById('pol-goods-reset')).not.toBeNull();
    expect(mod.goodsProfileHTML(pol, false)).toContain('disabled');
    expect(mod.goodsProfileHTML({}, true)).toBe('');
  });
});
