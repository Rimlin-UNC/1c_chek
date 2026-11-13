import { expect, test } from '@playwright/test';

test('возврат вкладки ограничивает запросы версии и сохраняет предупреждение об обновлении', async ({ page }) => {
  await page.addInitScript(() => {
    const now = Date.now;
    window.__clockOffset = 0;
    Date.now = () => now() + window.__clockOffset;
    window.__hiddenForTest = false;
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.__hiddenForTest });
  });
  let calls = 0;
  await page.route('**/api/v1/about', async route => {
    calls += 1;
    const response = await route.fetch();
    const data = await response.json();
    if (calls > 1) data.version = '99.99.99';
    await route.fulfill({ response, json: data });
  });
  await page.goto('/');
  await expect(page.locator('#footer-version')).not.toHaveText('');
  expect(calls).toBe(1);
  await page.evaluate(() => {
    for (let i = 0; i < 10; i++) {
      window.__hiddenForTest = true; document.dispatchEvent(new Event('visibilitychange'));
      window.__hiddenForTest = false; document.dispatchEvent(new Event('visibilitychange'));
    }
  });
  await page.waitForTimeout(500);
  expect(calls).toBe(1);
  await page.evaluate(() => {
    window.__hiddenForTest = true;
    window.__clockOffset += 61000;
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.waitForTimeout(200);
  expect(calls).toBe(1);
  await page.evaluate(() => {
    window.__hiddenForTest = false;
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await expect(page.locator('#update-banner')).toContainText('99.99.99');
  expect(calls).toBe(2);
});
