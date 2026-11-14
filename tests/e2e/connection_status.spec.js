import { expect, test } from '@playwright/test';

test('индикатор работает при недоступных живых обновлениях и без опроса на переходах', async ({ page, request }) => {
  test.setTimeout(120000);
  let response = await request.post('/api/v1/auth/login', { data: { username: 'admin', password: 'playwright-test-password' } });
  if (!response.ok()) {
    response = await request.post('/api/v1/auth/login', { data: { username: 'admin', password: 'admin123' } });
    expect(response.ok()).toBeTruthy();
    const oldToken = (await response.json()).access_token;
    const changed = await request.post('/api/v1/auth/change-password', {
      headers: { Authorization: `Bearer ${oldToken}` },
      data: { old_password: 'admin123', new_password: 'playwright-test-password' },
    });
    expect(changed.ok()).toBeTruthy();
    response = await request.post('/api/v1/auth/login', { data: { username: 'admin', password: 'playwright-test-password' } });
  }
  const token = (await response.json()).access_token;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(token => {
    localStorage.setItem('ymaster_check_token', token);
    localStorage.setItem('ymaster_seen_version', '1.72.25');
    // Быстро воспроизводим периодическую проверку локальной очереди в простое.
    const original = window.setInterval;
    window.setInterval = (fn, ms, ...args) => original(fn, ms === 30000 ? 500 : ms, ...args);
  }, token);
  await page.routeWebSocket('**/ws/status**', socket => socket.close());
  let countsCalls = 0, conflictCalls = 0;
  page.on('request', r => {
    if (r.url().includes('/api/v1/navigation/counts')) countsCalls += 1;
    if (r.url().includes('/api/v1/unified/stats')) conflictCalls += 1;
  });
  await page.goto('/#/dashboard');
  await expect(page.locator('#app-shell')).toBeVisible({ timeout: 30000 });
  const text = page.locator('#ws-status-text');
  await expect(text).toHaveText('Подключено · без живых обновлений', { timeout: 20000 });
  await expect(page.locator('#ws-status .live-dot')).toHaveClass(/on/);
  await expect.poll(() => countsCalls).toBeGreaterThan(0);
  await page.waitForTimeout(1500);
  const initial = countsCalls, initialConflicts = conflictCalls;
  await page.evaluate(() => { location.hash = '#/companies'; });
  await expect(page.locator('#page-title')).toHaveText('Компании');
  await page.evaluate(() => { location.hash = '#/users'; });
  await expect(page.locator('#page-title')).toHaveText('Пользователи и приглашения');
  await page.waitForTimeout(6000);
  expect(countsCalls).toBe(initial);
  expect(conflictCalls).toBe(initialConflicts);

  await page.route('**/api/v1/connectivity-test', route => route.abort());
  await page.evaluate(async () => {
    try { await (await import('/js/api.js')).api.get('/api/v1/connectivity-test', { retries: 1 }); } catch {}
  });
  await expect(text).toHaveText('Нет подключения');
  await expect(page.locator('#ws-status .live-dot')).toHaveClass(/off/);
  await page.evaluate(async () => (await import('/js/api.js')).api.get('/api/v1/auth/me', { retries: 1 }));
  await expect(text).toHaveText('Подключено · без живых обновлений');
  await page.context().setOffline(true);
  await expect(text).toHaveText('Нет подключения');
  await page.context().setOffline(false);
  await expect(text).toHaveText('Подключено · без живых обновлений', { timeout: 20000 });
});
