/* v1.72.38: браузерный замок общей вёрстки на реальных ширинах.
   Проверяем то, что нельзя проверить по тексту CSS:
   • ни на одном экране нет горизонтальной прокрутки страницы;
   • заголовок в шапке не обрезан и не разорван внутри слова;
   • на телефоне цели касания кнопок — не меньше 40 px (в .content 44 px),
     а шрифт полей — 16 px (иначе iOS зумит страницу на фокусе);
   • фильтры на телефоне стоят в одну колонку;
   • таблица прокручивается внутри карточки, а не растягивает страницу;
   • полоса табов настроек на планшете и ноутбуке не уезжает за край.
   Сервер и базу поднимает playwright.config.js (одноразовая SQLite). */
import { expect, test } from '@playwright/test';

const password = 'playwright-test-password';
const PAGES = [
  { route: '#/dashboard', title: 'Дашборд' },
  { route: '#/receipts', title: 'База чеков' },
  { route: '#/companies', title: 'Компании' },
  { route: '#/users', title: 'Пользователи и приглашения' },
  { route: '#/settings/data', title: 'Настройки' },
];

test.beforeAll(async ({ request }) => {
  const alreadySet = await request.post('/api/v1/auth/login', {
    data: { username: 'admin', password },
  });
  if (alreadySet.ok()) return;
  const loginResponse = await request.post('/api/v1/auth/login', {
    data: { username: 'admin', password: 'admin123' },
  });
  expect(loginResponse.ok()).toBeTruthy();
  const { access_token: token } = await loginResponse.json();
  const response = await request.post('/api/v1/auth/change-password', {
    headers: { Authorization: `Bearer ${token}` },
    data: { old_password: 'admin123', new_password: password },
  });
  expect(response.ok()).toBeTruthy();
});

async function login(page) {
  await page.goto('/');
  await page.fill('#login-username', 'admin');
  await page.fill('#login-password', password);
  await page.click('#login-submit');
  await expect(page.locator('#login-screen')).toBeHidden({ timeout: 15000 });
  await expect(page.locator('#page-title')).toHaveText('Дашборд', { timeout: 15000 });
  // v1.72.38: заголовок набран рукописным Caveat, а запасной cursive заметно шире.
  // Без этой строки замер попадал в момент до загрузки шрифта: в CI заголовок
  // «Пользователи и приглашения» мерился шире колонки и замок падал зря.
  await page.waitForFunction(() => document.fonts.status === 'loaded', null, { timeout: 15000 });
  const welcome = page.getByRole('button', { name: 'Понятно, работаем' });
  if (await welcome.isVisible().catch(() => false)) await welcome.click();
}

/** Горизонтальная прокрутка страницы (0 — её нет). */
const pageOverflow = (page) => page.evaluate(
  () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
);

for (const width of [1440, 1024, 768, 390]) {
  test.describe(`ширина ${width}px`, () => {
    test.use({ viewport: { width, height: width >= 768 ? 900 : 844 } });

    test('нет горизонтальной прокрутки и обрезанных заголовков', async ({ page }) => {
      await login(page);
      for (const { route, title } of PAGES) {
        await page.evaluate((h) => { location.hash = h; }, route);
        await expect(page.locator('#page-title')).toHaveText(title, { timeout: 15000 });
        await page.waitForTimeout(600);
        expect(await pageOverflow(page), `прокрутка на ${route}`).toBeLessThanOrEqual(1);
        const titleBox = await page.locator('#page-title').evaluate((el) => ({
          scrollW: el.scrollWidth,
          clientW: el.clientWidth,
          height: el.getBoundingClientRect().height,
          fontSize: parseFloat(getComputedStyle(el).fontSize),
          textOverflow: getComputedStyle(el).textOverflow,
        }));
        // заголовок либо помещается, либо переносится на две строки,
        // а если всё же не влезает — обрезается многоточием, но не «режется» посреди буквы
        expect(titleBox.clientW, `узкий заголовок на ${route}`).toBeGreaterThan(60);
        if (titleBox.scrollW > titleBox.clientW + 2) {
          expect(titleBox.textOverflow, `обрезан без многоточия на ${route}`).toBe('ellipsis');
        }
      }
    });

    test('таблицы прокручиваются внутри карточки', async ({ page }) => {
      await login(page);
      await page.evaluate(() => { location.hash = '#/companies'; });
      await expect(page.locator('#cp-table table')).toBeVisible({ timeout: 15000 });
      const info = await page.evaluate(() => {
        const table = document.querySelector('#cp-table table');
        const wrap = table.closest('.table-wrap');
        return {
          tableRight: table.getBoundingClientRect().right,
          wrapRight: wrap.getBoundingClientRect().right,
          wrapOverflowX: getComputedStyle(wrap).overflowX,
          wrapScrollable: wrap.scrollWidth > wrap.clientWidth,
        };
      });
      expect(info.wrapOverflowX).toBe('auto');
      if (info.wrapScrollable) {
        // таблица шире карточки — значит прокрутка, а не выход за страницу
        expect(info.tableRight).toBeGreaterThan(info.wrapRight - 2);
      }
      expect(await pageOverflow(page)).toBeLessThanOrEqual(1);
    });
  });
}

test.describe('телефон 390px', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('цели касания и шрифт полей', async ({ page }) => {
    await login(page);
    await page.evaluate(() => { location.hash = '#/receipts'; });
    await expect(page.locator('#f-q')).toBeVisible({ timeout: 15000 });
    await page.waitForTimeout(600);

    const small = await page.evaluate(() => [...document.querySelectorAll('.content button, .content .btn, .content a.btn')]
      .filter((el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.height < 40;
      })
      .map((el) => `${el.className}:${Math.round(el.getBoundingClientRect().height)}`));
    expect(small, 'кнопки ниже 40 px на телефоне').toEqual([]);

    const fs = await page.evaluate(() => [...document.querySelectorAll('.content input:not([type="checkbox"]):not([type="radio"]), .content select, .content textarea')]
      .filter((el) => el.getBoundingClientRect().height > 0)
      .map((el) => parseFloat(getComputedStyle(el).fontSize)));
    expect(Math.min(...fs), 'шрифт полей меньше 16 px — iOS зумит страницу').toBeGreaterThanOrEqual(16);
  });

  test('фильтры в одну колонку', async ({ page }) => {
    await login(page);
    await page.evaluate(() => { location.hash = '#/receipts'; });
    await expect(page.locator('#f-q')).toBeVisible({ timeout: 15000 });
    await page.waitForTimeout(600);
    const widths = await page.evaluate(() => [...document.querySelectorAll('.content .filter-bar .field')]
      .map((el) => Math.round(el.getBoundingClientRect().width)));
    const bar = await page.evaluate(() => Math.round(document.querySelector('.content .filter-bar').getBoundingClientRect().width));
    expect(widths.length).toBeGreaterThan(0);
    for (const w of widths) expect(w).toBeGreaterThan(bar * 0.9);
  });

  test('шапка: заголовок и компания на разных строках', async ({ page }) => {
    await login(page);
    await page.waitForTimeout(400);
    const geo = await page.evaluate(() => {
      const title = document.querySelector('#page-title').getBoundingClientRect();
      const select = document.querySelector('#company-filter').getBoundingClientRect();
      return { titleBottom: title.bottom, selectTop: select.top, titleWidth: title.width };
    });
    expect(geo.titleWidth).toBeGreaterThan(150);
    expect(geo.selectTop).toBeGreaterThanOrEqual(geo.titleBottom - 2);
  });

  test('выдвижное меню закрывает фон и не тянет страницу', async ({ page }) => {
    await login(page);
    await page.locator('#btn-sidebar').click();
    await expect(page.locator('body')).toHaveClass(/sb-open/);
    // панель выезжает за 0,25 с — ждём конца перехода, а не первый кадр
    await expect.poll(async () => (await page.locator('.sidebar').boundingBox()).x,
      { timeout: 5000, message: 'панель не доехала до края' }).toBeGreaterThanOrEqual(-1);
    const box = await page.locator('.sidebar').boundingBox();
    expect(box.width).toBeLessThanOrEqual(390);
    const items = await page.evaluate(() => [...document.querySelectorAll('.nav-item')]
      .filter((el) => el.getBoundingClientRect().width > 0)
      .map((el) => Math.round(el.getBoundingClientRect().height)));
    for (const h of items) expect(h).toBeGreaterThanOrEqual(40);
    await page.locator('#sb-close').click();
    await expect(page.locator('body')).not.toHaveClass(/sb-open/);
  });
});

test.describe('планшет и ноутбук: табы настроек', () => {
  for (const width of [1024, 768]) {
    test.use({ viewport: { width, height: 900 } });
    test(`полоса табов не уезжает за край на ${width}px`, async ({ page }) => {
      await login(page);
      await page.evaluate(() => { location.hash = '#/settings/data'; });
      await expect(page.locator('#st-backups')).toBeVisible({ timeout: 15000 });
      await page.waitForTimeout(600);
      const info = await page.evaluate(() => {
        const row = document.querySelector('.stg-tabs-row');
        const nav = document.querySelector('#settings-nav').getBoundingClientRect();
        const last = [...document.querySelectorAll('.stg-tab')].pop().getBoundingClientRect();
        return {
          missing: row.scrollWidth - row.clientWidth,
          lastRight: last.right,
          navRight: nav.right,
          rowHeight: row.getBoundingClientRect().height,
        };
      });
      // все табы помещаются: либо в одну строку, либо переносом на вторую
      expect(info.missing, 'табы ушли в прокрутку').toBeLessThanOrEqual(1);
      expect(info.lastRight).toBeLessThanOrEqual(info.navRight + 1);
      expect(info.rowHeight).toBeGreaterThan(40);
    });
  }
});
