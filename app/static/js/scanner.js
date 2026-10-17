// ======================================================================
// Ямастер Чек — модуль сканирования: камера, изображения, офлайн-очередь
// Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
// ======================================================================

const QUEUE_KEY = 'ymaster_check_offline_queue';

// --- Офлайн-очередь сканов (localStorage) --------------------------------
export const offlineQueue = {
  all() { try { return JSON.parse(localStorage.getItem(QUEUE_KEY) || '[]'); } catch { return []; } },
  push(item) {
    const q = this.all();
    q.push({ ...item, queued_at: new Date().toISOString() });
    localStorage.setItem(QUEUE_KEY, JSON.stringify(q));
    return q.length;
  },
  shift() {
    const q = this.all();
    const first = q.shift();
    localStorage.setItem(QUEUE_KEY, JSON.stringify(q));
    return first;
  },
  clear() { localStorage.setItem(QUEUE_KEY, '[]'); },
  size() { return this.all().length; },
};

// --- Разбор QR-строки на клиенте (зеркало серверной логики 54-ФЗ) ----------
export function parseQrClient(raw) {
  raw = (raw || '').trim();
  const tags = {};
  if (/^https?:\/\//i.test(raw)) {
    try {
      const q = new URL(raw).searchParams;
      q.forEach((v, k) => { tags[k.toLowerCase()] = v; });
    } catch { return null; }
  } else {
    raw.split(/[&;\n]/).forEach(part => {
      const i = part.indexOf('=');
      if (i > 0) {
        const k = part.slice(0, i).trim().toLowerCase();
        let canon = k;
        if (k === 'fpd') canon = 'fp';
        if (k === 'fd') canon = 'i';
        if (k === 'sum') canon = 's';
        if (k === 'dt') canon = 't';
        if (!(canon in tags)) tags[canon] = part.slice(i + 1).trim();
      }
    });
  }
  if (!tags.fn || !tags.i || !tags.fp) return null;
  if (!/^\d{8,20}$/.test(tags.fn) || !/^\d{1,10}$/.test(tags.i) || !/^\d{1,10}$/.test(tags.fp)) return null;
  let date = null;
  if (tags.t) {
    const m = tags.t.match(/^(\d{4})(\d{2})(\d{2})T(\d{2}):?(\d{2})/);
    if (m) date = `${m[3]}.${m[2]}.${m[1]} ${m[4]}:${m[5]}`;
  }
  return {
    fn: tags.fn, fd: tags.i, fp: tags.fp,
    sum: tags.s ? tags.s.replace(',', '.') : '',
    date, op: tags.n || '1',
  };
}

// --- Декодирование изображений (jsQR + BarcodeDetector) ----------------------
async function decodeImageElement(img) {
  const codes = [];
  const canvas = document.createElement('canvas');

  const scanCanvas = (w, h) => {
    canvas.width = w; canvas.height = h;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0, w, h);
    const data = ctx.getImageData(0, 0, w, h);
    const res = window.jsQR(data.data, w, h, { inversionAttempts: 'attemptBoth' });
    if (res && res.data) codes.push(res.data);
  };

  // 1) Прямое сканирование
  scanCanvas(img.naturalWidth, img.naturalHeight);

  // 2) Апскейл маленьких изображений
  if (img.naturalHeight < 900) {
    const k = Math.min(3, 1100 / Math.max(img.naturalHeight, 1));
    scanCanvas(Math.round(img.naturalWidth * k), Math.round(img.naturalHeight * k));
  }
  // 3) Бинаризация (блики/тени)
  if (!codes.length) {
    const w = img.naturalWidth, h = img.naturalHeight;
    canvas.width = w; canvas.height = h;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    const id = ctx.getImageData(0, 0, w, h);
    const d = id.data;
    for (let i = 0; i < d.length; i += 4) {
      const g = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
      const b = g > 128 ? 255 : 0;
      d[i] = d[i + 1] = d[i + 2] = b;
    }
    ctx.putImageData(id, 0, 0);
    const res = window.jsQR(ctx.getImageData(0, 0, w, h).data, w, h, { inversionAttempts: 'attemptBoth' });
    if (res && res.data) codes.push(res.data);
  }
  return codes;
}

export async function decodeImageFile(file) {
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise((res, rej) => {
      const i = new Image();
      i.onload = () => res(i);
      i.onerror = () => rej(new Error('Не удалось прочитать изображение'));
      i.src = url;
    });
    const raws = [...new Set(await decodeImageElement(img))];
    const valid = raws.map(parseQrClient).filter(Boolean);
    return { raws, valid };
  } finally {
    URL.revokeObjectURL(url);
  }
}

// --- Камера: живое сканирование -------------------------------------------
export class CameraScanner {
  constructor(videoEl, onDetect) {
    this.video = videoEl;
    this.onDetect = onDetect;
    this.stream = null;
    this.running = false;
    this._raf = 0;
    this._canvas = document.createElement('canvas');
    this._lastText = '';
    this._lastTime = 0;
    this._detector = ('BarcodeDetector' in window)
      ? new window.BarcodeDetector({ formats: ['qr_code'] }) : null;
  }

  async start() {
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'environment', width: { ideal: 1920 }, height: { ideal: 1080 } },
        audio: false,
      });
      this.video.srcObject = this.stream;
      await this.video.play();
      this.running = true;
      this._tick();
      return true;
    } catch (e) {
      throw new Error('Нет доступа к камере. Разрешите доступ в браузере или используйте загрузку фото / ручной ввод.');
    }
  }

  stop() {
    this.running = false;
    cancelAnimationFrame(this._raf);
    if (this.stream) { this.stream.getTracks().forEach(t => t.stop()); this.stream = null; }
  }

  _tick = () => {
    if (!this.running) return;
    if (this.video.readyState === this.video.HAVE_ENOUGH_DATA) {
      const now = Date.now();
      if (this._detector) {
        // Быстрый нативный путь (Chrome/Android)
        this._detector.detect(this.video)
          .then(codes => { codes.forEach(c => this._handle(c.rawValue, now)); })
          .catch(() => {});
      } else if (window.jsQR) {
        const w = 640;
        const h = Math.round(this.video.videoHeight / this.video.videoWidth * w) || 480;
        this._canvas.width = w; this._canvas.height = h;
        const ctx = this._canvas.getContext('2d', { willReadFrequently: true });
        ctx.drawImage(this.video, 0, 0, w, h);
        const id = ctx.getImageData(0, 0, w, h);
        const res = window.jsQR(id.data, w, h, { inversionAttempts: 'dontInvert' });
        if (res && res.data) this._handle(res.data, now);
      }
    }
    this._raf = requestAnimationFrame(this._tick);
  };

  _handle(text, now) {
    if (!text) return;
    // Антидребезг: один и тот же код не чаще раза в 2.5 секунды
    if (text === this._lastText && now - this._lastTime < 2500) return;
    this._lastText = text;
    this._lastTime = now;
    this.onDetect(text);
  }
}
