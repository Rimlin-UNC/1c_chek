import { test, expect } from '@playwright/test';

test('предупреждение о старых сканах видно, даже если IndexedDB долго не отвечает', async ({ page, request }) => {
  test.setTimeout(90000);
  const login = await request.post('/api/v1/auth/login', {
    data: { username: 'e2e_offline', password: 'e2e-offline-password' },
  });
  expect(login.ok()).toBeTruthy();
  const { access_token: token } = await login.json();
  const legacy = JSON.stringify([{ qr_data: 'старый скан', source: 'web' }]);
  await page.addInitScript(({ token, legacy }) => {
    localStorage.setItem('ymaster_check_token', token);
    localStorage.setItem('ymaster_seen_version', '1.72.23');
    localStorage.setItem('ymaster_check_offline_queue', legacy);
    const open = indexedDB.open.bind(indexedDB);
    indexedDB.open = (name, ...args) => name === 'ymaster-check-outbox' ? {} : open(name, ...args);
  }, { token, legacy });
  await page.goto('/#/scan');
  await expect(page.locator('#btn-paste')).toBeVisible({ timeout: 20000 });
  await expect(page.getByText('Старые сканы без подтверждённого владельца:', { exact: false }))
    .toBeVisible({ timeout: 10000 });
  await expect(page.getByText('Проверяем локальную очередь.', { exact: false })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem('ymaster_check_offline_queue'))).toBe(legacy);
});
