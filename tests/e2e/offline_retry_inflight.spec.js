import { test, expect } from '@playwright/test';

test('ручной повтор во время отправки не теряется после отказа сервера', async ({ page, request }) => {
  test.setTimeout(120000);
  const login = await request.post('/api/v1/auth/login', { data: { username: 'e2e_offline', password: 'e2e-offline-password' } });
  expect(login.ok()).toBeTruthy();
  const { access_token: token, user } = await login.json();
  await page.addInitScript(token => {
    localStorage.setItem('ymaster_check_token', token);
    localStorage.setItem('ymaster_seen_version', '1.72.26');
  }, token);
  let received = 0, release;
  const waiting = new Promise(resolve => { release = resolve; });
  await page.route('**/api/v1/receipts/scan/offline', async route => {
    received += 1;
    if (received === 1) {
      await waiting;
      await route.fulfill({ status: 422, json: { detail: 'Тестовый отказ' } });
    } else await route.continue();
  });
  await page.goto('/#/scan');
  await page.locator('#btn-paste').click();
  await page.locator('#paste-text').fill('t=20260922T1556&s=41.00&fn=8999909876543281&i=76587&fp=2882574271&n=1');
  await page.locator('#btn-paste-send').click();
  await expect.poll(() => received).toBe(1);
  await expect(page.locator('#btn-flush')).toBeVisible();
  await page.locator('#btn-flush').click();
  release();
  await expect.poll(() => received, { timeout: 30000 }).toBe(2);
  await expect.poll(async () => page.evaluate(async id => (await import('/js/offline_outbox.js')).list(id), user.id),
    { timeout: 30000 }).toHaveLength(0);
});

test('отложенный ручной повтор не выполняется после смены сеанса', async ({ page, request }) => {
  test.setTimeout(120000);
  const login = await request.post('/api/v1/auth/login', { data: { username: 'e2e_offline', password: 'e2e-offline-password' } });
  expect(login.ok()).toBeTruthy();
  const { access_token: token, user } = await login.json();
  await page.addInitScript(token => {
    localStorage.setItem('ymaster_check_token', token);
    localStorage.setItem('ymaster_seen_version', '1.72.26');
  }, token);
  let received = 0, release;
  const waiting = new Promise(resolve => { release = resolve; });
  await page.route('**/api/v1/receipts/scan/offline', async route => {
    received += 1;
    if (received === 1) {
      await waiting;
      await route.fulfill({ status: 422, json: { detail: 'Тестовый отказ' } });
    } else await route.fulfill({ status: 500, json: { detail: 'Повтор не должен выполняться' } });
  });
  await page.goto('/#/scan');
  await page.locator('#btn-paste').click();
  await page.locator('#paste-text').fill('t=20260922T1556&s=42.00&fn=8999909876543282&i=76588&fp=2882574272&n=1');
  await page.locator('#btn-paste-send').click();
  await expect.poll(() => received).toBe(1);
  await expect(page.locator('#btn-flush')).toBeVisible();
  await page.locator('#btn-flush').click();
  await page.evaluate(() => localStorage.setItem('ymaster_check_token', 'другая-сессия'));
  release();
  await expect.poll(async () => page.evaluate(async id => (await import('/js/offline_outbox.js')).list(id), user.id))
    .toMatchObject([{ state: 'attention' }]);
  await page.waitForTimeout(2000);
  expect(received).toBe(1);
});
