import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initBlogCarousel, initBlogFilters, pluralArticles } from '../../app/static/js/blog.js';

function buildBlogFixture() {
  const container = document.createElement('div');
  container.innerHTML = `
    <div class="blog-routes">
      <a href="#blog-catalog" data-jump-filter="fns">ФНС и ОФД</a>
    </div>
    <div class="bcar" data-blog-carousel role="region" aria-label="Карусель">
      <div class="bcar__track" data-carousel-track tabindex="0">
        <article class="bcar__slide" data-slide="0" data-cat="qr"><a class="slide-link" href="/blog/a1">1</a></article>
        <article class="bcar__slide" data-slide="1" data-cat="qr"><a class="slide-link" href="/blog/a2">2</a></article>
        <article class="bcar__slide" data-slide="2" data-cat="fns"><a class="slide-link" href="/blog/a3">3</a></article>
        <article class="bcar__slide" data-slide="3" data-cat="b2b"><a class="slide-link" href="/blog/a4">4</a></article>
      </div>
      <div class="bcar__controls">
        <button type="button" data-carousel-prev>←</button>
        <button type="button" data-carousel-next>→</button>
        <button type="button" data-carousel-toggle aria-pressed="false">Пауза</button>
        <div data-carousel-dots>
          <button type="button" class="bcar__dot" data-slide-to="0"><span class="bcar__dot-fill"></span></button>
          <button type="button" class="bcar__dot" data-slide-to="1"><span class="bcar__dot-fill"></span></button>
          <button type="button" class="bcar__dot" data-slide-to="2"><span class="bcar__dot-fill"></span></button>
          <button type="button" class="bcar__dot" data-slide-to="3"><span class="bcar__dot-fill"></span></button>
        </div>
        <p data-carousel-status>01 / 04</p>
      </div>
    </div>
    <section id="blog-catalog">
      <p data-filter-status></p>
      <div>
        <button type="button" data-filter="all" aria-pressed="true">Все</button>
        <button type="button" data-filter="qr" aria-pressed="false">QR</button>
        <button type="button" data-filter="fns" aria-pressed="false">ФНС</button>
        <button type="button" data-filter="b2b" aria-pressed="false">1С</button>
      </div>
      <ol>
        <li data-card-cat="qr">A1</li>
        <li data-card-cat="qr">A2</li>
        <li data-card-cat="fns">A3</li>
        <li data-card-cat="b2b">A4</li>
      </ol>
    </section>
  `;
  document.body.appendChild(container);
  const track = container.querySelector('[data-carousel-track]');
  track.scrollTo = vi.fn(({ left }) => {
    track.scrollLeft = left;
  });
  Object.defineProperty(track, 'clientWidth', { value: 800, configurable: true });
  Object.defineProperty(track, 'scrollWidth', { value: 3200, configurable: true });
  Array.from(track.querySelectorAll('[data-slide]')).forEach((slide, idx) => {
    Object.defineProperty(slide, 'offsetLeft', { value: idx * 700, configurable: true });
    Object.defineProperty(slide, 'offsetWidth', { value: 680, configurable: true });
  });
  return container;
}

describe('blog carousel and category filter', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = '';
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('formats Russian article plurals accurately', () => {
    expect(pluralArticles(0)).toBe('0 статей');
    expect(pluralArticles(1)).toBe('1 статья');
    expect(pluralArticles(2)).toBe('2 статьи');
    expect(pluralArticles(4)).toBe('4 статьи');
    expect(pluralArticles(5)).toBe('5 статей');
    expect(pluralArticles(10)).toBe('10 статей');
    expect(pluralArticles(11)).toBe('11 статей');
    expect(pluralArticles(21)).toBe('21 статья');
  });

  it('handles missing or empty DOM safely without throwing', () => {
    expect(initBlogCarousel(null)).toBeNull();
    expect(initBlogCarousel(document.createElement('div'))).toBeNull();
    expect(initBlogFilters(null)).toBeNull();
    expect(initBlogFilters(document.createElement('div'))).toBeNull();
  });

  it('navigates slides via buttons, dots, keyboard and wraps around cleanly', () => {
    const fixture = buildBlogFixture();
    const root = fixture.querySelector('[data-blog-carousel]');
    const api = initBlogCarousel(root, { reducedMotion: false, intervalMs: 3000 });

    expect(root.dataset.enhanced).toBe('true');
    expect(api.getIndex()).toBe(0);
    expect(root.querySelector('[data-carousel-status]').textContent).toBe('01 / 04');

    // Wrap backward 0 -> 3
    root.querySelector('[data-carousel-prev]').click();
    expect(api.getIndex()).toBe(3);
    expect(root.querySelector('[data-carousel-status]').textContent).toBe('04 / 04');
    expect(root.querySelector('[data-slide="3"]').dataset.active).toBe('true');
    expect(root.querySelector('[data-slide="2"]').dataset.near).toBe('prev');
    expect(root.querySelector('[data-slide="0"]').dataset.near).toBe('next');

    // Wrap forward 3 -> 0
    root.querySelector('[data-carousel-next]').click();
    expect(api.getIndex()).toBe(0);

    // Dot click -> slide 2
    root.querySelector('[data-slide-to="2"]').click();
    expect(api.getIndex()).toBe(2);
    expect(root.querySelector('[data-slide-to="2"]').getAttribute('aria-current')).toBe('true');
    expect(root.querySelector('[data-slide-to="0"]').hasAttribute('aria-current')).toBe(false);

    // Keyboard navigation
    root.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    expect(api.getIndex()).toBe(3);
    root.dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true }));
    expect(api.getIndex()).toBe(0);
    root.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }));
    expect(api.getIndex()).toBe(3);
    root.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
    expect(api.getIndex()).toBe(2);

    api.destroy();
    expect(root.dataset.enhanced).toBeUndefined();
  });

  it('autoplays, pauses on hover and toggle, and respects prefers-reduced-motion', () => {
    const fixture = buildBlogFixture();
    const root = fixture.querySelector('[data-blog-carousel]');
    const toggle = root.querySelector('[data-carousel-toggle]');
    const api = initBlogCarousel(root, { reducedMotion: false, intervalMs: 2500 });

    expect(root.dataset.paused).toBe('false');
    vi.advanceTimersByTime(2500);
    expect(api.getIndex()).toBe(1);

    // Hover pauses autoplay
    root.dispatchEvent(new Event('mouseenter'));
    expect(root.dataset.paused).toBe('true');
    vi.advanceTimersByTime(5000);
    expect(api.getIndex()).toBe(1);

    // Mouseleave resumes autoplay
    root.dispatchEvent(new Event('mouseleave'));
    expect(root.dataset.paused).toBe('false');
    vi.advanceTimersByTime(2500);
    expect(api.getIndex()).toBe(2);

    // Explicit pause toggle persists even after mouseleave
    toggle.click();
    expect(toggle.getAttribute('aria-pressed')).toBe('true');
    expect(toggle.textContent).toBe('Авто');
    expect(root.dataset.paused).toBe('true');
    vi.advanceTimersByTime(5000);
    expect(api.getIndex()).toBe(2);

    toggle.click();
    expect(toggle.getAttribute('aria-pressed')).toBe('false');
    expect(toggle.textContent).toBe('Пауза');
    api.destroy();

    // Reduced motion starts paused
    const reducedApi = initBlogCarousel(root, { reducedMotion: true, intervalMs: 2000 });
    expect(reducedApi.isPaused()).toBe(true);
    expect(root.dataset.paused).toBe('true');
    vi.advanceTimersByTime(4000);
    expect(reducedApi.getIndex()).toBe(0);
    reducedApi.destroy();
  });

  it('syncs active index on native scrollend', () => {
    const fixture = buildBlogFixture();
    const root = fixture.querySelector('[data-blog-carousel]');
    const track = root.querySelector('[data-carousel-track]');
    const api = initBlogCarousel(root, { autoplay: false });

    track.scrollLeft = 1350;
    track.dispatchEvent(new Event('scrollend'));
    expect(api.getIndex()).toBe(2);
    expect(root.querySelector('[data-carousel-status]').textContent).toBe('03 / 04');
    api.destroy();
  });

  it('filters catalog cards by category and syncs jump links with carousel', () => {
    const fixture = buildBlogFixture();
    const root = fixture.querySelector('[data-blog-carousel]');
    const carousel = initBlogCarousel(root, { autoplay: false });
    const filters = initBlogFilters(fixture, carousel);
    const cards = Array.from(fixture.querySelectorAll('[data-card-cat]'));
    const status = fixture.querySelector('[data-filter-status]');

    fixture.querySelector('[data-filter="qr"]').click();
    expect(filters.getActive()).toBe('qr');
    expect(cards.filter((c) => !c.hidden)).toHaveLength(2);
    expect(status.textContent).toBe('Рубрика «QR и реквизиты»: 2 статьи');
    expect(fixture.querySelector('[data-filter="qr"]').getAttribute('aria-pressed')).toBe('true');

    // Hero route card filters catalog AND focuses matching carousel slide
    fixture.querySelector('[data-jump-filter="fns"]').click();
    expect(filters.getActive()).toBe('fns');
    expect(cards.filter((c) => !c.hidden)).toHaveLength(1);
    expect(status.textContent).toBe('Рубрика «ФНС и ОФД»: 1 статья');
    expect(carousel.getIndex()).toBe(2);

    // Back to all
    fixture.querySelector('[data-filter="all"]').click();
    expect(cards.filter((c) => !c.hidden)).toHaveLength(4);
    expect(status.textContent).toBe('Показаны все 4 статьи в трёх рубриках');

    filters.destroy();
    carousel.destroy();
  });
});
