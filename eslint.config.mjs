/* v1.70.59: ESLint (flat config) — новая совместимая технология контроля
 *_JS: recommended-правила ловят опечатки, дубли ключей, недостижимый код
 * и обращение к необъявленным переменным ещё до браузера.
 * Запуск: npx eslint app/static/js (или tests/test_v17059.py проверит
 * наличие bin'а и нулевые ошибки, если ESLint установлен). */
import js from "@eslint/js";
import globals from "globals";

export default [
  /* сторонние библиотеки (Chart.js, jsQR) не линтуем: их код пришёл
     извне и правится только обновлением поставки. */
  { ignores: ["app/static/js/vendor/**", "node_modules/**"] },
  { files: ["app/static/js/**/*.js"] },
  js.configs.recommended,
  {
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...globals.browser },
    },
    rules: {
      /* unused-vars в веб-коде часто осмыслен (аргументы-заглушки
        для совместимости сигнатур) — предупреждение, не ошибка. */
      "no-unused-vars": "warn",
      /* пустые catch блоки у нас осмысленны (приватный режим
        localStorage и т.п.) — предупреждение, не ошибка. */
      "no-empty": ["error", { allowEmptyCatch: true }],
    },
  },
];
