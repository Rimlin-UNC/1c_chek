// Устойчивая локальная очередь: только данные одного владельца, транзакции IndexedDB.
// Это не резервная копия: браузер может очистить хранилище.
const DB_NAME = 'ymaster-check-outbox';
const STORE = 'scans';

function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    let blocked = false;
    request.onupgradeneeded = () => {
      const store = request.result.createObjectStore(STORE, { keyPath: 'id' });
      store.createIndex('ownerId', 'ownerId');
    };
    request.onsuccess = () => {
      if (blocked) request.result.close();
      else resolve(request.result);
    };
    request.onerror = () => reject(request.error || new Error('Хранилище недоступно'));
    request.onblocked = () => { blocked = true; reject(new Error('Хранилище занято другой вкладкой')); };
  });
}

async function transaction(mode, operation) {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    let value;
    let tx;
    try {
      tx = db.transaction(STORE, mode);
      tx.oncomplete = () => { db.close(); resolve(value); };
      tx.onabort = () => { db.close(); reject(tx.error || new Error('Запись в хранилище не завершена')); };
      tx.onerror = () => {}; // причина возвращается через onabort
      operation(tx.objectStore(STORE), result => { value = result; });
    } catch (err) {
      if (tx) { try { tx.abort(); } catch { /* транзакция уже закрыта */ } }
      db.close();
      reject(err);
    }
  });
}

function validId(value) { return typeof value === 'string' && value.length > 0 && value.length <= 36; }
function nullableId(value) { return value === null || validId(value); }
function identifier() { return crypto.randomUUID(); }

export async function enqueue(item) {
  if (!item || !validId(item.ownerId) || !nullableId(item.companyId) || !nullableId(item.clientId)
      || (item.clientId !== null && item.clientId !== item.companyId)
      || typeof item.qrData !== 'string' || !item.qrData || item.qrData.length > 4000
      || typeof item.source !== 'string' || !item.source || item.source.length > 20) {
    throw new Error('Не удалось определить исходные данные и компанию скана');
  }
  const row = { id: identifier(), ownerId: item.ownerId, companyId: item.companyId,
    clientId: item.clientId, qrData: item.qrData, source: item.source,
    queuedAt: Date.now(), state: 'pending', leaseId: null, leaseUntil: 0,
    retryAt: 0, attempts: 0, message: '' };
  return transaction('readwrite', (store, done) => {
    // Одновременное повторное сканирование в разных вкладках сериализуется
    // транзакцией. Не объединяем записи разных компаний или клиентов.
    const request = store.index('ownerId').openCursor(IDBKeyRange.only(row.ownerId));
    request.onsuccess = () => {
      const cur = request.result;
      if (!cur) { store.add(row); done(row); return; }
      const previous = cur.value;
      if (previous.companyId === row.companyId && previous.clientId === row.clientId
          && previous.qrData === row.qrData) { done(previous); return; }
      cur.continue();
    };
  });
}

export async function list(ownerId) {
  if (!validId(ownerId)) throw new Error('Не указан владелец очереди');
  return transaction('readonly', (store, done) => {
    const request = store.index('ownerId').getAll(ownerId);
    request.onsuccess = () => done(request.result.sort((a, b) => a.queuedAt - b.queuedAt));
  });
}

export async function claim(ownerId, now = Date.now(), leaseMs = 60000) {
  if (!validId(ownerId) || !Number.isFinite(now) || !Number.isFinite(leaseMs) || leaseMs <= 0) {
    throw new Error('Неверные параметры синхронизации');
  }
  return transaction('readwrite', (store, done) => {
    done(null);
    const cursor = store.index('ownerId').openCursor(IDBKeyRange.only(ownerId));
    cursor.onsuccess = () => {
      const cur = cursor.result;
      if (!cur) return;
      const row = cur.value;
      const ready = (row.state === 'pending' && row.retryAt <= now)
        || (row.state === 'sending' && row.leaseUntil <= now);
      if (!ready) { cur.continue(); return; }
      row.state = 'sending'; row.leaseId = identifier(); row.leaseUntil = now + leaseMs;
      row.attempts += 1;
      cur.update(row);
      done(row);
    };
  });
}

async function finish(id, leaseId, update) {
  if (!validId(id) || !validId(leaseId)) throw new Error('Неверный идентификатор синхронизации');
  return transaction('readwrite', (store, done) => {
    done(false);
    const request = store.get(id);
    request.onsuccess = () => {
      const row = request.result;
      if (!row || row.state !== 'sending' || row.leaseId !== leaseId) return;
      if (update === null) store.delete(id);
      else {
        row.state = update.attention ? 'attention' : 'pending';
        row.leaseId = null; row.leaseUntil = 0;
        row.retryAt = update.retryAt || 0;
        row.message = String(update.message || '').slice(0, 500);
        store.put(row);
      }
      done(true);
    };
  });
}

export function complete(id, leaseId) { return finish(id, leaseId, null); }
export function fail(id, leaseId, update) {
  if (!update || typeof update.attention !== 'boolean') throw new Error('Не указан результат попытки');
  return finish(id, leaseId, update);
}

// Явное действие владельца разрешает повторить записи, ожидавшие ответа или проверки.
// Работающую аренду не отзываем — другая вкладка может ещё отправлять запись.
export async function retry(ownerId) {
  if (!validId(ownerId)) throw new Error('Не указан владелец очереди');
  return transaction('readwrite', (store, done) => {
    const request = store.index('ownerId').openCursor(IDBKeyRange.only(ownerId));
    request.onsuccess = () => {
      const cur = request.result;
      if (!cur) { done(true); return; }
      const row = cur.value;
      if (row.state === 'pending' || row.state === 'attention') {
        row.state = 'pending'; row.retryAt = 0; row.message = '';
        cur.update(row);
      }
      cur.continue();
    };
  });
}
