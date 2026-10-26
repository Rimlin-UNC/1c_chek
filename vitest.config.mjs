/* v1.70.60: Vitest — юнит-тесты JS-модулей кабинета.
   Запуск: npm run test:js (или node_modules/.bin/vitest run).
   Среда jsdom: модули работают с localStorage и document, но юниты
   трогают только чистые функции и хранилище токена. */
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests/js/**/*.test.js'],
    // отказ одного файла не отменяет прогон остальных
    isolate: true,
  },
});
