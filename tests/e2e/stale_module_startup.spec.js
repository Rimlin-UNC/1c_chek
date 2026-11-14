import { expect, test } from '@playwright/test';

test.use({ serviceWorkers: 'block' });

test('новый модуль локального мониторинга запрашивается с версией', async ({ page }) => {
  test.setTimeout(90000);
  const errors = [], urls = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    localStorage.setItem('ymaster_check_token', 'invalid-test-token');
    localStorage.setItem('ymaster_check_offline_queue', 'сохранить-старую-очередь');
  });
  await page.route('**/js/server_monitor.js*', async route => {
    const url = new URL(route.request().url());
    urls.push(url.href);
    if (!url.search) await route.fulfill({ contentType: 'text/javascript', body: 'export const incompatible = true;' });
    else await route.continue();
  });
  await page.goto('/#/settings');
  await expect.poll(() => urls.length, { timeout: 20000 }).toBeGreaterThan(0);
  expect(urls.every(url => new URL(url).searchParams.has('v'))).toBe(true);
  await expect(page.locator('html')).not.toHaveClass(/ym-hold/, { timeout: 20000 });
  expect(errors).toEqual([]);
  expect(await page.evaluate(() => localStorage.getItem('ymaster_check_offline_queue'))).toBe('сохранить-старую-очередь');
});

for (const suffix of ['', '?v=1.72.18']) {
  test(`старый импорт ${suffix || 'без версии'} совместим с удалённым блоком`, async ({ page }) => {
    test.setTimeout(90000);
    const errors = [], legacyRequests = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      localStorage.setItem('ymaster_check_token', 'invalid-test-token');
      localStorage.setItem('ymaster_check_offline_queue', 'сохранить-старую-очередь');
    });
    await page.route('**/js/app.js*', async route => {
      const response = await route.fetch();
      const source = (await response.text()).replace(
        /import \{ localServerCardHtml, localServerWire, localServerStop \} from '\.\/server_monitor\.js\?v=[\d.]+';/,
        `import { osmCardHtml, osmWire, osmStop, localServerCardHtml, localServerWire, localServerStop } from './osm.js${suffix}';`);
      expect(source).toContain(`from './osm.js${suffix}'`);
      await route.fulfill({ response, body: source });
    });
    page.on('request', request => { if (new URL(request.url()).pathname === '/js/osm.js') legacyRequests.push(request.url()); });
    await page.goto('/#/settings');
    await expect.poll(() => legacyRequests.length, { timeout: 20000 }).toBeGreaterThan(0);
    await expect(page.locator('html')).not.toHaveClass(/ym-hold/, { timeout: 20000 });
    expect(errors).toEqual([]);
    expect(await page.evaluate(() => localStorage.getItem('ymaster_check_offline_queue'))).toBe('сохранить-старую-очередь');
  });
}
