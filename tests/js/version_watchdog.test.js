import { describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';

const source = readFileSync('app/static/js/app.js', 'utf8');
function harness(get) {
  const start = source.indexOf('// Общий ограничитель запросов версии');
  const end = source.indexOf('// v1.3.0: версия приложения', start);
  if (start < 0 || end < 0) throw new Error('ограничитель версии не найден');
  return new Function('api', `${source.slice(start, end)}; return requestAboutForWatchdog;`)({ get });
}

describe('сторож версии: запросы не чаще одного раза в минуту', () => {
  it('одновременный запрос и частые возвращения не создают повторов, после минуты проверка возобновляется', async () => {
    vi.useFakeTimers(); vi.setSystemTime(100000);
    try {
      let resolve;
      const get = vi.fn().mockImplementationOnce(() => new Promise(r => { resolve = r; }))
        .mockResolvedValue({ version: '2' });
      const check = harness(get);
      const first = check();
      await check();
      expect(get).toHaveBeenCalledTimes(1);
      resolve({ version: '1' }); await first;
      vi.advanceTimersByTime(59000);
      await check(); await check();
      expect(get).toHaveBeenCalledTimes(1);
      vi.advanceTimersByTime(1000);
      expect(await check()).toEqual({ version: '2' });
      expect(get).toHaveBeenCalledTimes(2);
    } finally { vi.useRealTimers(); }
  });

  it('зависший запрос отменяется и не блокирует последующие проверки навсегда', async () => {
    vi.useFakeTimers(); vi.setSystemTime(100000);
    try {
      const get = vi.fn().mockImplementationOnce((_path, { signal }) => new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(new Error('отменён')));
      })).mockResolvedValue({ version: '2' });
      const check = harness(get);
      const pending = check();
      const rejected = expect(pending).rejects.toThrow('отменён');
      await vi.advanceTimersByTimeAsync(20000);
      await rejected;
      await vi.advanceTimersByTimeAsync(40000);
      expect(await check()).toEqual({ version: '2' });
      expect(get).toHaveBeenCalledTimes(2);
    } finally { vi.useRealTimers(); }
  });

  it('ошибки не вызывают частых повторов, следующая попытка возможна через минуту', async () => {
    vi.useFakeTimers(); vi.setSystemTime(100000);
    try {
      const get = vi.fn().mockRejectedValueOnce(new Error('сеть')).mockResolvedValue({ version: '2' });
      const check = harness(get);
      await expect(check()).rejects.toThrow('сеть');
      await check(); expect(get).toHaveBeenCalledTimes(1);
      vi.advanceTimersByTime(60000);
      expect(await check()).toEqual({ version: '2' });
    } finally { vi.useRealTimers(); }
  });
});
