/* ==========================================================================
   Ямастер Чек — понимание чеков (v1.70.101, T-69, ADR-31)
   Позиции чека читаются по-человечески: «Цемент М500 Д0, 50 кг» вместо
   «[М] 4607… ЦЕМЕНТ М500 Д0 50КГ» (исходная строка — по наведению), у каждой
   позиции — категория с иконкой и причиной («по слову «цемент»», «модель —
   82 %»). Бухгалтер меняет категорию одним выбором — программа запоминает это
   для компании и сразу применяет к таким же позициям. Здесь же — сводка чека
   по категориям, подсказка статьи расходов, память исправлений и профиль.
   ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
   ========================================================================== */
import { api } from './api.js';
import { esc, fmtSum, toast } from './ui.js';

let goodsCatalogCache = null;

/** Справочник категорий (один запрос на сеанс). */
async function goodsCatalog() {
  if (!goodsCatalogCache) goodsCatalogCache = await api.get('/api/v1/goods/catalog');
  return goodsCatalogCache;
}

function goodsQty(q) {
  const n = Number(q) || 0;
  return Number.isInteger(n) ? String(n) : n.toFixed(3).replace(/\.?0+$/, '').replace('.', ',');
}

/** Чип категории позиции: иконка, название, «?» у догадки; подсказка — почему так. */
function goodsChipHTML(it, editable) {
  const title = it.category_title || it.category_group || 'Прочее';
  const why = [it.category_source_title, it.category_note].filter(Boolean).join(': ');
  const unsure = it.category_certain === false ? ' gd-unsure' : '';
  const src = ` gd-src-${esc(it.category_source || 'none')}`;
  const label = `${esc(it.category_icon || '❔')} ${esc(title)}${unsure ? ' ?' : ''}`;
  if (!editable || !it.id) {
    return `<span class="gd-chip${unsure}${src}" title="${esc(why)}">${label}</span>`;
  }
  return `<button type="button" class="gd-chip gd-edit-btn${unsure}${src}" data-gd-item="${esc(it.id)}"
    title="${esc(why ? why + ' · нажмите, чтобы изменить' : 'Нажмите, чтобы выбрать категорию')}">${label}</button>`;
}

/** Наименование: читаемое, исходная строка из ФНС — по наведению. */
function goodsNameHTML(it) {
  const shown = it.display_name || it.name || '—';
  const changed = it.name && shown !== it.name;
  return `<div class="gd-name"${changed ? ` title="${esc('В чеке: ' + it.name)}"` : ''}>${esc(shown)}</div>`;
}

/** Сводка чека по категориям: иконка, название, число позиций и сумма. */
function goodsSummaryHTML(goods) {
  const cats = (goods && goods.categories) || [];
  if (!cats.length) return '';
  return `<div class="gd-summary">${cats.map((c) => `<span class="gd-sum-chip" title="${esc(c.group || '')}">
    ${esc(c.icon || '❔')} ${esc(c.title)} · ${c.count} · ${fmtSum(c.sum)}</span>`).join('')}</div>`;
}

/** Подсказка статьи расходов по товарам чека (выбор бухгалтера не перезаписывается). */
function goodsArticleHintHTML(goods, canApply) {
  const hint = goods && goods.article_hint;
  if (!hint || !hint.article) return '';
  return `<div class="gd-hint">💡 Статья по товарам: <b>${esc(hint.article)}</b>
    <span class="form-hint">${esc(hint.reason || '')}</span>
    ${canApply ? `<button type="button" class="btn btn-sm" id="gd-apply-article" data-article="${esc(hint.article)}">Подставить</button>` : ''}</div>`;
}

/** Таблица позиций карточки чека: читаемые названия, категория, фасовка. */
function goodsItemsHTML(items, { editable = false, goods = null } = {}) {
  const list = items || [];
  return `${goodsSummaryHTML(goods)}
    <table class="data gd-table"><thead><tr><th>Наименование</th><th>Кол.</th><th class="gd-price">Цена</th><th>Сумма</th></tr></thead>
    <tbody>${list.map((it) => `<tr class="gd-row" data-gd-row="${esc(it.id || '')}">
      <td>${goodsNameHTML(it)}<div class="gd-meta"><span class="gd-chip-slot">${goodsChipHTML(it, editable)}</span>
        ${it.pack ? `<span class="gd-pack">${esc(it.pack)}</span>` : ''}</div></td>
      <td>${goodsQty(it.quantity)}</td>
      <td class="cell-sum gd-price">${fmtSum(it.price)}</td><td class="cell-sum">${fmtSum(it.total)}</td></tr>`).join('')}</tbody></table>
    ${editable ? '<p class="form-hint gd-note">Категорию можно поменять: нажмите на неё. Выбор запоминается для компании и сразу применяется к таким же товарам.</p>' : ''}`;
}

function goodsPickerHTML(catalog, current) {
  const byGroup = new Map();
  for (const c of (catalog && catalog.categories) || []) {
    if (!byGroup.has(c.group)) byGroup.set(c.group, []);
    byGroup.get(c.group).push(c);
  }
  const groups = [...byGroup.entries()].map(([group, cats]) => `<optgroup label="${esc(group)}">
    ${cats.map((c) => `<option value="${esc(c.code)}" ${c.code === current ? 'selected' : ''}>${esc(c.icon)} ${esc(c.title)}</option>`).join('')}
    </optgroup>`).join('');
  return `<span class="gd-editor"><select class="gd-pick" aria-label="Категория товара">${groups}</select>
    <label class="gd-remember"><input type="checkbox" class="gd-remember-box" checked> запомнить</label>
    <button type="button" class="btn btn-sm gd-cancel" aria-label="Отмена">✕</button></span>`;
}

/** Нажатие на чип → выбор категории → сохранение; onSaved(ответ) — обновить сводку и т. п. */
function goodsBindItems(root, items, { onSaved } = {}) {
  if (!root) return;
  const byId = new Map((items || []).map((it) => [String(it.id), it]));
  root.addEventListener('click', async (ev) => {
    const btn = ev.target.closest ? ev.target.closest('.gd-edit-btn') : null;
    if (!btn || !root.contains(btn)) return;
    const it = byId.get(btn.dataset.gdItem);
    if (!it) return;
    let catalog;
    try { catalog = await goodsCatalog(); } catch (e) { toast(e.message, 'err'); return; }
    const slot = btn.parentElement;
    slot.innerHTML = goodsPickerHTML(catalog, it.category);
    const sel = slot.querySelector('.gd-pick');
    const restore = () => { slot.innerHTML = goodsChipHTML(it, true); };
    slot.querySelector('.gd-cancel').onclick = restore;
    if (sel.focus) sel.focus();
    sel.onchange = async () => {
      const remember = slot.querySelector('.gd-remember-box').checked;
      try {
        const res = await api.put(`/api/v1/goods/items/${encodeURIComponent(it.id)}`, { category: sel.value, remember });
        Object.assign(it, res.item || {});
        restore();
        toast(res.message || 'Категория сохранена', 'ok', '🏷');
        if (onSaved) onSaved(res);
      } catch (e) { toast(e.message, 'err'); restore(); }
    };
  });
}

/** Карточка «Память товаров»: что программа запомнила по исправлениям бухгалтеров. */
function goodsMemoryHTML(rows, canEdit) {
  const list = rows || [];
  return `<div class="card-title">🏷 Память товаров
      <span class="form-hint">исправления категорий (v1.70.101)</span></div>
    <p class="form-hint gd-intro">Бухгалтер поменял категорию позиции — программа запомнила это для компании и
    применяет к таким же товарам в новых чеках. «Цемент М500 50 кг» и «ЦЕМЕНТ М500 25КГ» — один товар.</p>
    ${list.length ? `<div class="table-wrap"><table class="data gd-table gd-mem"><thead><tr><th>Товар</th><th>Категория</th>
      ${canEdit ? '<th></th>' : ''}</tr></thead><tbody>
      ${list.map((m) => `<tr><td>${esc(m.sample_name || m.name_key)}<div class="form-hint">ключ: ${esc(m.name_key)} ·
        применено: ${Number(m.hits) || 0}</div></td>
        <td>${esc(m.icon || '')} ${esc(m.category_title)}</td>
        ${canEdit ? `<td><button type="button" class="btn btn-sm gd-forget" data-id="${esc(m.id)}">Забыть</button></td>` : ''}</tr>`).join('')}
      </tbody></table></div>`
      : '<p class="form-hint gd-empty">Пока пусто: поменяйте категорию позиции в карточке чека — она появится здесь.</p>'}`;
}

function goodsBindMemory(root, { companyId = '', onChange } = {}) {
  if (!root) return;
  root.querySelectorAll('.gd-forget').forEach((b) => {
    b.onclick = async () => {
      try {
        const q = companyId ? `?company_id=${encodeURIComponent(companyId)}` : '';
        const res = await api.del(`/api/v1/goods/memory/${encodeURIComponent(b.dataset.id)}${q}`);
        toast(res.message || 'Забыто', 'ok', '🏷');
        if (onChange) onChange();
      } catch (e) { toast(e.message, 'err'); }
    };
  });
}

/** Поле «Профиль компании» в политике расходов. */
function goodsProfileHTML(pol, canEdit) {
  const profiles = (pol && pol.goods_profiles) || [];
  if (!profiles.length) return '';
  const current = profiles.find((p) => p.code === pol.goods_profile) || profiles[0];
  return `<label class="field gd-profile"><span>Профиль компании</span>
      <select id="pol-profile" ${canEdit ? '' : 'disabled'}>${profiles.map((p) => `<option value="${esc(p.code)}"
        ${p.code === current.code ? 'selected' : ''}>${esc(p.title)}</option>`).join('')}</select>
      <span class="form-hint" id="pol-profile-hint">${esc(current.hint || '')}</span></label>
    <div class="form-hint gd-own">${pol.goods_own
      ? `Запрещённые группы — своя настройка компании.${canEdit ? ' <button type="button" class="btn btn-sm" id="pol-goods-reset">Вернуть по профилю</button>' : ''}`
      : 'Запрещённые группы — по профилю компании; отметьте свои, если нужно.'}</div>`;
}

function goodsBindProfile(root, pol) {
  const sel = root && root.querySelector('#pol-profile');
  const hint = root && root.querySelector('#pol-profile-hint');
  if (!sel || !hint) return;
  sel.addEventListener('change', () => {
    const p = ((pol && pol.goods_profiles) || []).find((x) => x.code === sel.value);
    hint.textContent = p ? p.hint : '';
  });
}

export { goodsCatalog, goodsChipHTML, goodsNameHTML, goodsSummaryHTML, goodsArticleHintHTML, goodsItemsHTML,
  goodsBindItems, goodsMemoryHTML, goodsBindMemory, goodsProfileHTML, goodsBindProfile };
