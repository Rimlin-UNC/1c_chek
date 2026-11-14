import { test, expect } from '@playwright/test';

test('фоновая проверка неизменной очереди не заменяет кнопку синхронизации', async ({ page, request }) => {
  test.setTimeout(90000);
  const login = await request.post('/api/v1/auth/login', { data: { username: 'e2e_offline', password: 'e2e-offline-password' } });
  expect(login.ok()).toBeTruthy();
  const { access_token: token, user } = await login.json();
  await page.goto('/');
  await page.evaluate(async ownerId => {
    const box = await import('/js/offline_outbox.js');
    await box.enqueue({ ownerId, companyId: null, clientId: null, qrData: 'test-qr', source: 'web' });
  }, user.id);
  await page.addInitScript(token => {
    localStorage.setItem('ymaster_check_token', token);
    localStorage.setItem('ymaster_seen_version', '1.72.27');
    const original = window.setInterval;
    window.setInterval = (fn, ms, ...args) => original(fn, ms === 30000 ? 500 : ms, ...args);
  }, token);
  await page.route('**/api/v1/receipts/scan/offline', route => route.fulfill({ status: 503, json: { detail: 'Тестовый отказ' } }));
  await page.goto('/?stable-queue=1#/scan');
  await expect(page.locator('#btn-flush')).toBeVisible({ timeout: 30000 });
  const stable = await page.evaluate(async () => {
    const button = document.querySelector('#btn-flush');
    await new Promise(resolve => setTimeout(resolve, 2200));
    return button === document.querySelector('#btn-flush') && button.isConnected;
  });
  expect(stable).toBe(true);
  expect(await page.evaluate(async ownerId => (await import('/js/offline_outbox.js')).list(ownerId), user.id)).toHaveLength(1);
});
