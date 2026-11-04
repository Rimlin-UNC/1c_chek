/* v1.70.98: контроллер заставки загрузки js/splash.js (Vitest, jsdom).

   Разметка — настоящая, из index.html (#ym-splash); время и кадры управляются
   тестом: performance.now и requestAnimationFrame подменены, таймеры — фальшивые.
   Проверяется: версия из адреса стилей и с сервера, этапы 0 → 25 → 68 → 90 → 100 %,
   ожидание на 90 % без готовности приложения, подсказка о медленной сети,
   мятный финал, уход и событие ym-splash-done, быстрый повтор в той же вкладке,
   поздняя загрузка скрипта (страховка CSS уже скрыла заставку). */
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

function mount({ seen = false, startAt = 0 } = {}) {
  document.head.innerHTML = '<link rel="stylesheet" href="/css/app.css?v=9.8.7">';
  document.body.innerHTML = markup;
  sessionStorage.clear();
  if (seen) sessionStorage.setItem('ym-splash-seen', '1');
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

describe('заставка загрузки (v1.70.98)', () => {
  beforeEach(() => { vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] }); });
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); delete window.ymSplash; });

  it('разметка в index.html: 90 % экрана, этапы, версия, выпуск и владелец', () => {
    expect(start).toBeGreaterThan(0);
    expect(markup).toContain('<progress class="ym-splash__native" id="ym-splash-progress" max="100" value="0"');
    expect(markup).not.toContain('style="');
    expect(markup).toContain('Выпуск 2026<i>•</i>ООО «Ямастер»');
    expect(markup.match(/<li data-step=/g)).toHaveLength(3);
    const css = fs.readFileSync(path.join(root, 'app/static/css/splash.css'), 'utf8');
    expect(css).toContain('width: 90vw; height: 90vh; height: 90dvh;');
    expect(css).toContain('@media (prefers-color-scheme: dark)');
    expect(css).toContain('@media (prefers-reduced-motion: reduce)');
  });

  it('версия — сначала из адреса стилей, затем с сервера', () => {
    mount();
    expect(document.getElementById('ym-splash-version').textContent).toBe('9.8.7');
    window.ymSplash.setVersion('v1.70.98');
    expect(document.getElementById('ym-splash-version').textContent).toBe('1.70.98');
  });

  it('этапы идут по таймлайну и ждут приложение на 90 %', () => {
    const el = mount();
    expect(el.classList.contains('is-js')).toBe(true);
    expect(pct()).toBe('0 %');
    runTo(520);
    expect(el.getAttribute('data-stage')).toBe('1');
    expect(statusText()).toBe('Инициализация…');
    expect([stateOf(1), stateOf(2), stateOf(3)]).toEqual(['active', 'pending', 'pending']);
    runTo(1320);
    expect(el.getAttribute('data-stage')).toBe('2');
    expect([stateOf(1), stateOf(2)]).toEqual(['done', 'active']);
    runTo(2600);                                   // приложение ещё не готово
    expect(el.getAttribute('data-stage')).toBe('3');
    expect(statusText()).toBe('Финальная настройка…');
    const held = parseInt(pct(), 10);
    expect(held).toBeGreaterThanOrEqual(90);
    expect(held).toBeLessThan(95);
    expect(el.classList.contains('is-ready')).toBe(false);
    expect(document.getElementById('ym-splash-progress').value).toBe(held);
  });

  it('готовность: 100 %, мятный финал, уход и событие ym-splash-done', () => {
    const el = mount();
    const done = vi.fn();
    document.addEventListener('ym-splash-done', done);
    window.ymSplash.ready();                       // приложение готово сразу
    runTo(1900);
    expect(el.classList.contains('is-ready')).toBe(false);   // таймлайн доигрывается
    runTo(2500);
    expect(pct()).toBe('100 %');
    expect(el.classList.contains('is-ready')).toBe(true);
    expect(statusText()).toBe('Готово к запуску');
    expect([stateOf(1), stateOf(2), stateOf(3)]).toEqual(['done', 'done', 'done']);
    vi.advanceTimersByTime(320);
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

  it('повторная загрузка в той же вкладке — быстрее', () => {
    const el = mount({ seen: true });
    window.ymSplash.ready();
    runTo(1050);
    expect(el.classList.contains('is-ready')).toBe(true);
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
