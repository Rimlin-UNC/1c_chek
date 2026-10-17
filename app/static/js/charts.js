// ======================================================================
// Ямастер Чек — лёгкие SVG-графики (без внешних зависимостей)
// Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
// ======================================================================

const PALETTE = ['#6366f1', '#22d3ee', '#34d399', '#fbbf24', '#f87171', '#c084fc', '#60a5fa'];

function tooltipEl() {
  let el = document.querySelector('.chart-tooltip');
  if (!el) {
    el = document.createElement('div');
    el.className = 'chart-tooltip';
    document.body.appendChild(el);
  }
  return el;
}

function bindTooltip(elem, html) {
  const tip = tooltipEl();
  elem.addEventListener('mousemove', (e) => {
    tip.innerHTML = html;
    tip.style.opacity = 1;
    const x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 10);
    const y = Math.max(8, e.clientY - tip.offsetHeight - 12);
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  });
  elem.addEventListener('mouseleave', () => { tip.style.opacity = 0; });
}

/**
 * Столбчатая диаграмма: чеки по дням (count + sum).
 * data: [{date:'2025-09-01', count: 5, sum: 12345.5}]
 */
export function barChart(container, data, { height = 240 } = {}) {
  if (!data || !data.length) { container.innerHTML = emptyChart(); return; }
  const W = Math.max(container.clientWidth || 600, 320);
  const H = height;
  const padL = 44, padR = 12, padT = 16, padB = 30;
  const iw = W - padL - padR, ih = H - padT - padB;
  const maxCount = Math.max(4, ...data.map(d => d.count));
  const maxSum = Math.max(1, ...data.map(d => d.sum));

  const xw = iw / data.length;
  const bw = Math.max(6, Math.min(34, xw * 0.62));
  const defs = `<defs><linearGradient id="barGrad" x1="0" y1="1" x2="0" y2="0">
      <stop offset="0%" stop-color="#6366f1"/><stop offset="100%" stop-color="#22d3ee"/>
    </linearGradient></defs>`;

  let grid = '';
  const ySteps = 4;
  for (let i = 0; i <= ySteps; i++) {
    const y = padT + ih - (ih * i / ySteps);
    const val = Math.round(maxCount * i / ySteps);
    grid += `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="rgba(255,255,255,.07)"/>
             <text x="${padL - 8}" y="${y + 4}" text-anchor="end" font-size="10.5" fill="#6b7494">${val}</text>`;
  }

  let bars = '', labels = '';
  data.forEach((d, i) => {
    const h = d.count > 0 ? Math.max(3, ih * d.count / maxCount) : 2;
    const x = padL + i * xw + (xw - bw) / 2;
    const y = padT + ih - h;
    const isToday = i === data.length - 1;
    bars += `<rect x="${x}" y="${y}" width="${bw}" height="${h}" rx="${Math.min(6, bw / 2)}"
        fill="url(#barGrad)" opacity="${d.count ? (isToday ? 1 : 0.82) : 0.25}">
      </rect>`;
    const day = d.date.slice(8, 10), mon = d.date.slice(5, 7);
    if (data.length <= 20 || i % Math.ceil(data.length / 14) === 0) {
      labels += `<text x="${x + bw / 2}" y="${H - 10}" text-anchor="middle" font-size="10" fill="#6b7494">${day}.${mon}</text>`;
    }
    // Прозрачная зона для тултипа
    const hit = `<rect x="${padL + i * xw}" y="${padT}" width="${xw}" height="${ih}" fill="transparent" data-hit="1"/>`;
    bars += hit;
    const hitEl = () => {}; // тултип биндим после вставки в DOM
    bars += `<!-- tip:${i} -->`;
  });

  container.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" style="display:block">${defs}${grid}${bars}${labels}</svg>`;

  // Тултипы по зонам
  const zones = container.querySelectorAll('rect[data-hit]');
  zones.forEach((z, i) => {
    const d = data[i];
    bindTooltip(z, `<b>${new Date(d.date).toLocaleDateString('ru-RU', { day: 'numeric', month: 'long' })}</b><br>
      Чеков: <b>${d.count}</b><br>Сумма: <b>${new Intl.NumberFormat('ru-RU').format(d.sum)} ₽</b>`);
  });
}

/** Кольцевая диаграмма статусов. data: [{label, value, color?}] */
export function donutChart(container, data, { size = 190, centerTitle = '', centerSub = '' } = {}) {
  const total = data.reduce((s, d) => s + d.value, 0);
  if (!total) { container.innerHTML = emptyChart(); return; }
  const cx = size / 2, cy = size / 2, r = size / 2 - 10, sw = 22;
  const C = 2 * Math.PI * r;
  let offset = 0, segs = '', legend = '';
  data.forEach((d, i) => {
    const frac = d.value / total;
    const color = d.color || PALETTE[i % PALETTE.length];
    const len = frac * C;
    segs += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${color}"
      stroke-width="${sw}" stroke-dasharray="${Math.max(len - 2, 0)} ${C}"
      stroke-dashoffset="${-offset}" stroke-linecap="round" style="transition: stroke-dasharray .8s ease">
      <title>${d.label}: ${d.value}</title></circle>`;
    offset += len;
    legend += `<span class="li"><span class="swatch" style="background:${color}"></span>${d.label} · <b>${d.value}</b></span>`;
  });
  container.innerHTML = `
    <div style="display:flex;gap:22px;align-items:center;flex-wrap:wrap;justify-content:center">
      <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
        <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="rgba(255,255,255,.06)" stroke-width="${sw}"/>
        ${segs}
        <text x="${cx}" y="${cy - 2}" text-anchor="middle" font-size="24" font-weight="800" fill="#e8ecf8">${centerTitle}</text>
        <text x="${cx}" y="${cy + 18}" text-anchor="middle" font-size="10.5" fill="#6b7494">${centerSub}</text>
      </svg>
      <div class="legend" style="flex-direction:column;align-items:flex-start;gap:8px">${legend}</div>
    </div>`;
}

function emptyChart() {
  return `<div class="empty-state" style="padding:30px"><span class="big-ico">📉</span>Нет данных за период</div>`;
}
