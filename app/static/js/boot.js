/* v1.70.103 (T-74): решения до первой отрисовки. Обычный скрипт без defer в <head> — выполняется раньше, чем
   браузер нарисует страницу; встроенные скрипты запрещены политикой CSP.
   • Гость (в браузере нет токена входа) и заставка для гостей выключена администратором (так по умолчанию) —
     класс ym-guest: заставки нет, стартовая страница видна с первого кадра. Полноэкранная заставка перед
     содержимым — «навязчивое окно» по правилам Google, а стартовую страницу читают поисковики.
   • Есть токен или особый адрес (#/…, ?maxlogin=) — класс ym-hold: стартовая страница не мелькает, пока
     программа решает, какой экран показать; снимает класс app.js (showScreen). */
(function () {
  'use strict';
  var root = document.documentElement, token = '', guests = '';
  try {
    token = window.localStorage.getItem('ymaster_check_token') || '';
    guests = window.localStorage.getItem('ym-splash-guests') || '';
  } catch (e) { void e; /* хранилище недоступно — считаем гостем */ }
  var hash = window.location.hash || '';
  var route = (hash.length > 1 && hash.indexOf('#land-') !== 0) || /[?&]maxlogin=/.test(window.location.search);
  if (token || route) root.classList.add('ym-hold');
  if (!token && guests !== '1') root.classList.add('ym-guest');
})();
