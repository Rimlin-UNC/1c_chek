/* T-90: печатный отчёт отправляется только в открытое пользователем окно. */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const get = vi.fn();
vi.mock('../../app/static/js/api.js', () => ({ api: { get }, setClient: vi.fn() }));
const { clientsOpenReport } = await import('../../app/static/js/clients.js');

beforeEach(() => {
  document.body.innerHTML = '<div id="toast-container"></div>';
  get.mockReset();
  URL.createObjectURL = vi.fn(() => 'blob:report');
  URL.revokeObjectURL = vi.fn();
});

describe('печать отчёта клиентов', () => {
  it('открывает окно сразу, отключает opener, запрашивает выбранный период с авторизацией API', async () => {
    const win = { opener: window, location: { href: '' }, addEventListener: vi.fn(), close: vi.fn() };
    vi.spyOn(window, 'open').mockReturnValue(win);
    get.mockResolvedValue('<html>отчёт</html>');
    await clientsOpenReport('2026-10');
    expect(get).toHaveBeenCalledWith('/api/v1/clients/report?period=2026-10');
    expect(win.opener).toBeNull();
    expect(win.location.href).toBe('blob:report');
    expect(URL.createObjectURL).toHaveBeenCalledOnce();
    vi.restoreAllMocks();
  });
  it('закрывает окно при отказе сервера и не подменяет ошибку пустым отчётом', async () => {
    const win = { opener: window, location: { href: '' }, addEventListener: vi.fn(), close: vi.fn() };
    vi.spyOn(window, 'open').mockReturnValue(win);
    get.mockRejectedValue(new Error('Нет доступа'));
    await clientsOpenReport('2026-10');
    expect(win.close).toHaveBeenCalledOnce();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
    vi.restoreAllMocks();
  });
});
