import { expect, test } from '@playwright/test';

test.use({ serviceWorkers: 'block' });

test('запуск после обновления не загружает старый кешированный модуль мониторинга', async ({ page }) => {
  test.setTimeout(90000);
  const errors = [], urls = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem('ymaster_check_token', 'invalid-test-token');
    localStorage.setItem('ymaster_check_offline_queue', 'сохранить-старую-очередь');
  });
  await page.route('**/js/osm.js*', async route => {
    const url = new URL(route.request().url());
    urls.push(url.href);
    if (!url.search) {
      // Имитация браузерного кэша версии до появления localServer*.
      await route.fulfill({ contentType: 'text/javascript',
        body: 'export function osmCardHtml() {} export function osmWire() {} export function osmStop() {}' });
    } else await route.continue();
  });
  await page.goto('/#/settings');
  await expect.poll(() => urls.length, { timeout: 20000 }).toBeGreaterThan(0);
  expect(urls.every(url => new URL(url).searchParams.has('v'))).toBe(true);
  await expect.poll(() => errors, { timeout: 2000 }).toEqual([]);
  await expect(page.locator('html')).not.toHaveClass(/ym-hold/, { timeout: 20000 });
  expect(await page.evaluate(() => localStorage.getItem('ymaster_check_offline_queue'))).toBe('сохранить-старую-очередь');
});

test('контроль: старый незаверсионированный модуль действительно ломает старт', async ({ page }) => {
  test.setTimeout(90000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  await page.addInitScript(() => {
    localStorage.setItem('ymaster_check_token', 'invalid-test-token');
    localStorage.setItem('ymaster_check_offline_queue', 'сохранить-старую-очередь');
  });
  await page.route('**/js/app.js*', async route => {
    const response = await route.fetch();
    const source = (await response.text()).replace(/from '\.\/osm\.js\?v=[\d.]+'/,
      "from './osm.js'");
    await route.fulfill({ response, body: source });
  });
  await page.route('**/js/osm.js*', route => route.fulfill({ contentType: 'text/javascript',
    body: 'export function osmCardHtml() {} export function osmWire() {} export function osmStop() {}' }));
  await page.goto('/#/settings');
  await expect.poll(() => errors.join(' '), { timeout: 20000 }).toMatch(/does not provide an export|localServer/i);
  await expect(page.locator('html')).toHaveClass(/ym-hold/);
  expect(await page.evaluate(() => localStorage.getItem('ymaster_check_offline_queue'))).toBe('сохранить-старую-очередь');
});
