/* ======================================================================
   Ямастер Чек — кабинет компаний «API для бизнеса» (v1.70.45).

   Страница отдельная от основной SPA: своя авторизация (JWT typ=b2b),
   своё хранилище токена. Содержимое закрытых страниц приходит с
   /api/v1/b2b/content — сервер проверяет статус компании, поэтому
   спрятать раздел «в интерфейсе» недостаточно и не нужно: данные просто
   не отдаются неподтверждённой компании.

   Правила: динамический текст экранируется (данные компании вводит она
   сама), схемы вставляются только если это действительно SVG без
   вложенных скриптов, формы защищены полем-ловушкой и замером времени
   заполнения. Внешних зависимостей нет.
   ООО «Ямастер» | https://ymaster.ru | chek@ymaster.ru
   ====================================================================== */
(function () {
  "use strict";

  var API = "/api/v1/b2b";
  var TOKEN_KEY = "b2b_token";
  var HONEYPOT_MS = 800;

  var GATED = ["hub", "docs", "tariffs", "contact"];
  var AUTHED = GATED.concat(["cabinet"]);
  var ANON = ["start", "register", "login", "confirm", "newpass"];

  var state = { company: null, token: "", verified: false, formOpened: 0 };

  /* --- утилиты ---------------------------------------------------------- */
  function $(sel) { return document.querySelector(sel); }
  function $all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function fmt(value) {
    if (value === null || value === undefined || value === "") { return "—"; }
    return String(value);
  }

  function num(value) {
    var n = Number(value || 0);
    if (!isFinite(n)) { return "—"; }
    return n.toLocaleString("ru-RU");
  }

  /* Доля израсходованного лимита в процентах, 0…100. Лимит может быть нулевым
     у только что выданного ключа — тогда полоса пустая, а не «бесконечная». */
  function pct(used, limit) {
    var total = Number(limit || 0);
    if (!isFinite(total) || total <= 0) { return 0; }
    var part = Number(used || 0);
    if (!isFinite(part) || part <= 0) { return 0; }
    return Math.max(0, Math.min(100, Math.round((part / total) * 100)));
  }

  /* Полоса расхода лимита: цвет — уровнем (классом), а не инлайновым стилем,
     поэтому палитра остаётся в CSS и проверяется на контраст. Ширина — число,
     которое мы посчитали сами, вставка безопасна. */
  function usageBar(title, used, limit) {
    var p = pct(used, limit);
    var level = p >= 90 ? "bad" : (p >= 70 ? "warn" : "ok");
    var left = Math.max(0, Number(limit || 0) - Number(used || 0));
    return '<div class="b2b-usage">' +
      '<div class="b2b-usage-head"><span>' + esc(title) + "</span>" +
      '<span class="b2b-usage-num">' + num(used) + " из " + num(limit) +
      " · " + p + "%</span></div>" +
      '<div class="b2b-track" role="img" aria-label="' + esc(title) +
      ": израсходовано " + p + " процентов, осталось " + num(left) + '">' +
      '<span class="b2b-fill ' + level + '" style="width:' + p + '%"></span>' +
      "</div>" +
      '<p class="b2b-usage-hint">' + (p >= 90
        ? "Лимит почти исчерпан: повторите позже или обсудите смену тарифа."
        : (p >= 70 ? "Остаток мал — планируйте запросы."
          : "Осталось " + num(left) + " запросов.")) + "</p>" +
      "</div>";
  }

  function msg(text, kind) {
    var box = $("#b2b-msg");
    box.textContent = text || "";
    box.className = "b2b-msg" + (kind ? " " + kind : "");
    if (text) { box.classList.remove("hidden"); } else { box.classList.add("hidden"); }
  }

  function detail(err, fallback) {
    var data = err && err.body ? err.body : null;
    if (data && typeof data.detail === "string") { return data.detail; }
    if (data && data.message) { return data.message; }
    return fallback;
  }

  function api(path, options) {
    var opts = options || {};
    var headers = { "Content-Type": "application/json" };
    if (state.token) { headers["Authorization"] = "Bearer " + state.token; }
    return fetch(API + path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) {
          var err = new Error("refused");
          err.status = res.status;
          err.body = body;
          throw err;
        }
        return body;
      });
    });
  }

  function dropToken() {
    state.token = "";
    state.company = null;
    state.verified = false;
    try { window.localStorage.removeItem(TOKEN_KEY); } catch (e) { /* приватный режим */ }
  }

  function keepToken(token) {
    state.token = token || "";
    try { window.localStorage.setItem(TOKEN_KEY, state.token); } catch (e) { /* приватный режим */ }
  }

  /* --- навигация -------------------------------------------------------- */
  function parseHash() {
    var raw = (window.location.hash || "").replace(/^#\/?/, "");
    var parts = raw.split("?");
    var name = (parts[0] || "start").toLowerCase();
    var query = {};
    (parts[1] || "").split("&").forEach(function (pair) {
      if (!pair) { return; }
      var kv = pair.split("=");
      query[decodeURIComponent(kv[0])] = decodeURIComponent((kv[1] || "").replace(/\+/g, " "));
    });
    return { name: name, query: query };
  }

  function go(name) {
    if (window.location.hash === "#/" + name) { applyRoute(); } else {
      window.location.hash = "#/" + name;
    }
  }

  function syncNav(route) {
    $all(".b2b-tab").forEach(function (tab) {
      var view = tab.getAttribute("data-view");
      var gated = tab.getAttribute("data-gated") === "1";
      var authed = tab.getAttribute("data-auth") === "1";
      var anon = tab.getAttribute("data-anon") === "1";
      var show = true;
      if (gated) { show = state.verified; }
      if (authed) { show = !!state.company; }
      if (anon && state.company) { show = (view === "start"); }
      tab.classList.toggle("hidden", !show);
      tab.classList.toggle("active", view === route);
    });
    var who = $("#b2b-who");
    if (state.company) {
      who.innerHTML = "<b>" + esc(state.company.company_name || state.company.email) +
        "</b> · " + esc(state.company.email) + " · " +
        esc(state.company.status_title || state.company.status);
    } else {
      who.textContent = "Кабинет для компаний и ИП. Доступ к разделу — после " +
        "подтверждения статуса через корпоративную почту.";
    }
  }

  function applyRoute() {
    var parsed = parseHash();
    var route = parsed.name;
    var allowed = state.verified ? AUTHED.concat(ANON) :
      (state.company ? AUTHED.concat(ANON) : ANON);
    if (allowed.indexOf(route) < 0) { route = "start"; }
    if ((GATED.indexOf(route) >= 0) && !state.verified) {
      msg("Раздел откроется после подтверждения статуса компании: перейдите по " +
        "ссылке из письма на корпоративную почту", "bad");
      route = state.company ? "cabinet" : "start";
    }
    if (route === "cabinet" && !state.company) { route = "login"; }

    $all(".b2b-view").forEach(function (view) {
      view.classList.toggle("hidden", view.getAttribute("data-view") !== route);
    });
    syncNav(route);
    load(route, parsed.query);
    window.scrollTo(0, 0);
  }

  /* --- загрузка содержимого страниц ------------------------------------- */
  function load(route, query) {
    if (route === "start") { return loadStart(); }
    if (route === "hub") { return loadPage("hub", renderHub); }
    if (route === "docs") { return loadPage("docs", renderDocs); }
    if (route === "tariffs") { return loadPage("tariffs", renderTariffs); }
    if (route === "contact") { return loadPage("contact", renderContact); }
    if (route === "cabinet") { return loadCabinet(); }
    if (route === "confirm") { return confirmEmail(query.token || ""); }
    if (route === "register" || route === "login" || route === "newpass") {
      state.formOpened = Date.now();
      if (route === "newpass") { pendingReset = query.token || ""; }
    }
    return undefined;
  }

  function loadStart() {
    return api("/status").then(function (data) {
      fillList("#start-steps", (data.steps || []).map(function (s) {
        return "<li><b>" + esc(s.title) + "</b>" + esc(s.text) + "</li>";
      }), true);
      fillList("#start-requirements", data.requirements || []);
      fillList("#start-not-accepted", data.not_accepted || []);
      var body = $("#start-tariffs tbody");
      body.innerHTML = (data.tariffs || []).map(function (t) {
        return "<tr><td>" + esc(t.title) + "</td><td>" + num(t.rate_per_hour) +
          "</td><td>" + num(t.monthly_quota) + "</td><td>" + esc(t.price) + "</td></tr>";
      }).join("");
      var closed = $("#start-closed");
      if (data.enabled) {
        closed.textContent = "Подключение открыто: зарегистрируйте компанию — " +
          "письмо для подтверждения придёт на корпоративную почту.";
      } else {
        closed.textContent = data.closed_text || "Подключение пока закрыто";
      }
    }).catch(function (err) {
      msg(detail(err, "Не удалось загрузить условия подключения"), "bad");
    });
  }

  function loadPage(page, render) {
    // параметр section, а не page: page в этом API — номер страницы списка
    return api("/content?section=" + encodeURIComponent(page)).then(function (data) {
      render(data);
      if (data.diagrams && data.diagrams.length) { loadDiagrams(data.diagrams); }
    }).catch(function (err) {
      if (err.status === 401 || err.status === 403) {
        dropToken();
        msg(detail(err, "Нужно подтвердить статус компании"), "bad");
        go("start");
        return;
      }
      msg(detail(err, "Не удалось открыть страницу"), "bad");
    });
  }

  function fillList(sel, items, raw) {
    var box = $(sel);
    if (!box) { return; }
    if (!items || !items.length) {
      box.innerHTML = "<li>—</li>";
      return;
    }
    box.innerHTML = items.map(function (item) {
      return raw ? item : "<li>" + esc(item) + "</li>";
    }).join("");
  }

  function loadDiagrams(names) {
    var box = $("#hub-figs");
    if (!box) { return; }
    names.forEach(function (name) {
      api("/content/diagram/" + encodeURIComponent(name)).then(function (data) {
        if (!data.svg) { return; }
        var text = String(data.svg);
        /* защита: вставляем только чистый SVG без скриптов и обработчиков */
        if (text.indexOf("<svg") < 0 || text.indexOf("<script") >= 0 ||
            text.indexOf("onload=") >= 0) { return; }
        var fig = document.createElement("figure");
        fig.className = "b2b-fig";
        fig.innerHTML = text + "<figcaption>Схема доступна только компаниям с " +
          "подтверждённым статусом</figcaption>";
        box.appendChild(fig);
      }).catch(function () { /* схема не обязательна для работы страницы */ });
    });
  }

  /* --- страница «Раздел API для бизнеса» -------------------------------- */
  function renderHub(data) {
    $("#hub-lead").textContent = data.lead || "";
    fillList("#hub-give", data.boundary_give || []);
    fillList("#hub-never", data.boundary_never || []);
    $("#hub-flag").textContent = data.aggregate_flag || "";
    fillList("#hub-steps", (data.steps || []).map(function (s) {
      return "<li><b>" + esc(s.title) + "</b>" + esc(s.text) + "</li>";
    }), true);
    $("#hub-figs").innerHTML = "";
  }

  /* --- страница «Документация API» -------------------------------------- */
  function renderDocs(data) {
    $("#docs-lead").textContent = "Состав методов — из кода сервиса: " +
      (data.methods || []).length + " методов, базовый адрес " + (data.base_url || "") + ".";
    $("#docs-auth").textContent = data.auth || "";
    $("#docs-key").textContent = data.key_format || "";
    $("#docs-openapi").textContent = data.openapi || "";
    var box = $("#docs-methods");
    box.innerHTML = (data.methods || []).map(function (m) {
      var badge = m.method === "POST" ? "b2b-badge post" : "b2b-badge";
      return '<article class="b2b-method">' +
        '<div class="b2b-method-head"><span class="' + badge + '">' + esc(m.method) +
        '</span><span class="b2b-path">' + esc(m.path) + "</span></div>" +
        "<h3>" + esc(m.title) + "</h3>" +
        "<p>" + esc(m.text) + "</p>" +
        "<p><b>Параметры:</b> " + esc(m.params) + "</p>" +
        "<p><b>В ответе:</b> " + esc(m.fields) + "</p>" +
        "<p><b>Пример:</b></p><code class=\"b2b-code\">" + esc(m.example) + "</code>" +
        '<p class="b2b-fields">' + esc(m.note) + "</p>" +
        "</article>";
    }).join("");
    var body = $("#docs-errors tbody");
    body.innerHTML = (data.errors || []).map(function (e) {
      return "<tr><td>" + esc(e.code) + '</td><td class="doc-text">' + esc(e.text) +
        '</td><td class="doc-text">' + esc(e.why) + "</td></tr>";
    }).join("");
    var rates = $("#docs-rate-headers tbody");
    rates.innerHTML = (data.rate_headers || []).map(function (h) {
      return "<tr><td><code>" + esc(h.name) + '</code></td><td class="doc-text">' +
        esc(h.text) + "</td></tr>";
    }).join("");
    $("#docs-rate-note").textContent = data.rate_headers_note || "";
    fillList("#docs-rules", data.strict_rules || []);
  }

  /* --- страница «Тарифы и состав» --------------------------------------- */
  function renderTariffs(data) {
    $("#tariffs-lead").textContent = data.lead || "";
    var body = $("#tariffs-table tbody");
    body.innerHTML = (data.items || []).map(function (t) {
      return "<tr><td>" + esc(t.title) + "</td><td>" + num(t.rate_per_hour) +
        "</td><td>" + num(t.monthly_quota) + "</td><td>" + num(t.methods) +
        "</td><td>" + esc(t.price) + "</td></tr>";
    }).join("");
    $("#tariffs-price").textContent = data.price_note || "";
    fillList("#tariffs-included", data.included || []);
    fillList("#tariffs-never", data.never || []);
  }

  /* --- страница «Обсудить подключение» ---------------------------------- */
  function renderContact(data) {
    $("#contact-lead").textContent = data.lead || "";
    var select = $("#app-tariff");
    select.innerHTML = (data.tariffs || []).map(function (t) {
      return '<option value="' + esc(t.code) + '">' + esc(t.title) + "</option>";
    }).join("");
    $("#app-task-hint").textContent = data.task_hint || "";
    $("#app-volume-hint").textContent = data.volume_hint || "";
    state.formOpened = Date.now();
  }

  /* --- кабинет ---------------------------------------------------------- */
  function loadCabinet() {
    return api("/me").then(function (data) {
      applyState(data);
      renderCabinet(data);
    }).catch(function (err) {
      dropToken();
      msg(detail(err, "Сессия истекла — войдите заново"), "bad");
      go("login");
    });
  }

  function applyState(data) {
    state.company = data.company || null;
    state.verified = !!state.company && state.company.status === "verified";
  }

  function renderCabinet(data) {
    var company = data.company || {};
    var box = $("#cab-status");
    box.textContent = company.status_title || company.status || "";
    box.className = "b2b-status " + (company.status === "verified" ? "ok" :
      (company.status === "rejected" || company.status === "blocked" ? "bad" : "wait"));
    $("#cab-reason").textContent = company.reject_reason ||
      (data.key && data.key.issued ? "" : "");
    var defs = [
      ["Компания", fmt(company.company_name)],
      ["ИНН", fmt(company.inn) + (company.kind ? " (" + company.kind + ")" : "")],
      ["Почта", fmt(company.email)],
      ["Контактное лицо", fmt(company.contact_name)],
      ["Регистрация", fmt((company.created_at || "").slice(0, 10))],
      ["Почта подтверждена", company.email_verified_at ? "да" : "нет"],
      ["Риск", company.risk_score === 0 ? "0" : String(company.risk_score)]
    ];
    $("#cab-company").innerHTML = defs.map(function (row) {
      return "<dt>" + esc(row[0]) + "</dt><dd>" + esc(row[1]) + "</dd>";
    }).join("");

    var checks = data.checks || [];
    $("#cab-checks").innerHTML = checks.length ? checks.map(function (c) {
      return '<li><span class="b2b-mark ' + (c.ok ? "ok" : "bad") + '">' +
        (c.ok ? "✓" : "✕") + "</span><span><b>" + esc(c.title) + "</b>" +
        esc(c.detail) + "</span></li>";
    }).join("") : "<li>Проверки ещё не выполнялись</li>";

    var key = data.key || {};
    $("#cab-key").textContent = key.issued
      ? (key.active ? "Ключ " + key.prefix + " активен" : "Ключ " + key.prefix + " отозван")
      : "Ключ ещё не выдан";
    var usage = $("#cab-usage");
    if (key.issued) {
      usage.innerHTML =
        usageBar("Запросов за час", key.used_hour, key.rate_per_hour) +
        usageBar("Запросов за месяц", key.used_month, key.monthly_quota) +
        '<dl class="b2b-defs">' +
        [["Тариф", fmt(key.tariff)],
         ["Последний вызов", fmt(key.last_used_at)]].map(function (row) {
           return "<dt>" + esc(row[0]) + "</dt><dd>" + esc(row[1]) + "</dd>";
         }).join("") + "</dl>";
    } else {
      usage.innerHTML = '<p class="b2b-note">Ключ ещё не выдан — расход лимитов ' +
        "появится после одобрения заявки.</p>";
    }

    var apps = data.applications || [];
    $("#cab-applications").innerHTML = apps.length ? apps.map(function (a) {
      return "<li>" + esc(a.status_title) + " · тариф " + esc(a.tariff) +
        " · " + esc((a.created_at || "").slice(0, 10)) +
        (a.reject_reason ? " — " + esc(a.reject_reason) : "") + "</li>";
    }).join("") : "<li>Заявок пока нет</li>";

    var events = data.events || [];
    $("#cab-events").innerHTML = events.length ? events.slice(0, 10).map(function (e) {
      return "<li>" + esc((e.created_at || "").slice(0, 16).replace("T", " ")) +
        " · " + esc(e.action) + (e.details ? " — " + esc(e.details) : "") + "</li>";
    }).join("") : "<li>Событий пока нет</li>";

    $("#cab-resend").hidden = !!company.email_verified_at;
    $("#cab-go-contact").classList.toggle("hidden", !state.verified);
  }

  /* --- подтверждение почты ---------------------------------------------- */
  function confirmEmail(token) {
    var text = $("#confirm-text");
    $("#confirm-resend").hidden = true;
    if (!token) {
      text.textContent = "Ссылка без токена — откройте письмо и перейдите по " +
        "ссылке из него.";
      return undefined;
    }
    text.textContent = "Проверяем ссылку из письма…";
    return api("/verify-email", { method: "POST", body: { token: token } })
      .then(function (data) {
        if (data.token) { keepToken(data.token); }
        applyState(data);
        if (data.status === "verified") {
          text.textContent = "Корпоративная почта подтверждена, статус компании " +
            "подтверждён — раздел открыт.";
          msg(data.message || "Статус компании подтверждён", "good");
          go("hub");
        } else {
          text.textContent = data.message || "Почта подтверждена, статус " +
            "проверяется";
          msg(data.message || "", data.status === "rejected" ? "bad" : "");
          $("#confirm-resend").hidden = false;
          if (data.token) { go("cabinet"); }
        }
      })
      .catch(function (err) {
        text.textContent = detail(err, "Ссылка недействительна или истекла");
        msg(text.textContent, "bad");
        $("#confirm-resend").hidden = !state.token;
      });
  }

  /* --- формы ------------------------------------------------------------ */
  var pendingReset = "";

  function trapFilled(form) {
    var trap = form.querySelector('input[name="hp"]');
    return !!(trap && trap.value.trim());
  }

  function formMs() {
    var spent = Date.now() - (state.formOpened || Date.now());
    return spent > 0 ? spent : 0;
  }

  function bindRegister() {
    var form = $("#form-register");
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var btn = $("#reg-submit");
      if (trapFilled(form) || formMs() < HONEYPOT_MS) {
        msg("Заявка принята", "good");
        return;
      }
      if (!$("#reg-consent").checked) {
        msg("Нужно согласие на обработку персональных данных", "bad");
        return;
      }
      btn.disabled = true;
      api("/register", {
        method: "POST",
        body: {
          email: $("#reg-email").value,
          password: $("#reg-password").value,
          company_name: $("#reg-name").value,
          inn: $("#reg-inn").value,
          contact_name: $("#reg-contact").value,
          phone: $("#reg-phone").value,
          tariff: $("#reg-tariff").value,
          consent: $("#reg-consent").checked,
          hp: form.querySelector('input[name="hp"]').value,
          form_ms: formMs()
        }
      }).then(function (data) {
        btn.disabled = false;
        msg(data.message || "Регистрация принята", "good");
        form.reset();
        state.formOpened = Date.now();
        go("login");
      }).catch(function (err) {
        btn.disabled = false;
        msg(detail(err, "Регистрация не удалась"), "bad");
      });
    });
  }

  function bindLogin() {
    $("#form-login").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var btn = $("#log-submit");
      btn.disabled = true;
      api("/login", {
        method: "POST",
        body: { email: $("#log-email").value, password: $("#log-password").value }
      }).then(function (data) {
        btn.disabled = false;
        keepToken(data.token);
        applyState(data);
        msg("Вход выполнен", "good");
        go(state.verified ? "hub" : "cabinet");
      }).catch(function (err) {
        btn.disabled = false;
        msg(detail(err, "Неверная почта или пароль"), "bad");
      });
    });

    $("#form-reset").addEventListener("submit", function (ev) {
      ev.preventDefault();
      api("/reset-request", { method: "POST", body: { email: $("#res-email").value } })
        .then(function (data) { msg(data.message || "Ссылка отправлена", "good"); })
        .catch(function (err) { msg(detail(err, "Не удалось отправить ссылку"), "bad"); });
    });

    $("#reset-open").addEventListener("click", function () {
      $("#form-reset").classList.toggle("hidden");
    });

    $("#form-newpass").addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (!pendingReset) {
        msg("Откройте ссылку из письма — токен не передан", "bad");
        return;
      }
      api("/reset-confirm", {
        method: "POST",
        body: { token: pendingReset, password: $("#np-password").value }
      }).then(function (data) {
        keepToken(data.token);
        pendingReset = "";
        msg(data.message || "Пароль обновлён", "good");
        go("cabinet");
        return loadCabinet();
      }).catch(function (err) { msg(detail(err, "Ссылка истекла"), "bad"); });
    });
  }

  function bindApplication() {
    $("#form-contact").addEventListener("submit", function (ev) {
      ev.preventDefault();
      var btn = $("#app-submit");
      btn.disabled = true;
      api("/applications", {
        method: "POST",
        body: {
          tariff: $("#app-tariff").value,
          task: $("#app-task").value,
          expected_volume: $("#app-volume").value
        }
      }).then(function (data) {
        btn.disabled = false;
        var text = data.message || "Заявка отправлена";
        if (data.issued_key) {
          text += " Ключ: " + esc(data.issued_key) + " — " +
            esc(data.key_note || "сохраните его сейчас");
        }
        msg(text, "good");
        go("cabinet");
        return loadCabinet();
      }).catch(function (err) {
        btn.disabled = false;
        msg(detail(err, "Заявка не отправлена"), "bad");
      });
    });
  }

  function bindCommon() {
    $all("[data-go]").forEach(function (btn) {
      btn.addEventListener("click", function () { go(btn.getAttribute("data-go")); });
    });
    $all(".b2b-tab").forEach(function (tab) {
      tab.addEventListener("click", function () { go(tab.getAttribute("data-view")); });
    });
    $("#cab-logout").addEventListener("click", function () {
      dropToken();
      msg("Вы вышли из кабинета");
      go("start");
    });
    $("#cab-resend").addEventListener("click", function () {
      api("/resend-verify", { method: "POST", body: {} })
        .then(function (data) { msg(data.message || "Письмо отправлено", data.sent ? "good" : "bad"); })
        .catch(function (err) { msg(detail(err, "Не удалось отправить письмо"), "bad"); });
    });
    $("#confirm-resend").addEventListener("click", function () {
      api("/resend-verify", { method: "POST", body: {} })
        .then(function (data) { msg(data.message || "Письмо отправлено", data.sent ? "good" : "bad"); })
        .catch(function (err) { msg(detail(err, "Нужно войти в кабинет"), "bad"); });
    });
    window.addEventListener("hashchange", applyRoute);
  }

  /* --- старт ------------------------------------------------------------ */
  function boot() {
    try { state.token = window.localStorage.getItem(TOKEN_KEY) || ""; } catch (e) {
      state.token = "";
    }
    bindRegister();
    bindLogin();
    bindApplication();
    bindCommon();
    state.formOpened = Date.now();

    var start = state.token
      ? api("/me").then(function (data) { applyState(data); }).catch(function () {
        dropToken();
      })
      : Promise.resolve();
    start.then(function () { applyRoute(); });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else { boot(); }
}());
