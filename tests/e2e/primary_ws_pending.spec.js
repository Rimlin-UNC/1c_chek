import { readFileSync } from 'node:fs';
import { test, expect } from '@playwright/test';

test('отложенное обновление дашборда не затирает сверку и результат PDF', async ({ page, request }) => {
  test.setTimeout(120000);
  const login = await request.post('/api/v1/auth/login', { data: { username: 'admin', password: 'playwright-test-password' } });
  expect(login.ok()).toBeTruthy();
  const { access_token: token } = await login.json();
  await page.addInitScript(token => {
    localStorage.setItem('ymaster_check_token', token);
    localStorage.setItem('ymaster_seen_version', '1.72.28');
    const OriginalWebSocket = window.WebSocket;
    window.WebSocket = class extends OriginalWebSocket {
      constructor(...args) { super(...args); window.__testSocket = this; }
    };
    const originalTimeout = window.setTimeout;
    window.setTimeout = function (callback, delay, ...args) {
      if (window.__holdRefresh && delay === 1200) {
        window.__pendingRefresh = () => callback(...args);
        return -1;
      }
      return originalTimeout(callback, delay, ...args);
    };
  }, token);
  await page.goto('/#/dashboard');
  await expect(page.locator('#login-screen')).toBeHidden();
  await expect.poll(() => page.evaluate(() => !!window.__testSocket?.onmessage), { timeout: 25000 }).toBe(true);
  await page.evaluate(() => {
    window.__holdRefresh = true;
    window.__testSocket.onmessage({ data: JSON.stringify({ type: 'receipt_created', payload: { fn: '123', fd: '456', total_sum: 1 } }) });
  });
  await expect.poll(() => page.evaluate(() => typeof window.__pendingRefresh)).toBe('function');
  await page.locator('a[href="#/primary"]').click();
  await expect(page.locator('#prim-file')).toBeAttached();
  const stamp = Date.now();
  const pdf = Buffer.concat([readFileSync('tests/fixtures/primary/upd_print_text.pdf'), Buffer.from(`\n% e2e pending ${stamp}\n`)]);
  await page.setInputFiles('#prim-file', { name: 'upd_print.pdf', mimeType: 'application/pdf', buffer: pdf });
  await expect(page.locator('.modal-slot')).toContainText('Сверьте реквизиты: upd_print.pdf', { timeout: 20000 });
  const number = `УТ-215-${stamp}`;
  await page.locator('#prim-f-number').fill(number);
  await page.evaluate(() => { window.__holdRefresh = false; window.__pendingRefresh(); });
  await expect(page.locator('#prim-f-number')).toHaveValue(number);
  await page.getByRole('button', { name: 'Сохранить в реестр' }).click();
  await expect(page.locator('#prim-results')).toContainText('«upd_print.pdf»: сохранён в реестр', { timeout: 15000 });
  await expect(page.locator('#prim-out tbody tr', { hasText: number })).toContainText('PDF', { timeout: 15000 });
});
