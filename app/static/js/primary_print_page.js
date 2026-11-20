/* v1.70.114 (T-62): страница печатной формы УПД (ADR-45).
   Открывается в новом окне из «Первички». Скрипт вынесен отдельным файлом, потому что политика CSP
   запрещает встроенные скрипты (script-src 'self'). Кнопка печати и автопечать по «?print=1» —
   «Печать → Сохранить как PDF» даёт файл, который можно отправить контрагенту.

   v1.73.5 (Android): мобильный Chrome (и WebView внутри мессенджеров) намеренно НЕ открывает
   системное окно печати по вызову из скрипта — вызов молча игнорируется. Раньше человек нажимал
   «🖨 Печать» и не происходило ничего, а по «?print=1» не появлялось и бланка. Теперь на таком
   устройстве показывается подсказка, ГДЕ печать в самом браузере (⋮ → «Поделиться» → «Печать»),
   а бланк остаётся на экране и печатается из меню. */
(() => {
  const ua = navigator.userAgent || '';
  // Мобильный Chrome/WebView на Android: window.print() заблокирован платформой.
  const android = /Android/i.test(ua) && /Chrome|CriOS|wv\)/i.test(ua);
  const button = document.getElementById('pp-print');
  const hint = document.getElementById('pp-hint');

  const sayAndroid = () => {
    if (!hint) return;
    hint.textContent = 'В этом браузере печать открывается из меню: ⋮ → «Поделиться» → «Печать» '
      + '(или «Сохранить как PDF»). Бланк уже загружен — он на экране целиком.';
  };

  if (button) {
    button.addEventListener('click', () => {
      if (android) {
        sayAndroid();
        // всё равно пробуем: часть прошивок и «печать в PDF» это поддерживают
        setTimeout(() => { try { window.print(); } catch { /* платформа запрещает */ } }, 50);
        return;
      }
      window.print();
    });
  }

  // «?print=1» — сразу к печати, но только там, где браузер это умеет
  if (document.body && document.body.dataset.print === '1') {
    if (android) {
      sayAndroid();                 // вместо «ничего не произошло» — объяснение словами
    } else if (document.readyState === 'complete') {
      setTimeout(() => window.print(), 60);   // ждём раскладку и шрифты
    } else {
      window.addEventListener('load', () => setTimeout(() => window.print(), 60));
    }
  }
})();
