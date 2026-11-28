/* DocVer — интерактивная карусель и рубрикатор блога (/blog).
 * Прогрессивное улучшение поверх серверного HTML и CSS Scroll-Snap:
 * все 10 статей и ссылок доступны роботам Яндекса и Google без JS,
 * а в браузере включаются стрелки, пагинация с прогрессом слайда,
 * перетаскивание мышью, клавиши, автопауза и фильтрация рубрик.
 * Совместимо со строгим CSP (script-src 'self'), без innerHTML и зависимостей. */

const DEFAULT_INTERVAL_MS = 5500;
const DRAG_THRESHOLD_PX = 8;
const SCROLL_DEBOUNCE_MS = 120;

const CATEGORY_LABELS = {
  qr: 'QR и реквизиты',
  fns: 'ФНС и ОФД',
  b2b: 'Бизнес и 1С',
};

export function pluralArticles(count) {
  const n = Math.abs(Number(count) || 0);
  const mod100 = n % 100;
  const mod10 = n % 10;
  if (mod100 >= 11 && mod100 <= 14) return `${n} статей`;
  if (mod10 === 1) return `${n} статья`;
  if (mod10 >= 2 && mod10 <= 4) return `${n} статьи`;
  return `${n} статей`;
}

function prefersReducedMotion(win = globalThis) {
  try {
    return Boolean(win.matchMedia && win.matchMedia('(prefers-reduced-motion: reduce)').matches);
  } catch {
    return false;
  }
}

function pad2(value) {
  return String(value).padStart(2, '0');
}

export function initBlogCarousel(root, options = {}) {
  if (!root || typeof root.querySelector !== 'function') return null;

  const track = root.querySelector('[data-carousel-track]');
  const slides = track ? Array.from(track.querySelectorAll('[data-slide]')) : [];
  if (!track || slides.length === 0) return null;

  const prevBtn = root.querySelector('[data-carousel-prev]');
  const nextBtn = root.querySelector('[data-carousel-next]');
  const toggleBtn = root.querySelector('[data-carousel-toggle]');
  const statusEl = root.querySelector('[data-carousel-status]');
  const dots = Array.from(root.querySelectorAll('[data-slide-to]'));

  const total = slides.length;
  const intervalMs = Math.max(1500, Number(options.intervalMs) || DEFAULT_INTERVAL_MS);
  const reduced = options.reducedMotion ?? prefersReducedMotion();

  let current = 0;
  let userPaused = Boolean(reduced || options.autoplay === false);
  let hoverPaused = false;
  let focusPaused = false;
  let hiddenPaused = typeof document !== 'undefined' && document.visibilityState === 'hidden';
  let timerId = null;
  let scrollDebounceId = null;
  let dragState = null;
  let suppressClickUntil = 0;

  const listeners = [];
  const on = (target, event, handler, opts) => {
    if (!target || typeof target.addEventListener !== 'function') return;
    target.addEventListener(event, handler, opts);
    listeners.push(() => target.removeEventListener(event, handler, opts));
  };

  function isEffectivelyPaused() {
    return userPaused || hoverPaused || focusPaused || hiddenPaused || total < 2;
  }

  function clearTimer() {
    if (timerId !== null) {
      clearTimeout(timerId);
      timerId = null;
    }
  }

  function syncPauseUi() {
    const paused = isEffectivelyPaused();
    root.dataset.paused = paused ? 'true' : 'false';
    if (toggleBtn) {
      toggleBtn.setAttribute('aria-pressed', userPaused ? 'true' : 'false');
      toggleBtn.textContent = userPaused ? 'Авто' : 'Пауза';
      toggleBtn.setAttribute(
        'aria-label',
        userPaused ? 'Включить автопрокрутку карусели' : 'Остановить автопрокрутку карусели',
      );
    }
  }

  function restartProgressAnimation(activeDot) {
    if (!activeDot) return;
    activeDot.classList.remove('is-ticking');
    // Чтение offsetWidth сбрасывает CSS-анимацию полосы прогресса без inline-стилей.
    void activeDot.offsetWidth;
    activeDot.classList.add('is-ticking');
  }

  function scheduleNext() {
    clearTimer();
    syncPauseUi();
    if (isEffectivelyPaused()) return;
    timerId = setTimeout(() => {
      goTo(current + 1, { fromAuto: true });
    }, intervalMs);
  }

  function applyState(nextIndex) {
    const normalized = ((nextIndex % total) + total) % total;
    current = normalized;
    const prevIdx = (current - 1 + total) % total;
    const nextIdx = (current + 1) % total;

    slides.forEach((slide, idx) => {
      if (idx === current) {
        slide.dataset.active = 'true';
        delete slide.dataset.near;
      } else {
        delete slide.dataset.active;
        if (total > 2 && idx === prevIdx) {
          slide.dataset.near = 'prev';
        } else if (total > 2 && idx === nextIdx) {
          slide.dataset.near = 'next';
        } else {
          delete slide.dataset.near;
        }
      }
    });

    dots.forEach((dot, idx) => {
      const active = idx === current;
      dot.classList.toggle('is-active', active);
      if (active) {
        dot.setAttribute('aria-current', 'true');
        restartProgressAnimation(dot);
      } else {
        dot.classList.remove('is-ticking');
        dot.removeAttribute('aria-current');
      }
    });

    if (statusEl) {
      statusEl.textContent = `${pad2(current + 1)} / ${pad2(total)}`;
    }
  }

  function targetScrollLeft(slide) {
    const trackWidth = track.clientWidth || 0;
    const slideWidth = slide.offsetWidth || 0;
    const centered = slide.offsetLeft - Math.max(0, (trackWidth - slideWidth) / 2);
    const maxScroll = Math.max(0, (track.scrollWidth || 0) - trackWidth);
    return Math.min(maxScroll, Math.max(0, Math.round(centered)));
  }

  function scrollTrackTo(left, instant) {
    const behavior = instant || prefersReducedMotion() ? 'auto' : 'smooth';
    try {
      if (typeof track.scrollTo === 'function') {
        track.scrollTo({ left, behavior });
      } else {
        track.scrollLeft = left;
      }
    } catch {
      track.scrollLeft = left;
    }
  }

  function goTo(index, opts = {}) {
    const normalized = ((Number(index) || 0) % total + total) % total;
    applyState(normalized);
    if (!opts.skipScroll) {
      scrollTrackTo(targetScrollLeft(slides[normalized]), Boolean(opts.instant));
    }
    scheduleNext();
    return current;
  }

  function detectClosestSlide() {
    const trackWidth = track.clientWidth || 0;
    if (trackWidth <= 0) return current;
    const center = track.scrollLeft + trackWidth / 2;
    let bestIdx = current;
    let bestDist = Number.POSITIVE_INFINITY;
    slides.forEach((slide, idx) => {
      const slideCenter = slide.offsetLeft + slide.offsetWidth / 2;
      const dist = Math.abs(slideCenter - center);
      if (dist < bestDist) {
        bestDist = dist;
        bestIdx = idx;
      }
    });
    return bestIdx;
  }

  function syncFromScroll() {
    const closest = detectClosestSlide();
    if (closest !== current) {
      applyState(closest);
      scheduleNext();
    }
  }

  function setPaused(flag) {
    userPaused = Boolean(flag);
    if (!userPaused) {
      restartProgressAnimation(dots[current]);
    }
    scheduleNext();
  }

  on(prevBtn, 'click', () => goTo(current - 1));
  on(nextBtn, 'click', () => goTo(current + 1));
  on(toggleBtn, 'click', () => setPaused(!userPaused));

  dots.forEach((dot) => {
    on(dot, 'click', () => {
      const idx = Number.parseInt(dot.dataset.slideTo || '0', 10);
      if (!Number.isNaN(idx)) goTo(idx);
    });
  });

  on(root, 'keydown', (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      goTo(current + 1);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      goTo(current - 1);
    } else if (event.key === 'Home') {
      event.preventDefault();
      goTo(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      goTo(total - 1);
    }
  });

  on(track, 'scroll', () => {
    if (scrollDebounceId !== null) clearTimeout(scrollDebounceId);
    scrollDebounceId = setTimeout(syncFromScroll, SCROLL_DEBOUNCE_MS);
  }, { passive: true });

  on(track, 'scrollend', () => {
    if (scrollDebounceId !== null) {
      clearTimeout(scrollDebounceId);
      scrollDebounceId = null;
    }
    syncFromScroll();
  }, { passive: true });

  // Перетаскивание мышью на компьютере с защитой от случайного перехода по ссылке.
  on(track, 'pointerdown', (event) => {
    if (event.pointerType !== 'mouse' || event.button !== 0) return;
    if (event.target && typeof event.target.closest === 'function' && event.target.closest('button')) return;
    dragState = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startScroll: track.scrollLeft,
      moved: false,
    };
    hoverPaused = true;
    scheduleNext();
  });

  on(track, 'pointermove', (event) => {
    if (!dragState || event.pointerId !== dragState.pointerId) return;
    const delta = event.clientX - dragState.startX;
    if (!dragState.moved && Math.abs(delta) > DRAG_THRESHOLD_PX) {
      dragState.moved = true;
      track.classList.add('is-dragging');
    }
    if (dragState.moved) {
      event.preventDefault();
      track.scrollLeft = dragState.startScroll - delta;
    }
  });

  const finishDrag = (event) => {
    if (!dragState) return;
    const delta = typeof event.clientX === 'number' ? event.clientX - dragState.startX : 0;
    const wasMoved = dragState.moved;
    dragState = null;
    track.classList.remove('is-dragging');
    if (wasMoved) {
      suppressClickUntil = Date.now() + 250;
      if (Math.abs(delta) > 48) {
        goTo(delta < 0 ? current + 1 : current - 1);
      } else {
        goTo(detectClosestSlide());
      }
    }
  };

  on(track, 'pointerup', finishDrag);
  on(track, 'pointercancel', finishDrag);
  on(track, 'mouseleave', finishDrag);
  on(track, 'dragstart', (event) => event.preventDefault());

  on(track, 'click', (event) => {
    if (Date.now() < suppressClickUntil) {
      event.preventDefault();
      event.stopPropagation();
    }
  }, true);

  on(root, 'mouseenter', () => {
    hoverPaused = true;
    scheduleNext();
  });
  on(root, 'mouseleave', () => {
    hoverPaused = false;
    scheduleNext();
  });
  on(root, 'focusin', () => {
    focusPaused = true;
    scheduleNext();
  });
  on(root, 'focusout', (event) => {
    if (!root.contains(event.relatedTarget)) {
      focusPaused = false;
      scheduleNext();
    }
  });
  on(root, 'touchstart', () => {
    hoverPaused = true;
    scheduleNext();
  }, { passive: true });
  on(root, 'touchend', () => {
    hoverPaused = false;
    scheduleNext();
  }, { passive: true });

  if (typeof document !== 'undefined') {
    on(document, 'visibilitychange', () => {
      hiddenPaused = document.visibilityState === 'hidden';
      scheduleNext();
    });
  }

  try {
    const mq = globalThis.matchMedia && globalThis.matchMedia('(prefers-reduced-motion: reduce)');
    if (mq) {
      on(mq, 'change', (event) => {
        if (event.matches) setPaused(true);
      });
    }
  } catch {}

  root.dataset.enhanced = 'true';
  applyState(0);
  scheduleNext();

  return {
    goTo,
    next: () => goTo(current + 1),
    prev: () => goTo(current - 1),
    setPaused,
    isPaused: () => userPaused,
    getIndex: () => current,
    destroy() {
      clearTimer();
      if (scrollDebounceId !== null) clearTimeout(scrollDebounceId);
      while (listeners.length) {
        const off = listeners.pop();
        try { off(); } catch {}
      }
      delete root.dataset.enhanced;
      delete root.dataset.paused;
    },
  };
}

export function initBlogFilters(scope = globalThis.document, carousel = null) {
  if (!scope || typeof scope.querySelectorAll !== 'function') return null;

  const buttons = Array.from(scope.querySelectorAll('[data-filter]'));
  const cards = Array.from(scope.querySelectorAll('[data-card-cat]'));
  const jumpLinks = Array.from(scope.querySelectorAll('[data-jump-filter]'));
  const statusEl = scope.querySelector('[data-filter-status]');
  if (buttons.length === 0 || cards.length === 0) return null;

  let active = 'all';
  const listeners = [];

  function commitFilter(slug) {
    const next = slug && slug !== 'all' ? slug : 'all';
    active = next;
    let visibleCount = 0;

    cards.forEach((card) => {
      const match = next === 'all' || card.dataset.cardCat === next;
      card.hidden = !match;
      card.dataset.filtered = match ? 'in' : 'out';
      if (match) visibleCount += 1;
    });

    buttons.forEach((btn) => {
      const isSelected = (btn.dataset.filter || 'all') === next;
      btn.classList.toggle('is-active', isSelected);
      btn.setAttribute('aria-pressed', isSelected ? 'true' : 'false');
    });

    if (statusEl) {
      if (next === 'all') {
        statusEl.textContent = `Показаны все ${pluralArticles(visibleCount)} в трёх рубриках`;
      } else {
        const title = CATEGORY_LABELS[next] || next;
        statusEl.textContent = `Рубрика «${title}»: ${pluralArticles(visibleCount)}`;
      }
    }
  }

  function applyFilter(slug) {
    const useTransition =
      typeof document !== 'undefined'
      && typeof document.startViewTransition === 'function'
      && !prefersReducedMotion();
    if (useTransition) {
      try {
        document.startViewTransition(() => commitFilter(slug));
        return active;
      } catch {}
    }
    commitFilter(slug);
    return active;
  }

  buttons.forEach((btn) => {
    const handler = () => applyFilter(btn.dataset.filter || 'all');
    btn.addEventListener('click', handler);
    listeners.push(() => btn.removeEventListener('click', handler));
  });

  jumpLinks.forEach((link) => {
    const handler = () => {
      const slug = link.dataset.jumpFilter || 'all';
      applyFilter(slug);
      if (carousel && typeof carousel.goTo === 'function') {
        const slides = Array.from(scope.querySelectorAll('[data-slide][data-cat]'));
        const firstIdx = slides.findIndex((slide) => slide.dataset.cat === slug);
        if (firstIdx >= 0) carousel.goTo(firstIdx, { skipScroll: false });
      }
    };
    link.addEventListener('click', handler);
    listeners.push(() => link.removeEventListener('click', handler));
  });

  return {
    applyFilter,
    getActive: () => active,
    destroy() {
      while (listeners.length) {
        const off = listeners.pop();
        try { off(); } catch {}
      }
    },
  };
}

function bootBlog() {
  if (typeof document === 'undefined') return;
  const carouselRoot = document.querySelector('[data-blog-carousel]');
  const carousel = carouselRoot ? initBlogCarousel(carouselRoot) : null;
  initBlogFilters(document, carousel);
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootBlog, { once: true });
  } else {
    bootBlog();
  }
}
