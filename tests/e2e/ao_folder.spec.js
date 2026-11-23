/* v1.73.7: папка «Загрузки/АО/<сокращённое название компании>» в браузере.
   Сценарий 1 — File System Access API (Chrome/Edge на ПК и Android): выбор
   папки подменён стендом, чтобы проверить создание «АО» и запись трёх файлов.
   Сценарий 2 — Firefox/Safari/iOS: прямого доступа к папкам нет, приходит ZIP
   с готовой структурой. Playwright поднимает сервер сам (playwright.config.js). */
import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';

const password = 'playwright-test-password';
const company = 'E2E Согласование';

async function login(page) {
  await page.goto('/');
  await page.fill('#login-username', 'admin');
  await page.fill('#login-password', password);
  await page.click('#login-submit');
  await expect(page.locator('#login-screen')).toBeHidden({ timeout: 15000 });
  const welcome = page.getByRole('button', { name: 'Понятно, работаем' });
  if (await welcome.isVisible()) await welcome.click();
}

async function openAdvanceReport(page) {
  // На телефоне меню выдвижное: сначала открываем гамбургер, иначе пункт
  // «Чеки» есть в DOM, но находится за пределами экрана.
  const width = page.viewportSize()?.width || 1280;
  if (width <= 860) {
    await page.click('#btn-sidebar');
    await expect(page.locator('body')).toHaveClass(/sb-open/);
  }
  await page.locator('a[href="#/receipts"]').click();
  await expect(page.locator('#btn-ao1')).toBeVisible({ timeout: 15000 });
  await page.click('#btn-ao1');
  await expect(page.locator('#ao-save')).toBeChecked();
  await expect(page.locator('#ao-folder-hint')).toContainText('Загрузки/АО/');
  await page.selectOption('#ao-company', { label: company });
  await page.fill('#ao-from', '2026-10-01');
  await page.fill('#ao-to', '2026-10-31');
  await page.fill('#ao-date', '2026-10-31');
  await page.fill('#ao-start-after', '0');
}

test.describe('авансовый отчёт: папка в «Загрузках»', () => {
  test('на ПК создаётся «АО/компания» и пишутся HTML, CSV и JSON', async ({ page }) => {
    await page.addInitScript(() => {
      window.__aoDirs = [];
      window.__aoFiles = [];
      window.__aoPickerOptions = null;
      const makeDir = (name, path) => ({
        name,
        queryPermission: async () => 'granted',
        requestPermission: async () => 'granted',
        async getDirectoryHandle(child) {
          const next = `${path}${child}/`;
          window.__aoDirs.push(next);
          return makeDir(child, next);
        },
        async getFileHandle(file) {
          return {
            async createWritable() {
              return {
                async write(blob) {
                  window.__aoFiles.push({ name: file, path: `${path}${file}`,
                    text: await blob.text() });
                },
                async close() {},
              };
            },
          };
        },
      });
      // Жест пользователя: showDirectoryPicker вызывается сразу по клику
      // «Собрать отчёт», ещё до запросов к серверу.
      window.showDirectoryPicker = async (opts) => {
        window.__aoPickerOptions = opts;
        return makeDir('Загрузки', '');
      };
    });
    await login(page);
    await openAdvanceReport(page);
    await page.click('#ao-make');
    await expect(page.locator('#ao-folder-status')).toContainText('Готово:', { timeout: 30000 });
    await expect(page.locator('#ao-folder-status')).toContainText(
      'Загрузки/АО/ООО «E2E Согласование»');

    const state = await page.evaluate(() => ({
      options: window.__aoPickerOptions,
      dirs: window.__aoDirs,
      files: window.__aoFiles,
    }));
    expect(state.options).toMatchObject({ id: 'ymaster-ao-downloads',
      startIn: 'downloads', mode: 'readwrite' });
    expect(state.dirs).toEqual(['АО/', 'АО/ООО «E2E Согласование»/']);
    expect(state.files.map(f => f.path.split('.').pop())).toEqual(['html', 'csv', 'json']);
    expect(state.files.every(f => f.path.startsWith('АО/ООО «E2E Согласование»/'))).toBe(true);
    expect(state.files[0].text).toContain('<div id="print-root">');
    expect(state.files[1].text).toContain('ИТОГО');
    expect(state.files[2].text).toContain('АвансовыйОтчет');
  });
});

test.describe('авансовый отчёт: браузер без доступа к папкам', () => {
  test.use({ viewport: { width: 390, height: 844 } });   // телефон

  test('на телефоне приходит ZIP с папкой «АО» внутри', async ({ page }) => {
    await page.addInitScript(() => {
      Object.defineProperty(window, 'showDirectoryPicker', {
        value: undefined, configurable: true, writable: true,
      });
    });
    await login(page);
    await openAdvanceReport(page);
    const downloadPromise = page.waitForEvent('download', { timeout: 30000 });
    await page.click('#ao-make');
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(/^AO_OOO_E2E_Soglasovanie_.*\.zip$/);
    await expect(page.locator('#ao-folder-status')).toContainText(
      'Браузер не создаёт папки', { timeout: 30000 });
    const path = await download.path();
    const bytes = readFileSync(path);
    expect(bytes.subarray(0, 4).toString('hex')).toBe('504b0304');   // локальный заголовок ZIP
    expect(bytes.length).toBeGreaterThan(500);
  });
});
