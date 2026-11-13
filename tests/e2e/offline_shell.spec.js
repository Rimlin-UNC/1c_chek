import { test, expect } from '@playwright/test';

test('офлайн после первой установки: скрипты не подменяются HTML', async ({ page, context }) => {
  await page.goto('/');
  await page.evaluate(async () => {
    await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) {
      await new Promise(resolve => navigator.serviceWorker.addEventListener('controllerchange', resolve, { once: true }));
    }
  });
  await page.evaluate(async () => {
    localStorage.setItem('ymaster_check_offline_queue', 'защищённая-старая-очередь');
    const box = await import('/js/offline_outbox.js');
    await box.enqueue({ ownerId: 'offline-test', companyId: null, clientId: null, qrData: 'test-qr', source: 'web' });
  });
  await context.setOffline(true);
  const replies = await page.evaluate(async () => {
    const appUrl = new URL(document.querySelector('script[src*="app.js?v="]').src);
    const urls = ['/js/approvals.js', '/js/clients.js', '/js/counterparties.js',
      '/js/primary.js', `/js/osm.js?v=${appUrl.searchParams.get('v')}`, '/js/vendor/chart.umd.js'];
    return await Promise.all(urls.map(async path => {
      const response = await fetch(path);
      return { path, ok: response.ok, type: response.headers.get('content-type') };
    }));
  });
  for (const item of replies) {
    expect(item.ok, item.path).toBeTruthy();
    expect(item.type, item.path).toMatch(/javascript/);
  }
  const missing = await page.evaluate(async () => {
    try { await fetch('/js/does-not-exist-offline.js'); return 'success'; }
    catch { return 'network-error'; }
  });
  expect(missing).toBe('network-error');
  await page.reload();
  await expect(page.locator('#land-login')).toBeAttached();
  await page.waitForFunction(() => typeof document.querySelector('#login-form').onsubmit === 'function');
  expect(await page.evaluate(() => localStorage.getItem('ymaster_check_offline_queue'))).toBe('защищённая-старая-очередь');
  expect(await page.evaluate(async () => (await import('/js/offline_outbox.js')).list('offline-test')))
    .toMatchObject([{ qrData: 'test-qr' }]);
});

test('потеря сети при перезапуске не удаляет токен, а настоящий отказ входа удаляет', async ({ page, context }) => {
  await page.addInitScript(() => {
    const original = window.setTimeout;
    window.setTimeout = (fn, ms, ...args) => original(fn, ms >= 1200 && ms <= 7200 && ms % 1200 === 0 ? 1 : ms, ...args);
  });
  await page.goto('/');
  await page.evaluate(async () => {
    await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) {
      await new Promise(resolve => navigator.serviceWorker.addEventListener('controllerchange', resolve, { once: true }));
    }
    localStorage.setItem('ymaster_check_token', 'synthetic-token');
  });
  await context.setOffline(true);
  await page.reload();
  await page.waitForFunction(() => typeof document.querySelector('#login-form').onsubmit === 'function');
  expect(await page.evaluate(() => localStorage.getItem('ymaster_check_token'))).toBe('synthetic-token');
  await context.setOffline(false);
  await page.reload();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('ymaster_check_token'))).toBe(null);
});
