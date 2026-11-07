/* v1.70.114 (T-62): страница печатной формы УПД (ADR-45).
   Открывается в новом окне из «Первички». Скрипт вынесен отдельным файлом, потому что политика CSP
   запрещает встроенные скрипты (script-src 'self'). Кнопка печати и автопечать по «?print=1» —
   «Печать → Сохранить как PDF» даёт файл, который можно отправить контрагенту. */
(() => {
  const auto = document.body && document.body.dataset.print === '1';
  const button = document.getElementById('pp-print');
  if (button) button.addEventListener('click', () => window.print());
  if (auto) {
    // ждём раскладку и шрифты: иначе в PDF попадает недостроенная таблица
    const run = () => window.print();
    if (document.readyState === 'complete') setTimeout(run, 60);
    else window.addEventListener('load', () => setTimeout(run, 60));
  }
})();
