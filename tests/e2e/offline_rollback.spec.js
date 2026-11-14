import { test, expect } from '@playwright/test';

test('старый сервер не принимает новую очередь: записи остаются до обновления', async ({ page, request }) => {
  test.setTimeout(120000);
  const login = await request.post('/api/v1/auth/login', {
    data: { username: 'e2e_offline', password: 'e2e-offline-password' },
  });
  expect(login.ok()).toBeTruthy();
  const { access_token: token, user } = await login.json();
  const legacy = JSON.stringify([{ qr_data: 'анонимный старый скан', source: 'web' }]);
  await page.addInitScript(({ token, legacy }) => {
    localStorage.setItem('ymaster_check_token', token);
    localStorage.setItem('ymaster_seen_version', '1.72.28');
    localStorage.setItem('ymaster_check_offline_queue', legacy);
  }, { token, legacy });
  let oldServer = true;
  let fallbackCalls = 0;
  await page.route('**/api/v1/receipts/scan', async route => {
    fallbackCalls += 1;
    await route.abort();
  });
  await page.route('**/api/v1/receipts/scan/offline', async route => {
    if (oldServer) {
      await route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"Not Found"}' });
    } else {
      await route.continue();
    }
  });
  await page.goto('/#/scan');
  await expect(page.locator('#btn-paste')).toBeVisible({ timeout: 30000 });
  await page.locator('#btn-paste').click();
  const qr = 't=20260922T1556&s=41.00&fn=8999909876543221&i=76551&fp=2882574211&n=1';
  await page.locator('#paste-text').fill(qr);
  await page.locator('#btn-paste-send').click();
  await expect.poll(async () => page.evaluate(async id => (await import('/js/offline_outbox.js')).list(id), user.id), { timeout: 30000 })
    .toMatchObject([{ state: 'attention', qrData: qr }]);
  expect(fallbackCalls).toBe(0);
  expect(await page.evaluate(() => localStorage.getItem('ymaster_check_offline_queue'))).toBe(legacy);

  oldServer = false;
  await page.locator('#btn-flush').click();
  await expect.poll(async () => page.evaluate(async id => (await import('/js/offline_outbox.js')).list(id), user.id), { timeout: 30000 }).toHaveLength(0);
  expect(fallbackCalls).toBe(0);
  expect(await page.evaluate(() => localStorage.getItem('ymaster_check_offline_queue'))).toBe(legacy);
  const receipts = await request.get('/api/v1/receipts', { headers: { Authorization: `Bearer ${token}` } });
  expect((await receipts.json()).items.filter(r => r.fn === '8999909876543221')).toHaveLength(1);
});
