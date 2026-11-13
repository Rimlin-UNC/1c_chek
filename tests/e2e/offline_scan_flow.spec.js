import { test, expect } from '@playwright/test';

test('потерянный ответ: скан сохраняется до отправки и повтор не создаёт дубль', async ({ page, request }) => {
  test.setTimeout(120000);
  const login = await request.post('/api/v1/auth/login', { data: { username: 'e2e_offline', password: 'e2e-offline-password' } });
  expect(login.ok()).toBeTruthy();
  const { access_token: freshToken, user } = await login.json();
  const legacy = JSON.stringify([{ qr_data: 'старый скан', source: 'web' }]);
  await page.addInitScript(({ token, legacy }) => {
    localStorage.setItem('ymaster_check_token', token);
    localStorage.setItem('ymaster_seen_version', '1.72.17');
    localStorage.setItem('ymaster_check_offline_queue', legacy);
    const original = window.setInterval;
    window.setInterval = (fn, ms, ...args) => original(fn, ms === 30000 ? 1000 : ms, ...args);
  }, { token: freshToken, legacy });
  let received = 0;
  let mode = 'normal';
  await page.route('**/api/v1/receipts/scan/offline', async route => {
    received += 1;
    if (mode === 'invalid') {
      await route.fulfill({ status: 422, contentType: 'application/json', body: JSON.stringify({ detail: 'Тестовый отказ' }) });
    } else if (mode === 'down') {
      await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Сервер недоступен' }) });
    } else if (mode === 'foreign') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ receipt: null, duplicate: true, message: 'Чек другой компании' }) });
    } else if (received === 1) {
      const response = await route.fetch();
      expect(response.ok()).toBeTruthy();
      await route.abort('failed');
    } else await route.continue();
  });
  await page.goto('/#/scan');
  await expect(page.locator('#btn-paste')).toBeVisible({ timeout: 20000 });
  await expect(page.getByText('Старые сканы без подтверждённого владельца:', { exact: false })).toBeVisible();
  await page.locator('#btn-paste').click();
  const qr = 't=20260922T1556&s=123.45&fn=8999909876543210&i=76543&fp=2882574203&n=1';
  await page.locator('#paste-text').fill(qr);
  await page.locator('#btn-paste-send').click();
  await expect.poll(async () => page.evaluate(async id => (await import('/js/offline_outbox.js')).list(id), user.id), { timeout: 30000 })
    .toHaveLength(1);
  await expect.poll(() => received, { timeout: 30000 }).toBe(1);
  await expect(page.locator('#btn-flush')).toBeVisible({ timeout: 15000 });
  await page.locator('#btn-flush').click();
  await expect.poll(async () => page.evaluate(async id => (await import('/js/offline_outbox.js')).list(id), user.id), { timeout: 30000 })
    .toHaveLength(0);
  expect(received).toBeGreaterThanOrEqual(2);
  const list = await request.get('/api/v1/receipts', { headers: { Authorization: `Bearer ${freshToken}` } });
  const receipts = (await list.json()).items.filter(r => r.fn === '8999909876543210');
  expect(receipts).toHaveLength(1);
  expect(await page.evaluate(() => localStorage.getItem('ymaster_check_offline_queue'))).toBe(legacy);

  // После входа сканирование продолжается без сети, синхронизация при возврате.
  await page.context().setOffline(true);
  await page.locator('#btn-paste').click();
  await page.locator('#paste-text').fill('t=20260922T1556&s=34.00&fn=8999909876543211&i=76544&fp=2882574204&n=1');
  await page.locator('#btn-paste-send').click();
  await expect.poll(async () => page.evaluate(async id => (await import('/js/offline_outbox.js')).list(id), user.id)).toHaveLength(1);
  await page.context().setOffline(false);
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await expect.poll(async () => page.evaluate(async id => (await import('/js/offline_outbox.js')).list(id), user.id), { timeout: 40000 }).toHaveLength(0);
  expect(await page.evaluate(() => localStorage.getItem('ymaster_check_offline_queue'))).toBe(legacy);

  // Отклонённую запись нельзя молча удалить; ручной повтор после устранения ошибки.
  mode = 'invalid';
  await page.locator('#btn-paste').click();
  await page.locator('#paste-text').fill('t=20260922T1556&s=35.00&fn=8999909876543212&i=76545&fp=2882574205&n=1');
  await page.locator('#btn-paste-send').click();
  await expect.poll(async () => page.evaluate(async id => (await import('/js/offline_outbox.js')).list(id), user.id), { timeout: 20000 })
    .toMatchObject([{ state: 'attention', message: 'Тестовый отказ' }]);
  await expect(page.getByText(/Нужна помощь: Тестовый отказ/)).toBeVisible();
  mode = 'normal';
  await page.locator('#btn-flush').click();
  await expect.poll(async () => page.evaluate(async id => (await import('/js/offline_outbox.js')).list(id), user.id), { timeout: 30000 }).toHaveLength(0);

  // 503 остаётся на устройстве для повтора, а чужой дубликат не считается успехом.
  mode = 'down';
  await page.locator('#btn-paste').click();
  await page.locator('#paste-text').fill('t=20260922T1556&s=37.00&fn=8999909876543214&i=76547&fp=2882574207&n=1');
  await page.locator('#btn-paste-send').click();
  await expect.poll(async () => page.evaluate(async id => (await import('/js/offline_outbox.js')).list(id), user.id), { timeout: 20000 })
    .toMatchObject([{ state: 'pending', message: 'Сервер недоступен' }]);
  mode = 'normal';
  // Интернет не прерывался: фоновый повтор обнаруживает восстановление сервера.
  await expect.poll(async () => page.evaluate(async id => (await import('/js/offline_outbox.js')).list(id), user.id), { timeout: 20000 }).toHaveLength(0);
  mode = 'foreign';
  await page.locator('#btn-paste').click();
  await page.locator('#paste-text').fill('t=20260922T1556&s=38.00&fn=8999909876543215&i=76548&fp=2882574208&n=1');
  await page.locator('#btn-paste-send').click();
  await expect.poll(async () => page.evaluate(async id => (await import('/js/offline_outbox.js')).list(id), user.id), { timeout: 20000 })
    .toMatchObject([{ state: 'attention', message: 'Чек другой компании' }]);
  mode = 'normal';
  await page.locator('#btn-flush').click();
  await expect.poll(async () => page.evaluate(async id => (await import('/js/offline_outbox.js')).list(id), user.id), { timeout: 30000 }).toHaveLength(0);

  // Ошибка квоты не выдаётся за сохранение и форма с QR остаётся открытой.
  await page.evaluate(() => {
    window.__originalAdd = IDBObjectStore.prototype.add;
    IDBObjectStore.prototype.add = function () { throw new DOMException('Хранилище заполнено', 'QuotaExceededError'); };
  });
  await page.locator('#btn-paste').click();
  await page.locator('#paste-text').fill('t=20260922T1556&s=36.00&fn=8999909876543213&i=76546&fp=2882574206&n=1');
  await page.locator('#btn-paste-send').click();
  await expect(page.getByText(/Скан НЕ сохранён на устройстве/)).toBeVisible();
  await expect(page.locator('#paste-text')).toBeVisible();
  expect(await page.locator('#paste-text').inputValue()).toContain('8999909876543213');
  await page.evaluate(() => { IDBObjectStore.prototype.add = window.__originalAdd; });
  await page.locator('#modal-root [data-close]').click();
  // При получении и отправке следующего скана камера должна оставаться включённой.
  await page.evaluate(() => {
    const canvas = document.createElement('canvas');
    canvas.width = 64; canvas.height = 64;
    window.__cameraStream = canvas.captureStream(5);
    const ctx = canvas.getContext('2d');
    window.__cameraDraw = setInterval(() => { ctx.fillStyle = Math.random() > 0.5 ? '#fff' : '#000'; ctx.fillRect(0, 0, 64, 64); }, 100);
    Object.defineProperty(navigator.mediaDevices, 'getUserMedia', { configurable: true, value: async () => window.__cameraStream });
    window.BarcodeDetector = class {
      constructor() { this.sent = false; }
      async detect() {
        if (this.sent) return [];
        this.sent = true;
        return [{ rawValue: 't=20260922T1556&s=39.00&fn=8999909876543216&i=76549&fp=2882574209&n=1' }];
      }
    };
  });
  const previous = received;
  await page.locator('#btn-camera-start').click();
  await expect.poll(() => received, { timeout: 20000 }).toBeGreaterThan(previous);
  await expect.poll(() => page.evaluate(() => window.__cameraStream.active), { timeout: 10000 }).toBe(true);
  await expect(page.locator('[data-camstop]')).toBeVisible();
});
