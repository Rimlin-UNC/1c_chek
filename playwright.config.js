/* v1.70.60: Playwright — браузерные сценарии пользователя.
   Запуск: npm run test:e2e (нужны браузеры: npx playwright install chromium;
   в песочнице без доступа к CDN браузеров конфиг и сценарии проверяются
   перечислением: npx playwright test --list).
   Сервер поднимается сам на порту 8123 с отдельной базой. */
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: 'tests/e2e',
  timeout: 30_000,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:8123',
    headless: true,
    // следы только при падении — прогон остаётся быстрым
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'env DATABASE_URL=sqlite:////tmp/pw_e2e.db '
      + 'YM_OSMON_OFF=1 .venv/bin/python run.py --host 127.0.0.1 --port 8123',
    url: 'http://127.0.0.1:8123/',
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
