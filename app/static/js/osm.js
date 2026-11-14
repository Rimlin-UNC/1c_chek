// Совместимость с ранее сохранённым app.js: удалённый контур не выполняет действий.
// После обновления старых вкладок этот переходный модуль можно убрать отдельно.
export { localServerCardHtml, localServerWire, localServerStop } from './server_monitor.js?v=1.72.27';
export function osmCardHtml() { return ''; }
export function osmWire() {}
export function osmStop() {}
