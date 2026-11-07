/* Контроллер заставки загрузки js/splash.js (Vitest, jsdom).

   v1.70.98: этапы 0 → 25 → 68 → 90 → 100 %, ожидание на 90 % без готовности приложения, подсказка о медленной
   сети, мятный финал, уход и событие ym-splash-done, поздняя загрузка скрипта (страховка CSS уже скрыла заставку).
   v1.70.100: длительность задаёт администратор — 0–60 с, по умолчанию 5 с; таймлайн растягивается под заданное
   время (короткая до 2 с — без этапов, длинная от 8 с — с подсказками, 0 — без заставки); последнее значение с
   сервера — в localStorage, свежее — ymSplash.setDuration; автотесты (webdriver) — не дольше 1,3 с; просмотр
   из настроек — ymSplash.preview. Разметка — настоящая, из index.html (#ym-splash); время и кадры управляются
   тестом: performance.now и requestAnimationFrame подменены, таймеры — фальшивые. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

const root = path.resolve(__dirname, '../..');
const splashJs = fs.readFileSync(path.join(root, 'app/static/js/splash.js'), 'utf8');
const indexHtml = fs.readFileSync(path.join(root, 'app/static/index.html'), 'utf8');
const start = indexHtml.indexOf('<div id="ym-splash"');
const end = indexHtml.indexOf('<!-- Анимированный фон -->');
const markup = indexHtml.slice(start, end);

let now = 0;
let frames = [];

function mount({ seconds = null, startAt = 0, webdriver = false } = {}) {
  document.head.innerHTML = '<link rel="stylesheet" href="/css/app.css?v=9.8.7">';
  document.body.innerHTML = markup;
  localStorage.clear();
  if (seconds !== null) localStorage.setItem('ym-splash-sec', String(seconds));
  Object.defineProperty(window.navigator, 'webdriver', { value: webdriver, configurable: true });
  delete window.ymSplash;
  now = startAt;
  frames = [];
  vi.spyOn(performance, 'now').mockImplementation(() => now);
  window.requestAnimationFrame = (cb) => { frames.push(cb); return frames.length; };
  (0, eval)(splashJs);
  return document.getElementById('ym-splash');
}

// продвинуть время до t (мс) кадрами по 16 мс
function runTo(t) {
  while (now < t) {
    now = Math.min(t, now + 16);
    const batch = frames; frames = [];
    batch.forEach((cb) => cb(now));
    vi.advanceTimersByTime(16);
  }
}

const pct = () => document.getElementById('ym-splash-pct').textContent;
const stateOf = (n) => document.querySelector(`.ym-splash__steps li[data-step="${n}"]`).getAttribute('data-state');
const statusText = () => document.getElementById('ym-splash-status').textContent;
const tipEl = () => document.getElementById('ym-splash-tip');

describe('заставка загрузки', () => {
  beforeEach(() => { vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] }); });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    delete window.ymSplash;
    Object.defineProperty(window.navigator, 'webdriver', { value: false, configurable: true });
  });

  it('разметка в index.html: 90 % экрана, этапы, подсказки, версия, выпуск и владелец', () => {
    expect(start).toBeGreaterThan(0);
    expect(markup).toContain('<progress class="ym-splash__native" id="ym-splash-progress" max="100" value="0"');
    expect(markup).not.toContain('style="');
    expect(markup).toContain('Выпуск 2026<i>•</i>ООО «Ямастер»');
    expect(markup).toContain('<div class="ym-splash__tip" id="ym-splash-tip" hidden></div>');
    expect(markup.match(/<li data-step=/g)).toHaveLength(3);
    const css = fs.readFileSync(path.join(root, 'app/static/css/splash.css'), 'utf8');
    expect(css).toContain('width: 90vw; height: 90vh; height: 90dvh;');
    expect(css).toContain('@media (prefers-color-scheme: dark)');
    expect(css).toContain('@media (prefers-reduced-motion: reduce)');
    expect(css).toContain('.ym-splash__stage { animation-duration: calc(1s * var(--ym-t, 1)); }');
    expect(css).toContain('.ym-splash[data-length="short"] .ym-splash__steps { display: none; }');
  });

  it('версия — сначала из адреса стилей, затем с сервера', () => {
    mount();
    expect(document.getElementById('ym-splash-version').textContent).toBe('9.8.7');
    window.ymSplash.setVersion('v1.70.100');
    expect(document.getElementById('ym-splash-version').textContent).toBe('1.70.100');
  });

  it('по умолчанию 5 с: этапы идут по таймлайну и ждут приложение на 90 %', () => {
    const el = mount();
    expect(window.ymSplash.duration()).toBe(5);
    expect(el.classList.contains('is-js')).toBe(true);
    expect(el.getAttribute('data-length')).toBe('normal');
    expect(el.style.getPropertyValue('--ym-t')).toBe('1.515');
    expect(pct()).toBe('0 %');
    runTo(770);
    expect(el.getAttribute('data-stage')).toBe('1');
    expect(statusText()).toBe('Инициализация…');
    expect([stateOf(1), stateOf(2), stateOf(3)]).toEqual(['active', 'pending', 'pending']);
    runTo(2020);
    expect(el.getAttribute('data-stage')).toBe('2');
    expect([stateOf(1), stateOf(2)]).toEqual(['done', 'active']);
    runTo(4200);                                   // приложение ещё не готово
    expect(el.getAttribute('data-stage')).toBe('3');
    expect(statusText()).toBe('Финальная настройка…');
    const held = parseInt(pct(), 10);
    expect(held).toBeGreaterThanOrEqual(90);
    expect(held).toBeLessThan(95);
    expect(el.classList.contains('is-ready')).toBe(false);
    expect(document.getElementById('ym-splash-progress').value).toBe(held);
    expect(tipEl().hidden).toBe(true);             // обычная заставка — без подсказок
  });

  it('готовность: 100 % к заданному времени, мятный финал, уход и событие ym-splash-done', () => {
    const el = mount();
    const done = vi.fn();
    document.addEventListener('ym-splash-done', done);
    window.ymSplash.ready();                       // приложение готово сразу — заставка всё равно идёт 5 с
    runTo(3000);
    expect(el.classList.contains('is-ready')).toBe(false);
    runTo(3850);
    expect(pct()).toBe('100 %');
    expect(el.classList.contains('is-ready')).toBe(true);
    expect(statusText()).toBe('Готово к запуску');
    expect([stateOf(1), stateOf(2), stateOf(3)]).toEqual(['done', 'done', 'done']);
    vi.advanceTimersByTime(400);
    expect(el.classList.contains('is-leaving')).toBe(true);
    vi.advanceTimersByTime(560);
    expect(el.classList.contains('is-gone')).toBe(true);
    expect(done).toHaveBeenCalledTimes(1);
    expect(window.ymSplash.isDone()).toBe(true);
    document.removeEventListener('ym-splash-done', done);
  });

  it('медленная сеть: через 10 с — подсказка и кнопка обновления', () => {
    mount();
    const slow = document.getElementById('ym-splash-slow');
    runTo(9000);
    expect(slow.hidden).toBe(true);
    runTo(10100);
    expect(slow.hidden).toBe(false);
    expect(document.getElementById('ym-splash-reload').textContent).toBe('обновите страницу');
  });

  it('длинная заставка (от 8 с): этапы растянуты, подсказки сменяют друг друга', () => {
    const el = mount({ seconds: 20 });
    expect(el.getAttribute('data-length')).toBe('long');
    expect(el.style.getPropertyValue('--ym-t')).toBe('3.000');       // вступление растянуто, но не больше чем втрое
    runTo(2000);
    expect(el.getAttribute('data-stage')).toBe('0');                 // 0,15 × 20 с = 3 с — ещё подготовка
    expect(tipEl().hidden).toBe(true);
    runTo(2500);                                                      // 0,12 × 20 с = 2,4 с — первая подсказка
    expect(tipEl().hidden).toBe(false);
    const first = tipEl().textContent;
    expect(first).toBe('Сканируйте QR-код — чек сам проверится в ФНС');
    runTo(3020);
    expect(el.getAttribute('data-stage')).toBe('1');
    runTo(5500);                                                      // смена каждые 20/7 с, не чаще 3 с
    expect(tipEl().textContent).not.toBe(first);
    expect(tipEl().classList.contains('is-swap')).toBe(true);
    window.ymSplash.ready();
    runTo(15600);
    expect(pct()).toBe('100 %');
  });

  it('короткая заставка (до 2 с) — без списка этапов; 0 — без заставки', () => {
    const el = mount({ seconds: 2 });
    expect(el.getAttribute('data-length')).toBe('short');
    window.ymSplash.ready();
    runTo(1560);
    expect(pct()).toBe('100 %');
    const off = mount({ seconds: 0 });
    expect(off.classList.contains('is-gone')).toBe(true);
    expect(window.ymSplash.duration()).toBe(0);
    expect(() => window.ymSplash.ready()).not.toThrow();
  });

  it('свежая длительность с сервера: запоминается и перестраивает текущий показ', () => {
    const el = mount();
    runTo(500);
    window.ymSplash.setDuration(12);
    expect(localStorage.getItem('ym-splash-sec')).toBe('12');
    expect(el.getAttribute('data-length')).toBe('long');
    expect(window.ymSplash.duration()).toBe(12);
    window.ymSplash.setDuration(99);                                  // за пределами — до 60
    expect(localStorage.getItem('ym-splash-sec')).toBe('60');
    window.ymSplash.setDuration(0);                                   // администратор отключил заставку
    expect(el.classList.contains('is-gone')).toBe(true);
    expect(localStorage.getItem('ym-splash-sec')).toBe('0');
  });

  it('автотесты (webdriver) — не дольше 1,3 с при любом заданном времени', () => {
    const el = mount({ seconds: 30, webdriver: true });
    window.ymSplash.ready();
    runTo(1060);
    expect(el.classList.contains('is-ready')).toBe(true);
  });

  it('просмотр из настроек: заставка с выбранным временем, закрывается щелчком, без события загрузки', () => {
    const el = mount({ seconds: 1 });
    window.ymSplash.ready();
    runTo(2600);
    expect(el.classList.contains('is-gone')).toBe(true);
    const done = vi.fn();
    document.addEventListener('ym-splash-done', done);
    expect(window.ymSplash.preview(0)).toBe(false);
    expect(window.ymSplash.preview(9)).toBe(true);
    expect(el.classList.contains('is-gone')).toBe(false);
    expect(el.getAttribute('data-length')).toBe('long');
    runTo(4000);
    el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
    vi.advanceTimersByTime(560);
    expect(el.classList.contains('is-gone')).toBe(true);
    expect(done).not.toHaveBeenCalled();
    expect(localStorage.getItem('ym-splash-sec')).toBe('1');          // просмотр не меняет настройку
    document.removeEventListener('ym-splash-done', done);
  });

  it('скрипт пришёл позже страховки CSS — заставка не возвращается', () => {
    const el = mount({ startAt: 20000 });
    expect(el.classList.contains('is-gone')).toBe(true);
    expect(window.ymSplash).toBeUndefined();
  });

  it('повторный запуск скрипта не создаёт второй контроллер', () => {
    mount();
    const first = window.ymSplash;
    (0, eval)(splashJs);
    expect(window.ymSplash).toBe(first);
  });
});
