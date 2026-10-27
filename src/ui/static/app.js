/* TendersParser UI — клиентская логика. Без зависимостей, офлайн. */

// ── Утилиты ─────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);

function toast(msg, type = "") {
  const el = document.createElement("div");
  el.className = "toast " + type;
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity .3s";
    setTimeout(() => el.remove(), 300);
  }, 3200);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const ct = res.headers.get("content-type") || "";
  if (!ct.includes("application/json")) {
    // например, файл (excel) — не парсим
    return res;
  }
  const data = await res.json();
  if (!res.ok && data && data.error) {
    throw new Error(data.error);
  }
  // Ответ 428 «сначала исправьте» несёт список проблем — отдаём его вызывающему
  if (!res.ok && data && data.problems) {
    return { ...data, _status: res.status };
  }
  if (!res.ok) {
    throw new Error("Ошибка " + res.status);
  }
  return data;
}

// ── Диалоги ─────────────────────────────────────────────────────
// Системные confirm()/alert() в окне приложения (WebView2) не показываются:
// нажатие «Удалить» выглядело как «ничего не произошло». Свои модалки.
function ask(text, title = "Подтвердите действие", okText = "Да") {
  return new Promise((resolve) => {
    $("ask-title").textContent = title;
    $("ask-text").textContent = text;
    $("ask-yes").textContent = okText;
    $("ask-modal").classList.remove("hidden");

    const close = (answer) => {
      $("ask-modal").classList.add("hidden");
      $("ask-yes").removeEventListener("click", onYes);
      $("ask-no").removeEventListener("click", onNo);
      resolve(answer);
    };
    const onYes = () => close(true);
    const onNo = () => close(false);
    $("ask-yes").addEventListener("click", onYes);
    $("ask-no").addEventListener("click", onNo);
  });
}

// ── Вкладки ─────────────────────────────────────────────────────
document.querySelectorAll(".nav-item[data-tab]").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

function switchTab(name) {
  document.querySelectorAll(".nav-item[data-tab]").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name)
  );
  document.querySelectorAll(".panel").forEach((p) =>
    p.classList.toggle("active", p.id === "tab-" + name)
  );
  if (name === "dashboard") refreshCounters();
  if (name === "companies") loadCompanies();
  if (name === "keywords") loadKeywords();
  if (name === "settings") loadSettings();
  if (name === "logs") loadLogs();
}

// ── Тосты-состояния ─────────────────────────────────────────────
function setRunStatus(cls, text) {
  const el = $("run-status");
  el.className = "run-status " + cls;
  el.textContent = text;
}

// ── Парсинг ─────────────────────────────────────────────────────
let pollTimer = null;
let startTs = null;
let elapsedTimer = null;

$("btn-parse").addEventListener("click", () => startParse());
$("btn-stop").addEventListener("click", stopParse);

async function startParse(force = false) {
  // Сначала спрашиваем сервер, не помешает ли что-то запуску: пустой список
  // компаний и фраз, выключенные площадки. Раньше в таких
  // случаях парсинг молча отрабатывал вхолостую и заканчивался нулём.
  if (!force) {
    try {
      const pre = await api("/api/preflight");
      if (pre && pre.problems && pre.problems.length) {
        showPreflight(pre.problems);
        return;
      }
    } catch (e) {
      // проверка недоступна — не мешаем запуску
    }
  }

  $("btn-parse").disabled = true;
  $("btn-stop").classList.remove("hidden");
  $("btn-stop").disabled = false;
  $("log-feed").innerHTML = "";
  $("parse-progress").classList.remove("hidden");
  $("result-card").classList.add("hidden");
  $("progress-fill").style.width = "0%";
  $("progress-counter").textContent = "0 / 0";
  $("current-company").textContent = "—";
  setRunStatus("run", "Запуск…");

  startTs = Date.now();
  elapsedTimer = setInterval(() => {
    const s = Math.floor((Date.now() - startTs) / 1000);
    const m = Math.floor(s / 60), sec = s % 60;
    $("run-timer").textContent = (m > 0 ? m + " мин " : "") + sec + " сек";
  }, 1000);

  try {
    const started = await api("/api/parse/start", {
      method: "POST",
      body: JSON.stringify({ force: true }),
    });
    if (started && started.problems && started.problems.length) {
      resetRunControls();
      showPreflight(started.problems);
      return;
    }
    pollTimer = setInterval(pollStatus, 1200);
  } catch (e) {
    resetRunControls();
    setRunStatus("err", "Ошибка: " + e.message);
    toast("Не удалось запустить парсинг: " + e.message, "error");
  }
}

function resetRunControls() {
  clearInterval(elapsedTimer);
  clearInterval(pollTimer);
  pollTimer = null;
  $("btn-parse").disabled = false;
  $("btn-stop").classList.add("hidden");
  setRunStatus("idle", "Готов к запуску");
}

function showPreflight(problems) {
  $("preflight-title").textContent = "Прежде чем запускать";
  $("preflight-list").innerHTML = problems.map((p) => "<li>" + esc(p) + "</li>").join("");
  $("preflight-modal").classList.remove("hidden");
}

$("preflight-fix").addEventListener("click", () => {
  $("preflight-modal").classList.add("hidden");
  switchTab("settings");
});
$("preflight-force").addEventListener("click", () => {
  $("preflight-modal").classList.add("hidden");
  startParse(true);
});

async function stopParse() {
  $("btn-stop").disabled = true;
  setRunStatus("run", "Останавливаю…");
  try {
    await api("/api/parse/stop", { method: "POST" });
    toast("Останавливаю парсинг…", "success");
  } catch (e) {
    $("btn-stop").disabled = false;
    toast("Не удалось остановить: " + e.message, "error");
  }
}

async function pollStatus() {
  try {
    const d = await api("/api/status");
    renderLogFeed(d.progress || []);
    // Прогресс по площадкам: «готово N из M», в «Обрабатываю» — какие идут
    // сейчас (события раннера, main_ui._on_progress).
    const total = d.total_units || 0;
    const done = d.units_done || 0;
    if (total > 0) {
      $("progress-fill").style.width = Math.min(99, Math.round((done / total) * 100)) + "%";
      $("progress-counter").textContent = done + " / " + total;
    }
    const cur = d.current_company || "";
    $("current-company").textContent = cur || (done > 0 ? "…ожидание" : "—");
    // Счётчик растёт по ходу обхода — видно, что программа работает, а не
    // «висит с нулём» до самого конца.
    if (d.found_so_far != null) $("tile-found").textContent = d.found_so_far;

    if (d.running && d.stopping) {
      setRunStatus("run", "Останавливаю…");
    }

    if (!d.running) {
      clearInterval(pollTimer);
      pollTimer = null;
      clearInterval(elapsedTimer);
      $("btn-parse").disabled = false;
      $("btn-stop").classList.add("hidden");
      $("progress-fill").style.width = "100%";

      if (d.stopped) {
        setRunStatus("idle", "Остановлено пользователем");
        toast("Парсинг остановлен", "success");
      } else if (d.error) {
        setRunStatus("err", "Ошибка: " + d.error);
        toast("Парсинг завершился ошибкой", "error");
        // Лоты уже сохранены: книгу можно повторить без нового обхода.
        if (d.data_saved) {
          $("result-card").classList.remove("hidden");
          $("result-count").textContent = d.tenders_count || 0;
          $("result-path").textContent = d.excel_path || "файл не создан";
          renderCompleteness(d.completeness);
        }
      } else {
        const n = d.tenders_count || 0;
        $("tile-found").textContent = n;
        setRunStatus("done", "Готово. Найдено: " + n);
        if (n > 0) {
          $("result-card").classList.remove("hidden");
          $("result-count").textContent = n;
          $("result-path").textContent = d.excel_path || "файл не сохранён";
        }
        renderCompleteness(d.completeness);
        toast("Парсинг завершён, найдено: " + n, "success");
      }
    }
  } catch (e) {
    // сеть моргнула — продолжаем
  }
}

// Полнота сбора: честно говорим, всё ли удалось забрать у площадки.
// Отчёт, собранный не полностью, не должен выглядеть полным.
function renderCompleteness(c) {
  const box = $("completeness");
  if (!box) return;
  if (!c || !c.checked) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");

  if (c.data_complete) {
    $("completeness-line").innerHTML =
      '<span class="ok">✅ Собрано полностью</span> — все ' +
      c.complete +
      " заказчиков и фраз обойдены целиком.";
    $("completeness-list").innerHTML = "";
    return;
  }

  const parts = [];
  if (c.partial) parts.push(c.partial + " обойдено частично");
  if (c.skipped) parts.push(c.skipped + " не обойдено");
  $("completeness-line").innerHTML =
    '<span class="warn">⚠ Данные неполные</span> — ' +
    parts.join(", ") +
    ". Ниже видно, чего не хватает:";

  // Сначала — по площадкам (ТЗ-8): пользователь должен увидеть, КТО недобрал,
  // прежде чем разбираться с отдельными объектами.
  const platformRows = (c.platforms || [])
    .filter((p) => !p.data_complete)
    .map(
      (p) =>
        "<li><b>" +
        esc(p.platform) +
        "</b>: неполных " +
        p.partial +
        ", не обойдено " +
        p.skipped +
        " из " +
        p.units +
        " объектов" +
        (p.reason ? " — " + esc(p.reason) : "") +
        "</li>"
    );

  $("completeness-list").innerHTML =
    platformRows.join("") +
    (c.units || [])
    .map((u) => {
      let detail;
      if (u.status === "skipped") {
        detail = "не обойдено";
      } else if (u.expected_pages != null) {
        detail = "получено страниц " + u.collected_pages + " из " + u.expected_pages;
      } else if (u.expected_lots != null) {
        detail = "собрано " + u.collected + " из " + u.expected_lots;
      } else {
        detail = "собрано " + u.collected;
      }
      return "<li><b>" + esc(u.label) + "</b>: " + esc(detail) + " — " + esc(u.reason) + "</li>";
    })
    .join("");
}

function renderLogFeed(lines) {
  const el = $("log-feed");
  el.innerHTML = lines
    .map((line) => {
      const cls = line.startsWith("✅") || /Готово/.test(line) ? "ok"
        : line.startsWith("❌") ? "err"
        : line.startsWith("⚠") ? "warn" : "";
      return '<div class="' + cls + '">' + esc(line) + "</div>";
    })
    .join("");
  el.scrollTop = el.scrollHeight;
}

// ── Счётчики ───────────────────────────────────────────────────
async function refreshCounters() {
  try {
    const d = await api("/api/status");
    // Догоняющий сбор уже мог начаться при открытии программы.
    if (d.running && !pollTimer) {
      $("btn-parse").disabled = true;
      $("btn-stop").classList.remove("hidden");
      $("btn-stop").disabled = false;
      $("parse-progress").classList.remove("hidden");
      $("result-card").classList.add("hidden");
      setRunStatus("run", "Сбор выполняется…");
      pollTimer = setInterval(pollStatus, 1200);
    }
    $("tile-companies").textContent = (d.counters && d.counters.companies) ?? "—";
    $("tile-keywords").textContent = (d.counters && d.counters.keywords_enabled) ?? "—";
    $("tile-today").textContent = (d.counters && d.counters.tenders_today) ?? "—";
    if (d.tenders_count != null) $("tile-found").textContent = d.tenders_count;
  } catch (e) {}
}

// ── Компании ────────────────────────────────────────────────────
let companies = [];

async function loadCompanies() {
  try {
    const d = await api("/api/companies");
    companies = d.companies || [];
    renderCompanies();
  } catch (e) {
    toast("Не удалось загрузить компании: " + e.message, "error");
  }
}

function renderCompanies() {
  const q = ($("company-filter").value || "").toLowerCase();
  const list = companies.filter((c) => c.toLowerCase().includes(q));
  $("company-count").textContent = "(" + companies.length + ")";
  const el = $("company-list");
  if (!list.length) {
    el.innerHTML = '<div class="empty">Список пуст</div>';
    return;
  }
  el.innerHTML = list
    .map(
      (c, i) =>
        '<div class="list-item"><span class="idx">' +
        (i + 1) +
        '.</span><span class="txt">' +
        esc(c) +
        '</span><button class="icon-btn" data-del="' +
        escAttr(c) +
        '" title="Удалить">🗑</button></div>'
    )
    .join("");
  el.querySelectorAll("[data-del]").forEach((b) =>
    b.addEventListener("click", () => delCompany(b.dataset.del))
  );
}

$("company-filter").addEventListener("input", renderCompanies);

async function addCompany() {
  const inp = $("company-input");
  const v = inp.value.trim();
  if (!v) return;
  if (companies.some((c) => c.toLowerCase() === v.toLowerCase())) {
    toast("Такая компания уже есть", "warn");
    return;
  }
  companies.push(v);
  inp.value = "";
  await saveCompanies();
  renderCompanies();
  toast("Компания добавлена", "success");
}
$("company-add").addEventListener("click", addCompany);
$("company-input").addEventListener("keypress", (e) => {
  if (e.key === "Enter") addCompany();
});

async function delCompany(name) {
  if (!(await ask('Удалить компанию «' + name + '» из списка?', "Удаление", "Удалить")))
    return;
  companies = companies.filter((c) => c !== name);
  await saveCompanies();
  renderCompanies();
  toast("Удалено", "success");
}

async function saveCompanies() {
  await api("/api/companies", {
    method: "POST",
    body: JSON.stringify({ companies }),
  });
}

// ── Ключевые слова ──────────────────────────────────────────────
let keywords = [];

async function loadKeywords() {
  try {
    const d = await api("/api/keywords");
    keywords = d.keywords || [];
    $("kw-filter").checked = !!d.keyword_filter_enabled;
    renderKeywords();
  } catch (e) {
    toast("Не удалось загрузить фразы: " + e.message, "error");
  }
}

function renderKeywords() {
  $("kw-count").textContent = "(" + keywords.length + ")";
  const el = $("kw-list");
  if (!keywords.length) {
    el.innerHTML = '<div class="empty">Список пуст</div>';
    return;
  }
  el.innerHTML = keywords
    .map(
      (kw, i) =>
        '<div class="list-item"><span class="idx">' +
        (i + 1) +
        '.</span><span class="txt">' +
        esc(kw.phrase) +
        '</span><label class="check small"><input type="checkbox" data-toggle="' +
        i +
        '" ' +
        (kw.enabled ? "checked" : "") +
        '><span></span></label><button class="icon-btn" data-del="' +
        i +
        '" title="Удалить">🗑</button></div>'
    )
    .join("");
  el.querySelectorAll("[data-toggle]").forEach((cb) =>
    cb.addEventListener("change", () => {
      keywords[+cb.dataset.toggle].enabled = cb.checked;
      saveKeywords();
    })
  );
  el.querySelectorAll("[data-del]").forEach((b) =>
    b.addEventListener("click", () => delKeyword(+b.dataset.del))
  );
}

async function addKeyword() {
  const inp = $("kw-input");
  const v = inp.value.trim();
  if (!v) return;
  if (keywords.some((k) => k.phrase.toLowerCase() === v.toLowerCase())) {
    toast("Такая фраза уже есть", "warn");
    return;
  }
  keywords.push({ phrase: v, enabled: true });
  inp.value = "";
  await saveKeywords();
  renderKeywords();
  toast("Фраза добавлена", "success");
}
$("kw-add").addEventListener("click", addKeyword);
$("kw-input").addEventListener("keypress", (e) => {
  if (e.key === "Enter") addKeyword();
});

async function delKeyword(i) {
  if (
    !(await ask('Удалить фразу «' + keywords[i].phrase + '»?', "Удаление", "Удалить"))
  )
    return;
  keywords.splice(i, 1);
  await saveKeywords();
  renderKeywords();
  toast("Удалено", "success");
}

$("kw-filter").addEventListener("change", saveKeywords);

async function saveKeywords() {
  await api("/api/keywords", {
    method: "POST",
    body: JSON.stringify({
      keyword_filter_enabled: $("kw-filter").checked,
      keywords,
    }),
  });
}

// ── Настройки ───────────────────────────────────────────────────
// Последние прочитанные настройки: лента спрашивает у них имя менеджера,
// а ходить в /api/settings на каждое нажатие «Беру» незачем.
let SETTINGS_CACHE = null;

async function loadSettings() {
  try {
    const d = await api("/api/settings");
    SETTINGS_CACHE = d;
    $("set-manager").value = d.manager_name || "";
    $("set-days").value = d.days_back ?? 1;
    $("set-min").value = d.min_price ?? 0;
    $("set-max").value = d.max_price ?? 0;
    $("set-noprice").checked = !!d.include_no_price;
    $("set-tp").checked = !!d.parse_tender_pro;
    $("set-gpb").checked = !!d.parse_gpb;
    $("set-zakupki").checked = !!d.parse_zakupki;
    $("set-tektorg").checked = !!d.parse_tektorg;
    $("set-roseltorg").checked = !!d.parse_roseltorg;
    $("set-bidzaar").checked = !!d.parse_bidzaar;
    $("set-headless").checked = !d.browser_headless;
  } catch (e) {
    toast("Не удалось загрузить настройки: " + e.message, "error");
  }
}

$("settings-save").addEventListener("click", async () => {
  const body = {
    manager_name: $("set-manager").value.trim(),
    days_back: intOf($("set-days").value, 1),
    min_price: numOf($("set-min").value, 0),
    max_price: numOf($("set-max").value, 0),
    include_no_price: $("set-noprice").checked,
    parse_tender_pro: $("set-tp").checked,
    parse_gpb: $("set-gpb").checked,
    parse_zakupki: $("set-zakupki").checked,
    parse_tektorg: $("set-tektorg").checked,
    parse_roseltorg: $("set-roseltorg").checked,
    parse_bidzaar: $("set-bidzaar").checked,
    browser_headless: !$("set-headless").checked,
  };
  try {
    await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    toast("Настройки сохранены", "success");
  } catch (e) {
    toast("Ошибка: " + e.message, "error");
  }
});

// ── Результаты на диске ─────────────────────────────────────────
$("excel-open").addEventListener("click", async () => {
  try {
    await api("/api/results/open", { method: "POST" });
  } catch (e) {
    toast("Не удалось открыть папку: " + e.message, "error");
  }
});

$("excel-save").addEventListener("click", async () => {
  const btn = $("excel-save");
  btn.disabled = true;
  try {
    const d = await api("/api/excel/save", { method: "POST" });
    if (d && d.ok) {
      $("result-path").textContent = d.path;
      toast("Файл сохранён", "success");
    }
  } catch (e) {
    toast("Не удалось сохранить: " + e.message, "error");
  } finally {
    btn.disabled = false;
  }
});

// ── Диагностика ──────────────────────────────────────────────
$("diag-save").addEventListener("click", async () => {
  const btn = $("diag-save");
  btn.disabled = true;
  btn.textContent = "🩺 Собираю…";
  try {
    const d = await api("/api/diagnostics/save", { method: "POST" });
    if (d && d.ok) toast("Архив сохранён: " + d.path, "success");
  } catch (e) {
    toast("Не удалось собрать диагностику: " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "🩺 Собрать диагностику";
  }
});

// ── Логи ────────────────────────────────────────────────────────
let rawLog = "";
$("log-refresh").addEventListener("click", loadLogs);
$("log-filter").addEventListener("input", () => applyLogFilter($("log-filter").value));

async function loadLogs() {
  try {
    const d = await api("/api/logs?lines=500");
    rawLog = d.lines || "";
    applyLogFilter($("log-filter").value);
  } catch (e) {
    toast("Не удалось загрузить логи: " + e.message, "error");
  }
}

function applyLogFilter(q) {
  const view = $("log-view");
  const lines = rawLog.split("\n");
  const ql = q.toLowerCase();
  const out = ql ? lines.filter((l) => l.toLowerCase().includes(ql)) : lines;
  view.textContent = out.join("\n");
  if ($("log-autoscroll").checked) view.scrollTop = view.scrollHeight;
}

// ── Выход ───────────────────────────────────────────────────────
$("btn-shutdown").addEventListener("click", () => $("shutdown-confirm").classList.remove("hidden"));
$("shutdown-cancel").addEventListener("click", () => $("shutdown-confirm").classList.add("hidden"));
$("shutdown-ok").addEventListener("click", async () => {
  try {
    await api("/api/shutdown", { method: "POST" });
  } catch (e) {}
  toast("Программа закрывается…", "warn");
});

// ── Хелперы ─────────────────────────────────────────────────────
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function escAttr(s) {
  return esc(s).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
// Метка времени из базы («2026-10-16T10:05:00») — как привык человек:
// «16.10.2026 10:05». Непохожее на ISO показываем как есть.
function fmtStamp(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/.exec(String(s || ""));
  return m ? `${m[3]}.${m[2]}.${m[1]} ${m[4]}:${m[5]}` : String(s || "");
}
function intOf(v, dflt) { const n = parseInt(v, 10); return isNaN(n) ? dflt : n; }
function numOf(v, dflt) { const n = parseFloat(v); return isNaN(n) ? dflt : n; }

// ── Первый запуск ───────────────────────────────────────────────
// Если программа ещё не готова к работе (пуст список компаний и фраз,
// выключены все площадки), не ждём, пока пользователь нажмёт «Запустить» и получит ноль —
// говорим об этом сразу и ведём в настройки.
async function checkFirstRun() {
  try {
    const pre = await api("/api/preflight");
    if (pre && pre.problems && pre.problems.length) {
      $("preflight-list").innerHTML = pre.problems
        .map((p) => "<li>" + esc(p) + "</li>")
        .join("");
      $("preflight-title").textContent = "Программу нужно донастроить";
      $("preflight-modal").classList.remove("hidden");
    }
  } catch (e) {
    // сервер ещё не готов — не мешаем
  }
}

// ── Полнота прошлого прогона ────────────────────────────────────
// После перезапуска программы состояние текущей сессии пусто, и список лотов
// снова выглядит полным. Показываем отчёт последнего прогона с диска, иначе
// неполный сбор молча выдаётся за полный.
async function showLastRun() {
  try {
    const d = await api("/api/last-run");
    if (!d || !d.record) return;
    const box = $("completeness");
    if (!box) return;
    box.classList.remove("hidden");
    const метка = d.data_complete ? '<span class="ok">✅</span> ' : '<span class="warn">⚠</span> ';
    $("completeness-line").innerHTML = метка + esc(d.text);
    $("completeness-list").innerHTML = (d.record.incomplete_units || [])
      .slice(0, 10)
      .map((u) => "<li>" + esc(u.label) + " — " + esc(u.reason_text || "") + "</li>")
      .join("");
  } catch (e) {
    // журнала нет или сервер не готов — молча, это не ошибка
  }
}

// ── Старт ───────────────────────────────────────────────────────
refreshCounters();
checkFirstRun();
showLastRun();

/* ── Лента лотов (фича 002, T017) ──────────────────────────────────────────
 * Читает единую базу. Оценку показывает готовой — её посчитал профиль при
 * сборе, поэтому в ленте, в карточке и в Excel она одна и та же. Корзины
 * («горит», «истёк») сервер считает на сегодняшнюю дату.
 */
const FEED_STATE = { loaded: false, topic: null, data: null };

function money(v) {
  if (typeof v !== "number" || v <= 0) return "без цены";
  return v.toLocaleString("ru-RU", { maximumFractionDigits: 0 }) + " ₽";
}

function lotRow(lot) {
  const left = lot.days_left;
  const days =
    left === null || left === undefined
      ? ""
      : left < 0
        ? `просрочен на ${-left} дн.`
        : left === 0
          ? "сегодня последний день"
          : `${left} дн.`;
  // Предоплата — весомый плюс: помечаем прямо в ленте, чтобы её было видно
  // без открытия карточки. «Условие не найдено» не показываем как «нет»:
  // молчание текста лота не означает отсутствия аванса.
  const prepay =
    lot.prepayment === "yes"
      ? '<span class="prepay-badge">предоплата</span>'
      : "";

  // «Новое» — то, что принёс последний сбор. Утром это главный вопрос:
  // лот недельной давности и найденный ночью выглядели одинаково.
  const fresh = lot.is_new ? '<span class="new-badge">новое</span>' : "";

  // Ссылка — на первоисточник (main_href). У лота без ссылки (у ТЭК-Торга и
  // Bidzaar таких сотни) раньше стоял href="#", и «открыть в новой вкладке»
  // вело на эту же страницу; теперь — номер, по которому искать на площадке.
  const href = lot.main_href
    ? `href="${escAttr(lot.main_href)}" target="_blank" rel="noopener"`
    : 'role="button" tabindex="0" style="cursor:pointer"';
  const searchNo = lot.main_href
    ? ""
    : `<div class="muted">№ ${esc(lot.tender_no || lot.lot_id || "")} — искать на площадке</div>`;

  const el = document.createElement("div");
  el.className = lot.is_new ? "feed-lot feed-lot-new" : "feed-lot";
  el.innerHTML = `
    <div class="feed-lot-score">${lot.score ?? 0}</div>
    <div class="feed-lot-main">
      <a class="feed-lot-name" ${href}>${
        (lot.name || "Без названия").replace(/</g, "&lt;")
      }</a>${fresh}
      <div class="feed-lot-company">${(lot.company || "").replace(/</g, "&lt;")}${
        lot.topic ? `<span class="topic-badge">${lot.topic}</span>` : ""
      }</div>
      <div class="feed-lot-why">${(lot.score_explain || "").replace(/</g, "&lt;")}${prepay}</div>
    </div>
    <div class="feed-lot-side">
      <div>${money(lot.sum)}</div>
      <div class="muted">${lot.main_platform || lot.platform || ""}</div>
      <div class="muted">${lot.date_deadline || ""} ${days ? "· " + days : ""}</div>
      ${searchNo}
    </div>`;
  return el;
}

function renderTopics(data) {
  const box = $("feed-topics");
  if (!box) return;
  box.innerHTML = "";
  const topics = data.topics || [];
  if (!topics.length) return;

  const all = document.createElement("button");
  all.className = "topic-chip" + (FEED_STATE.topic ? "" : " on");
  all.innerHTML = `Все темы<span class="n">${data.total}</span>`;
  all.addEventListener("click", () => {
    FEED_STATE.topic = null;
    renderFeed(FEED_STATE.data);
  });
  box.appendChild(all);

  topics.forEach((t) => {
    const b = document.createElement("button");
    b.className = "topic-chip" + (FEED_STATE.topic === t.id ? " on" : "");
    b.innerHTML = `${t.title}<span class="n">${t.lots}</span>`;
    b.addEventListener("click", () => {
      // Повторный клик по выбранной теме снимает фильтр — так проще
      // вернуться к общему списку, не целясь в «Все темы».
      FEED_STATE.topic = FEED_STATE.topic === t.id ? null : t.id;
      renderFeed(FEED_STATE.data);
    });
    box.appendChild(b);
  });
}

// ── Плашка «что принесла ночь» ──────────────────────────────────────────────
//
// Сценарий: человек приходит в девять утра, открывает окно и должен без
// единого клика понять, состоялся ли ночной сбор и что он принёс. Раньше это
// была строка мелким серым текстом рядом с кнопками — её не замечали.
// Молчание тут хуже плохой новости: если сбор не прошёл (ПК был выключен,
// сессия площадки протухла), плашка говорит об этом прямо и предлагает
// собрать сейчас.
function renderFresh(data) {
  const box = $("feed-fresh");
  if (!box) return;
  const run = data.last_run;
  const fresh = data.fresh || {};

  if (!run) {
    box.className = "feed-fresh feed-fresh-warn";
    box.innerHTML =
      `<div class="feed-fresh-main">Сбор ещё не запускался — лента пуста не потому, что тендеров нет.</div>` +
      `<button class="btn" id="btn-fresh-collect">Собрать сейчас</button>`;
    box.hidden = false;
  } else {
    // ISO «2026-09-01T03:30» читается человеком как «01.09 в 03:30»,
    // а не «09-01 03:30»: день впереди месяца — как во всех наших формах.
    const raw = String(run.finished_at || run.started_at || "");
    const when = raw.length >= 16
      ? `${raw.slice(8, 10)}.${raw.slice(5, 7)} в ${raw.slice(11, 16)}`
      : raw;
    const failed = run.outcome !== "ok";
    const partial = run.outcome === "ok" && !run.data_complete;
    const parts = [];
    if (fresh.total) {
      parts.push(`нашлось ${fresh.total} ${plural(fresh.total, "лот", "лота", "лотов")}`);
      if (fresh.core) parts.push(`${fresh.core} по нашему профилю`);
      if (fresh.urgent) parts.push(`${fresh.urgent} горит`);
    } else {
      parts.push("новых лотов нет");
    }
    const headline = failed
      ? `Последний сбор ${when} не довёлся до конца — данные неполные`
      : partial
        ? `Сбор ${when}: ⚠ собрано не всё`
        : `Сбор ${when}: ${parts.join(", ")}`;
    const detail = failed || partial ? run.summary || "" : failed ? "" : parts.join(", ");

    box.className = "feed-fresh" + (failed || partial ? " feed-fresh-warn" : "");
    box.innerHTML =
      `<div class="feed-fresh-main">${headline}</div>` +
      (detail && (failed || partial) ? `<div class="feed-fresh-note">${detail}</div>` : "") +
      `<div class="feed-fresh-actions">` +
      (failed || partial ? `<button class="btn" id="btn-fresh-collect">Собрать сейчас</button>` : "") +
      `<button class="btn btn-ghost" id="btn-fresh-folder">Папка с выгрузкой</button>` +
      `</div>`;
    box.hidden = false;
  }

  const collect = $("btn-fresh-collect");
  if (collect) collect.addEventListener("click", () => startParse());
  const folder = $("btn-fresh-folder");
  if (folder) {
    folder.addEventListener("click", async () => {
      await fetch("/api/results/open", { method: "POST" });
    });
  }
}

function plural(n, one, few, many) {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

function renderFeed(data) {
  FEED_STATE.data = data;
  const body = $("feed-body");
  body.innerHTML = "";
  renderFresh(data);
  renderTopics(data);
  // Состояние сбора рядом со счётчиками: молчаливая лента выглядит полной,
  // даже когда данные обрезаны (NFR-002).
  const run = data.last_run;
  let state = "";
  if (!run) {
    state = " · сбор ещё не запускался";
  } else {
    const when = (run.finished_at || "").replace("T", " ").slice(0, 16);
    if (run.outcome === "ok" && run.data_complete) {
      state = ` · собрано ${when}, данные полные`;
    } else if (run.outcome === "ok") {
      state = ` · собрано ${when}, ⚠ данные неполные`;
    } else if (run.outcome === "stopped") {
      state = ` · прогон ${when} остановлен, ⚠ данные неполные`;
    } else {
      state = ` · прогон ${when} завершился ошибкой`;
    }
  }
  const parts = [`${data.total} ждут разбора`];
  if (data.in_work) parts.push(`${data.in_work} в работе`);
  if (data.rejected) parts.push(`${data.rejected} отклонено`);
  $("feed-summary").textContent = parts.join(" · ") + state;
  $("feed-summary").title = (run && run.summary) || "";

  data.titles.forEach((t) => {
    const allLots = data.buckets[t.key] || [];
    // Фильтр по теме применяется к показанным лотам; счётчик корзины при этом
    // показывает, сколько в ней всего — иначе непонятно, что скрыто фильтром.
    const lots = FEED_STATE.topic
      ? allLots.filter((lot) => (lot.topic_id || "_none") === FEED_STATE.topic)
      : allLots;
    const count = FEED_STATE.topic ? lots.length : data.counts[t.key] || 0;
    if (FEED_STATE.topic && !lots.length) return;
    const box = document.createElement("div");
    box.className = "feed-bucket";
    // Свёрнутые по умолчанию корзины (ТЗ-5): сомнительное и отсеянное
    // копится, но не вытесняет рабочий поток из первого экрана.
    if (t.collapsed) box.classList.add("collapsed");
    const head = document.createElement("div");
    head.className = "feed-bucket-head" + (t.collapsed ? " togglable" : "");
    head.innerHTML =
      (t.collapsed ? '<span class="feed-bucket-arrow">▶</span>' : "") +
      `<span class="feed-bucket-title">${t.title}</span>` +
      `<span class="feed-bucket-count">${count}</span>` +
      `<span class="muted">${t.hint}</span>`;
    if (t.collapsed) {
      head.addEventListener("click", () => box.classList.toggle("collapsed"));
    }
    box.appendChild(head);
    if (!lots.length) {
      const empty = document.createElement("div");
      empty.className = "muted feed-empty";
      empty.textContent = "пусто";
      box.appendChild(empty);
    } else {
      lots.forEach((lot) => {
        const row = lotRow(lot);
        attachLotActions(row, lot);
        box.appendChild(row);
      });
      if (count > lots.length) {
        const more = document.createElement("div");
        more.className = "muted feed-empty";
        more.textContent = `показано ${lots.length} из ${count}`;
        box.appendChild(more);
      }
    }
    body.appendChild(box);
  });
}

async function loadFeed() {
  $("feed-summary").textContent = "загрузка…";
  try {
    const r = await fetch("/api/feed");
    const data = await r.json();
    if (data.error) {
      $("feed-summary").textContent = data.error;
      return;
    }
    renderFeed(data);
    FEED_STATE.loaded = true;
  } catch (e) {
    $("feed-summary").textContent = "не удалось загрузить ленту: " + e;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const refresh = $("btn-feed-refresh");
  if (refresh) refresh.addEventListener("click", loadFeed);
  const rescore = $("btn-feed-rescore");
  if (rescore)
    rescore.addEventListener("click", async () => {
      rescore.disabled = true;
      $("feed-summary").textContent = "пересчитываю оценки…";
      try {
        const r = await fetch("/api/rescore", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ only_missing: false }),
        });
        const res = await r.json();
        $("feed-summary").textContent = res.error
          ? res.error
          : `пересчитано лотов: ${res.updated}`;
        if (!res.error) await loadFeed();
      } finally {
        rescore.disabled = false;
      }
    });
  loadFeed();
});

/* ── Работа с лотом: «Беру» и «Мимо» (T018, T019) ──────────────────────────
 * Кнопки прямо в ленте: решение принимается там, где лот виден, без
 * перехода в отдельный экран. Причина отказа обязательна — без неё мы не
 * узнаем, где промахиваемся, поэтому пустую программа не примет.
 */
async function currentManager() {
  // Имя берём из настроек программы: реестр требует, чтобы у каждого решения
  // был автор, а зашитое в коде имя подписывало бы чужие решения именем
  // владельца машины разработчика. localStorage остаётся запасным вариантом
  // для тех, кто уже вводил имя до появления настройки.
  const fromSettings = (SETTINGS_CACHE && SETTINGS_CACHE.manager_name) || "";
  const known = fromSettings.trim() || localStorage.getItem("tw_manager") || "";
  if (known) return known;
  // Реестр не принимает решение без автора. Спрашиваем один раз и запоминаем
  // на этой машине — своей модалкой, потому что window.prompt в окне WebView2
  // не показывается и выглядит как «кнопка не работает». Постоянное место
  // для имени — «Настройки → Ваше имя».
  const v = await openStepForm({
    title: "Ваше имя",
    note: "Этим именем подписываются решения в реестре. Постоянное место для него — «Настройки → Ваше имя».",
    fields: [{ name: "manager", label: "Как вас звать?", type: "text", required: true }],
    submitText: "Продолжить",
  });
  if (v && v.manager) {
    localStorage.setItem("tw_manager", v.manager);
    return v.manager;
  }
  return "";
}

async function lotAction(lotKey, action, body) {
  const r = await fetch(`/api/lot/${encodeURIComponent(lotKey)}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || "не получилось");
  return data;
}


/** Убрать разобранный лот из ленты и поправить счётчик его корзины. */
function removeLotRow(row, what) {
  const bucket = row.closest(".feed-bucket");
  row.style.opacity = "0.4";
  row.querySelector(".feed-lot-main").insertAdjacentHTML(
    "beforeend",
    `<div class="muted feed-lot-done">${what} — ушёл в реестр</div>`
  );
  setTimeout(() => {
    row.remove();
    if (bucket) {
      const counter = bucket.querySelector(".feed-bucket-count");
      if (counter) {
        const left = Math.max(0, Number(counter.textContent) - 1);
        counter.textContent = String(left);
      }
      if (!bucket.querySelector(".feed-lot")) {
        bucket.insertAdjacentHTML(
          "beforeend",
          '<div class="muted feed-empty">пусто</div>'
        );
      }
    }
  }, 700);
}

/** Форма «почему мимо» — общая для ленты, карточки и реестра. */
function askSkipReason(lotLabel) {
  return openStepForm({
    title: "Почему лот мимо?",
    lot: lotLabel,
    note: "Без причины не учимся на отказах — реестр пустую причину не примет.",
    fields: [{ name: "reason", label: "Причина отказа", type: "textarea", required: true }],
    submitText: "Мимо",
  });
}

function attachLotActions(el, lot) {
  // Название открывает карточку, а не площадку: сначала решение, потом уход
  // на сайт. Ссылка на площадку есть внутри карточки.
  const name = el.querySelector(".feed-lot-name");
  if (name) {
    name.addEventListener("click", (e) => {
      e.preventDefault();
      openLotCard(lot.lot_key);
    });
  }

  const box = document.createElement("div");
  box.className = "feed-lot-actions";

  const take = document.createElement("button");
  take.className = "btn-mini take";
  take.textContent = "Беру";
  take.addEventListener("click", async () => {
    take.disabled = true;
    try {
      const manager = await currentManager();
      if (!manager) {
        take.disabled = false;
        return;
      }
      await lotAction(lot.lot_key, "take", { manager });
      // Решение принято — лот уходит из ленты в реестр. Оставлять его здесь
      // значит заставлять разбирать одно и то же по второму разу.
      removeLotRow(el, "взят в работу");
    } catch (e) {
      take.disabled = false;
      // Ошибка регламента приходит словами; тост вместо alert — системные
      // диалоги в WebView2 не показываются.
      toast(e.message, "error");
    }
  });

  const skip = document.createElement("button");
  skip.className = "btn-mini skip";
  skip.textContent = "Мимо";
  skip.addEventListener("click", async () => {
    const manager = await currentManager();
    if (!manager) return;
    const v = await askSkipReason(`${lot.name || ""} · ${lot.company || ""}`);
    if (!v) return;
    skip.disabled = true;
    try {
      await lotAction(lot.lot_key, "skip", { manager, reason: v.reason });
      removeLotRow(el, "отклонён");
    } catch (e) {
      skip.disabled = false;
      toast(e.message, "error");
    }
  });

  box.appendChild(take);
  box.appendChild(skip);
  el.querySelector(".feed-lot-main").appendChild(box);
}

/* ── Реестр заявок ─────────────────────────────────────────────────────────
 * ТЗ-9: реестр доводит лот до «подался». В каждой строке видно, где заявка
 * стоит (шаг словами и время последнего перехода), ровно одна кнопка
 * следующего шага (карту присылает сервер из ALLOWED_TRANSITIONS —
 * перескок через шаг невозможен даже кнопкой), «Мимо» с любого шага и
 * ссылка на площадку-первоисточник.
 */
const REGISTRY_STATE = { titles: {}, nextSteps: {} };

function resultTitle(row) {
  if (row.result === "won") return "выиграли";
  if (row.result === "lost") return "проиграли";
  return row.result || "";
}

function registryRow(row) {
  const titles = REGISTRY_STATE.titles;
  const next = REGISTRY_STATE.nextSteps[row.status];
  const el = document.createElement("div");
  el.className = "reg-app";
  el.dataset.key = row.lot_key;

  // Где заявка стоит. Без времени последнего перехода восемь строк
  // «взят в работу» не отвечают, живы ли они вообще.
  const step = document.createElement("div");
  step.className = "reg-app-step";
  step.innerHTML =
    `<span class="reg-status">${esc(titles[row.status] || row.status)}</span>` +
    (row.updated_at ? `<div class="reg-app-when">обновлён ${esc(fmtStamp(row.updated_at))}</div>` : "");

  // Контекст шага: то, что важно знать именно на нём.
  const bits = [];
  if (row.manager) bits.push("ведёт <b>" + esc(row.manager) + "</b>");
  if (row.reject_reason) bits.push("причина: " + esc(row.reject_reason));
  if (row.bid_price) bits.push("цена подачи: <b>" + esc(money(row.bid_price)) + "</b>");
  if (row.submission_no) bits.push("заявка <b>" + esc(row.submission_no) + "</b>");
  if (row.result)
    bits.push(
      "итог: <b>" + esc(resultTitle(row)) + "</b>" +
      (row.deviation != null ? ` (отклонение ${row.deviation}%)` : "")
    );
  if (row.contact) bits.push("контакт: " + esc(row.contact));
  if (row.next_touch) bits.push("касание: " + esc(row.next_touch));

  const main = document.createElement("div");
  main.innerHTML =
    `<div class="reg-app-name">${esc(row.name || "Без названия")}</div>` +
    `<div class="reg-app-company">${esc(row.company || "")}` +
    (row.date_deadline ? " · до " + esc(row.date_deadline) : "") +
    `</div>` +
    (bits.length ? `<div class="reg-app-bits">${bits.join(" · ")}</div>` : "");

  const actions = document.createElement("div");
  actions.className = "reg-app-actions";
  const rowBtns = document.createElement("div");
  rowBtns.className = "row";
  if (next) {
    const b = document.createElement("button");
    b.className = "btn-mini take";
    b.textContent = next.title;
    b.addEventListener("click", () => openLotStep(row, next.route));
    rowBtns.appendChild(b);
  }
  // «Мимо» — с любого шага, кроме отказа: отказ уже финал.
  if (row.status !== "REJECTED") {
    const s = document.createElement("button");
    s.className = "btn-mini skip";
    s.textContent = "Мимо";
    s.addEventListener("click", () => openRegistrySkip(row));
    rowBtns.appendChild(s);
  }
  actions.appendChild(rowBtns);
  // Ссылка — на первоисточник (main_href): заявку подают там, агрегатор
  // дал бы только выжимку без документации.
  if (row.main_href) {
    const a = document.createElement("a");
    a.className = "reg-link";
    a.href = row.main_href;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "Открыть на площадке →";
    actions.appendChild(a);
  }

  el.appendChild(step);
  el.appendChild(main);
  el.appendChild(actions);
  return el;
}

function replaceRegistryRow(row) {
  // Ответ шага приходит обновлённой заявкой в том же виде, что и
  // /api/registry, — строка перерисовывается без второго запроса (ТЗ-9).
  const body = $("reg-body");
  for (const el of Array.from(body.children)) {
    if (el.dataset && el.dataset.key === row.lot_key) {
      body.replaceChild(registryRow(row), el);
      return;
    }
  }
  body.appendChild(registryRow(row));
}

function renderRegistry(data) {
  REGISTRY_STATE.titles = data.titles || {};
  REGISTRY_STATE.nextSteps = data.next_steps || {};

  const summary = data.stats || {};
  $("reg-summary").textContent =
    `в работе: ${summary.in_work ?? 0} · отказов: ${summary.rejected ?? 0}`;

  const red = $("reg-red");
  red.innerHTML = "";
  const zones = data.red_zone || {};
  const hasRed = Object.values(zones).some((rows) => rows && rows.length);
  if (hasRed) {
    Object.entries(zones).forEach(([title, rows]) => {
      if (!rows || !rows.length) return;
      const group = document.createElement("div");
      group.className = "reg-red-group";
      group.innerHTML = `<div class="reg-red-title">${title} — ${rows.length}</div>`;
      rows.forEach((row) => {
        const line = document.createElement("div");
        line.className = "muted reg-red-row";
        // why — человеческая формулировка случая («подача через 2 дня,
        // заявка не отправлена»): она и есть главное в строке.
        line.textContent =
          `${row.name || ""} · ${row.company || ""} · до ${row.date_deadline || "?"}` +
          (row.why ? ` — ${row.why}` : "");
        group.appendChild(line);
      });
      red.appendChild(group);
    });
  }

  const body = $("reg-body");
  body.innerHTML = "";
  const items = data.items || [];
  if (!items.length) {
    body.innerHTML =
      '<div class="muted">пока ничего не взято — лоты берутся в «Ленте» кнопкой «Беру»</div>';
    return;
  }
  items.forEach((row) => body.appendChild(registryRow(row)));
}

async function loadRegistry() {
  $("reg-summary").textContent = "загрузка…";
  try {
    const r = await fetch("/api/registry");
    const data = await r.json();
    if (data.error) {
      $("reg-summary").textContent = data.error;
      return;
    }
    renderRegistry(data);
  } catch (e) {
    $("reg-summary").textContent = "не удалось загрузить реестр: " + e;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const btn = $("btn-reg-refresh");
  if (btn) btn.addEventListener("click", loadRegistry);
  document.querySelectorAll('.nav-item[data-tab="registry"]').forEach((b) =>
    b.addEventListener("click", loadRegistry)
  );
});

/* ── Форма шага воронки (ТЗ-9) ─────────────────────────────────────────────
 * Одна модалка на все шаги, поля подставляются по маршруту. Своя, а не
 * системный prompt/confirm: в окне WebView2 системные диалоги не
 * показываются — нажатие кнопки выглядело бы как «ничего не произошло».
 * Кнопка следующего шага приходит с сервера (next_steps из
 * ALLOWED_TRANSITIONS), поэтому интерфейс не может предложить перескок
 * через шаг: реестр его не примет, и кнопки такой не будет.
 */
function whoField(label, name = "who") {
  // Имя подставляем из настроек (manager_name): у каждого шага должен быть
  // автор. Пустое поле человек допишет руками — реестр без имени не примет.
  const known =
    ((SETTINGS_CACHE && SETTINGS_CACHE.manager_name) || "").trim() ||
    localStorage.getItem("tw_manager") ||
    "";
  return { name, label, type: "text", required: true, value: known };
}

const STEP_FORMS = {
  assess: {
    title: "Оценка лота",
    submitText: "Оценить",
    fields: () => [
      whoField("Кто оценил"),
      { name: "assessment", label: "Оценка словами", type: "textarea", placeholder: "суть закупки, на что обратить внимание" },
      { name: "volume_t", label: "Объём, т", type: "number" },
      { name: "packaging", label: "Тара", type: "text", placeholder: "мешки, ящики, паллеты…" },
      { name: "hazard", label: "Опасный груз", type: "checkbox" },
      { name: "docs_needed", label: "Не хватает документов", type: "text" },
    ],
  },
  decide: {
    title: "Решение об участии",
    submitText: "Записать решение",
    fields: () => [
      whoField("Кто решил", "by"),
      { name: "participate", label: "Участвуем", type: "checkbox", value: true },
      {
        name: "comment",
        label: "Комментарий",
        type: "textarea",
        placeholder: "при отказе — причина, обязательна",
        requiredIf: (v) => !v.participate,
      },
    ],
  },
  price: {
    title: "Сбор цены",
    submitText: "Сохранить цену",
    fields: () => [
      whoField("Кто собрал цену"),
      { name: "supplier", label: "Поставщик", type: "text", required: true },
      { name: "purchase_price", label: "Закупочная цена, ₽", type: "number" },
      { name: "logistics", label: "Логистика, ₽", type: "number" },
      { name: "cost", label: "Себестоимость, ₽", type: "number" },
      { name: "markup", label: "Наценка, %", type: "number" },
    ],
  },
  "approve-price": {
    title: "Утверждение цены",
    submitText: "Утвердить",
    fields: () => [
      whoField("Кто утверждает", "by"),
      { name: "bid_price", label: "Цена подачи, ₽", type: "number", required: true },
    ],
  },
  submit: {
    title: "Отметка о подаче",
    submitText: "Подал",
    note:
      "Программа не подаёт заявку на площадке — она фиксирует, что подал человек. " +
      "Номер заявки возьмите на площадке: по нему заявку ищут в протоколах.",
    fields: () => [
      whoField("Кто подал"),
      { name: "submission_no", label: "Номер поданной заявки", type: "text", required: true },
    ],
  },
  result: {
    title: "Итог участия",
    submitText: "Записать итог",
    fields: () => [
      whoField("Кто узнал итог"),
      {
        name: "result",
        label: "Итог",
        type: "select",
        required: true,
        value: "won",
        options: [["won", "выиграли"], ["lost", "проиграли"]],
      },
      { name: "winner_price", label: "Цена победителя, ₽", type: "number" },
      {
        name: "loss_reason",
        label: "Почему проиграли",
        type: "text",
        requiredIf: (v) => v.result === "lost",
      },
    ],
  },
  followup: {
    title: "Follow-up",
    submitText: "Записать",
    note: "Проигранный тендер — не конец отношений: контакт, заведённый сейчас, дешевле нового поиска.",
    fields: () => [
      whoField("Кто ведёт контакт"),
      { name: "contact", label: "Контакт", type: "text", placeholder: "имя, телефон или e-mail" },
      { name: "next_touch", label: "Следующее касание", type: "date" },
      { name: "note", label: "Заметка", type: "textarea" },
    ],
  },
};

function stepFieldHtml(f) {
  const req = f.required ? " *" : "";
  if (f.type === "checkbox") {
    return (
      `<label class="check"><input type="checkbox" data-f="${f.name}"` +
      (f.value ? " checked" : "") +
      `><span>${esc(f.label)}</span></label>`
    );
  }
  const ph = f.placeholder ? ` placeholder="${escAttr(f.placeholder)}"` : "";
  let control;
  if (f.type === "textarea") {
    control = `<textarea data-f="${f.name}"${ph}>${esc(f.value || "")}</textarea>`;
  } else if (f.type === "select") {
    control =
      `<select data-f="${f.name}"${ph}>` +
      (f.options || [])
        .map(
          ([v, t]) =>
            `<option value="${escAttr(v)}"${f.value === v ? " selected" : ""}>${esc(t)}</option>`
        )
        .join("") +
      `</select>`;
  } else {
    control = `<input type="${f.type || "text"}" data-f="${f.name}"${ph} value="${escAttr(f.value ?? "")}">`;
  }
  return `<div class="field"><label>${esc(f.label)}${req}</label>${control}</div>`;
}

/**
 * Открыть форму и дождаться её заполнения.
 * cfg: {title, lot, note, fields, submitText, send(values)}.
 * send бросает Error с текстом сервера — текст показывается в модалке,
 * не закрывая её: человек правит поле и отправляет снова.
 * resolve(values) — успех, resolve(null) — отмена.
 */
function openStepForm(cfg) {
  return new Promise((resolve) => {
    const modal = $("step-modal");
    const body = $("step-modal-body");
    const fields = cfg.fields;
    body.innerHTML =
      `<div class="step-form-title">${esc(cfg.title)}</div>` +
      (cfg.lot ? `<div class="step-form-lot">${esc(cfg.lot)}</div>` : "") +
      (cfg.note ? `<div class="step-note">${esc(cfg.note)}</div>` : "") +
      `<form class="step-form" id="step-form">` +
      fields.map(stepFieldHtml).join("") +
      `<div class="step-err hidden" id="step-err"></div>` +
      `<div class="btn-row right">` +
      `<button type="button" class="btn btn-secondary" id="step-cancel">Отмена</button>` +
      `<button type="submit" class="btn btn-primary" id="step-ok">${esc(cfg.submitText || "Сохранить")}</button>` +
      `</div></form>`;
    modal.classList.remove("hidden");

    const err = $("step-err");
    const showErr = (text) => {
      err.textContent = text;
      err.classList.remove("hidden");
    };
    const onCancel = () => close(null);
    const onBackdrop = (e) => {
      if (e.target === modal) close(null);
    };
    const close = (value) => {
      modal.classList.add("hidden");
      $("step-cancel").removeEventListener("click", onCancel);
      $("step-modal-close").removeEventListener("click", onCancel);
      modal.removeEventListener("click", onBackdrop);
      resolve(value);
    };

    $("step-cancel").addEventListener("click", onCancel);
    $("step-modal-close").addEventListener("click", onCancel);
    modal.addEventListener("click", onBackdrop);

    $("step-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      // Чекбокс даёт boolean, остальное — строки; числа проверит и
      // приведёт сервер, чтобы правило жило в одном месте.
      const values = {};
      body.querySelectorAll("[data-f]").forEach((el) => {
        values[el.dataset.f] = el.type === "checkbox" ? el.checked : el.value.trim();
      });
      const missing = fields.filter(
        (f) =>
          (f.required || (f.requiredIf && f.requiredIf(values))) &&
          f.type !== "checkbox" &&
          !values[f.name]
      );
      if (missing.length) {
        showErr("Заполните: " + missing.map((f) => f.label).join(", "));
        return;
      }
      const btn = $("step-ok");
      btn.disabled = true;
      try {
        // send необязателен: форма может быть просто вопросом (например,
        // имя менеджера) без запроса к серверу.
        if (cfg.send) await cfg.send(values);
        close(values);
      } catch (e2) {
        showErr(e2.message);
        btn.disabled = false;
      }
    });
  });
}

async function openLotStep(row, route) {
  const form = STEP_FORMS[route];
  if (!form) return;
  await openStepForm({
    title: form.title,
    lot: `${row.name || ""} · ${row.company || ""}`,
    note: form.note,
    fields: form.fields(),
    submitText: form.submitText,
    send: async (values) => {
      const updated = await lotAction(row.lot_key, route, values);
      toast(`Готово: ${REGISTRY_STATE.titles[updated.status] || updated.status}`, "success");
      // Ответ — обновлённая заявка тем же видом, что /api/registry:
      // перерисовываем строку без второго запроса (ТЗ-9).
      replaceRegistryRow(updated);
      // Красная зона зависит от статуса («заявка не отправлена» снимается
      // после подачи) — обновляем и её.
      loadRegistry();
    },
  });
}

async function openRegistrySkip(row) {
  const manager =
    ((SETTINGS_CACHE && SETTINGS_CACHE.manager_name) || "").trim() ||
    localStorage.getItem("tw_manager") ||
    row.manager ||
    "";
  await openStepForm({
    title: "Мимо?",
    lot: `${row.name || ""} · ${row.company || ""}`,
    note: "Отказ возможен с любого шага, но только с причиной: по причинам видно, где мы промахиваемся.",
    fields: [{ name: "reason", label: "Причина отказа", type: "textarea", required: true }],
    submitText: "Мимо",
    send: async (values) => {
      const updated = await lotAction(row.lot_key, "skip", { manager, reason: values.reason });
      toast("Лот отклонён", "success");
      replaceRegistryRow(updated);
      loadRegistry();
    },
  });
}

/* ── Карточка лота (T018) ──────────────────────────────────────────────────
 * Отвечает на три вопроса регламента: почему лот в списке (оценка словами),
 * чей он (заказчик и уровень работы), что с ним делать (текущий шаг воронки
 * и история). Оценка берётся из базы — та же, что в ленте и в отчётах.
 */
function fmtDeadline(lot) {
  const left = lot.days_left;
  if (left === null || left === undefined) return lot.date_deadline || "не указан";
  const tail =
    left < 0
      ? `просрочен на ${-left} дн.`
      : left === 0
        ? "сегодня последний день"
        : `осталось ${left} дн.`;
  return `${lot.date_deadline} · ${tail}`;
}

function renderLotCard(lot) {
  const titles = lot.status_titles || {};
  const app = lot.application;
  const rows = [
    ["Площадка", lot.platform || "—"],
    ["Номер процедуры", lot.tender_no || "—"],
    ["Тип процедуры", lot.tender_type || "—"],
    ["Начальная цена", money(lot.sum)],
    ["Опубликован", lot.date_published || "—"],
    ["Приём заявок до", fmtDeadline(lot)],
    ["ИНН заказчика", lot.inn || "—"],
    ["Найден по", lot.search_key || "—"],
    ["Предоплата", lot.prepayment === "yes" ? "есть" : lot.prepayment === "no" ? "нет" : "условие не найдено"],
  ];

  const html = [];
  html.push(`<div class="lot-card-title">${(lot.name || "Без названия").replace(/</g, "&lt;")}</div>`);
  html.push(`<div class="lot-card-company">${(lot.company || "").replace(/</g, "&lt;")}</div>`);
  html.push(
    `<div class="lot-card-why"><b>Почему в списке:</b> ${
      (lot.score_explain || "оценка не считалась").replace(/</g, "&lt;")
    } <span class="muted">(оценка ${lot.score ?? 0})</span></div>`
  );
  html.push('<dl class="lot-card-grid">');
  rows.forEach(([k, v]) => html.push(`<dt>${k}</dt><dd>${v}</dd>`));
  html.push("</dl>");

  // Переход — на первоисточник (main_href); без ссылки — номер для поиска.
  if (lot.main_href) {
    html.push(
      `<div><a href="${escAttr(lot.main_href)}" target="_blank" rel="noopener">Открыть лот на площадке →</a></div>`
    );
  } else {
    html.push(
      `<div class="muted">Ссылки нет: № ${esc(lot.tender_no || lot.lot_id || "")} — искать на площадке ${esc(lot.main_platform || lot.platform || "")}</div>`
    );
  }

  if ((lot.sources || []).length > 1) {
    html.push('<div class="lot-card-section">Ещё публикуется</div>');
    lot.sources.forEach((src) =>
      html.push(
        src.href
          ? `<div class="muted">${esc(src.platform)} — <a href="${escAttr(src.href)}" target="_blank" rel="noopener">ссылка</a></div>`
          : `<div class="muted">${esc(src.platform)} — без ссылки</div>`
      )
    );
  }

  // Что внутри (ТЗ-10): состав сборного лота — главный ответ на вопрос
  // «наша ли это позиция», по названию его не решить.
  if ((lot.items || []).length) {
    html.push('<div class="lot-card-section">Что внутри</div>');
    const shown = lot.items.slice(0, 20);
    shown.forEach((it) => {
      let qty = "";
      const n = Number(it.quantity);
      // Количество печатаем без хвостовых нулей и с запятой — как в книге.
      if (it.quantity !== null && it.quantity !== undefined && Number.isFinite(n)) {
        qty = " — " + (Number.isInteger(n) ? String(n) : String(n).replace(".", ","));
        if (it.unit) qty += " " + it.unit.replace(/</g, "&lt;");
      }
      html.push(`<div>${it.position}. ${(it.name || "").replace(/</g, "&lt;")}${qty}</div>`);
    });
    if (lot.items.length > shown.length) {
      html.push(`<div class="muted">…ещё ${lot.items.length - shown.length} позиций</div>`);
    }
  }

  html.push('<div class="lot-card-section">Что с ним делать</div>');
  if (!app) {
    html.push('<div class="muted">лот ещё не в работе</div>');
  } else {
    html.push(
      `<div>шаг: <span class="reg-status">${titles[app.status] || app.status}</span>` +
        (app.manager ? ` · ведёт ${app.manager}` : "") +
        (app.reject_reason ? ` · причина отказа: ${app.reject_reason}` : "") +
        "</div>"
    );
  }

  if ((lot.history || []).length) {
    html.push('<div class="lot-card-section">История</div>');
    html.push('<div class="lot-card-history">');
    lot.history.forEach((h) =>
      html.push(
        `<div>${fmtStamp(h.at)} · ${titles[h.to_status] || h.to_status}${
          h.who ? " · " + h.who : ""
        }${h.comment ? " — " + h.comment : ""}</div>`
      )
    );
    html.push("</div>");
  }

  html.push('<div class="lot-card-actions">');
  if (!app) {
    html.push(`<button class="btn-mini take" id="card-take">Беру</button>`);
    html.push(`<button class="btn-mini skip" id="card-skip">Мимо</button>`);
  }
  html.push("</div>");

  $("lot-modal-body").innerHTML = html.join("");

  const take = $("card-take");
  if (take)
    take.addEventListener("click", async () => {
      take.disabled = true;
      try {
        const manager = await currentManager();
        if (!manager) {
          take.disabled = false;
          return;
        }
        await lotAction(lot.lot_key, "take", { manager });
        openLotCard(lot.lot_key);
        loadFeed();
      } catch (e) {
        take.disabled = false;
        toast(e.message, "error");
      }
    });
  const skip = $("card-skip");
  if (skip)
    skip.addEventListener("click", async () => {
      const manager = await currentManager();
      if (!manager) return;
      const v = await askSkipReason(`${lot.name || ""} · ${lot.company || ""}`);
      if (!v) return;
      try {
        await lotAction(lot.lot_key, "skip", { manager, reason: v.reason });
        openLotCard(lot.lot_key);
        loadFeed();
      } catch (e) {
        toast(e.message, "error");
      }
    });
}

async function openLotCard(lotKey) {
  const modal = $("lot-modal");
  modal.classList.remove("hidden");
  $("lot-modal-body").innerHTML = '<div class="muted">загрузка…</div>';
  try {
    const r = await fetch(`/api/lot/${encodeURIComponent(lotKey)}`);
    const lot = await r.json();
    if (lot.error) {
      $("lot-modal-body").innerHTML = `<div class="muted">${lot.error}</div>`;
      return;
    }
    renderLotCard(lot);
  } catch (e) {
    $("lot-modal-body").innerHTML = `<div class="muted">не удалось открыть: ${e}</div>`;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const close = $("lot-modal-close");
  if (close) close.addEventListener("click", () => $("lot-modal").classList.add("hidden"));
  const modal = $("lot-modal");
  if (modal)
    modal.addEventListener("click", (e) => {
      if (e.target === modal) modal.classList.add("hidden");
    });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    // Форму шага закрываем «кнопкой» — так срабатывает отмена promise,
    // и ожидающий код не повисает в ожидании ответа навсегда.
    const step = $("step-modal");
    if (step && !step.classList.contains("hidden")) {
      $("step-modal-close").click();
      return;
    }
    if (modal) modal.classList.add("hidden");
  });
});

/* ── Справочники (T020) ────────────────────────────────────────────────────
 * Правится источник истины — группы номенклатуры и заказчики. Фразы поиска,
 * объекты обхода и список ИНН программа пересобирает сама при сохранении:
 * раньше это делал скрипт, который человек запускал руками, и справочники
 * двух программ разъезжались.
 */
const CATALOG = { nomenclature: null, clients: null, meta: {} };

function catStatus(text) {
  $("cat-status").textContent = text;
}

function renderNomenclature() {
  const box = $("cat-nomenclature");
  box.innerHTML = "";
  const groups = (CATALOG.nomenclature || {}).groups || [];

  groups.forEach((g, idx) => {
    const el = document.createElement("div");
    el.className = "cat-group";
    const daily = g.tier === "daily";
    el.innerHTML = `
      <div class="cat-group-head">
        <label class="check">
          <input type="checkbox" data-group="${idx}" ${g.enabled ? "checked" : ""}>
          <span class="cat-group-title">${(g.title || g.id || "").replace(/</g, "&lt;")}</span>
        </label>
        <span class="cat-badge">вес ${g.weight ?? 0}</span>
        <span class="cat-badge">${daily ? "каждый день" : g.tier === "off" ? "не ищем" : "раз в неделю"}</span>
        <span class="muted">${(g.phrases || []).length} фраз</span>
      </div>
      <div class="cat-phrases">${(g.phrases || []).slice(0, 12).map((p) => p.replace(/</g, "&lt;")).join(" · ")}${
        (g.phrases || []).length > 12 ? " …" : ""
      }</div>
      ${g.source ? `<div class="cat-source">${g.source.replace(/</g, "&lt;")}</div>` : ""}`;
    box.appendChild(el);
  });

  const save = document.createElement("button");
  save.className = "btn btn-primary cat-save";
  save.textContent = "Сохранить номенклатуру";
  save.addEventListener("click", async () => {
    box.querySelectorAll("input[data-group]").forEach((input) => {
      groups[Number(input.dataset.group)].enabled = input.checked;
    });
    save.disabled = true;
    catStatus("сохраняю…");
    try {
      const r = await fetch("/api/catalog/nomenclature", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ groups }),
      });
      const res = await r.json();
      catStatus(
        res.error
          ? res.error
          : `сохранено · фраз ${res.derived.keywords} (включено ${res.derived.keywords_enabled})`
      );
    } finally {
      save.disabled = false;
    }
  });
  box.appendChild(save);
}

function renderClients() {
  const box = $("cat-clients");
  box.innerHTML = "";
  const clients = (CATALOG.clients || {}).clients || [];
  const levels = { full: "полная работа", partial: "совместно", market: "рынок" };

  clients.forEach((c, idx) => {
    const el = document.createElement("div");
    el.className = "cat-client";
    const inns = (c.inns || []).map(
      (i) => `${i.inn}${i.verified ? "" : " ⚠"}`
    );
    el.innerHTML = `
      <div class="cat-client-head">
        <span class="cat-group-title">${(c.name || "").replace(/</g, "&lt;")}</span>
        <select data-client="${idx}" class="cat-level">
          ${Object.entries(levels)
            .map(
              ([k, t]) =>
                `<option value="${k}" ${c.level === k ? "selected" : ""}>${t}</option>`
            )
            .join("")}
        </select>
        ${inns.length ? `<span class="cat-badge">ИНН: ${inns.join(", ")}</span>` : '<span class="muted">ИНН не задан</span>'}
      </div>
      ${c.platforms && c.platforms["где_публикует"] ? `<div class="cat-source">публикует: ${c.platforms["где_публикует"]}</div>` : ""}`;
    box.appendChild(el);
  });

  const legend = document.createElement("div");
  legend.className = "cat-source";
  legend.textContent = "⚠ — совпадение по названию не сверено с ЕГРЮЛ; на отбор не влияет";
  box.appendChild(legend);

  const save = document.createElement("button");
  save.className = "btn btn-primary cat-save";
  save.textContent = "Сохранить заказчиков";
  save.addEventListener("click", async () => {
    box.querySelectorAll("select[data-client]").forEach((sel) => {
      clients[Number(sel.dataset.client)].level = sel.value;
    });
    save.disabled = true;
    catStatus("сохраняю…");
    try {
      const r = await fetch("/api/catalog/clients", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clients, meta: CATALOG.meta }),
      });
      const res = await r.json();
      catStatus(
        res.error
          ? res.error
          : `сохранено · объектов обхода ${res.derived.companies}, ИНН ${res.derived.client_inns}`
      );
    } finally {
      save.disabled = false;
    }
  });
  box.appendChild(save);
}

async function loadCatalog() {
  catStatus("загрузка…");
  try {
    const r = await fetch("/api/catalog");
    const data = await r.json();
    if (data.error) {
      catStatus(data.error);
      return;
    }
    CATALOG.nomenclature = data.nomenclature;
    CATALOG.clients = data.clients;
    CATALOG.meta = Object.fromEntries(
      Object.entries(data.clients || {}).filter(([k]) => k !== "clients")
    );
    renderNomenclature();
    renderClients();
    const groups = (data.nomenclature.groups || []).length;
    const clients = (data.clients.clients || []).length;
    catStatus(`групп номенклатуры: ${groups} · заказчиков: ${clients}`);
  } catch (e) {
    catStatus("не удалось загрузить справочники: " + e);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".cat-tab").forEach((btn) =>
    btn.addEventListener("click", () => {
      document.querySelectorAll(".cat-tab").forEach((b) =>
        b.classList.toggle("active", b === btn)
      );
      document.querySelectorAll(".cat-pane").forEach((p) =>
        p.classList.toggle("active", p.id === "cat-" + btn.dataset.cat)
      );
    })
  );
  document.querySelectorAll('.nav-item[data-tab="catalog"]').forEach((b) =>
    b.addEventListener("click", loadCatalog)
  );
});

/* ── Расписание сбора (Э4) ────────────────────────────────────────────────
 * Задача ставится в планировщик Windows, а не крутится внутри программы:
 * программа может быть закрыта, а сбор всё равно должен пойти. Пропущенный
 * слот (ПК был выключен) программа догоняет при следующем запуске.
 */
const DAY_TITLES = [
  ["mon", "пн"], ["tue", "вт"], ["wed", "ср"], ["thu", "чт"],
  ["fri", "пт"], ["sat", "сб"], ["sun", "вс"],
];
const SCHED = { days: new Set() };

function renderDays() {
  const box = $("sched-days");
  if (!box) return;
  box.innerHTML = '<div class="day-toggles"></div>';
  const row = box.firstChild;
  DAY_TITLES.forEach(([key, title]) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "day-toggle" + (SCHED.days.has(key) ? " on" : "");
    b.textContent = title;
    b.addEventListener("click", () => {
      if (SCHED.days.has(key)) SCHED.days.delete(key);
      else SCHED.days.add(key);
      renderDays();
    });
    row.appendChild(b);
  });
}

async function loadSchedule() {
  try {
    const r = await fetch("/api/schedule");
    const d = await r.json();
    const s = d.schedule || {};
    $("sched-enabled").checked = !!s.enabled;
    $("sched-time").value = s.time || "03:00";
    $("sched-catchup").checked = s.catch_up !== false;
    SCHED.days = new Set(s.days || []);
    renderDays();
    $("sched-status").textContent = d.task_registered
      ? `${d.text} · задача в планировщике стоит`
      : d.text;
  } catch (e) {
    $("sched-status").textContent = "не удалось загрузить расписание: " + e;
  }
}

// Настройки читаем сразу при открытии окна, а не при заходе в свой раздел:
// из них берётся имя человека, которым подписываются решения по лоту. Без
// этого поле «кто» в формах воронки открывалось пустым, хотя имя в настройках
// давно указано (найдено 04.09.2026 при живой проверке реестра).
document.addEventListener("DOMContentLoaded", () => {
  loadSettings().catch(() => {});
});

document.addEventListener("DOMContentLoaded", () => {
  const save = $("sched-save");
  if (!save) return;
  loadSchedule();
  save.addEventListener("click", async () => {
    save.disabled = true;
    $("sched-status").textContent = "сохраняю…";
    try {
      const r = await fetch("/api/schedule", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          schedule: {
            enabled: $("sched-enabled").checked,
            time: $("sched-time").value,
            days: [...SCHED.days],
            catch_up: $("sched-catchup").checked,
          },
        }),
      });
      const res = await r.json();
      $("sched-status").textContent = res.error
        ? res.error
        : `${res.text} — ${res.message}`;
    } finally {
      save.disabled = false;
    }
  });
});

/* ── Отчёты (T022) ────────────────────────────────────────────────────────
 * Отвечают на три вопроса: откуда приходят лоты, что с ними происходит и
 * что мы вообще ищем. Считаются по всей базе, а не по последнему прогону:
 * лот, найденный неделю назад и ещё актуальный, — такая же наша работа.
 */
function repTable(title, hint, rows) {
  if (!rows.length) return "";
  const body = rows
    .map((r) => `<div class="reg-row rep-row">${r}</div>`)
    .join("");
  return `<div class="rep-block"><div class="lot-card-section">${title}</div>
    ${hint ? `<div class="muted rep-hint">${hint}</div>` : ""}${body}</div>`;
}

function renderReports(d) {
  const unknownShare = d.total_lots
    ? Math.round((d.payment_unknown / d.total_lots) * 100)
    : 0;
  $("rep-summary").textContent =
    `${d.total_lots} лотов · с предоплатой ${d.prepaid} · условие неизвестно у ${unknownShare}%`;

  const platforms = (d.platforms || []).map(
    (p) =>
      `<div>${p.platform}${p.silent ? ' <span class="rep-warn">молчит</span>' : ""}</div>
       <div class="muted">профиль ${p.core} · клиенты ${p.clients}</div>
       <div class="muted">предоплата ${p.prepaid}</div>
       <div><b>${p.total}</b></div>`
  );

  const groups = (d.groups || []).map(
    (g) => `<div>${g.group}</div><div></div><div></div><div><b>${g.lots}</b></div>`
  );

  const steps = d.funnel && d.funnel.steps ? d.funnel.steps : {};
  const stepTitles = {
    SELECTED: "взято в работу",
    ASSESSED: "оценено",
    DECIDED: "решение принято",
    PRICED: "цена собрана",
    PRICE_APPROVED: "цена утверждена",
    SUBMITTED: "подано",
    RESULT: "итог подведён",
    FOLLOWUP: "follow-up",
    REJECTED: "отказ",
  };
  const funnel = Object.entries(steps).map(
    ([k, v]) => `<div>${stepTitles[k] || k}</div><div></div><div></div><div><b>${v}</b></div>`
  );

  const reasons = ((d.funnel || {}).reject_reasons || []).map(
    (r) => `<div>${(r.reason || "").replace(/</g, "&lt;")}</div><div></div><div></div><div><b>${r.count}</b></div>`
  );

  const dl = d.deadlines || {};
  const deadlines = [
    `<div>горит (≤ 3 дней)</div><div></div><div></div><div><b>${dl.urgent ?? 0}</b></div>`,
    `<div>на этой неделе</div><div></div><div></div><div><b>${dl.this_week ?? 0}</b></div>`,
    `<div>срок истёк</div><div></div><div></div><div><b>${dl.expired ?? 0}</b></div>`,
    `<div>пропал — проверить</div><div></div><div></div><div><b>${dl.missing ?? 0}</b></div>`,
  ];

  $("rep-body").innerHTML =
    repTable("Откуда приходят лоты", "молчащая площадка выглядит как «ничего не нашлось» — поэтому помечаем явно", platforms) +
    repTable("Что мы ищем", "группа без единого попадания — повод пересмотреть фразы, а не считать рынок пустым", groups) +
    repTable("Наши сроки", "по лотам профиля и закреплённых заказчиков", deadlines) +
    repTable("Воронка заявок", "", funnel) +
    repTable("Причины отказов", "по ним видно, где мы промахиваемся", reasons);
}

async function loadReports() {
  $("rep-summary").textContent = "загрузка…";
  try {
    const r = await fetch("/api/reports");
    const d = await r.json();
    if (d.error) {
      $("rep-summary").textContent = d.error;
      return;
    }
    renderReports(d);
  } catch (e) {
    $("rep-summary").textContent = "не удалось загрузить отчёты: " + e;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll('.nav-item[data-tab="reports"]').forEach((b) =>
    b.addEventListener("click", loadReports)
  );
});
