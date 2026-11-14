// Состояние по уже произошедшим событиям, без проверочных HTTP-запросов.
export function createConnectionStatus({ render, now = () => Date.now() }) {
  const MAX_AGE = 120000;
  let networkOffline = false;
  let live = false;
  let api = null;
  let lastApiAt = 0;
  let latestId = 0;
  let timer = null;

  function update() {
    clearTimeout(timer);
    const fresh = api === true && now() - lastApiAt < MAX_AGE;
    let state;
    if (networkOffline || api === false) state = 'disconnected';
    else if (live || fresh) state = 'connected';
    else if (api === true) state = 'unknown';
    else state = 'connecting';
    render({ state, live });
    if (!live && fresh && !networkOffline) timer = setTimeout(update, Math.max(1, MAX_AGE - (now() - lastApiAt)));
  }
  function apiResult(id, ok) {
    if (id <= latestId) return;
    latestId = id;
    api = ok;
    if (ok) { lastApiAt = now(); networkOffline = false; }
    update();
  }
  function offline(id = 0) {
    latestId = Math.max(latestId, id);
    networkOffline = true;
    live = false;
    api = null;
    update();
  }
  function online(id = 0) {
    latestId = Math.max(latestId, id);
    networkOffline = false;
    live = false;
    api = null;
    update();
  }
  function wsOpen() { networkOffline = false; live = true; update(); }
  function wsClosed() { live = false; update(); }
  function reset(id = 0) { latestId = Math.max(latestId, id); networkOffline = false; live = false; api = null; update(); }
  update();
  return { apiResult, offline, online, wsOpen, wsClosed, reset, update };
}
