// Контрактные тесты «интерфейс ↔ API» (T-41, v1.70.110).
//
// Извлекает из кода интерфейса вызовы API: метод, шаблон пути и поля JSON-тела.
// Код разбирает настоящий парсер JavaScript (acorn), а не регулярные выражения
// по тексту: шаблонные строки, регулярные выражения и «?.» его не сбивают.
// Результат — JSON в stdout; сверку с маршрутами и моделями FastAPI делает
// tests/test_ui_api_contract.py.
//
// По каждому вызову: файл и строка, имя функции интерфейса вокруг вызова,
// метод, варианты пути («{}» — динамическая часть: идентификатор, строка
// запроса), поля тела. Для тела «const body = {…}» учитываются и поздние
// «body.x = …» в той же функции — с условиями, под которыми поле попадает в
// тело (например, «!coreLocked»), и поля вложенных объектов и списков
// (позиции чека из collectItems()).
//
// Запуск: node tests/ui_api_calls.mjs [файлы…] (по умолчанию — app/static/js/*.js,
// мини-приложение MAX и кабинет B2B)
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'acorn';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const PARAM = '\u0000';                  // метка динамической части пути
const METHODS = { get: 'GET', post: 'POST', put: 'PUT', patch: 'PATCH', del: 'DELETE' };
const FUNCS = new Set(['FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression']);
const SKIP = new Set(['loc', 'start', 'end', 'range']);
const LIMIT = 16;                        // предел вариантов пути у одного вызова

function children(node) {
  const out = [];
  for (const key of Object.keys(node)) {
    if (SKIP.has(key)) continue;
    const value = node[key];
    if (Array.isArray(value)) {
      for (const v of value) if (v && typeof v.type === 'string') out.push(v);
    } else if (value && typeof value.type === 'string') {
      out.push(value);
    }
  }
  return out;
}

// Обход дерева со стеком предков: предки нужны, чтобы найти область видимости
function walk(node, visit, ancestors = []) {
  visit(node, ancestors);
  ancestors.push(node);
  for (const child of children(node)) walk(child, visit, ancestors);
  ancestors.pop();
}

// Обход одной области видимости (тело функции без вложенных функций) с предками
function walkScope(scope, visit) {
  const inner = (node, ancestors) => {
    visit(node, ancestors);
    if (FUNCS.has(node.type)) return;
    ancestors.push(node);
    for (const child of children(node)) inner(child, ancestors);
    ancestors.pop();
  };
  const ancestors = [scope];
  for (const child of children(scope)) inner(child, ancestors);
}

function scopesOf(ancestors) {
  return ancestors.filter((a) => a.type === 'Program' || FUNCS.has(a.type)).reverse();
}

// Ближайшее объявление переменной до места вызова — от внутренней области к внешней
function declaration(name, ancestors, before) {
  for (const scope of scopesOf(ancestors)) {
    let best = null;
    walkScope(scope, (node, path) => {
      if (node.type === 'VariableDeclarator' && node.id.type === 'Identifier'
          && node.id.name === name && node.start < before
          && (!best || node.start > best.declarator.start)) {
        const parent = path[path.length - 1];
        best = { declarator: node, kind: parent && parent.kind, scope };
      }
    });
    if (best) return best;
  }
  return null;
}

// Имя функции интерфейса вокруг вызова (анонимные обработчики пропускаются)
function functionName(ancestors) {
  for (let i = ancestors.length - 1; i >= 0; i--) {
    const node = ancestors[i];
    if (!FUNCS.has(node.type)) continue;
    if (node.id && node.id.name) return node.id.name;
    const parent = ancestors[i - 1];
    if (parent && parent.type === 'VariableDeclarator' && parent.id.type === 'Identifier') {
      return parent.id.name;
    }
    if (parent && parent.type === 'Property' && !parent.computed && parent.key.type === 'Identifier') {
      return parent.key.name;
    }
  }
  return null;
}

function stringValue(node) {
  return node.type === 'Literal' && typeof node.value === 'string' ? node.value : null;
}

// Значения константы-сегмента: «const path = blocked ? 'block' : 'unblock'»
function constStrings(node) {
  const literal = stringValue(node);
  if (literal !== null) return [literal];
  if (node.type === 'ConditionalExpression') {
    const a = constStrings(node.consequent);
    const b = constStrings(node.alternate);
    return a && b ? [...a, ...b] : null;
  }
  return null;
}

// Варианты пути: строка с метками PARAM на месте динамических частей
function urlVariants(node, ancestors, depth = 0) {
  if (!node || depth > 4) return [PARAM];
  const literal = stringValue(node);
  if (literal !== null) return [literal];
  if (node.type === 'TemplateLiteral') {
    let variants = [''];
    node.quasis.forEach((quasi, i) => {
      variants = variants.map((v) => v + quasi.value.cooked);
      if (i < node.expressions.length) {
        const parts = urlVariants(node.expressions[i], ancestors, depth + 1);
        variants = variants.flatMap((v) => parts.map((p) => v + p));
      }
    });
    return variants.slice(0, LIMIT);
  }
  if (node.type === 'BinaryExpression' && node.operator === '+') {
    const left = urlVariants(node.left, ancestors, depth + 1);
    const right = urlVariants(node.right, ancestors, depth + 1);
    return left.flatMap((l) => right.map((r) => l + r)).slice(0, LIMIT);
  }
  if (node.type === 'ConditionalExpression') {
    return [...urlVariants(node.consequent, ancestors, depth + 1),
      ...urlVariants(node.alternate, ancestors, depth + 1)].slice(0, LIMIT);
  }
  if (node.type === 'CallExpression' && node.callee.type === 'Identifier'
      && node.callee.name === 'encodeURIComponent') {
    return urlVariants(node.arguments[0], ancestors, depth + 1);
  }
  if (node.type === 'Identifier') {
    const found = declaration(node.name, ancestors, node.start);
    const init = found && found.declarator.init;
    if (init && found.kind === 'const') {
      // константа-сегмент — подставляем все её значения
      const values = constStrings(init);
      if (values) return values;
    }
    if (init && (init.type === 'TemplateLiteral'
        || (init.type === 'BinaryExpression' && init.operator === '+'))) {
      // константа с путём («const url = '/api/v1/…' + id»)
      const parts = urlVariants(init, ancestors, depth + 1);
      if (parts.some((p) => p.startsWith('/api/'))) return parts;
    }
  }
  return [PARAM];
}

// «x || null», «x ?? null»: стёртое поле уходит как null
function nullWhenEmpty(value) {
  return value.type === 'LogicalExpression' && ['||', '??'].includes(value.operator)
    && value.right.type === 'Literal' && value.right.value === null;
}

function objectKeys(obj) {
  const keys = [];
  const nullish = [];
  let open = false;
  for (const prop of obj.properties) {
    if (prop.type === 'SpreadElement' || prop.computed) { open = true; continue; }
    let key = null;
    if (prop.key.type === 'Identifier') key = prop.key.name;
    else if (prop.key.type === 'Literal') key = String(prop.key.value);
    if (key === null) continue;
    keys.push(key);
    if (nullWhenEmpty(prop.value)) nullish.push(key);
  }
  return { keys, open, nullish };
}

function returned(fn) {
  if (fn.body.type !== 'BlockStatement') return fn.body;
  const ret = fn.body.body.find((s) => s.type === 'ReturnStatement');
  return ret ? ret.argument : null;
}

// Поля вложенного значения: объект, список объектов, «….map(x => ({…}))»,
// вызов константы-функции («collectItems()»)
function nested(node, ancestors, depth = 0) {
  if (!node || depth > 4) return null;
  if (node.type === 'ObjectExpression') return { kind: 'object', ...objectKeys(node) };
  if (node.type === 'ArrayExpression') {
    const objects = node.elements.filter((e) => e && e.type === 'ObjectExpression');
    if (!objects.length) return null;
    const keys = new Set();
    let open = false;
    for (const obj of objects) {
      const info = objectKeys(obj);
      info.keys.forEach((k) => keys.add(k));
      open = open || info.open;
    }
    return { kind: 'array', keys: [...keys], open };
  }
  if (node.type === 'CallExpression') {
    const callee = node.callee;
    if (callee.type === 'MemberExpression' && !callee.computed) {
      const name = callee.property.name;
      if (name === 'map' && node.arguments[0] && FUNCS.has(node.arguments[0].type)) {
        const item = nested(returned(node.arguments[0]), ancestors, depth + 1);
        return item && item.kind === 'object' ? { ...item, kind: 'array' } : null;
      }
      if (['filter', 'slice', 'concat', 'sort', 'reverse'].includes(name)) {
        return nested(callee.object, ancestors, depth + 1);
      }
      return null;
    }
    if (callee.type === 'Identifier') {
      const found = declaration(callee.name, ancestors, node.start);
      const init = found && found.declarator.init;
      if (init && FUNCS.has(init.type)) return nested(returned(init), ancestors, depth + 1);
    }
    return null;
  }
  if (node.type === 'Identifier') {
    const found = declaration(node.name, ancestors, node.start);
    if (found && found.declarator.init) return nested(found.declarator.init, ancestors, depth + 1);
  }
  return null;
}

// Поля тела: литерал объекта или переменная «const body = {…}» плюс «body.x = …»
function bodyInfo(node, ancestors, call, source) {
  const result = (kind, keys = [], open = false, extra = {}) =>
    ({ kind, keys, open, nullish: [], conditions: {}, nested: {}, ...extra });
  if (!node) return result('none');
  const nestedOf = (obj, path) => {
    const out = {};
    for (const prop of obj.properties) {
      if (prop.type !== 'Property' || prop.computed || prop.key.type !== 'Identifier') continue;
      const info = nested(prop.value, path);
      if (info) out[prop.key.name] = info;
    }
    return out;
  };
  if (node.type === 'ObjectExpression') {
    const { keys, open, nullish } = objectKeys(node);
    return result('json', keys, open, { nullish, nested: nestedOf(node, ancestors) });
  }
  if (node.type !== 'Identifier') return result('dynamic', [], true);
  const found = declaration(node.name, ancestors, call.start);
  const init = found && found.declarator.init;
  if (!init) return result('dynamic', [], true);
  if (init.type === 'NewExpression' && init.callee.type === 'Identifier'
      && ['FormData', 'URLSearchParams', 'Blob'].includes(init.callee.name)) {
    return result('form', [], true);
  }
  if (init.type !== 'ObjectExpression') return result('dynamic', [], true);
  const { keys, open: literalOpen, nullish } = objectKeys(init);
  let open = literalOpen;
  const conditions = {};
  const nestedKeys = nestedOf(init, ancestors);
  const name = node.name;
  walkScope(found.scope, (n, path) => {
    if (n.type === 'AssignmentExpression' && n.left.type === 'MemberExpression'
        && n.left.object.type === 'Identifier' && n.left.object.name === name) {
      if (n.left.computed) { open = true; return; }
      const key = n.left.property.name;
      keys.push(key);
      if (nullWhenEmpty(n.right)) nullish.push(key);
      // условия «if (…)» между началом функции и присваиванием
      const guards = [];
      for (let i = 1; i < path.length; i++) {
        const parent = path[i - 1];
        const child = path[i];
        if (parent.type === 'IfStatement' && child !== parent.test) {
          const test = source.slice(parent.test.start, parent.test.end);
          guards.push(child === parent.consequent ? test : `!(${test})`);
        }
      }
      if (guards.length) conditions[key] = guards;
      const info = nested(n.right, [...ancestors.slice(0, ancestors.indexOf(found.scope) + 1), ...path.slice(1)]);
      if (info) nestedKeys[key] = info;
    }
    if (n.type === 'CallExpression' && n.callee.type === 'MemberExpression'
        && n.callee.object.type === 'Identifier' && n.callee.object.name === 'Object'
        && n.callee.property.name === 'assign' && n.arguments[0]
        && n.arguments[0].type === 'Identifier' && n.arguments[0].name === name) {
      for (const extra of n.arguments.slice(1)) {
        if (extra.type === 'ObjectExpression') {
          const more = objectKeys(extra);
          keys.push(...more.keys);
          open = open || more.open;
        } else open = true;
      }
    }
  });
  return result('json', [...new Set(keys)], open,
    { nullish: [...new Set(nullish)], conditions, nested: nestedKeys });
}

// Свойство литерала опций: «{ method: 'POST', body: {…} }»
function option(options, name) {
  if (!options || options.type !== 'ObjectExpression') return undefined;
  const prop = options.properties.find((p) => p.type === 'Property' && !p.computed
    && p.key.type === 'Identifier' && p.key.name === name);
  return prop ? prop.value : undefined;
}

function optionMethod(options) {
  const value = option(options, 'method');
  if (value === undefined) return 'GET';
  const text = stringValue(value);
  return text ? text.toUpperCase() : null;
}

// Префикс клиента B2B: «var API = "/api/v1/b2b"» в области видимости вызова
function prefixOf(ancestors, before) {
  const found = declaration('API', ancestors, before);
  const init = found && found.declarator.init;
  return init ? stringValue(init) : null;
}

function calls(file) {
  const source = readFileSync(file, 'utf8');
  const options = { ecmaVersion: 'latest', locations: true, allowHashBang: true };
  let ast;
  try {
    ast = parse(source, { ...options, sourceType: 'script' });
  } catch {
    ast = parse(source, { ...options, sourceType: 'module' });
  }
  const out = [];
  walk(ast, (node, ancestors) => {
    if (node.type !== 'CallExpression') return;
    const callee = node.callee;
    const args = node.arguments;
    let method = null;
    let body = null;
    let urlNode = args[0];
    let prefix = '';
    let via = 'api';
    if (callee.type === 'MemberExpression' && !callee.computed
        && callee.object.type === 'Identifier' && callee.object.name === 'api'
        && callee.property.type === 'Identifier') {
      // кабинет: api.get/post/put/patch/del/download (app/static/js/api.js)
      const name = callee.property.name;
      if (name in METHODS) method = METHODS[name];
      else if (name === 'download') method = args.length > 1 ? 'POST' : 'GET';
      else return;
      if (['POST', 'PUT', 'PATCH'].includes(method)) body = args[1];
    } else if (callee.type === 'Identifier' && callee.name === 'api') {
      const verb = args[0] ? stringValue(args[0]) : null;
      if (verb && /^(GET|POST|PUT|PATCH|DELETE)$/i.test(verb)) {
        // мини-приложение MAX: api(метод, путь, тело)
        method = verb.toUpperCase();
        urlNode = args[1];
        body = args[2] || null;
      } else {
        // кабинет B2B: api(путь, { method, body }) — путь без префикса API
        method = optionMethod(args[1]);
        if (!method) return;
        prefix = prefixOf(ancestors, node.start);
        if (prefix === null) return;
        body = option(args[1], 'body') || null;
      }
    } else if (callee.type === 'Identifier' && callee.name === 'fetch') {
      method = optionMethod(args[1]);
      via = 'fetch';
      if (!method) return;
    } else {
      return;
    }
    if (body && !['POST', 'PUT', 'PATCH'].includes(method)) body = null;
    const base = { file: relative(ROOT, file).split('\\').join('/'), line: node.loc.start.line,
      func: functionName(ancestors), method, via };
    const urls = urlVariants(urlNode, ancestors).map((v) => prefix + v)
      .filter((v) => v.startsWith('/api/'));
    if (!urls.length) {
      // путь целиком вычисляется (переменная без значения в коде) — в отчёт как
      // «не проверен», чтобы тест видел, сколько вызовов осталось вне контракта
      if (via === 'api') out.push({ ...base, urls: [], dynamic: true, body: bodyInfo(null) });
      return;
    }
    out.push({ ...base, urls: [...new Set(urls)].map((u) => u.split(PARAM).join('{}')),
      dynamic: false, body: body === null ? bodyInfo(null) : bodyInfo(body, ancestors, node, source) });
  });
  return out;
}

// Весь интерфейс: кабинет, мини-приложение MAX и кабинет B2B
const dir = join(ROOT, 'app', 'static', 'js');
const DEFAULT = [
  ...readdirSync(dir).filter((f) => f.endsWith('.js')).sort().map((f) => join(dir, f)),
  join(ROOT, 'app', 'static', 'maxapp', 'maxapp.js'),
  join(ROOT, 'app', 'static', 'b2b', 'b2b.js'),
];
const given = process.argv.slice(2);
const files = given.length ? given.map((f) => join(ROOT, f)) : DEFAULT;
process.stdout.write(JSON.stringify(files.flatMap(calls)));
