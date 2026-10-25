// Веб-чат agentkit. Без сборки и зависимостей: один файл, чтобы любой проект
// на шаблоне получал рабочий интерфейс сразу, а правки ядра доезжали обновлением пакета.
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
};
const icon = (name, cls = "") => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  if (cls) s.setAttribute("class", cls);
  const u = document.createElementNS("http://www.w3.org/2000/svg", "use");
  u.setAttribute("href", "#i-" + name);
  s.append(u);
  return s;
};

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* приватный режим */ } },
};

const S = {
  token: store.get("agent_token"),
  config: null,
  me: null,
  sessionId: null,
  sessions: [],
  lastSeq: 0,
  stream: null,      // AbortController текущего SSE
  running: false,
  bubble: null,      // текущий ответ, который печатается
  bubbleText: "",
  group: null,       // текущая группа действий агента
  steps: new Map(),  // tool_call_id → шаг
  subs: new Map(),   // id дочерней сессии → контейнер субагента
  cards: new Map(),  // approval_id → карточка
  cost: 0,
  attachments: [],
  uploading: 0,
  queued: [],        // сообщения, отправленные во время работы агента
};

// ── API ─────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${S.token}`, ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (r.status === 401) { logout(); throw new Error("нужен вход"); }
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(data?.detail || data || r.statusText);
  return data;
}

// ── текст ───────────────────────────────────────────────────────────
function esc(s) { return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
}
function md(src) {
  const lines = (src || "").replace(/\r/g, "").split("\n");
  let out = "", i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (/^```/.test(l)) {
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; out += `<pre><code>${esc(buf.join("\n"))}</code></pre>`; continue;
    }
    if (/^\s*\|.*\|\s*$/.test(l) && /^\s*\|?[\s:-]+\|/.test(lines[i + 1] || "")) {
      const row = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const head = row(l); i += 2; const body = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) body.push(row(lines[i++]));
      out += `<table><thead><tr>${head.map(c => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>` +
        body.map(r => `<tr>${r.map(c => `<td>${inline(c)}</td>`).join("")}</tr>`).join("") + "</tbody></table>";
      continue;
    }
    let m;
    if ((m = l.match(/^(#{1,3})\s+(.*)/))) { out += `<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`; i++; continue; }
    if (/^\s*([-*]|\d+\.)\s+/.test(l)) {
      const ordered = /^\s*\d+\./.test(l); const items = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, ""));
      out += `<${ordered ? "ol" : "ul"}>${items.map(x => `<li>${inline(x)}</li>`).join("")}</${ordered ? "ol" : "ul"}>`;
      continue;
    }
    if (!l.trim()) { i++; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,3}\s|\s*([-*]|\d+\.)\s|\s*\|)/.test(lines[i])) para.push(lines[i++]);
    if (!para.length) para.push(lines[i++]);
    out += `<p>${para.map(inline).join("<br>")}</p>`;
  }
  return out;
}
function renderMd(el, text) {
  el.innerHTML = md(text);
  for (const pre of el.querySelectorAll("pre")) pre.append(copyBtn(() => pre.querySelector("code")?.textContent || ""));
}
function copyBtn(get) {
  const b = h("button", { class: "icon-btn copy", title: "Скопировать", type: "button" }, icon("copy"));
  b.addEventListener("click", e => {
    e.stopPropagation();
    navigator.clipboard?.writeText(get()).then(() => { b.replaceChildren(icon("check")); setTimeout(() => b.replaceChildren(icon("copy")), 1200); });
  });
  return b;
}

const plural = (n, one, few, many) => {
  const m10 = n % 10, m100 = n % 100;
  return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? few : many;
};
const secs = ms => ms < 1000 ? `${ms} мс` : `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0).replace(".", ",")} с`;
function humanSize(n) { return !n ? "" : n < 1024 ? `${n} Б` : n < 1048576 ? `${(n / 1024).toFixed(0)} КБ` : `${(n / 1048576).toFixed(1)} МБ`; }

// ── лента ───────────────────────────────────────────────────────────
const box = () => $("#messages");
function nearBottom() { const el = box(); return el.scrollHeight - el.scrollTop - el.clientHeight < 160; }
function scroll(force) { if (force || nearBottom()) box().scrollTop = box().scrollHeight; }
function setEmpty(on) { $(".main").classList.toggle("is-empty", on); $("#below").hidden = !on; }
function add(el, { keepGroup = false } = {}) {
  if ($("#empty")) { $("#empty").remove(); setEmpty(false); }
  // Любой элемент, кроме шагов, закрывает текущую группу действий.
  if (!keepGroup) closeGroup();
  box().append(el); scroll(); return el;
}

function splitContent(content) {
  if (typeof content === "string") return { text: content, files: [] };
  const parts = Array.isArray(content) ? content : [];
  return { text: parts.filter(p => p.type === "text").map(p => p.text).join("\n"), files: parts.filter(p => p.type === "file") };
}

function userMsg(content, seq, { queued = false } = {}) {
  const { text, files } = splitContent(content);
  const badge = queued ? h("span", { class: "queued-badge" }, "агент прочтёт после текущего шага") : null;
  const rewind = seq ? h("button", { class: "btn ghost sm", title: "Откатить разговор и файлы агента к моменту до этого сообщения",
    onclick: () => doRewind(seq, text) }, icon("undo"), "Изменить") : null;
  const el = add(h("div", { class: "msg user" + (queued ? " queued" : "") },
    files.length ? h("div", { class: "files" }, files.map(fileView)) : null,
    text ? h("div", { class: "bubble" }, text) : null,
    h("div", { class: "meta" }, badge, rewind)));
  el._badge = badge;
  return el;
}

function fileView(f) {
  if (f.kind === "image") {
    const img = h("img", { class: "thumb", alt: f.name, title: f.name });
    blobURL(f.path).then(u => { img.src = u; }).catch(() => {});
    img.addEventListener("click", () => openPreview(f));
    return img;
  }
  return h("button", { class: "file-chip", title: canPreview(f.name) ? "Открыть" : "Скачать", type: "button",
    onclick: () => canPreview(f.name) ? openPreview(f) : downloadFile(f) },
    icon("file"), h("span", {}, f.name), h("small", {}, humanSize(f.size)));
}

const blobCache = new Map();
async function blobURL(path) {
  const key = S.sessionId + path;
  if (blobCache.has(key)) return blobCache.get(key);
  const r = await fetch(`/api/sessions/${S.sessionId}/files?path=${encodeURIComponent(path)}`, { headers: { Authorization: `Bearer ${S.token}` } });
  if (!r.ok) throw new Error("файл недоступен");
  const u = URL.createObjectURL(await r.blob());
  blobCache.set(key, u);
  return u;
}

function finishBubble(b, text, intermediate = false) {
  b.classList.remove("streaming");
  if (intermediate) return; // «Проверю по реестру.» перед действиями — копировать нечего
  const msg = b.parentElement;
  msg.querySelector(".msg-actions")?.remove();
  msg.append(h("div", { class: "msg-actions" }, copyBtn(() => text)));
}
function assistantBubble() {
  const b = h("div", { class: "md streaming" });
  add(h("div", { class: "msg assistant" }, b));
  return b;
}

// ── действия агента: группа шагов ───────────────────────────────────
function toolTitle(name, title) {
  if (title) return title;
  if (S.config?.titles?.[name]) return S.config.titles[name];
  if (name.startsWith("mcp__")) { const [, srv, t] = name.split("__"); return `${srv} · ${t.replace(/_/g, " ")}`; }
  const s = name.replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}
const PRIMARY = ["query", "path", "file_path", "url", "command", "text", "question", "description", "name", "title", "id", "sku", "pattern"];
function argSummary(args) {
  if (!args || typeof args !== "object") return String(args ?? "");
  let v = PRIMARY.map(k => args[k]).find(x => x != null && x !== "");
  if (v == null) v = Object.values(args).find(x => typeof x === "string" || typeof x === "number");
  if (v == null) return Object.keys(args).length ? "" : "";
  v = String(v).replace(/\s+/g, " ");
  return v.length > 90 ? v.slice(0, 90) + "…" : v;
}

function newGroup() {
  const head = h("button", { class: "steps-head", type: "button" }, icon("chev", "chev"), h("span", { class: "summary" }), h("span", { class: "time" }));
  const list = h("div", { class: "steps-list" });
  const el = h("div", { class: "steps open" }, head, list);
  const g = { el, head, list, steps: [], userToggled: false };
  head.addEventListener("click", () => { g.userToggled = true; el.classList.toggle("open"); });
  add(el, { keepGroup: true });
  return g;
}
function closeGroup() {
  const g = S.group;
  if (!g) return;
  S.group = null;
  // Готовая группа сворачивается — ход мысли виден одной строкой; ошибки и ожидание оставляют её открытой.
  if (!g.userToggled && g.steps.every(s => s.state === "ok")) g.el.classList.remove("open");
  updateGroup(g);
}
function updateGroup(g) {
  const n = g.steps.length;
  const running = g.steps.filter(s => s.state === "run");
  const errs = g.steps.filter(s => s.state === "err").length;
  const waiting = g.steps.some(s => s.state === "wait");
  const ms = g.steps.reduce((a, s) => a + (s.ms || 0), 0);
  const sum = g.head.querySelector(".summary");
  sum.replaceChildren();
  if (running.length) sum.append(h("b", {}, running.at(-1).title), "…");
  else if (waiting) sum.append(h("b", {}, "Ждёт подтверждения"));
  else sum.append(`${n} ${plural(n, "действие", "действия", "действий")}`);
  if (errs) sum.append(h("span", { style: "color:var(--err)" }, ` · ${errs} ${plural(errs, "ошибка", "ошибки", "ошибок")}`));
  if (n > 1 && running.length) sum.append(h("span", {}, ` · шаг ${n - running.length + 1} из ${n}`));
  g.head.querySelector(".time").textContent = ms ? secs(ms) : "";
}

// Инструменты, которые сами по себе — часть ответа (график, дашборд): без строки «1 действие»,
// чтобы текст, визуализация и снова текст читались одним целым.
const SILENT_TOOLS = new Set(["show_visual"]);
function stepRow(id, name, args, title, agent) {
  if (S.steps.has(id)) return S.steps.get(id);
  if (SILENT_TOOLS.has(name) && !agent) {
    const ghost = { el: h("div"), st: h("span"), time: h("span"), detail: h("div"), title: "", state: "run", ms: 0, group: null, silent: true };
    S.steps.set(id, ghost);
    return ghost;
  }
  const st = h("span", { class: "st run" });
  const ttl = toolTitle(name, title);
  const time = h("span", { class: "step-time" });
  const row = h("button", { class: "step-row", type: "button", title: name }, st, h("span", { class: "step-title" }, ttl),
    h("span", { class: "step-args" }, argSummary(args)), time);
  const detail = h("div", { class: "step-detail" });
  const argText = JSON.stringify(args ?? {}, null, 2);
  if (argText !== "{}") detail.append(h("div", {}, h("div", { class: "label" }, "Аргументы", copyBtn(() => argText)), h("pre", {}, argText)));
  const el = h("div", { class: "step" }, row, detail);
  row.addEventListener("click", () => el.classList.toggle("open"));
  const step = { el, st, time, detail, title: ttl, state: "run", ms: 0, group: null };
  S.steps.set(id, step);
  if (agent && S.subs.has(agent)) {
    S.subs.get(agent).append(el);
  } else {
    if (!S.group) S.group = newGroup();
    step.group = S.group;
    S.group.steps.push(step);
    S.group.list.append(el);
    updateGroup(S.group);
  }
  scroll();
  return step;
}
function setStepState(step, state) {
  step.state = state;
  step.st.className = "st " + state;
  step.st.replaceChildren(state === "ok" ? icon("check") : state === "err" ? icon("x") : state === "wait" ? icon("pause") : "");
  if (step.group) {
    updateGroup(step.group);
    if (state === "err" || state === "wait") step.group.el.classList.add("open");
  }
}
function stepResult(id, content, isError, ui, ms) {
  const step = S.steps.get(id);
  if (!step) return;
  step.ms = ms || 0;
  if (ms) step.time.textContent = secs(ms);
  setStepState(step, isError ? "err" : "ok");
  // Группа уже закрыта (действие подтверждали позже) — сворачиваем, когда всё прошло успешно.
  const g = step.group;
  if (g && g !== S.group && !g.userToggled && g.steps.every(s => s.state === "ok")) g.el.classList.remove("open");
  step.detail.append(h("div", {}, h("div", { class: "label" }, isError ? "Ошибка" : "Результат", copyBtn(() => content || "")),
    h("pre", { class: isError ? "err" : "" }, content || "—")));
  for (const w of ui || []) widgetAfter(w);
}
function widgetAfter(w) {
  const el = widget(w);
  if (el) add(el, { keepGroup: true });
}

function widget(w) {
  if (w.type === "table") {
    const t = h("table", {}, h("thead", {}, h("tr", {}, (w.columns || []).map(c => h("th", {}, c)))),
      h("tbody", {}, (w.rows || []).map(r => h("tr", {}, r.map(c => h("td", {}, c ?? ""))))));
    return h("div", { class: "msg assistant" }, h("div", { class: "md" }, w.title ? h("h3", {}, w.title) : null, t));
  }
  if (w.type === "kv") {
    return h("div", { class: "msg assistant" }, h("div", { class: "md" },
      h("table", {}, h("tbody", {}, (w.items || []).map(([k, v]) => h("tr", {}, h("td", {}, k), h("td", {}, v)))))));
  }
  if (w.type === "attachments") {
    const imgs = (w.items || []).filter(a => a.type === "image");
    return imgs.length ? h("div", { class: "msg assistant" }, imgs.map(a => h("img", { class: "thumb", src: `data:${a.mime};base64,${a.data}` }))) : null;
  }
  if (w.type === "visual") return h("div", { class: "msg assistant viz-msg" }, VIZ.render(w));
  if (w.type === "files") return h("div", { class: "msg assistant file-cards" }, (w.files || []).map(fileCard));
  if (w.type === "choices") return null; // вопрос рисуется по событию ask_user
  return null;
}

function choices(question, options) {
  const wrap = h("div", { class: "choices" }, question ? h("div", { class: "q" }, "Выберите вариант или ответьте своими словами") : null);
  for (const o of options) {
    wrap.append(h("button", { class: "choice", type: "button", onclick: () => { wrap.remove(); send(o.label); } },
      h("span", {}, o.label), o.description ? h("small", {}, o.description) : null));
  }
  return wrap;
}

function approvalCard(a) {
  if (S.cards.has(a.approval_id)) return;
  const comment = h("input", { placeholder: "Комментарий агенту (необязательно)" });
  const yes = h("button", { class: "btn primary", type: "button", onclick: () => decide(a.approval_id, true, comment.value, card) }, "Подтвердить");
  const no = h("button", { class: "btn", type: "button", onclick: () => decide(a.approval_id, false, comment.value, card) }, "Отклонить");
  comment.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); yes.click(); } });
  const rows = (a.view || []).map(([k, v]) => h("tr", {}, h("td", {}, k), h("td", {}, v)));
  const name = a.tool || a.tool_name;
  const card = h("div", { class: "card" + (a.destructive ? " destructive" : "") },
    h("div", { class: "card-head" }, icon("shield"), a.destructive ? "Необратимое действие — подтвердите" : "Нужно подтверждение",
      h("span", { class: "tool-name" }, "· " + toolTitle(name, a.title))),
    a.reason ? h("div", { class: "reason" }, a.reason) : null,
    rows.length ? h("table", {}, h("tbody", {}, rows)) : null,
    h("div", { class: "actions" }, comment, no, yes));
  S.cards.set(a.approval_id, card);
  add(card);
  scroll(true); // карточка требует действия — она не должна оказаться под полем ввода
  const step = S.steps.get(a.tool_call_id);
  if (step) setStepState(step, "wait");
}
function closeCard(id, approved, expired) {
  const c = S.cards.get(id);
  if (!c) return;
  c.classList.add("done");
  c.querySelector(".actions")?.replaceWith(h("div", { class: "verdict " + (expired ? "" : approved ? "ok" : "no") },
    icon(expired ? "x" : approved ? "check" : "x"), expired ? "Не выполнено — разговор пошёл дальше" : approved ? "Подтверждено" : "Отклонено"));
}

function note(text) { add(h("div", { class: "note" }, text)); }
function alertMsg(text) { add(h("div", { class: "alert", role: "alert" }, text)); }

function renderTodos(todos) {
  const list = todos || [];
  const done = list.filter(t => t.status === "completed").length;
  $("#todo-list").replaceChildren(...(list.length ? list.map(t => h("li", { class: t.status },
    h("span", { class: "box" }, t.status === "completed" ? icon("check") : null), h("span", {}, t.content)))
    : [h("li", { class: "muted" }, "Агент заведёт список, если задача из нескольких шагов")]));
  $("#tasks-progress").textContent = list.length ? `${done} из ${list.length}` : "";
  $("#tasks-bar").style.width = list.length ? `${(done / list.length) * 100}%` : "0";
  const left = list.length - done;
  $("#tasks-count").hidden = !left; $("#tasks-count").textContent = left;
  $("#tasks").classList.toggle("all-done", !!list.length && !left);
  // Сам открываем один раз за разговор и только на широком экране; закрыл человек — больше не навязываем.
  if (list.length && !S.tasksShown && innerWidth > 960) { toggleTasks(true); S.tasksShown = true; }
}

function setCost(v) { S.cost = v; $("#cost").textContent = v > 0 ? `$${v.toFixed(v < 0.1 ? 4 : 2)}` : ""; }

// ── события прогона ─────────────────────────────────────────────────
function onEvent(ev) {
  if (ev.seq) S.lastSeq = Math.max(S.lastSeq, ev.seq);
  const d = ev.data || {};
  switch (ev.type) {
    case "run_started": setRunning(true); break;
    case "user_message": {
      const el = S.queued.shift();
      if (el) { el.classList.remove("queued"); el._badge?.remove(); closeGroup(); }
      else userMsg(d.content, d.seq);
      break;
    }
    case "text_delta":
      if (ev.agent) break;
      if (!S.bubble) { S.bubble = assistantBubble(); S.bubbleText = ""; }
      S.bubbleText = d.snapshot ? d.text : S.bubbleText + d.text;
      S.bubble.innerHTML = md(S.bubbleText); scroll(); break;
    case "message":
      if (ev.agent) break;
      if (d.content) {
        if (!S.bubble) S.bubble = assistantBubble();
        renderMd(S.bubble, d.content); finishBubble(S.bubble, d.content, !!d.tool_calls?.length);
      } else S.bubble?.parentElement.remove();
      S.bubble?.classList.remove("streaming");
      S.bubble = null; S.bubbleText = "";
      for (const tc of d.tool_calls || []) stepRow(tc.id, tc.function.name, safeJSON(tc.function.arguments));
      break;
    case "tool_call": stepRow(d.id, d.name, d.args, d.title, ev.agent); break;
    case "tool_result": {
      stepResult(d.id, d.content, d.is_error, ev.agent ? [] : d.ui, d.elapsed_ms);
      // Готовый файл — сразу в панель сбоку (только вживую, не при открытии старого разговора).
      const files = (ev.agent ? [] : d.ui || []).filter(w => w.type === "files").flatMap(w => w.files || []);
      // Из нескольких — главный: документ важнее таблицы, таблица важнее картинки.
      const rank = n => ["pdf", "html", "htm", "docx", "xlsx", "md", "csv", "svg", "png", "jpg", "jpeg", "webp"].indexOf(extOf(n));
      const pick = files.filter(f => AUTO_OPEN.has(extOf(f.name))).sort((a, b) => rank(a.name) - rank(b.name))[0];
      if (pick && !d.is_error) openPreview(pick);
      break;
    }
    case "subagent_started": {
      const step = S.steps.get(d.tool_call_id);
      const sub = h("div", { class: "sub" }, h("div", { class: "sub-title" }, `${d.type}: ${d.description}`));
      S.subs.set(d.session_id, sub);
      step?.el.insertBefore(sub, step.detail);
      break;
    }
    case "approval_required": approvalCard(d); break;
    case "approval_resolved": closeCard(d.approval_id, d.approved, d.expired); break;
    case "ask_user": add(choices(d.question, d.options)); break;
    case "todo": if (!ev.agent) renderTodos(d.todos); break;
    case "compacted": note("История сжата, чтобы поместиться в контекст"); break;
    case "notice": note(d.message); break;
    case "error": alertMsg(d.message); break;
    case "usage": setCost(S.cost + (d.cost || 0)); break;
    case "run_finished": {
      setRunning(false);
      S.bubble?.classList.remove("streaming"); S.bubble = null;
      for (const step of S.steps.values()) if (step.state === "run") setStepState(step, "err");
      closeGroup();
      const st = d.status;
      if (st === "max_steps") note("Достигнут лимит шагов — напишите «продолжай»");
      else if (st === "cancelled") note("Остановлено");
      else if (st === "error" && d.error) alertMsg(d.error);
      loadSessions();
      break;
    }
  }
}
function safeJSON(s) { try { return JSON.parse(s); } catch { return s; } }

async function follow() {
  S.stream?.abort();
  const ctrl = new AbortController(); S.stream = ctrl;
  const sid = S.sessionId;
  try {
    const r = await fetch(`/api/sessions/${sid}/events?after=${S.lastSeq}`, {
      headers: { Authorization: `Bearer ${S.token}` }, signal: ctrl.signal });
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, i); buf = buf.slice(i + 2);
        const data = block.split("\n").filter(l => l.startsWith("data:")).map(l => l.slice(5).trim()).join("\n");
        if (!data || data === "{}") continue;
        if (sid !== S.sessionId) return;
        try { onEvent(JSON.parse(data)); } catch (e) { console.error(e); }
      }
    }
  } catch (e) {
    if (e.name !== "AbortError") { note("Связь прервалась — переподключаюсь…"); setTimeout(() => S.running && follow(), 2000); }
  }
}

function setRunning(on) {
  // Отправка доступна и во время работы: сообщение встанет в очередь, агент прочтёт его между шагами.
  S.running = on;
  $("#stop").hidden = !on;
  $("#input").placeholder = on ? "Дописать агенту, пока он работает…" : "Сообщение…";
  updateSend();
}
function updateSend() { $("#send").disabled = !$("#input").value.trim() && !S.attachments.length; }

// ── действия ────────────────────────────────────────────────────────
async function ensureSession() {
  if (S.sessionId) return S.sessionId;
  const s = await api("/api/sessions", { method: "POST", body: { model: S.model, mode: S.mode, project_id: S.projectId || null } });
  S.sessionId = s.id; S.lastSeq = 0;
  history.replaceState(null, "", `#${s.id}`);
  return s.id;
}

async function send(text) {
  const fromInput = text == null;
  text = (text ?? $("#input").value).trim();
  if (S.uploading) { note("Дождитесь окончания загрузки файлов"); return; }
  const files = fromInput ? S.attachments.slice() : [];
  if (!text && !files.length) return;
  if (fromInput) { $("#input").value = ""; autosize(); S.attachments = []; renderAttachments(); }
  const sid = await ensureSession();
  const wasRunning = S.running;
  const content = files.length ? [...(text ? [{ type: "text", text }] : []), ...files] : text;
  const el = userMsg(content, null, { queued: wasRunning });
  if (wasRunning) S.queued.push(el);
  scroll(true);
  try {
    const r = await api(`/api/sessions/${sid}/messages`, { method: "POST", body: { text, files, model: S.model, mode: S.mode } });
    if (!r.queued) { setRunning(true); follow(); }
    else if (!wasRunning) { S.queued.push(el); el.classList.add("queued"); setRunning(true); follow(); }
  } catch (e) { alertMsg(e.message); }
  loadSessions();
}

async function attach(fileList) {
  const list = [...fileList];
  if (!list.length) return;
  const sid = await ensureSession();
  S.uploading++; renderAttachments();
  try {
    for (const f of list) {
      const fd = new FormData(); fd.append("file", f, f.name || "image.png");
      const r = await fetch(`/api/sessions/${sid}/files`, { method: "POST", headers: { Authorization: `Bearer ${S.token}` }, body: fd });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) { alertMsg(`${f.name}: ${d.detail || r.statusText}`); continue; }
      S.attachments.push(d);
    }
  } finally { S.uploading--; renderAttachments(); }
}

function renderAttachments() {
  const el = $("#attachments");
  el.replaceChildren(...S.attachments.map((a, i) => h("span", { class: "chip", title: a.note || a.name }, icon(a.kind === "image" ? "file" : "file"),
    h("span", {}, a.name), h("small", { class: "muted" }, humanSize(a.size)),
    h("button", { class: "chip-x", type: "button", "aria-label": "Убрать", onclick: () => { S.attachments.splice(i, 1); renderAttachments(); } }, icon("x")))),
    S.uploading ? h("span", { class: "chip loading" }, h("span", { class: "st run" }), "загрузка…") : null);
  el.hidden = !S.attachments.length && !S.uploading;
  updateSend();
}

async function decide(id, approve, comment, card) {
  card.querySelectorAll("button, input").forEach(b => (b.disabled = true));
  try {
    await api(`/api/approvals/${id}`, { method: "POST", body: { approve, comment } });
    setRunning(true); follow();
  } catch (e) { alertMsg(e.message); card.querySelectorAll("button, input").forEach(b => (b.disabled = false)); }
}

async function doRewind(seq, text) {
  if (S.running) return;
  if (!confirm("Откатить разговор и файлы агента к моменту до этого сообщения? Текст вернётся в поле ввода.")) return;
  await api(`/api/sessions/${S.sessionId}/rewind`, { method: "POST", body: { seq: seq - 1 } });
  await openSession(S.sessionId);
  $("#input").value = text; autosize(); $("#input").focus();
}

// ── сессии ──────────────────────────────────────────────────────────
async function loadSessions() {
  [S.sessions, S.projects] = await Promise.all([api("/api/sessions"), api("/api/projects").catch(() => S.projects || [])]);
  const cur = S.sessions.find(s => s.id === S.sessionId);
  if (cur) $("#chat-title").textContent = cur.title;
  renderSessions();
}
function renderSessions() {
  const q = $("#session-filter").value.trim().toLowerCase();
  const items = S.sessions.filter(s => !q || s.title.toLowerCase().includes(q));
  const day = 86400, now = new Date(); now.setHours(0, 0, 0, 0);
  const today = now.getTime() / 1000;
  const groups = [["Сегодня", t => t >= today], ["Вчера", t => t >= today - day], ["Последние 7 дней", t => t >= today - 7 * day], ["Ранее", () => true]];
  const out = [];
  const used = new Set();
  for (const [label, test] of groups) {
    const g = items.filter(s => !used.has(s.id) && test(s.updated_at));
    if (!g.length) continue;
    g.forEach(s => used.add(s.id));
    out.push(h("div", { class: "group-label" }, label));
    for (const s of g) {
      const more = h("button", { class: "icon-btn more", type: "button", title: "Ещё", "aria-haspopup": "menu", "aria-label": "Действия с разговором" }, icon("dots"));
      const item = h("div", {
        class: "session" + (s.id === S.sessionId ? " active" : ""), role: "button", tabindex: "0", title: s.title,
        onclick: e => { if (!e.target.closest(".more, input")) { openSession(s.id); toggleSidebar(false); } },
        onkeydown: e => { if (e.key === "Enter" && e.target === item) openSession(s.id); },
      }, h("span", {}, s.title), s.project ? h("small", { class: "s-proj", title: `Проект «${s.project}»` }, icon("folder")) : null,
      s.running ? h("i", { class: "dot", title: "Агент работает" }) : null, more);
      more.addEventListener("click", e => { e.stopPropagation(); sessionMenu(s, item, more); });
      out.push(item);
    }
  }
  if (!out.length) out.push(h("div", { class: "group-label" }, q ? "Ничего не найдено" : "Разговоров пока нет"));
  $("#sessions").replaceChildren(...(q ? [] : projectsSection()), ...out);
}

// ── проекты в боковой панели (как в Claude): раскрываются в свои разговоры ──
const sideState = {
  get open() { return store.get("side_projects") !== "0"; },
  set open(v) { store.set("side_projects", v ? "1" : "0"); },
  expanded: new Set((() => { try { return JSON.parse(store.get("side_expanded") || "[]"); } catch { return []; } })()),
  save() { store.set("side_expanded", JSON.stringify([...this.expanded])); },
  all: false,
};
function projectsSection() {
  const list = S.projects || [];
  const open = sideState.open;
  const head = h("div", { class: "side-sec" },
    h("button", { class: "side-sec-toggle", type: "button", "aria-expanded": String(open), onclick: () => { sideState.open = !open; renderSessions(); } },
      h("span", {}, "Проекты"), icon("chev", "chev" + (open ? " open" : ""))),
    h("button", { class: "icon-btn sm", type: "button", title: "Все проекты", "aria-label": "Все проекты", onclick: () => { toggleSidebar(false); openProjects(); } }, icon("folder")),
    h("button", { class: "icon-btn sm", type: "button", title: "Новый проект", "aria-label": "Новый проект", onclick: () => newProjectForm() }, icon("plus")));
  if (!open) return [head];
  const curProject = S.view?.startsWith("project:") ? S.view.slice(8) : S.sessions.find(x => x.id === S.sessionId)?.project_id;
  const shown = sideState.all ? list : list.slice(0, 5);
  const rows = shown.map(p => {
    const chats = S.sessions.filter(x => x.project_id === p.id);
    const exp = sideState.expanded.has(p.id) || (curProject === p.id && S.sessionId && chats.some(c => c.id === S.sessionId));
    const tog = h("button", { class: "proj-tog", type: "button", "aria-label": exp ? "Свернуть" : "Показать разговоры", title: exp ? "Свернуть" : "Разговоры проекта",
      onclick: e => { e.stopPropagation(); exp ? sideState.expanded.delete(p.id) : sideState.expanded.add(p.id); sideState.save(); renderSessions(); } },
      icon("folder", "fold"), icon("chev", "chev" + (exp ? " open" : "")));
    const item = h("div", { class: "session proj-item" + (S.view === "project:" + p.id ? " active" : curProject === p.id ? " within" : ""), role: "button", tabindex: "0", title: p.name,
      onclick: () => { toggleSidebar(false); openProject(p.id); }, onkeydown: e => { if (e.key === "Enter" && e.target === item) openProject(p.id); } },
      tog, h("span", {}, p.name), p.role !== "owner" || p.shared ? h("small", { class: "s-proj", title: p.role !== "owner" ? `Открыт вам: ${ROLE_NAMES[p.role]}` : "Общий проект" }, icon("users")) : null);
    if (!exp) return item;
    const sub = chats.slice(0, 8).map(c => h("div", { class: "session nested" + (c.id === S.sessionId ? " active" : ""), role: "button", tabindex: "0", title: c.title,
      onclick: () => { toggleSidebar(false); openSession(c.id); } }, h("span", {}, c.title), c.running ? h("i", { class: "dot" }) : null));
    return h("div", { class: "proj-group" }, item,
      sub.length ? sub : h("button", { class: "session nested new", type: "button", onclick: () => openProject(p.id) }, icon("plus"), h("span", {}, "Новый разговор")),
      chats.length > 8 ? h("button", { class: "side-more nested", type: "button", onclick: () => openProject(p.id) }, `Все ${chats.length}`) : null);
  });
  if (!list.length) rows.push(h("button", { class: "side-empty", type: "button", onclick: () => newProjectForm() }, icon("plus"), "Создать проект"));
  if (list.length > 5) rows.push(h("button", { class: "side-more", type: "button", onclick: () => { sideState.all = !sideState.all; renderSessions(); } },
    sideState.all ? "Свернуть" : `Ещё ${list.length - 5}`));
  return [head, ...rows, h("div", { class: "side-sec plain" }, h("span", { class: "side-sec-label" }, "Недавние"))];
}

function greeting() {
  const hr = new Date().getHours();
  const part = hr < 5 ? "Доброй ночи" : hr < 12 ? "Доброе утро" : hr < 18 ? "Добрый день" : "Добрый вечер";
  const name = S.me?.call_me || (S.me?.name || "").split(/\s+/)[0];
  return name && name !== S.me?.id ? `${part}, ${name}` : part;
}
function emptyState() {
  // Пустой разговор: приветствие и поле ввода по центру, примеры — под полем (как у Claude).
  const proj = S.projectId && S.project?.id === S.projectId ? S.project : null;
  const sugg = proj ? [] : S.config?.suggestions || [];
  $("#below").replaceChildren(
    sugg.length ? h("div", { class: "suggestions" }, sugg.map(t => h("button", { class: "suggestion", type: "button", onclick: () => send(t) },
      h("span", {}, t), icon("chev")))) : "",
    h("div", { class: "facts" },
      h("span", {}, icon("shield"), "Изменения — только после вашего подтверждения"),
      h("span", {}, icon("clip"), "Можно приложить файлы и фото"),
      h("span", {}, icon("send"), "Можно дописывать, пока агент работает")));
  setEmpty(true);
  return h("div", { class: "empty", id: "empty" },
    h("div", { class: "empty-mark" }, (S.config?.name || "A").trim().charAt(0).toUpperCase()),
    h("h1", {}, proj ? proj.name : greeting()),
    h("p", {}, proj ? (proj.description || "Агент видит инструкции, файлы и память проекта.")
      : S.config?.description || "Опишите задачу — агент сам найдёт, посчитает и подготовит."));
}

function resetView() {
  S.stream?.abort();
  S.steps.clear(); S.subs.clear(); S.cards.clear(); S.bubble = null; S.group = null; S.lastSeq = 0; setCost(0);
  box().replaceChildren(emptyState());
  S.tasksShown = false; renderTodos([]); toggleTasks(false); setRunning(false);
  S.queued = []; S.attachments = []; renderAttachments();
  if (S.preview) closePreview();
}

async function openSession(id) {
  resetView();
  S.sessionId = id;
  history.replaceState(null, "", `#${id}`);
  let s;
  try { s = await api(`/api/sessions/${id}`); } catch { S.sessionId = null; history.replaceState(null, "", "#"); return; }
  showPage(false);
  $("#chat-title").textContent = s.title || "Новый разговор";
  S.projectId = s.project?.id || null; setCrumb(s.project);
  if (s.state?.model) setModel(s.state.model);
  setMode(s.state?.mode || S.config.default_mode);
  if (!s.messages.length) box().replaceChildren(emptyState());
  for (const m of s.messages) {
    if (m.meta?.hidden) continue;
    if (m.role === "user") userMsg(m.content, m.seq);
    else if (m.role === "assistant") {
      if (m.content) { const b = assistantBubble(); renderMd(b, m.content); finishBubble(b, m.content, !!m.tool_calls?.length); }
      for (const tc of m.tool_calls || []) stepRow(tc.id, tc.function.name, safeJSON(tc.function.arguments));
    } else if (m.role === "tool") stepResult(m.tool_call_id, m.content, m.meta?.is_error, m.meta?.ui, m.meta?.elapsed_ms);
  }
  // Последний вопрос агента с вариантами — снова кнопками, если на него ещё не ответили.
  const last = s.messages.filter(m => !m.meta?.hidden).at(-1);
  if (last?.role === "tool" && last.name === "ask_user") {
    const w = (last.meta?.ui || []).find(x => x.type === "choices");
    if (w) add(choices(w.question, w.options || []));
  }
  for (const a of s.approvals) approvalCard({ ...a, approval_id: a.id, tool: a.tool_name });
  if (!s.running_run) closeGroup();
  renderTodos(s.state?.todos);
  setCost((s.runs || []).reduce((acc, r) => acc + (r.usage?.cost || 0), 0));
  S.lastSeq = s.last_seq;
  requestAnimationFrame(() => scroll(true));
  if (s.running_run) { setRunning(true); follow(); }
  loadSessions();
}

// ── общие элементы настроек ─────────────────────────────────────────
const row = (main, ...right) => h("div", { class: "row" }, h("div", { class: "grow" }, main), ...right);
function toggle(on, onChange, label) {
  const b = h("button", { type: "button", class: "switch", role: "switch", "aria-checked": String(!!on), "aria-label": label || "" });
  b.addEventListener("click", async e => {
    e.stopPropagation();
    on = !on; b.setAttribute("aria-checked", String(on));
    try { await onChange(on, b); } catch (err) { on = !on; b.setAttribute("aria-checked", String(on)); if (err.message) toast(err.message); }
  });
  return b;
}
const switchRow = (title, help, on, onChange) => h("div", { class: "switch-row" },
  h("div", {}, h("div", { class: "sr-title" }, title), help ? h("div", { class: "help" }, help) : null), toggle(on, onChange, title));
const emptyBlock = (ic, title, text, ...extra) => h("div", { class: "empty-block" },
  h("div", { class: "eb-icon" }, icon(ic)), h("b", {}, title), text ? h("p", {}, text) : null, ...extra);
function searchBox(placeholder, onInput) {
  const input = h("input", { type: "search", placeholder, "aria-label": placeholder });
  input.addEventListener("input", () => onInput(input.value.trim().toLowerCase()));
  return [h("label", { class: "search-box" }, icon("search"), input), input];
}
function chip(label, on, onChange, ic) {
  const b = h("button", { type: "button", class: "chip" + (on ? " on" : ""), "aria-pressed": String(!!on) }, ic ? icon(ic) : null, label);
  b.addEventListener("click", () => { on = !on; b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on)); onChange(on); });
  return b;
}
let toastTimer = null;
function toast(text, action) {
  document.querySelector(".toast")?.remove();
  const el = h("div", { class: "toast", role: "status" }, h("span", {}, text),
    action ? h("button", { class: "toast-btn", onclick: () => { el.remove(); action[1](); } }, action[0]) : null);
  document.body.append(el);
  clearTimeout(toastTimer); toastTimer = setTimeout(() => el.remove(), 6000);
}
function rel(ts) {
  const d = ts * 1000 - Date.now(), a = Math.abs(d) / 60000;
  const n = a < 60 ? Math.max(1, Math.round(a)) : a < 1440 ? Math.round(a / 60) : Math.round(a / 1440);
  const s = a < 1 ? "меньше минуты" : a < 60 ? `${n} мин` : a < 1440 ? `${n} ч` : `${n} ${plural(n, "день", "дня", "дней")}`;
  return d > 0 ? `через ${s}` : `${s} назад`;
}
const fmtWhen = ts => new Date(ts * 1000).toLocaleString("ru-RU", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const fmtDay = ts => new Date(ts * 1000).toLocaleDateString("ru-RU", { day: "numeric", month: "short", year: new Date(ts * 1000).getFullYear() === new Date().getFullYear() ? undefined : "numeric" });
function popMenu(btn, items) {
  closeMenus();
  const pop = $("#popover");
  pop.replaceChildren(...items.map(([ic, label, fn, danger]) => h("button", { class: "menu-item" + (danger ? " danger" : ""), role: "menuitem",
    onclick: () => { closeMenus(); fn(); } }, icon(ic), label)));
  const r = btn.getBoundingClientRect();
  pop.style.left = `${Math.max(8, Math.min(r.right - 200, innerWidth - 216))}px`; pop.style.top = `${r.bottom + 4}px`;
  pop.hidden = false; pop._owner = btn; btn.setAttribute("aria-expanded", "true");
}

// ── права агента ────────────────────────────────────────────────────
const MODES = {
  default: { icon: "ask", name: "Спрашивать", help: "Любое изменение — через карточку подтверждения" },
  accept_edits: { icon: "pen", name: "Правки без вопросов", help: "Файлы рабочей папки правит сам, остальное — с подтверждением" },
  plan: { icon: "map", name: "Только план", help: "Изучает и предлагает план, ничего не меняет" },
  readonly: { icon: "eye", name: "Только чтение", help: "Читает и ищет, ничего не меняет" },
  auto: { icon: "bolt", name: "Авто", help: "Меняет без вопросов, кроме необратимого", tone: "warn" },
  bypass: { icon: "unlock", name: "Без вопросов", help: "Всё без подтверждения — только для доверенных задач", tone: "err" },
};
const modeInfo = m => MODES[m] || { icon: "shield", name: m, help: "" };
function setMode(m, { save = false } = {}) {
  S.mode = m;
  const info = modeInfo(m);
  $("#mode-icon use").setAttribute("href", "#i-" + info.icon);
  $("#mode-name").textContent = info.name;
  $("#mode-btn").dataset.tone = info.tone || "";
  $("#mode-btn").title = `Права агента: ${info.name}. ${info.help}`;
  if (save && S.sessionId) api(`/api/sessions/${S.sessionId}`, { method: "PATCH", body: { mode: m } }).catch(e => toast(e.message));
}
function openModePicker() {
  const pop = $("#mode-pop"), btn = $("#mode-btn");
  if (!pop.hidden) { closeMenus(); return; }
  closeMenus();
  const items = S.config.modes.map(m => {
    const info = modeInfo(m);
    return h("button", { class: "mode-opt" + (m === S.mode ? " on" : ""), type: "button", role: "option", "aria-selected": String(m === S.mode),
      "data-tone": info.tone || "", onclick: () => { setMode(m, { save: true }); closeMenus(); btn.focus(); } },
      h("span", { class: "mode-ic" }, icon(info.icon)),
      h("span", { class: "mode-text" }, h("b", {}, info.name), h("span", {}, info.help)),
      m === S.mode ? h("span", { class: "mo-check" }, icon("check")) : null);
  });
  pop.replaceChildren(h("div", { class: "pop-label" }, "Права агента в этом разговоре"), ...items);
  const r = btn.getBoundingClientRect();
  pop.style.left = `${Math.max(8, Math.min(r.left, innerWidth - 336))}px`;
  pop.style.bottom = `${innerHeight - r.top + 6}px`;
  pop.hidden = false; btn.setAttribute("aria-expanded", "true");
  (pop.querySelector(".mode-opt.on") || pop.querySelector(".mode-opt"))?.focus();
  pop.onkeydown = e => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const all = [...pop.querySelectorAll(".mode-opt")], i = all.indexOf(document.activeElement);
    all[(i + (e.key === "ArrowDown" ? 1 : -1) + all.length) % all.length].focus();
  };
}

// ── модели ──────────────────────────────────────────────────────────
const VENDORS = { anthropic: "Anthropic", openai: "OpenAI", google: "Google", "x-ai": "xAI", "meta-llama": "Meta", mistralai: "Mistral",
  deepseek: "DeepSeek", qwen: "Qwen", moonshotai: "Moonshot", "z-ai": "Z.ai", yandex: "Яндекс", cohere: "Cohere", amazon: "Amazon",
  microsoft: "Microsoft", nvidia: "NVIDIA", perplexity: "Perplexity", minimax: "MiniMax", openrouter: "OpenRouter", ollama: "Ollama" };
const vendorName = v => VENDORS[v] || (v ? v.charAt(0).toUpperCase() + v.slice(1) : "");
const shortModel = id => id.replace(/^[^:/]+:/, "").replace(/^[^/]+\//, "");
const modelInfo = id => S.catalog?.models.find(m => m.id === id);
const modelName = id => modelInfo(id)?.name || shortModel(id);
const fmtCtx = n => !n ? "—" : n >= 1e6 ? `${+(n / 1e6).toFixed(1)}M` : `${Math.round(n / 1000)}K`;
const fmtUsd = x => x >= 10 ? x.toFixed(0) : x >= 1 ? (+x.toFixed(2)).toString() : (+x.toFixed(3)).toString();
const fmtPrice = m => m.price_in == null ? "—" : !m.price_in && !m.price_out ? "бесплатно" : `$${fmtUsd(m.price_in)} / $${fmtUsd(m.price_out)}`;
function caps(m) {
  return [m?.vision ? h("span", { class: "cap", title: "Понимает картинки" }, icon("eye")) : null,
    m?.reasoning ? h("span", { class: "cap", title: "Умеет рассуждать перед ответом" }, icon("spark")) : null];
}
function setModel(id, { save = false } = {}) {
  S.model = id;
  $("#model-name").textContent = modelName(id);
  $("#model-btn").title = `Модель: ${id}`;
  if (save && S.sessionId) api(`/api/sessions/${S.sessionId}`, { method: "PATCH", body: { model: id } }).catch(e => toast(e.message));
}
async function loadCatalog(refresh = false) {
  S.catalog = await api("/api/models" + (refresh ? "?refresh=1" : ""));
  if (S.model) setModel(S.model);
  return S.catalog;
}
const usable = () => (S.catalog?.models || S.config.models.map(id => ({ id, name: shortModel(id), vendor: "" }))).filter(m => m.allowed !== false);
async function savePrefs(patch) {
  const r = await api("/api/models/prefs", { method: "PUT", body: patch });
  if (S.catalog) Object.assign(S.catalog, r);
  S.config.models = r.favorites; S.config.default_model = r.default;
  return r;
}
async function toggleFav(id) {
  const favs = S.catalog?.favorites || S.config.models;
  const next = favs.includes(id) ? favs.filter(x => x !== id) : [...favs, id];
  await savePrefs({ favorites: next });
  return next.includes(id);
}
function starBtn(id, onDone) {
  const on = () => (S.catalog?.favorites || S.config.models).includes(id);
  const b = h("button", { type: "button", class: "star" + (on() ? " on" : ""), "aria-pressed": String(on()),
    title: on() ? "Убрать из избранных" : "В избранные" }, icon("star"));
  b.addEventListener("click", async e => {
    e.stopPropagation();
    try { const v = await toggleFav(id); b.classList.toggle("on", v); b.setAttribute("aria-pressed", String(v)); b.title = v ? "Убрать из избранных" : "В избранные"; onDone?.(v); }
    catch (err) { toast(err.message); }
  });
  return b;
}

function openModelPicker() {
  const pop = $("#model-pop"), btn = $("#model-btn");
  if (!pop.hidden) { closeMenus(); return; }
  closeMenus();
  if (!S.catalog) loadCatalog().then(() => !pop.hidden && draw()).catch(() => {});
  const input = h("input", { type: "search", placeholder: "Найти модель…", "aria-label": "Найти модель" });
  const list = h("div", { class: "model-list", role: "listbox" });
  let active = 0, opts = [];
  const option = m => {
    const el = h("div", { class: "model-opt" + (m.id === S.model ? " on" : ""), role: "option", "aria-selected": String(m.id === S.model),
      onclick: () => { setModel(m.id, { save: true }); closeMenus(); btn.focus(); } },
      h("div", { class: "mo-main" }, h("div", { class: "mo-name" }, h("span", {}, m.name || shortModel(m.id)), ...caps(m)),
        h("div", { class: "mo-meta" }, [vendorName(m.vendor), m.context_window ? fmtCtx(m.context_window) : "", m.price_in != null ? fmtPrice(m) : ""].filter(Boolean).join(" · "))),
      m.id === S.model ? h("span", { class: "mo-check" }, icon("check")) : null,
      starBtn(m.id, () => draw()));
    opts.push(el);
    return el;
  };
  const draw = () => {
    const q = input.value.trim().toLowerCase();
    const all = usable(), favs = S.catalog?.favorites || S.config.models;
    const only = S.catalog?.only_favorites;
    const byId = new Map(all.map(m => [m.id, m]));
    const favModels = favs.map(id => byId.get(id)).filter(Boolean);
    if (S.model && !favs.includes(S.model) && byId.has(S.model)) favModels.unshift(byId.get(S.model));
    opts = [];
    const out = [];
    const match = m => !q || `${m.name} ${m.id} ${vendorName(m.vendor)}`.toLowerCase().includes(q);
    const f = favModels.filter(match);
    if (f.length) out.push(h("div", { class: "pop-label" }, "Избранные"), ...f.map(option));
    if (!only) {
      const rest = all.filter(m => !favs.includes(m.id) && m.id !== S.model && match(m))
        .sort((a, b) => (b.created || 0) - (a.created || 0));
      const shown = rest.slice(0, q ? 80 : 30);
      if (shown.length) out.push(h("div", { class: "pop-label" }, q ? "Все модели" : "Новые в каталоге"), ...shown.map(option));
      if (rest.length > shown.length) out.push(h("div", { class: "pop-hint" }, `Ещё ${rest.length - shown.length} — уточните поиск`));
    }
    if (!out.length) out.push(h("div", { class: "pop-hint" }, q ? "Ничего не нашлось" : "Нет избранных моделей — отметьте их звёздочкой в настройках"));
    list.replaceChildren(...out);
    active = Math.max(0, opts.findIndex(o => o.classList.contains("on")));
    mark();
  };
  const mark = () => opts.forEach((o, i) => o.classList.toggle("active", i === active));
  input.addEventListener("input", () => { draw(); active = 0; mark(); });
  input.addEventListener("keydown", e => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault(); active = (active + (e.key === "ArrowDown" ? 1 : -1) + opts.length) % Math.max(1, opts.length); mark();
      opts[active]?.scrollIntoView({ block: "nearest" });
    }
    if (e.key === "Enter") { e.preventDefault(); opts[active]?.click(); }
  });
  pop.replaceChildren(h("label", { class: "pop-search" }, icon("search"), input), list,
    h("div", { class: "pop-foot" }, h("button", { class: "menu-item", onclick: () => { closeMenus(); openSettings("models"); } }, icon("gear"), "Настроить список моделей")));
  draw();
  const r = btn.getBoundingClientRect();
  pop.style.left = `${Math.max(8, Math.min(r.left, innerWidth - 376))}px`;
  pop.style.bottom = `${innerHeight - r.top + 6}px`;
  pop.hidden = false; btn.setAttribute("aria-expanded", "true");
  input.focus();
}

const MODEL_SORTS = [["new", "Новые"], ["name", "По названию"], ["cheap", "Дешевле"], ["ctx", "Больше контекст"]];
const settingLine = (title, help, control) => h("div", { class: "setting-line" },
  h("div", { class: "sl-text" }, h("div", { class: "sr-title" }, title), help ? h("div", { class: "help" }, help) : null), control);
async function modelsTab(body) {
  const c = await loadCatalog();
  const admin = S.config.is_admin;
  const st = { q: "", fav: false, vision: false, reasoning: false, free: false, vendor: "", sort: "new", limit: 100 };
  let custom = !!c.policy?.custom;
  const allowed = new Set(c.models.filter(m => m.allowed).map(m => m.id));
  let saveTimer = null;
  const savePolicy = () => {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(async () => {
      try { await api("/api/models/policy", { method: "PUT", body: { ids: custom ? [...allowed] : null } }); await loadCatalog(); }
      catch (e) { toast(e.message); }
    }, 350);
  };

  // по умолчанию и «только избранные»
  const defSel = h("select", { "aria-label": "Модель по умолчанию" });
  const fillDefault = () => {
    const ids = [...new Set([c.default, ...c.favorites])].filter(id => c.models.some(m => m.id === id && m.allowed));
    defSel.replaceChildren(...ids.map(id => h("option", { value: id }, `${modelName(id)}  ·  ${vendorName(modelInfo(id)?.vendor)}`)));
    defSel.value = c.default;
  };
  fillDefault();
  defSel.addEventListener("change", async () => { try { await savePrefs({ default: defSel.value }); setModel(defSel.value); toast("Модель по умолчанию сохранена"); } catch (e) { toast(e.message); } });

  // каталог
  const list = h("div", { class: "mtable", role: "table" });
  const vendors = [...new Set(c.models.map(m => m.vendor))].sort((a, b) => vendorName(a).localeCompare(vendorName(b)));
  const vendorSel = h("select", { class: "mini-select", "aria-label": "Компания" }, h("option", { value: "" }, "Все компании"),
    ...vendors.map(v => h("option", { value: v }, `${vendorName(v)} · ${c.models.filter(m => m.vendor === v).length}`)));
  vendorSel.addEventListener("change", () => { st.vendor = vendorSel.value; st.limit = 100; draw(); });
  const sortSel = h("select", { class: "mini-select", "aria-label": "Сортировка" }, ...MODEL_SORTS.map(([v, l]) => h("option", { value: v }, l)));
  sortSel.addEventListener("change", () => { st.sort = sortSel.value; draw(); });
  const [search] = searchBox(`Найти среди ${c.models.length} моделей`, q => { st.q = q; st.limit = 100; draw(); });
  const filters = h("div", { class: "toolbar" }, search, vendorSel, sortSel,
    h("div", { class: "chips" },
      chip("Избранные", false, v => { st.fav = v; draw(); }, "star"),
      chip("Картинки", false, v => { st.vision = v; draw(); }, "eye"),
      chip("Рассуждения", false, v => { st.reasoning = v; draw(); }, "spark"),
      chip("Бесплатные", false, v => { st.free = v; draw(); })));
  const bulk = h("div", { class: "bulk" });

  const draw = () => {
    const favs = new Set(S.catalog.favorites);
    let items = c.models.filter(m => (!st.q || `${m.name} ${m.id} ${vendorName(m.vendor)}`.toLowerCase().includes(st.q))
      && (!st.fav || favs.has(m.id)) && (!st.vision || m.vision) && (!st.reasoning || m.reasoning)
      && (!st.free || (m.price_in === 0 && m.price_out === 0)) && (!st.vendor || m.vendor === st.vendor));
    const cmp = { new: (a, b) => (b.created || 0) - (a.created || 0), name: (a, b) => a.name.localeCompare(b.name),
      cheap: (a, b) => (a.price_in ?? 1e9) - (b.price_in ?? 1e9), ctx: (a, b) => (b.context_window || 0) - (a.context_window || 0) }[st.sort];
    items.sort((a, b) => (favs.has(b.id) - favs.has(a.id)) || cmp(a, b));
    const total = items.length;
    const head = h("div", { class: "mrow mhead" + (admin && custom ? " with-access" : ""), role: "row" },
      h("span", {}), h("span", {}, "Модель"), h("span", { class: "c-vendor" }, "Компания"), h("span", { class: "c-num" }, "Контекст"),
      h("span", { class: "c-num", title: "Долларов за миллион токенов: вход / выход" }, "Цена за 1M"), admin && custom ? h("span", { class: "c-num" }, "Доступ") : null);
    const rows = items.slice(0, st.limit).map(m => { const r = h("div", { class: "mrow" + (admin && custom ? " with-access" : "") + (admin && custom && !allowed.has(m.id) ? " off" : ""), role: "row", title: m.description || "" },
      starBtn(m.id, () => fillDefault()),
      h("div", { class: "mrow-main" }, h("div", { class: "mrow-name" }, h("span", {}, m.name), ...caps(m),
        m.id === c.main ? h("span", { class: "tag" }, "основная") : null),
        h("div", { class: "mrow-id" }, m.id.replace(/^openrouter:/, ""))),
      h("span", { class: "c-vendor" }, vendorName(m.vendor)),
      h("span", { class: "c-num" }, fmtCtx(m.context_window)),
      h("span", { class: "c-num price" }, fmtPrice(m)),
      admin && custom ? h("span", { class: "c-num" }, m.id === c.main ? h("span", { class: "help" }, "всегда") : toggle(allowed.has(m.id), on => {
        on ? allowed.add(m.id) : allowed.delete(m.id); m.allowed = on;
        r.classList.toggle("off", !on); savePolicy();
      }, "Доступна всем")) : null); return r; });
    list.replaceChildren(head, ...rows,
      total > st.limit ? h("button", { class: "btn ghost block more-btn", onclick: () => { st.limit += 200; draw(); } }, `Показать ещё ${Math.min(200, total - st.limit)} из ${total - st.limit}`) : null,
      !total ? emptyBlock("search", "Ничего не нашлось", "Попробуйте другое название или снимите фильтры") : null);
    bulk.replaceChildren(admin && custom && total ? h("div", { class: "bulk-bar" },
      h("span", { class: "help" }, `Доступно ${allowed.size} из ${c.models.length}. Найдено: ${total}`),
      h("button", { class: "btn sm", onclick: () => { items.forEach(m => allowed.add(m.id)); draw(); savePolicy(); } }, "Открыть найденные"),
      h("button", { class: "btn sm", onclick: () => { items.forEach(m => m.id !== c.main && allowed.delete(m.id)); draw(); savePolicy(); } }, "Скрыть найденные")) : "");
  };

  const fetched = c.fetched_at ? `обновлён ${rel(c.fetched_at)}` : "ещё не загружен";
  const errors = Object.entries(c.policy?.errors || {});
  body.append(
    group(null, null,
      settingLine("Модель по умолчанию", "С неё начинается новый разговор", defSel),
      settingLine("Только избранные", "Остальные модели не предлагаются в чате", toggle(c.only_favorites, async on => { await savePrefs({ only_favorites: on }); }, "Только избранные")),
      admin ? settingLine("Доступ для пользователей", c.policy?.patterns ? h("span", {}, "Сейчас по AGENT_MODELS_ALLOW: ", h("code", {}, c.policy.patterns))
        : "Основная модель доступна всегда — на ней работают сжатие истории и расписание",
      segmented([["all", c.policy?.patterns ? "По настройке" : "Все модели"], ["custom", "Только отмеченные"]], custom ? "custom" : "all", v => {
        custom = v === "custom";
        if (custom && allowed.size === c.models.length) { allowed.clear(); [c.main, ...c.favorites].forEach(id => allowed.add(id)); }
        draw(); savePolicy();
      })) : null),
    group(null, null,
      h("div", { class: "group-head" }, h("h4", {}, "Каталог"),
        h("span", { class: "help" }, `${c.models.length} ${plural(c.models.length, "модель", "модели", "моделей")} с поддержкой инструментов · ${fetched}`),
        admin ? h("button", { class: "btn ghost sm", onclick: async e => { e.currentTarget.disabled = true; await loadCatalog(true); openSettings("models"); } }, icon("refresh"), "Обновить") : null),
      errors.length ? h("div", { class: "inline-alert" }, errors.map(([p, e]) => `${p}: ${e}`).join("; ")) : null,
      filters, bulk, list));
  draw();
}

// ── память ──────────────────────────────────────────────────────────
const MEMORY_EXAMPLES = ["Отвечай коротко, по пунктам", "Суммы — в рублях с пробелами: 12 500 ₽", "Наш основной склад — в Кирове"];
async function memoryTab(body) {
  const items = await api("/api/memory");
  const admin = S.config.is_admin;
  const own = m => m.scope === `user:${S.me.id}`;
  let scope = "user", filter = "all", q = "";
  const input = h("textarea", { rows: 2, placeholder: "Что запомнить? Например: «отчёты присылай по пятницам»" });
  const addBtn = h("button", { class: "btn primary sm", type: "button", disabled: true }, "Запомнить");
  input.addEventListener("input", () => { addBtn.disabled = !input.value.trim(); input.style.height = "auto"; input.style.height = input.scrollHeight + 2 + "px"; });
  const add = async () => {
    const text = input.value.trim(); if (!text) return;
    addBtn.disabled = true;
    try { await api("/api/memory", { method: "POST", body: { text, scope } }); openSettings("memory"); } catch (e) { toast(e.message); addBtn.disabled = false; }
  };
  addBtn.addEventListener("click", add);
  input.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); add(); } });
  const compose = h("div", { class: "compose-card" }, input, h("div", { class: "compose-bar" },
    admin ? segmented([["user", "Только мне"], ["global", "Всем пользователям"]], "user", v => (scope = v)) : h("span", { class: "help" }, "Видно только вам"),
    h("span", { class: "grow" }), h("span", { class: "help kbd-hint" }, "Ctrl + Enter"), addBtn));

  const list = h("div", { class: "mem-list" });
  const card = m => {
    const canEdit = own(m) || admin;
    const text = h("div", { class: "mem-text" }, m.text);
    const el = h("div", { class: "mem-card" }, h("div", { class: "mem-dot " + (m.scope === "global" ? "shared" : "") }), h("div", { class: "mem-body" }, text,
      h("div", { class: "mem-meta" }, h("span", { class: "tag" + (m.scope === "global" ? " shared" : "") }, m.scope === "global" ? "для всех" : "только мне"),
        m.created_at ? h("span", {}, fmtDay(m.created_at)) : null,
        m.scope === "global" && m.created_by ? h("span", {}, `добавил ${m.created_by}`) : null)),
      canEdit ? h("div", { class: "mem-actions" },
        h("button", { class: "icon-btn", title: "Изменить", "aria-label": "Изменить", onclick: () => edit() }, icon("pen")),
        h("button", { class: "icon-btn", title: "Удалить", "aria-label": "Удалить", onclick: () => remove() }, icon("trash"))) : null);
    const edit = () => {
      const ta = h("textarea", { rows: 3 }); ta.value = m.text;
      const done = () => text.replaceChildren(m.text);
      const save = async () => {
        const v = ta.value.trim(); if (!v || v === m.text) return done();
        try { await api(`/api/memory/${m.id}`, { method: "PATCH", body: { text: v } }); m.text = v; done(); } catch (e) { toast(e.message); }
      };
      ta.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) save(); if (e.key === "Escape") { e.stopPropagation(); done(); } });
      text.replaceChildren(h("div", { class: "mem-edit" }, ta, h("div", { class: "compose-bar" }, h("span", { class: "grow" }),
        h("button", { class: "btn ghost sm", onclick: done }, "Отмена"), h("button", { class: "btn primary sm", onclick: save }, "Сохранить"))));
      ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
    };
    const remove = async () => {
      try { await api(`/api/memory/${m.id}`, { method: "DELETE" }); } catch (e) { toast(e.message); return; }
      items.splice(items.indexOf(m), 1); draw();
      toast("Запись удалена", ["Вернуть", async () => { await api("/api/memory", { method: "POST", body: { text: m.text, scope: m.scope === "global" ? "global" : "user" } }); openSettings("memory"); }]);
    };
    return el;
  };
  const seg = h("div");
  const draw = () => {
    const mine = items.filter(own).length, shared = items.filter(m => m.scope === "global").length;
    seg.replaceChildren(segmented([["all", `Все · ${items.length}`], ["mine", `Мои · ${mine}`], ["global", `Общие · ${shared}`]], filter, v => { filter = v; draw(); }));
    const shown = items.filter(m => (filter === "all" || (filter === "mine" ? own(m) : m.scope === "global")) && (!q || m.text.toLowerCase().includes(q)))
      .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
    list.replaceChildren(...shown.map(card));
    if (!items.length) list.replaceChildren(emptyBlock("memory", "Пока ничего не запомнено",
      "Правила и факты отсюда агент учитывает в каждом разговоре. Можно и прямо в чате: «запомни: …».",
      h("div", { class: "examples" }, MEMORY_EXAMPLES.map(x => h("button", { class: "chip", type: "button", onclick: () => { input.value = x; input.dispatchEvent(new Event("input")); input.focus(); } }, x)))));
    else if (!shown.length) list.replaceChildren(h("p", { class: "muted center" }, "Ничего не нашлось"));
  };
  const [search] = searchBox("Поиск по памяти", v => { q = v; draw(); });
  body.append(h("p", { class: "lead-text tab-lead" }, "Агент помнит это во всех разговорах. Добавляйте правила, факты о работе и предпочтения."),
    compose, items.length ? h("div", { class: "toolbar" }, search, seg) : null, list);
  draw();
}

// ── инструменты ─────────────────────────────────────────────────────
const TOOL_GROUPS = { core: "Основные", files: "Файлы", web: "Интернет", memory: "Память", shell: "Команды" };
const openSources = new Set(["domain"]);
// ── свой MCP-сервер ─────────────────────────────────────────────────
const slug = v => v.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 32);
function nameFromUrl(u) {
  try {
    const x = new URL(u);
    // localhost и IP ничего не говорят о сервере — берём первую часть пути (/crm/mcp → crm).
    if (/^(localhost|[\d.]+|\[.*\])$/.test(x.hostname)) return slug(x.pathname.split("/").find(p => p && p !== "mcp") || "");
    return slug(x.hostname.replace(/^(www|mcp|api)\./, "").split(".")[0] || "");
  } catch { return ""; }
}
function parseHeaders(text) {
  const out = {};
  for (const line of text.split("\n")) {
    const i = line.indexOf(":");
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}
function mcpEditor(srv, onClose) {
  const editing = !!srv;
  let auth = srv?.auth || "none", deferred = srv?.deferred ?? null, nameTouched = editing;
  const name = h("input", { value: srv?.name || "", placeholder: "crm", spellcheck: "false", autocomplete: "off" });
  const url = h("input", { value: srv?.address || "", placeholder: "https://example.com/mcp", spellcheck: "false", autocomplete: "off", inputmode: "url" });
  const token = h("input", { type: "password", autocomplete: "new-password",
    placeholder: editing && srv.has_token ? "Токен сохранён — оставьте пустым, чтобы не менять" : "Bearer-токен сервера" });
  const headers = h("textarea", { rows: 2, spellcheck: "false", class: "mono",
    placeholder: srv?.header_names?.length ? `Заданы: ${srv.header_names.join(", ")} — впишите заново, чтобы изменить` : "X-Api-Key: …\nX-Workspace: …" });
  const result = h("div", { class: "test-result", hidden: true });
  name.addEventListener("input", () => { nameTouched = true; });
  url.addEventListener("input", () => { if (!nameTouched) name.value = nameFromUrl(url.value.trim()); result.hidden = true; });
  const tokenRow = fieldRow("Токен", token, "Хранится в базе сервиса и никому не показывается");
  const syncAuth = () => { tokenRow.hidden = auth !== "token"; };
  const payload = () => ({
    url: url.value.trim(), auth,
    token: auth === "token" ? (token.value.trim() || (editing && srv.has_token ? null : "")) : "",
    headers: headers.value.trim() ? parseHeaders(headers.value) : (editing ? null : {}),
  });
  const test = h("button", { class: "btn sm", type: "button", onclick: async () => {
    test.disabled = true; result.hidden = false; result.className = "test-result"; result.replaceChildren(h("span", { class: "spin" }), "Проверяю…");
    try {
      const r = await api("/api/mcp/test", { method: "POST", body: { ...payload(), name: editing ? srv.name : null } });
      result.className = "test-result " + (r.ok ? "ok" : r.needs_auth ? "warn" : "err");
      result.replaceChildren(icon(r.ok ? "check" : r.needs_auth ? "lock" : "x"), h("div", {},
        h("b", {}, r.ok ? `Отвечает${r.server ? ` · ${r.server}` : ""}${r.version ? ` v${r.version}` : ""} · ${r.count} ${plural(r.count, "инструмент", "инструмента", "инструментов")}`
          : r.needs_auth ? (auth === "oauth" ? "Сервер просит вход — после сохранения нажмите «Подключить»" : "Сервер просит вход — выберите OAuth или токен") : "Не отвечает"),
        r.ok && r.tools.length ? h("div", { class: "help" }, r.tools.slice(0, 12).join(", ") + (r.count > 12 ? "…" : "")) : null,
        !r.ok && r.error && !r.needs_auth ? h("div", { class: "help" }, r.error) : null));
    } catch (e) { result.className = "test-result err"; result.replaceChildren(icon("x"), e.message); }
    finally { test.disabled = false; }
  } }, icon("plug"), "Проверить");
  const save = h("button", { class: "btn primary sm", type: "button", onclick: async () => {
    save.disabled = true;
    try {
      const r = await api("/api/mcp/servers", { method: "POST", body: { ...payload(), name: slug(name.value), deferred, enabled: srv?.enabled ?? true,
        original_name: editing ? srv.name : null } });
      const st = r.status;
      toast(st?.ok ? `«${slug(name.value)}» подключён: ${st.tools} ${plural(st.tools, "инструмент", "инструмента", "инструментов")}`
        : st?.needs_auth ? "Сохранено — нажмите «Подключить», чтобы войти" : `Сохранено, но сервер не отвечает${st?.error ? `: ${st.error}` : ""}`);
      openSources.add("mcp:" + slug(name.value));
      onClose(true);
    } catch (e) { toast(e.message); save.disabled = false; }
  } }, editing ? "Сохранить" : "Добавить");
  const el = h("div", { class: "sch-editor mcp-editor" },
    h("div", { class: "ed-head" }, h("b", {}, editing ? `Сервер ${srv.name}` : "Новый MCP-сервер"),
      h("button", { class: "icon-btn", type: "button", "aria-label": "Закрыть", onclick: () => onClose(false) }, icon("x"))),
    h("div", { class: "two" }, fieldRow("Адрес", url, "Streamable HTTP, обычно оканчивается на /mcp"), fieldRow("Имя", name, "Латиницей — так инструменты будут подписаны у агента")),
    h("div", { class: "field-row" }, h("label", {}, "Вход"),
      segmented([["none", "Без входа"], ["token", "Токен"], ["oauth", "OAuth — войти в браузере"]], auth, v => { auth = v; syncAuth(); result.hidden = true; })),
    tokenRow,
    h("details", { class: "more-opts" }, h("summary", {}, "Дополнительно"),
      fieldRow("Заголовки", headers, "По одному на строку: «Имя: значение»"),
      h("div", { class: "field-row" }, h("label", {}, "Описания инструментов"),
        segmented([[null, "Авто"], [false, "Сразу в промпте"], [true, "По запросу"]], deferred, v => { deferred = v; }),
        h("div", { class: "help" }, "«По запросу» экономит контекст, если у сервера много инструментов. Авто — так, если их больше 15."))),
    result,
    h("div", { class: "compose-bar" }, test, h("span", { class: "grow" }), h("button", { class: "btn ghost sm", type: "button", onclick: () => onClose(false) }, "Отмена"), save));
  syncAuth();
  setTimeout(() => (editing ? url : url).focus(), 0);
  return el;
}
async function toolsTab(body) {
  const d = await api("/api/tools");
  const admin = S.config.is_admin;
  let q = "";
  const access = t => t.read_only ? ["eye", "чтение", "ok"] : t.destructive ? ["shield", "необратимо", "err"] : ["pen", "изменяет", "warn"];
  const toolRow = t => {
    const [ic, label, cls] = access(t);
    return h("div", { class: "tool-row" },
      h("div", { class: "tr-main" }, h("div", { class: "tr-title" }, h("span", {}, toolTitle(t.name, t.title)), h("code", {}, t.name)),
        t.description ? h("div", { class: "tr-desc" }, t.description.replace(/^\[[^\]]+\]\s*/, "")) : null),
      h("div", { class: "tr-tags" }, t.deferred ? h("span", { class: "tag", title: "Описание подгружается, когда агенту понадобится инструмент — экономит контекст" }, "по запросу") : null,
        h("span", { class: "tag " + cls, title: { ok: "Только читает данные", warn: "Изменяет данные — спросит подтверждение", err: "Необратимое действие — всегда с подтверждением" }[cls] }, icon(ic), label)));
  };
  const card = ({ key, ic, title, sub, status, actions, tools, extra }) => {
    const matched = tools.filter(t => !q || `${t.name} ${t.title} ${t.description}`.toLowerCase().includes(q));
    if (q && !matched.length && !title.toLowerCase().includes(q)) return null;
    const open = q ? true : openSources.has(key);
    const el = h("div", { class: "src-card" + (open ? " open" : "") });
    const headBtn = h("button", { class: "src-head", type: "button", "aria-expanded": String(open), onclick: () => {
      const o = !el.classList.contains("open"); el.classList.toggle("open", o); headBtn.setAttribute("aria-expanded", String(o));
      o ? openSources.add(key) : openSources.delete(key);
    } },
      h("span", { class: "src-icon" }, icon(ic)),
      h("span", { class: "src-title" }, h("b", {}, title), h("span", { class: "src-sub" }, sub)),
      status || null,
      (tools.length || extra) ? h("span", { class: "src-chev" }, icon("chev")) : null);
    el.append(h("div", { class: "src-top" }, headBtn, actions ? h("div", { class: "src-actions" }, actions) : null),
      h("div", { class: "src-body" }, extra || null, ...matched.map(toolRow)));
    return el;
  };
  const count = n => `${n} ${plural(n, "инструмент", "инструмента", "инструментов")}`;
  const pill = (cls, text) => h("span", { class: "pill " + cls }, h("i"), text);
  const cards = h("div", { class: "src-list" });
  const draw = () => {
    const domain = d.tools.filter(t => t.source === "domain");
    const builtin = d.tools.filter(t => t.source === "builtin");
    const out = [];
    if (domain.length) out.push(card({ key: "domain", ic: "cube", title: "Инструменты агента", sub: count(domain.length), tools: domain }));
    if (d.mcp.length || admin) out.push(h("div", { class: "src-label" }, h("span", {}, "MCP-серверы"),
      admin ? h("button", { class: "btn ghost sm", type: "button", onclick: () => openEditor(null) }, icon("plus"), "Добавить сервер") : null));
    for (const s of d.mcp) {
      const tools = d.tools.filter(t => t.group === `mcp:${s.name}`);
      const acts = [];
      if (admin && s.enabled && s.needs_auth) acts.push(h("button", { class: "btn primary sm", onclick: () => mcpConnect(s.name) }, icon("plug"), "Подключить"));
      if (admin && s.custom) {
        const more = h("button", { class: "icon-btn", title: "Ещё", "aria-label": "Действия", "aria-haspopup": "menu" }, icon("dots"));
        more.addEventListener("click", e => { e.stopPropagation(); popMenu(more, [
          ["pen", "Изменить", () => openEditor(s)],
          s.oauth ? ["logout", "Выйти из аккаунта", async () => { await api(`/api/mcp/${encodeURIComponent(s.name)}/connect`, { method: "DELETE" }); openSettings("tools"); }] : null,
          [s.enabled ? "pause" : "play", s.enabled ? "Выключить" : "Включить", async () => {
            await api(`/api/mcp/servers/${encodeURIComponent(s.name)}`, { method: "PATCH", body: { enabled: !s.enabled } }); openSettings("tools"); }],
          ["trash", "Удалить", async () => {
            if (!confirm(`Удалить сервер ${s.name}? Его инструменты пропадут у агента.`)) return;
            await api(`/api/mcp/servers/${encodeURIComponent(s.name)}`, { method: "DELETE" }); openSettings("tools"); }, true],
        ].filter(Boolean)); });
        acts.push(more);
      } else if (admin && s.oauth) acts.push(h("button", { class: "btn ghost sm", onclick: async () => {
        if (!confirm(`Отключить ${s.name}? Токены будут отозваны.`)) return;
        await api(`/api/mcp/${encodeURIComponent(s.name)}/connect`, { method: "DELETE" }); openSettings("tools"); } }, "Отключить"));
      const sub = !s.enabled ? "Выключен — агент его не видит"
        : s.ok ? [count(s.tools), s.server && s.server !== s.name ? s.server : "", s.version ? `v${s.version}` : "", s.oauth ? "OAuth" : ""].filter(Boolean).join(" · ")
        : s.needs_auth ? "Нужно войти — подключение по OAuth" : (s.error || "нет связи");
      const c = card({ key: "mcp:" + s.name, ic: s.transport === "stdio" ? "tools" : "server", title: s.name, sub, tools,
        actions: acts.length ? acts : null,
        status: !s.enabled ? pill("", "выключен") : s.ok ? pill("ok", "на связи") : s.needs_auth ? pill("warn", "нужен вход") : pill("err", "ошибка"),
        extra: admin && s.address ? h("div", { class: "src-addr" }, h("code", {}, s.address), h("span", { class: "tag" }, s.custom ? "добавлен в настройках" : "mcp.json")) : null });
      c?.classList.toggle("disabled", !s.enabled);
      out.push(c);
    }
    if (admin && !d.mcp.length && !q) out.push(h("button", { class: "add-card", type: "button", onclick: () => openEditor(null) },
      h("span", { class: "src-icon" }, icon("plus")), h("span", { class: "src-title" }, h("b", {}, "Подключить MCP-сервер"),
        h("span", { class: "src-sub" }, "CRM, склад, почта, база знаний — всё, что умеет MCP, станет инструментами агента"))));
    if (builtin.length) {
      const groups = [...new Set(builtin.map(t => t.group || "core"))];
      out.push(card({ key: "builtin", ic: "gear", title: "Встроенные", sub: `${count(builtin.length)} · ${groups.map(g => TOOL_GROUPS[g] || g).join(", ")}`, tools: builtin }));
    }
    if (d.skills.length) {
      const matched = d.skills.filter(k => !q || `${k.name} ${k.description}`.toLowerCase().includes(q));
      if (!q || matched.length) out.push(card({ key: "skills", ic: "book", title: "Навыки", sub: `${d.skills.length} ${plural(d.skills.length, "навык", "навыка", "навыков")} — длинные инструкции, которые агент открывает по необходимости`, tools: [],
        extra: h("div", {}, matched.map(k => h("div", { class: "tool-row" }, h("div", { class: "tr-main" }, h("div", { class: "tr-title" }, h("span", {}, k.name)), h("div", { class: "tr-desc" }, k.description))))) }));
    }
    cards.replaceChildren(...out.filter(Boolean));
    if (!cards.childElementCount) cards.append(h("p", { class: "muted center" }, "Ничего не нашлось"));
  };
  const okMcp = d.mcp.filter(s => s.ok).length;
  const slot = h("div");
  const openEditor = srv => {
    slot.replaceChildren(mcpEditor(srv, saved => { slot.replaceChildren(); if (saved) openSettings("tools"); }));
    slot.scrollIntoView({ block: "nearest", behavior: "smooth" });
  };
  const [search] = searchBox(`Найти среди ${d.tools.length} инструментов`, v => { q = v; draw(); });
  body.append(
    h("div", { class: "tiles" },
      h("div", { class: "tile" }, h("b", {}, d.tools.length), h("span", {}, plural(d.tools.length, "инструмент", "инструмента", "инструментов"))),
      h("div", { class: "tile" }, h("b", {}, d.mcp.length ? `${okMcp}/${d.mcp.length}` : "0"), h("span", {}, "MCP-серверов на связи")),
      h("div", { class: "tile" }, h("b", {}, d.tools.filter(t => !t.read_only).length), h("span", {}, "изменяют данные — с подтверждением"))),
    h("div", { class: "toolbar" }, search, admin ? h("button", { class: "btn sm", title: "Перечитать mcp.json и папку навыков без перезапуска", onclick: async e => {
      e.currentTarget.disabled = true; await api("/api/reload", { method: "POST" }); openSettings("tools"); toast("Инструменты перечитаны"); } }, icon("refresh"), "Перечитать") : null),
    slot, cards);
  draw();
}

// ── расписание ──────────────────────────────────────────────────────
const WEEKDAYS = [[1, "Пн"], [2, "Вт"], [3, "Ср"], [4, "Чт"], [5, "Пт"], [6, "Сб"], [0, "Вс"]];
const WD_NAMES = { 0: "вс", 1: "пн", 2: "вт", 3: "ср", 4: "чт", 5: "пт", 6: "сб", 7: "вс" };
const pad = n => String(n).padStart(2, "0");
function parseCron(cron) {
  const c = (cron || "").trim().replace(/\s+/g, " ");
  let m;
  if ((m = c.match(/^(\d+) (\d+) \* \* \*$/))) return { freq: "daily", time: `${pad(m[2])}:${pad(m[1])}` };
  if ((m = c.match(/^(\d+) (\d+) \* \* 1-5$/))) return { freq: "weekdays", time: `${pad(m[2])}:${pad(m[1])}` };
  if ((m = c.match(/^(\d+) (\d+) \* \* ([0-7](?:,[0-7])*)$/))) return { freq: "weekly", time: `${pad(m[2])}:${pad(m[1])}`, days: m[3].split(",").map(x => +x % 7) };
  if ((m = c.match(/^(\d+) \* \* \* \*$/))) return { freq: "hourly", minute: +m[1] };
  if ((m = c.match(/^(\d+) (\d+) (\d+) \* \*$/))) return { freq: "monthly", time: `${pad(m[2])}:${pad(m[1])}`, dom: +m[3] };
  return { freq: "custom", cron: c };
}
function buildCron(st) {
  const [hh, mm] = (st.time || "09:00").split(":").map(Number);
  switch (st.freq) {
    case "daily": return `${mm} ${hh} * * *`;
    case "weekdays": return `${mm} ${hh} * * 1-5`;
    case "weekly": return `${mm} ${hh} * * ${(st.days?.length ? [...st.days].sort((a, b) => ((a || 7) - (b || 7))) : [1]).join(",")}`;
    case "hourly": return `${st.minute || 0} * * * *`;
    case "monthly": return `${mm} ${hh} ${st.dom || 1} * *`;
    default: return st.cron || "";
  }
}
function describeCron(cron) {
  const p = parseCron(cron), t = p.time?.replace(/^0(\d)/, "$1");
  switch (p.freq) {
    case "daily": return `Каждый день в ${t}`;
    case "weekdays": return `По будням в ${t}`;
    case "weekly": return p.days.length === 7 ? `Каждый день в ${t}` : `По ${[...p.days].sort((a, b) => (a || 7) - (b || 7)).map(d => WD_NAMES[d]).join(", ")} в ${t}`;
    case "hourly": return `Каждый час в :${pad(p.minute)}`;
    case "monthly": return `Каждый месяц ${p.dom}-го в ${t}`;
    default: return `По расписанию ${cron}`;
  }
}
const SCHEDULE_EXAMPLES = [
  { name: "Утренняя сводка", cron: "0 9 * * 1-5", prompt: "Собери, что важно сделать сегодня, и пришли коротким списком." },
  { name: "Итоги недели", cron: "0 17 * * 5", prompt: "Подведи итоги недели: что сделано, что зависло, что запланировать." },
];
function scheduleEditor(rec, onClose) {
  const st = parseCron(rec?.cron || "0 9 * * 1-5");
  if (st.freq === "custom" && !rec?.cron) st.freq = "weekdays";
  const name = h("input", { value: rec?.name || "", placeholder: "Например: утренняя сводка" });
  const prompt = h("textarea", { rows: 3, placeholder: "Что сделать агенту — так же, как написали бы в чате" });
  prompt.value = rec?.prompt || "";
  const time = h("input", { type: "time", value: st.time || "09:00", "aria-label": "Время" });
  const minute = h("input", { type: "number", min: 0, max: 59, value: st.minute ?? 0, class: "num", "aria-label": "Минута" });
  const dom = h("input", { type: "number", min: 1, max: 28, value: st.dom ?? 1, class: "num", "aria-label": "Число месяца" });
  const cron = h("input", { value: rec?.cron || "", placeholder: "мин час день месяц день_недели", class: "mono", "aria-label": "Cron" });
  const days = new Set(st.days || [1]);
  const dayChips = h("div", { class: "day-chips" }, WEEKDAYS.map(([v, l]) => chip(l, days.has(v), on => { on ? days.add(v) : days.delete(v); update(); })));
  const controls = h("div", { class: "when-controls" });
  const preview = h("div", { class: "when-preview" });
  let keep = !!rec?.session_id;
  const current = () => buildCron({ ...st, time: time.value, minute: +minute.value, dom: +dom.value, days: [...days], cron: cron.value });
  let timer = null;
  const update = () => {
    const parts = { daily: [time], weekdays: [time], weekly: [dayChips, h("span", { class: "help" }, "в"), time], hourly: [h("span", { class: "help" }, "в минуту"), minute],
      monthly: [dom, h("span", { class: "help" }, "числа в"), time], custom: [cron, h("a", { href: "https://crontab.guru", target: "_blank", rel: "noopener", class: "help" }, "подсказка")] }[st.freq];
    if (controls._freq !== st.freq) { controls.replaceChildren(...parts); controls._freq = st.freq; }
    const c = current();
    if (st.freq !== "custom") cron.value = c;
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (!c.trim()) { preview.replaceChildren(); return; }
      const r = await api(`/api/schedules/preview?cron=${encodeURIComponent(c)}`).catch(() => ({ ok: false }));
      preview.className = "when-preview" + (r.ok ? "" : " bad");
      preview.replaceChildren(r.ok ? h("span", {}, h("b", {}, describeCron(c)), " · ближайшие: ", r.next.map(fmtWhen).join(" · ")) : "Не получается разобрать расписание — 5 полей: минута, час, день, месяц, день недели");
    }, 200);
  };
  for (const el of [time, minute, dom, cron]) el.addEventListener("input", update);
  const save = h("button", { class: "btn primary sm", type: "button", onclick: async () => {
    save.disabled = true;
    try {
      await api("/api/schedules", { method: "POST", body: { id: rec?.id || null, name: name.value, cron: current(), prompt: prompt.value, enabled: rec?.enabled ?? true, keep_session: keep } });
      onClose(true);
    } catch (e) { toast(e.message); save.disabled = false; }
  } }, rec?.id ? "Сохранить" : "Создать");
  const el = h("div", { class: "sch-editor" },
    h("div", { class: "ed-head" }, h("b", {}, rec?.id ? "Изменить задачу" : "Новая задача"), h("button", { class: "icon-btn", "aria-label": "Закрыть", onclick: () => onClose(false) }, icon("x"))),
    fieldRow("Название", name),
    fieldRow("Задание для агента", prompt),
    h("div", { class: "field-row" }, h("label", {}, "Когда"),
      segmented([["daily", "Каждый день"], ["weekdays", "По будням"], ["weekly", "По дням недели"], ["hourly", "Каждый час"], ["monthly", "Раз в месяц"], ["custom", "Cron"]],
        st.freq, v => { st.freq = v; update(); }),
      controls, preview),
    switchRow("Один разговор для всех запусков", "Агент будет видеть прошлые результаты. Иначе каждый запуск — новый разговор.", keep, on => { keep = on; }),
    h("div", { class: "compose-bar" }, h("span", { class: "grow" }), h("button", { class: "btn ghost sm", type: "button", onclick: () => onClose(false) }, "Отмена"), save));
  update();
  setTimeout(() => (rec?.id ? prompt : name).focus(), 0);
  return el;
}
async function schedulesTab(body) {
  const items = await api("/api/schedules");
  const slot = h("div");
  const openEditor = rec => {
    slot.replaceChildren(scheduleEditor(rec, saved => { slot.replaceChildren(); if (saved) openSettings("schedules"); }));
    slot.scrollIntoView({ block: "nearest" });
  };
  const card = s => {
    const more = h("button", { class: "icon-btn", title: "Ещё", "aria-label": "Действия", "aria-haspopup": "menu", "aria-expanded": "false" }, icon("dots"));
    const runNow = async () => {
      try { await api(`/api/schedules/${s.id}/run`, { method: "POST" }); toast(`«${s.name}» запущена — результат появится в разговоре`); setTimeout(loadSessions, 800); }
      catch (e) { toast(e.message); }
    };
    more.addEventListener("click", e => { e.stopPropagation(); popMenu(more, [["play", "Запустить сейчас", runNow], ["pen", "Изменить", () => openEditor(s)],
      ["trash", "Удалить", async () => { if (!confirm(`Удалить задачу «${s.name}»?`)) return; await api(`/api/schedules/${s.id}`, { method: "DELETE" }); openSettings("schedules"); }, true]]); });
    const meta = h("div", { class: "sch-meta" });
    const drawMeta = () => meta.replaceChildren(...[
      s.enabled && s.next_run_at ? h("span", {}, icon("clock"), `Следующий ${rel(s.next_run_at)}`, h("span", { class: "faint" }, ` · ${fmtWhen(s.next_run_at)}`)) : h("span", {}, icon("pause"), "На паузе"),
      s.last_run_at ? h("span", {}, `Последний ${rel(s.last_run_at)}`) : h("span", { class: "faint" }, "Ещё не запускалась"),
      s.session_id ? h("button", { class: "link", onclick: () => { closeSettings(); openSession(s.session_id); } }, "Открыть разговор") : null].filter(Boolean));
    drawMeta();
    const el = h("div", { class: "sch-card" + (s.enabled ? "" : " paused") },
      h("div", { class: "sch-head" }, h("span", { class: "sch-icon" }, icon("clock")),
        h("div", { class: "sch-title" }, h("b", {}, s.name), h("span", {}, describeCron(s.cron))),
        h("button", { class: "btn ghost sm run-btn", title: "Запустить сейчас", onclick: runNow }, icon("play"), "Запустить"),
        toggle(s.enabled, async on => {
          const r = await api(`/api/schedules/${s.id}`, { method: "PATCH", body: { enabled: on } });
          Object.assign(s, r); el.classList.toggle("paused", !on); drawMeta();
        }, "Включена"), more),
      h("div", { class: "sch-prompt" }, s.prompt), meta);
    return el;
  };
  body.append(
    h("div", { class: "tab-intro" }, h("p", { class: "lead-text" }, "Агент сам выполняет задания в заданное время. Результат каждого запуска — в разговоре с ⏰ в списке слева."),
      h("button", { class: "btn primary sm", onclick: () => openEditor(null) }, icon("plus"), "Новая задача")),
    slot);
  if (!items.length) body.append(emptyBlock("clock", "Задач пока нет", "Например, утренняя сводка по будням или итоги недели в пятницу.",
    h("div", { class: "examples" }, SCHEDULE_EXAMPLES.map(x => h("button", { class: "chip", type: "button", onclick: () => openEditor(x) }, `${x.name} — ${describeCron(x.cron).toLowerCase()}`)))));
  else body.append(h("div", { class: "sch-list" }, items.map(card)));
}

const panels = {
  memory: memoryTab,
  tools: toolsTab,
  schedules: schedulesTab,
  async usage(body) {
    const u = await api("/api/usage?days=30");
    const fmt = n => Number(n || 0).toLocaleString("ru-RU");
    body.append(h("div", { class: "section-title" }, "За 30 дней"),
      h("div", { class: "stat" },
        h("div", {}, h("b", {}, fmt(u.runs)), h("span", {}, "запусков")),
        h("div", {}, h("b", {}, `$${(u.cost || 0).toFixed(2)}`), h("span", {}, "стоимость")),
        h("div", {}, h("b", {}, u.input_tokens ? `${Math.round((u.cached_tokens / u.input_tokens) * 100)}%` : "—"), h("span", {}, "из кэша"))),
      row("Токенов на входе", h("span", {}, fmt(u.input_tokens))),
      row("Токенов на выходе", h("span", {}, fmt(u.output_tokens))));
    const models = Object.entries(u.cost_by_model || {}).sort((a, b) => b[1] - a[1]);
    if (models.length) {
      body.append(h("div", { class: "section-title" }, "По моделям"));
      for (const [m, c] of models) body.append(row(h("span", {}, modelName(m), " ", h("code", {}, m)), h("span", {}, `$${c.toFixed(4)}`)));
    }
  },
};

// ── страницы: проекты ───────────────────────────────────────────────
// Маршруты: #projects — список, #p/<id> — проект, #<id сессии> — разговор.
function showPage(on) {
  $(".main").classList.toggle("has-page", on);
  $("#page").hidden = !on;
  for (const id of ["messages", "below"]) if (on) $("#" + id).hidden = true;
  $("#messages").hidden = on;
  $(".composer-wrap").hidden = on;
  if (on) { $("#below").hidden = true; $("#to-bottom").hidden = true; toggleTasks(false); }
  if (!on) S.view = null;
}
function setCrumb(project) {
  const c = $("#crumb");
  c.hidden = !project;
  if (project) { c.querySelector("span").textContent = project.name; c.onclick = () => openProject(project.id); }
}
const ROLE_NAMES = { owner: "владелец", editor: "редактор", viewer: "читатель" };
const fileIcon = kind => ({ pdf: "file", docx: "file", xlsx: "chart", image: "eye", text: "file" }[kind] || "file");

async function openProjects() {
  S.stream?.abort(); S.sessionId = null; S.view = "projects"; renderSessions();
  history.replaceState(null, "", "#projects");
  showPage(true); setCrumb(null);
  $("#chat-title").textContent = "Проекты";
  const page = $("#page");
  page.replaceChildren(h("div", { class: "page-inner" }, h("p", { class: "muted" }, "Загрузка…")));
  let items;
  try { items = await api("/api/projects"); } catch (e) { page.replaceChildren(h("div", { class: "page-inner" }, h("p", { class: "error-text" }, e.message))); return; }
  const list = h("div", { class: "proj-grid" });
  let q = "";
  const draw = () => {
    const shown = items.filter(p => !q || `${p.name} ${p.description}`.toLowerCase().includes(q));
    list.replaceChildren(...shown.map(p => h("button", { class: "proj-card", type: "button", onclick: () => openProject(p.id) },
      h("div", { class: "pc-head" }, h("span", { class: "pc-icon" }, icon("folder")), h("b", {}, p.name),
        p.role !== "owner" ? h("span", { class: "tag shared" }, ROLE_NAMES[p.role]) : p.public ? h("span", { class: "tag" }, icon("users"), "всем") : p.shared ? h("span", { class: "tag" }, icon("users"), "общий") : null),
      h("p", {}, p.description || h("span", { class: "faint" }, "Без описания")),
      h("div", { class: "pc-meta" }, `${p.chats} ${plural(p.chats, "разговор", "разговора", "разговоров")} · ${p.files} ${plural(p.files, "файл", "файла", "файлов")} · ${rel(p.updated_at)}`))));
    if (!shown.length) list.replaceChildren(items.length ? h("p", { class: "muted" }, "Ничего не нашлось")
      : emptyBlock("folder", "Проектов пока нет", "Проект собирает разговоры по одной теме: общие инструкции, файлы-знания и память. Например, «Поверка счётчиков воды» с методиками и шаблонами протоколов.",
        h("button", { class: "btn primary sm", type: "button", onclick: () => newProjectForm() }, icon("plus"), "Создать проект")));
  };
  const [search] = searchBox("Поиск проектов", v => { q = v; draw(); });
  page.replaceChildren(h("div", { class: "page-inner" },
    h("div", { class: "page-head" }, h("div", {}, h("h1", {}, "Проекты"), h("p", { class: "muted" }, "Инструкции, файлы и память для разговоров на одну тему")),
      h("button", { class: "btn primary", type: "button", onclick: () => newProjectForm() }, icon("plus"), "Новый проект")),
    items.length ? h("div", { class: "toolbar" }, search) : null, list));
  draw();
}

function newProjectForm() {
  const name = h("input", { placeholder: "Например: Поверка счётчиков воды", maxlength: 200 });
  const desc = h("textarea", { rows: 2, placeholder: "Над чем работаете — коротко, для себя и коллег" });
  const close = () => $("#modal2").remove();
  const create = h("button", { class: "btn primary", type: "button", onclick: async () => {
    if (!name.value.trim()) { name.focus(); return; }
    create.disabled = true;
    try { const p = await api("/api/projects", { method: "POST", body: { name: name.value, description: desc.value } }); close(); await loadSessions(); openProject(p.id); }
    catch (e) { toast(e.message); create.disabled = false; }
  } }, "Создать");
  const m = h("div", { class: "modal", id: "modal2", onmousedown: e => { if (e.target.id === "modal2") close(); } },
    h("div", { class: "modal-card dialog", role: "dialog", "aria-modal": "true" },
      h("div", { class: "dialog-head" }, h("h3", {}, "Новый проект"), h("button", { class: "icon-btn", "aria-label": "Закрыть", onclick: close }, icon("x"))),
      fieldRow("Название", name), fieldRow("Описание", desc),
      h("div", { class: "compose-bar" }, h("span", { class: "grow" }), h("button", { class: "btn ghost", type: "button", onclick: close }, "Отмена"), create)));
  m.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); close(); } if (e.key === "Enter" && e.target === name) create.click(); });
  document.body.append(m); name.focus();
}

async function openProject(id) {
  S.stream?.abort(); S.sessionId = null; S.view = "project:" + id; renderSessions();
  history.replaceState(null, "", `#p/${id}`);
  showPage(true); setCrumb(null);
  const page = $("#page");
  let p;
  try { p = await api(`/api/projects/${id}`); } catch (e) { toast(e.message); openProjects(); return; }
  S.project = p;
  $("#chat-title").textContent = p.name;
  const canEdit = p.role === "owner" || p.role === "editor";

  // начать разговор в проекте
  const input = h("textarea", { rows: 2, placeholder: `Новый разговор в проекте «${p.name}»…` });
  const start = async () => {
    const text = input.value.trim(); if (!text) return;
    S.projectId = p.id; showPage(false); resetView(); S.sessionId = null;
    $("#chat-title").textContent = "Новый разговор"; setCrumb({ id: p.id, name: p.name });
    await send(text);
  };
  input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); start(); } });
  const starter = h("div", { class: "compose-card proj-start" }, input,
    h("div", { class: "compose-bar" }, h("span", { class: "help" }, "Агент увидит инструкции, файлы и память проекта"), h("span", { class: "grow" }),
      h("button", { class: "send", type: "button", "aria-label": "Начать", onclick: start }, icon("send"))));

  const chats = h("div", { class: "proj-chats" },
    p.chats.length ? p.chats.map(c => h("button", { class: "proj-chat", type: "button", onclick: () => openSession(c.id) },
      icon("chat"), h("span", {}, c.title), h("small", {}, rel(c.updated_at))))
      : h("p", { class: "muted small" }, "Ваших разговоров в этом проекте пока нет."));

  const more = h("button", { class: "icon-btn", title: "Ещё", "aria-label": "Действия с проектом" }, icon("dots"));
  more.addEventListener("click", e => { e.stopPropagation(); popMenu(more, [
    canEdit ? ["pen", "Переименовать", () => editProjectField(p, "name")] : null,
    canEdit ? ["file", "Изменить описание", () => editProjectField(p, "description")] : null,
    p.role === "owner" ? ["trash", "Удалить проект", async () => {
      if (!confirm(`Удалить проект «${p.name}»? Файлы и память проекта удалятся, разговоры останутся без проекта.`)) return;
      await api(`/api/projects/${p.id}`, { method: "DELETE" }); toast("Проект удалён"); openProjects(); loadSessions(); }, true] : null,
  ].filter(Boolean)); });

  page.replaceChildren(h("div", { class: "page-inner proj-page" },
    h("button", { class: "back-link", type: "button", onclick: openProjects }, icon("back"), "Все проекты"),
    h("div", { class: "proj-layout" },
      h("div", { class: "proj-main" },
        h("div", { class: "page-head" }, h("div", {}, h("h1", {}, p.name), p.description ? h("p", { class: "muted" }, p.description) : null),
          h("div", { class: "head-actions" }, p.role === "owner" ? h("button", { class: "btn sm", type: "button", onclick: () => shareDialog(p) }, icon("users"), "Доступ") : h("span", { class: "tag shared" }, ROLE_NAMES[p.role]), (canEdit ? more : null))),
        starter,
        h("div", { class: "section-title" }, "Разговоры"), chats),
      h("aside", { class: "proj-side" },
        instructionsCard(p, canEdit), filesCard(p, canEdit), memoryCard(p, canEdit)))));
  setTimeout(() => input.focus(), 0);
}

async function editProjectField(p, field) {
  const v = prompt(field === "name" ? "Название проекта" : "Описание проекта", p[field] || "");
  if (v == null) return;
  try { await api(`/api/projects/${p.id}`, { method: "PATCH", body: { [field]: v } }); openProject(p.id); loadSessions(); } catch (e) { toast(e.message); }
}

function sideCard(title, ic, action, ...kids) {
  return h("section", { class: "side-card" }, h("div", { class: "sc-head" }, icon(ic), h("b", {}, title), h("span", { class: "grow" }), action || null), ...kids);
}

function instructionsCard(p, canEdit) {
  const body = h("div", { class: "sc-body" });
  const view = () => body.replaceChildren(p.instructions ? h("div", { class: "instr" }, p.instructions)
    : h("p", { class: "muted small" }, canEdit ? "Как агенту работать в этом проекте: тон, формат, на что опираться." : "Инструкций нет."));
  const edit = () => {
    const ta = h("textarea", { rows: 8, placeholder: "Например: отвечай по методикам из файлов проекта, суммы в рублях, протоколы — по шаблону." });
    ta.value = p.instructions || "";
    const save = h("button", { class: "btn primary sm", type: "button", onclick: async () => {
      save.disabled = true;
      try { await api(`/api/projects/${p.id}`, { method: "PATCH", body: { instructions: ta.value } }); p.instructions = ta.value.trim(); view(); toast("Инструкции сохранены"); }
      catch (e) { toast(e.message); save.disabled = false; }
    } }, "Сохранить");
    body.replaceChildren(h("div", { class: "mem-edit" }, ta, h("div", { class: "compose-bar" }, h("span", { class: "grow" }),
      h("button", { class: "btn ghost sm", type: "button", onclick: view }, "Отмена"), save)));
    ta.focus();
  };
  view();
  return sideCard("Инструкции", "book", canEdit ? h("button", { class: "icon-btn", title: "Изменить", "aria-label": "Изменить инструкции", onclick: edit }, icon("pen")) : null, body);
}

function filesCard(p, canEdit) {
  const list = h("div", { class: "pfiles" });
  const draw = () => {
    const total = p.files.reduce((a, f) => a + (f.text_chars || 0), 0);
    list.replaceChildren(...p.files.map(f => h("div", { class: "pfile" },
      h("span", { class: "pf-icon" }, icon(fileIcon(f.kind))),
      h("button", { class: "pf-name", type: "button", title: "Скачать", onclick: () => downloadProjectFile(p.id, f) }, h("span", {}, f.name),
        h("small", {}, [humanSize(f.size), f.text_chars ? `${Math.round(f.text_chars / 1000) || 1} тыс. симв.` : "без текста"].filter(Boolean).join(" · "))),
      canEdit ? h("button", { class: "icon-btn", title: "Удалить", "aria-label": "Удалить файл", onclick: async () => {
        if (!confirm(`Удалить «${f.name}» из проекта?`)) return;
        await api(`/api/projects/${p.id}/files/${f.id}`, { method: "DELETE" }); p.files = p.files.filter(x => x.id !== f.id); draw(); } }, icon("trash")) : null)));
    if (!p.files.length) list.append(h("p", { class: "muted small" }, canEdit ? "PDF, Word, Excel, текст — агент будет опираться на них." : "Файлов нет."));
    else list.append(h("div", { class: "pf-note" }, total <= (p.inline_limit || 40000) && p.inline !== false
      ? "Текст всех файлов агент видит целиком" : "Файлов много — агент ищет по ним, когда нужно"));
  };
  const input = h("input", { type: "file", multiple: true, hidden: true });
  const upload = async files => {
    for (const file of files) {
      const fd = new FormData(); fd.append("file", file);
      const ph = h("div", { class: "pfile loading" }, h("span", { class: "spin" }), h("span", {}, file.name));
      list.prepend(ph);
      try {
        const r = await fetch(`/api/projects/${p.id}/files`, { method: "POST", headers: { Authorization: `Bearer ${S.token}` }, body: fd });
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
        p.files.push(await r.json());
      } catch (e) { toast(`${file.name}: ${e.message}`); }
      ph.remove();
    }
    p.inline = p.files.reduce((a, f) => a + (f.text_chars || 0), 0) <= 40000;
    draw();
  };
  input.addEventListener("change", () => { upload([...input.files]); input.value = ""; });
  const card = sideCard("Файлы", "file", canEdit ? h("button", { class: "icon-btn", title: "Добавить файлы", "aria-label": "Добавить файлы", onclick: () => input.click() }, icon("upload")) : null, input, list);
  if (canEdit) {
    card.addEventListener("dragover", e => { if (e.dataTransfer?.types?.includes("Files")) { e.preventDefault(); e.stopPropagation(); card.classList.add("drop"); } });
    card.addEventListener("dragleave", () => card.classList.remove("drop"));
    card.addEventListener("drop", e => { e.preventDefault(); e.stopPropagation(); card.classList.remove("drop"); document.body.classList.remove("dragging"); upload([...e.dataTransfer.files]); });
  }
  draw();
  return card;
}

async function downloadProjectFile(pid, f) {
  try {
    const r = await fetch(`/api/projects/${pid}/files/${f.id}`, { headers: { Authorization: `Bearer ${S.token}` } });
    if (!r.ok) throw new Error(r.statusText);
    const u = URL.createObjectURL(await r.blob());
    const a = h("a", { href: u, download: f.name }); document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(u), 10000);
  } catch (e) { toast(e.message); }
}

function memoryCard(p, canEdit) {
  const list = h("div", { class: "pmem" });
  const draw = () => {
    list.replaceChildren(...p.memory.map(m => h("div", { class: "pmem-item" }, h("span", {}, m.text),
      canEdit ? h("button", { class: "icon-btn", title: "Забыть", "aria-label": "Забыть", onclick: async () => {
        await api(`/api/projects/${p.id}/memory/${m.id}`, { method: "DELETE" }); p.memory = p.memory.filter(x => x.id !== m.id); draw(); } }, icon("x")) : null)));
    if (!p.memory.length) list.append(h("p", { class: "muted small" }, "Скажите агенту в разговоре проекта «запомни для проекта: …»."));
  };
  draw();
  return sideCard("Память проекта", "memory", null, list);
}

async function shareDialog(p) {
  const [users, d] = await Promise.all([api("/api/users").catch(() => []), api(`/api/projects/${p.id}`)]);
  let members = d.members;
  const close = () => { $("#modal2")?.remove(); openProject(p.id); };
  const body = h("div", { class: "share-list" });
  const set = async (uid, role) => {
    try { members = (await api(`/api/projects/${p.id}/members`, { method: "PUT", body: { user_id: uid, role } })).members; draw(); }
    catch (e) { toast(e.message); }
  };
  const roleSel = (value, onChange) => {
    const s = h("select", { class: "mini-select" }, h("option", { value: "viewer" }, "Читатель"), h("option", { value: "editor" }, "Редактор"));
    s.value = value; s.addEventListener("change", () => onChange(s.value)); return s;
  };
  const name = id => users.find(u => u.id === id)?.name || id;
  const all = members.find(m => m.user_id === "*");
  const everyone = h("div", { class: "share-row everyone" }, h("span", { class: "avatar sm" }, icon("users")),
    h("div", { class: "grow" }, h("b", {}, "Все пользователи"), h("div", { class: "help" }, "Любой, кто входит в этот сервис")), h("span", {}));
  const draw = () => {
    const allNow = members.find(m => m.user_id === "*");
    everyone.lastChild.replaceWith(h("div", { class: "share-ctl" }, allNow ? roleSel(allNow.role, r => set("*", r)) : null,
      toggle(!!allNow, on => set("*", on ? "viewer" : null), "Открыть всем")));
    body.replaceChildren(
      h("div", { class: "share-row" }, h("span", { class: "avatar sm" }, (d.owner_name || "?").charAt(0).toUpperCase()),
        h("div", { class: "grow" }, h("b", {}, d.owner_name), h("div", { class: "help" }, "владелец"))),
      ...members.filter(m => m.user_id !== "*").map(m => h("div", { class: "share-row" },
        h("span", { class: "avatar sm" }, name(m.user_id).charAt(0).toUpperCase()),
        h("div", { class: "grow" }, h("b", {}, name(m.user_id)), h("div", { class: "help" }, m.user_id)),
        roleSel(m.role, r => set(m.user_id, r)),
        h("button", { class: "icon-btn", title: "Убрать доступ", "aria-label": "Убрать доступ", onclick: () => set(m.user_id, null) }, icon("x")))));
  };
  const who = h("input", { list: "known-users", placeholder: "id пользователя" });
  const dl = h("datalist", { id: "known-users" }, users.filter(u => u.id !== S.me.id).map(u => h("option", { value: u.id }, u.name)));
  const newRole = roleSel("viewer", () => {});
  const add = h("button", { class: "btn sm", type: "button", onclick: async () => { if (!who.value.trim()) return; await set(who.value.trim(), newRole.value); who.value = ""; } }, "Открыть");
  const m = h("div", { class: "modal", id: "modal2", onmousedown: e => { if (e.target.id === "modal2") close(); } },
    h("div", { class: "modal-card dialog", role: "dialog", "aria-modal": "true" },
      h("div", { class: "dialog-head" }, h("h3", {}, `Доступ к «${p.name}»`), h("button", { class: "icon-btn", "aria-label": "Закрыть", onclick: close }, icon("x"))),
      h("p", { class: "help" }, "Читатели общаются в проекте и видят файлы. Редакторы меняют инструкции, файлы и память. Разговоры у каждого свои."),
      everyone, h("div", { class: "share-add" }, who, dl, newRole, add), body));
  m.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); close(); } });
  document.body.append(m);
  draw(); if (all) { /* уже открыт всем */ }
  who.focus();
}

// ── компьютер ───────────────────────────────────────────────────────
async function refreshDevices() {
  try { S.devices = await api("/api/devices"); } catch { S.devices = []; }
  const on = (S.devices || []).filter(d => d.online);
  const chip = $("#device-chip");
  chip.hidden = !on.length;
  if (on.length) {
    chip.querySelector("span").textContent = on.length > 1 ? `${on.length} компьютера` : on[0].name;
    chip.title = "Агент видит: " + on.map(d => `${d.name} — ${(d.info.folders || []).map(f => f.name).join(", ") || "папок нет"}`).join("; ");
  }
  return S.devices;
}
const detectOS = () => /Win/i.test(navigator.userAgent) ? "win" : /Mac/i.test(navigator.userAgent) ? "mac" : "linux";
const OS_NAMES = { win: "Windows", mac: "macOS", linux: "Linux" };
function codeBlock(lines, big) {
  const text = lines.join("\n");
  const b = h("button", { class: "btn primary sm copy-big", type: "button", onclick: () => {
    navigator.clipboard?.writeText(text).then(() => { b.replaceChildren(icon("check"), "Скопировано"); setTimeout(() => b.replaceChildren(icon("copy"), "Скопировать"), 1600); });
  } }, icon("copy"), "Скопировать");
  return h("div", { class: "code-block" + (big ? " big" : "") }, h("pre", {}, text), big ? b : copyBtn(() => text));
}
async function downloadConnectBat(key) {
  const tpl = await fetch("/connect.bat").then(r => r.text());
  // Ключ подставляем здесь, в браузере: в адресе запроса и логах сервера его нет.
  const blob = new Blob([tpl.replace(/__KEY__/g, key)], { type: "application/octet-stream" });
  const u = URL.createObjectURL(blob);
  const a = h("a", { href: u, download: "Подключить компьютер.bat" }); document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(u), 10000);
}
async function deviceAction(d, op, args, waitText) {
  const t = waitText ? toastWait(waitText) : null;
  try { const r = await api(`/api/devices/${d.id}/action`, { method: "POST", body: { op, args } }); return r.result; }
  catch (e) { toast(e.message); return null; }
  finally { t?.(); }
}
function toastWait(text) {
  document.querySelector(".toast")?.remove();
  clearTimeout(toastTimer);
  const el = h("div", { class: "toast", role: "status" }, h("span", { class: "spin light" }), h("span", {}, text));
  document.body.append(el);
  return () => el.remove();
}
async function computerTab(body) {
  const devices = await refreshDevices();
  const slot = h("div");
  let timer = null;
  const stopTimer = () => { clearInterval(timer); timer = null; };

  const connect = async () => {
    const os = detectOS();
    let r;
    try { r = await api("/api/devices", { method: "POST", body: { name: os === "mac" ? "Mac" : os === "win" ? "Компьютер с Windows" : "Компьютер с Linux" } }); }
    catch (e) { toast(e.message); return; }
    let cur = os;
    const steps = h("div");
    const oneLiner = `curl -fsSL ${location.origin}/connect.sh | bash -s -- ${r.key}`;
    const draw = () => steps.replaceChildren(cur === "win"
      ? h("ol", { class: "steps-howto big" },
        h("li", {}, h("b", {}, "Скачайте файл подключения"),
          h("div", {}, h("button", { class: "btn primary", type: "button", onclick: () => downloadConnectBat(r.key) }, icon("download"), "Скачать «Подключить компьютер.bat»"))),
        h("li", {}, h("b", {}, "Откройте его двойным щелчком"),
          h("div", { class: "help" }, "Если Windows предупредит: «Подробнее» → «Выполнить в любом случае». Python, если его нет, поставится сам.")),
        h("li", {}, h("b", {}, "Выберите папку в появившемся окне"),
          h("div", { class: "help" }, "Только её и увидит агент. Потом можно добавить ещё — здесь или когда агент попросит.")))
      : h("ol", { class: "steps-howto big" },
        h("li", {}, h("b", {}, cur === "mac" ? "Откройте Терминал" : "Откройте терминал"),
          h("div", { class: "help" }, cur === "mac" ? "⌘ + Пробел → наберите «Терминал» → Enter" : "Обычно Ctrl + Alt + T")),
        h("li", {}, h("b", {}, "Вставьте эту строку и нажмите Enter"), codeBlock([oneLiner], true)),
        h("li", {}, h("b", {}, "Выберите папку в появившемся окне"),
          h("div", { class: "help" }, "Только её и увидит агент. Мост спросит, запускаться ли ему самому при входе — тогда Терминал можно закрыть."))));
    const status = h("div", { class: "test-result" }, h("span", { class: "spin" }), h("div", {}, h("b", {}, "Ждём подключения…"), h("div", { class: "help" }, "Статус обновится сам")));
    const manual = h("details", { class: "more-opts" }, h("summary", {}, "Вручную, без установки"),
      h("p", { class: "help" }, "Скачать мост и запустить его с нужными папками (:ro — только чтение, --allow-shell — разрешить команды):"),
      codeBlock([`curl -fsSLo bridge.py ${location.origin}/bridge.py`, `python3 bridge.py --server ${location.origin} --key ${r.key} --folder ~/Documents`]));
    draw();
    slot.replaceChildren(h("div", { class: "sch-editor pair" },
      h("div", { class: "ed-head" }, h("b", {}, "Подключить компьютер"), h("button", { class: "icon-btn", "aria-label": "Закрыть", onclick: () => { stopTimer(); openSettings("computer"); } }, icon("x"))),
      segmented([["win", "Windows"], ["mac", "macOS"], ["linux", "Linux"]], cur, v => { cur = v; draw(); }),
      steps, status, manual));
    stopTimer();
    timer = setInterval(async () => {
      if (!document.body.contains(status)) return stopTimer();
      const d = (await api("/api/devices").catch(() => [])).find(x => x.id === r.device.id);
      if (!d?.online) return;
      const folders = d.info.folders || [];
      status.className = "test-result ok";
      status.replaceChildren(icon("check"), h("div", {}, h("b", {}, `Подключено: ${d.info.hostname || d.name}`),
        h("div", { class: "help" }, folders.length ? `Агент видит: ${folders.map(f => f.name).join(", ")}` : "Ждём, пока выберете папку…")));
      if (folders.length) { stopTimer(); refreshDevices(); setTimeout(() => document.body.contains(status) && openSettings("computer"), 2500); }
    }, 2000);
  };

  const card = d => {
    const info = d.info || {}, on = d.online;
    const more = h("button", { class: "icon-btn", title: "Ещё", "aria-label": "Действия" }, icon("dots"));
    more.addEventListener("click", e => { e.stopPropagation(); popMenu(more, [
      ["pen", "Переименовать", async () => { const v = prompt("Название компьютера", d.name); if (v == null) return; await api(`/api/devices/${d.id}`, { method: "PATCH", body: { name: v } }); openSettings("computer"); }],
      ["trash", "Отключить компьютер", async () => { if (!confirm(`Отключить «${d.name}»? Мост на нём остановится и уберёт автозапуск.`)) return; await api(`/api/devices/${d.id}`, { method: "DELETE" }); refreshDevices(); openSettings("computer"); }, true]]); });
    const rows = (info.folders || []).map(f => h("div", { class: "tool-row folder-row" },
      h("div", { class: "tr-main" }, h("div", { class: "tr-title" }, icon("folder"), h("span", {}, f.name)), h("div", { class: "tr-desc mono" }, f.path)),
      h("label", { class: "inline-switch", title: f.read_only ? "Сейчас только чтение" : "Агент может изменять файлы (с подтверждением в чате)" },
        h("span", {}, "изменение"), on ? toggle(!f.read_only, async v => {
          const res = await deviceAction(d, "set_folder", { name: f.name, read_only: !v }, v ? "Подтвердите на компьютере…" : null);
          if (!res) throw new Error(""); refreshDevices();
        }, "Разрешить изменение файлов") : h("span", { class: "tag" }, f.read_only ? "чтение" : "запись")),
      on ? h("button", { class: "icon-btn", title: "Закрыть доступ к папке", "aria-label": "Закрыть доступ", onclick: async () => {
        if (await deviceAction(d, "remove_folder", { name: f.name })) { await refreshDevices(); openSettings("computer"); } } }, icon("x")) : null));
    const shellRow = h("div", { class: "tool-row folder-row" },
      h("div", { class: "tr-main" }, h("div", { class: "tr-title" }, icon("terminal"), h("span", {}, "Команды")),
        h("div", { class: "tr-desc" }, "Запуск программ и скриптов — каждая команда с подтверждением в чате")),
      on ? toggle(!!info.shell, async v => {
        const res = await deviceAction(d, "set_shell", { enabled: v }, v ? "Подтвердите на компьютере…" : null);
        if (!res) throw new Error(""); refreshDevices();
      }, "Разрешить команды") : h("span", { class: "tag" }, info.shell ? "разрешены" : "запрещены"));
    const addBtn = h("button", { class: "btn sm", type: "button", disabled: !on, title: on ? "" : "Компьютер не на связи", onclick: async () => {
      const res = await deviceAction(d, "pick_folder", { reason: "Вы нажали «Добавить папку» в чате" }, "Выберите папку в окне на компьютере…");
      if (res) { toast(`Открыта папка «${res.name}»`); await refreshDevices(); openSettings("computer"); }
    } }, icon("plus"), "Добавить папку");
    return h("div", { class: "src-card dev-card open" + (on ? "" : " disabled") },
      h("div", { class: "src-top" }, h("div", { class: "src-head static" },
        h("span", { class: "src-icon" }, icon("monitor")),
        h("span", { class: "src-title" }, h("b", {}, d.name), h("span", { class: "src-sub" }, [info.hostname, info.platform].filter(Boolean).join(" · ") || "ещё не подключался")),
        on ? h("span", { class: "pill ok" }, h("i"), "на связи") : h("span", { class: "pill" }, h("i"), d.last_seen ? `был ${rel(d.last_seen)}` : "ждёт подключения")),
        h("div", { class: "src-actions" }, more)),
      d.last_seen ? h("div", { class: "src-body dev-body" }, ...rows,
        !rows.length ? h("div", { class: "tool-row" }, h("div", { class: "tr-main" }, h("div", { class: "tr-desc" }, "Папок не открыто. Агент попросит сам, когда понадобится, или добавьте сейчас."))) : null,
        shellRow,
        h("div", { class: "dev-foot" }, addBtn, !on ? h("span", { class: "help" }, "Включите компьютер — мост подключится сам, если включён автозапуск") : null)) : null);
  };
  const real = devices.filter(d => d.last_seen || d.online);
  const pending = devices.filter(d => !d.last_seen && !d.online);
  // Ключи, которые так и не подключились (закрыли окно на полпути), — тихо убираем.
  for (const d of pending) if (Date.now() / 1000 - d.created_at > 3600) api(`/api/devices/${d.id}`, { method: "DELETE" }).catch(() => {});
  body.append(
    h("div", { class: "tab-intro" }, h("p", { class: "lead-text" }, "Агент может работать с файлами и программами на вашем компьютере. Папки выбираете вы — в обычном окне на компьютере."),
      h("button", { class: "btn primary sm", type: "button", onclick: connect }, icon("plus"), "Подключить компьютер")),
    slot,
    real.length ? h("div", { class: "src-list" }, real.map(card))
      : emptyBlock("monitor", "Компьютер не подключён", "Подключение — один файл на Windows или одна строка в Терминале на Mac. Агент будет читать и править файлы в папках, которые вы откроете, — каждое изменение с вашего подтверждения.",
        h("button", { class: "btn primary sm", type: "button", onclick: connect }, icon("plus"), "Подключить компьютер")));
}

// ── файлы: карточки в чате и панель предпросмотра сбоку ────────────────
const FILE_TYPES = {
  pdf: ["PDF", "pdf"], html: ["HTML", "html"], htm: ["HTML", "html"], md: ["Markdown", "md"], txt: ["Текст", "text"],
  csv: ["CSV", "table"], tsv: ["TSV", "table"], xlsx: ["Excel", "sheets"], xls: ["Excel", ""], docx: ["Word", "doc"], doc: ["Word", ""],
  pptx: ["PowerPoint", ""], json: ["JSON", "code"], xml: ["XML", "code"], yaml: ["YAML", "code"], yml: ["YAML", "code"],
  py: ["Python", "code"], js: ["JavaScript", "code"], ts: ["TypeScript", "code"], sql: ["SQL", "code"], log: ["Журнал", "code"], sh: ["Shell", "code"],
  png: ["Картинка", "image"], jpg: ["Картинка", "image"], jpeg: ["Картинка", "image"], gif: ["Картинка", "image"], webp: ["Картинка", "image"], svg: ["SVG", "image"],
  zip: ["Архив", ""],
};
// Что открывать само, когда агент закончил файл: то, что человек и так открыл бы посмотреть.
const AUTO_OPEN = new Set(["pdf", "html", "htm", "md", "csv", "xlsx", "docx", "png", "jpg", "jpeg", "svg", "webp"]);
const extOf = name => (String(name).match(/\.([a-z0-9]+)$/i)?.[1] || "").toLowerCase();
const typeOf = name => FILE_TYPES[extOf(name)] || [extOf(name).toUpperCase() || "Файл", ""];
const canPreview = name => !!typeOf(name)[1];

async function fileBlob(path) {
  const r = await fetch(`/api/sessions/${S.sessionId}/files?path=${encodeURIComponent(path)}`, { headers: { Authorization: `Bearer ${S.token}` } });
  if (!r.ok) throw new Error("файл недоступен");
  return r.blob();
}
async function downloadFile(f) {
  try {
    const u = await blobURL(f.path);
    const a = h("a", { href: u, download: f.name }); document.body.append(a); a.click(); a.remove();
  } catch (e) { toast(e.message); }
}
function fileTile(name) {
  const ext = extOf(name);
  return h("span", { class: "ftile ft-" + (typeOf(name)[1] || "other") + " ext-" + ext }, (ext || "?").slice(0, 4).toUpperCase());
}
// Карточка файла в чате: тип, размер, «Открыть» в панели и «Скачать».
function fileCard(f) {
  const [label, kind] = typeOf(f.name);
  if (kind === "image") {
    const img = h("img", { class: "fc-img", alt: f.name });
    blobURL(f.path).then(u => { img.src = u; }).catch(() => img.remove());
    return h("button", { class: "file-card image", type: "button", title: "Открыть", onclick: () => openPreview(f) }, img,
      h("span", { class: "fc-cap" }, h("span", {}, f.name), h("small", {}, humanSize(f.size))));
  }
  return h("div", { class: "file-card" + (kind ? " can" : ""), role: kind ? "button" : null, tabindex: kind ? "0" : null,
    onclick: e => { if (kind && !e.target.closest(".fc-dl")) openPreview(f); },
    onkeydown: e => { if (kind && e.key === "Enter") openPreview(f); } },
    fileTile(f.name),
    h("span", { class: "fc-text" }, h("b", {}, f.name), h("small", {}, [label, humanSize(f.size)].filter(Boolean).join(" · "))),
    kind ? h("span", { class: "fc-open" }, "Открыть") : null,
    h("button", { class: "icon-btn fc-dl", type: "button", title: "Скачать", "aria-label": "Скачать", onclick: () => downloadFile(f) }, icon("download")));
}

S.preview = null;
async function openPreview(f) {
  if (!S.sessionId) return;
  const [label, kind] = typeOf(f.name);
  S.preview = f;
  toggleTasks(false);
  const pv = $("#preview");
  pv.hidden = false;
  if (innerWidth <= 960) $("#scrim").hidden = false;
  $("#pv-icon").replaceChildren(fileTile(f.name));
  $("#pv-name").textContent = f.name;
  $("#pv-meta").textContent = [label, humanSize(f.size)].filter(Boolean).join(" · ");
  $("#pv-download").onclick = () => downloadFile(f);
  const body = $("#pv-body");
  body.className = "pv-body pv-" + (kind || "none");
  body.replaceChildren(h("div", { class: "pv-loading" }, h("span", { class: "spin" }), "Открываю…"));
  try {
    body.replaceChildren(await previewContent(f, kind));
  } catch (e) {
    body.replaceChildren(h("div", { class: "pv-empty" }, fileTile(f.name), h("b", {}, "Не получилось показать"), h("p", {}, e.message),
      h("button", { class: "btn sm", type: "button", onclick: () => downloadFile(f) }, icon("download"), "Скачать")));
  }
}
function closePreview() {
  $("#preview").hidden = true; S.preview = null;
  if (innerWidth <= 960 && $("#tasks").hidden && !$("#sidebar").classList.contains("open")) $("#scrim").hidden = true;
}

async function previewContent(f, kind) {
  if (kind === "image") {
    const img = h("img", { alt: f.name }); img.src = await blobURL(f.path);
    return h("div", { class: "pv-image" }, img);
  }
  if (kind === "pdf") {
    // Встроенный просмотрщик есть в настольных браузерах; на Android и в части мобильных — нет:
    // тогда рисуем страницы сами через pdf.js (подгружается только в этом случае).
    if (navigator.pdfViewerEnabled !== false) return h("iframe", { class: "pv-frame", src: await blobURL(f.path), title: f.name });
    return pdfPages(await (await fileBlob(f.path)).arrayBuffer());
  }
  if (kind === "html") {
    // Свой HTML — в песочнице без доступа к странице чата: никакого blob-URL с нашим происхождением,
    // иначе скрипты файла прочитали бы токен из localStorage.
    const text = await (await fileBlob(f.path)).text();
    return h("iframe", { class: "pv-frame white", sandbox: "allow-scripts allow-popups allow-forms", srcdoc: text, title: f.name });
  }
  if (kind === "md") {
    const d = h("div", { class: "md pv-doc" }); renderMd(d, await (await fileBlob(f.path)).text()); return d;
  }
  if (kind === "table") {
    const text = await (await fileBlob(f.path)).text();
    return sheetView([{ name: f.name, rows: parseCSV(text, extOf(f.name) === "tsv" ? "\t" : null) }]);
  }
  if (kind === "code" || kind === "text") {
    const text = await (await fileBlob(f.path)).text();
    return h("pre", { class: "pv-code" + (kind === "text" ? " wrap" : "") }, text.length > 400000 ? text.slice(0, 400000) + "\n…" : text);
  }
  if (kind === "sheets" || kind === "doc") {
    const d = await api(`/api/sessions/${S.sessionId}/files/preview?path=${encodeURIComponent(f.path)}`);
    if (d.kind === "sheets") return sheetView(d.sheets);
    const doc = h("div", { class: "md pv-doc" });
    for (const b of d.blocks) {
      if (b.type === "h") doc.append(h("h" + Math.min(4, b.level + 1), {}, b.text));
      else if (b.type === "p") doc.append(h("p", {}, b.text));
      else doc.append(h("table", {}, h("tbody", {}, b.rows.map(r => h("tr", {}, r.map(c => h("td", {}, c)))))));
    }
    return doc;
  }
  throw new Error("Для этого типа файла предпросмотра нет — скачайте его.");
}
const PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/";
let pdfjsReady = null;
function loadPdfJs() {
  pdfjsReady ??= new Promise((ok, fail) => {
    const sc = h("script", { src: PDFJS + "pdf.min.js" });
    sc.onload = () => { window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS + "pdf.worker.min.js"; ok(window.pdfjsLib); };
    sc.onerror = () => { pdfjsReady = null; fail(new Error("Браузер не показывает PDF, а просмотрщик не загрузился — скачайте файл.")); };
    document.head.append(sc);
  });
  return pdfjsReady;
}
async function pdfPages(buf) {
  const lib = await loadPdfJs();
  const doc = await lib.getDocument({ data: buf }).promise;
  const wrap = h("div", { class: "pv-pdf" });
  const width = Math.min(900, ($("#pv-body").clientWidth || innerWidth) - 24);
  for (let i = 1; i <= Math.min(doc.numPages, 50); i++) {
    const page = await doc.getPage(i);
    const vp0 = page.getViewport({ scale: 1 });
    const scale = (width / vp0.width) * (devicePixelRatio || 1);
    const vp = page.getViewport({ scale });
    const c = h("canvas", { width: Math.floor(vp.width), height: Math.floor(vp.height) });
    c.style.width = `${width}px`; c.style.maxWidth = "100%"; c.style.height = "auto";
    await page.render({ canvasContext: c.getContext("2d"), viewport: vp }).promise;
    wrap.append(c);
  }
  if (doc.numPages > 50) wrap.append(h("p", { class: "muted center" }, `Показаны 50 страниц из ${doc.numPages} — остальное в скачанном файле`));
  return wrap;
}
function parseCSV(text, sep) {
  const first = text.split("\n", 1)[0];
  sep = sep || ([";", ",", "\t"].map(c => [c, first.split(c).length]).sort((a, b) => b[1] - a[1])[0][0]);
  const rows = []; let row = [], cell = "", q = false;
  for (let i = 0; i < text.length && rows.length < 1000; i++) {
    const c = text[i];
    if (q) { if (c === '"') { if (text[i + 1] === '"') { cell += '"'; i++; } else q = false; } else cell += c; continue; }
    if (c === '"') q = true;
    else if (c === sep) { row.push(cell); cell = ""; }
    else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(cell); rows.push(row); row = []; cell = ""; }
    else cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows;
}
function sheetView(sheets) {
  const holder = h("div", { class: "pv-sheet" });
  const draw = i => {
    const rows = sheets[i].rows || [];
    const width = Math.max(0, ...rows.map(r => r.length));
    const num = c => /^-?[\d\s]+([.,]\d+)?$/.test(String(c).trim()) && String(c).trim() !== "";
    holder.replaceChildren(h("table", {},
      rows.length ? h("thead", {}, h("tr", {}, h("th", { class: "rn" }, ""), Array.from({ length: width }, (_, ci) => h("th", {}, rows[0][ci] ?? "")))) : null,
      h("tbody", {}, rows.slice(1).map((r, ri) => h("tr", {}, h("td", { class: "rn" }, ri + 2),
        Array.from({ length: width }, (_, ci) => h("td", { class: num(r[ci] ?? "") ? "num" : "" }, r[ci] ?? "")))))));
  };
  const tabs = sheets.length > 1 ? h("div", { class: "pv-tabs" }, sheets.map((s, i) => h("button", { type: "button", class: i ? "" : "on",
    onclick: e => { e.currentTarget.parentElement.querySelectorAll("button").forEach(b => b.classList.remove("on")); e.currentTarget.classList.add("on"); draw(i); } }, s.name))) : null;
  draw(0);
  return h("div", { class: "pv-sheet-wrap" }, holder, tabs);
}
// Ширина панели — тянуть за левый край; запоминается.
(() => {
  const pv = $("#preview"), grip = $("#pv-resize");
  const apply = w => pv.style.setProperty("--pv-w", `${Math.max(340, Math.min(w, innerWidth - 520))}px`);
  const saved = +store.get("agent_pv_w"); if (saved) apply(saved);
  grip.addEventListener("pointerdown", e => {
    e.preventDefault(); grip.setPointerCapture(e.pointerId); pv.classList.add("resizing");
    const move = ev => apply(innerWidth - ev.clientX);
    const up = ev => { grip.removeEventListener("pointermove", move); grip.removeEventListener("pointerup", up); pv.classList.remove("resizing"); store.set("agent_pv_w", String(innerWidth - ev.clientX)); };
    grip.addEventListener("pointermove", move); grip.addEventListener("pointerup", up);
  });
})();
$("#pv-close").addEventListener("click", closePreview);

// ── всплывающие меню ────────────────────────────────────────────────
function closeMenus() {
  $("#user-menu").hidden = true; $("#user-btn").setAttribute("aria-expanded", "false");
  $("#model-pop").hidden = true; $("#model-btn").setAttribute("aria-expanded", "false");
  $("#mode-pop").hidden = true; $("#mode-btn").setAttribute("aria-expanded", "false");
  const pop = $("#popover");
  if (!pop.hidden) { pop.hidden = true; pop._owner?.setAttribute("aria-expanded", "false"); }
}
function toggleUserMenu() {
  const open = $("#user-menu").hidden;
  closeMenus();
  if (!open) return;
  $("#user-menu").hidden = false; $("#user-btn").setAttribute("aria-expanded", "true");
  for (const b of document.querySelectorAll("[data-theme-set]")) b.classList.toggle("on", b.dataset.themeSet === (store.get("agent_theme") || "auto"));
  $("#user-menu .menu-item")?.focus();
}
function sessionMenu(s, item, btn) {
  closeMenus();
  const pop = $("#popover");
  pop.replaceChildren(
    h("button", { class: "menu-item", role: "menuitem", onclick: () => { closeMenus(); renameSession(s, item); } }, icon("pen"), "Переименовать"),
    h("button", { class: "menu-item danger", role: "menuitem", onclick: async () => {
      closeMenus();
      if (!confirm(`Удалить разговор «${s.title}»?`)) return;
      try { await api(`/api/sessions/${s.id}`, { method: "DELETE" }); } catch (e) { alert(e.message); return; }
      if (s.id === S.sessionId) newChat();
      loadSessions();
    } }, icon("trash"), "Удалить"));
  const r = btn.getBoundingClientRect();
  pop.style.left = `${Math.min(r.left, innerWidth - 200)}px`; pop.style.top = `${r.bottom + 4}px`;
  pop.hidden = false; pop._owner = btn; btn.setAttribute("aria-expanded", "true");
}
function renameSession(s, item) {
  const input = h("input", { class: "rename", value: s.title });
  const span = item.querySelector("span");
  span.replaceWith(input); input.focus(); input.select();
  const done = async save => {
    if (save && input.value.trim() && input.value.trim() !== s.title) {
      await api(`/api/sessions/${s.id}`, { method: "PATCH", body: { title: input.value.trim() } });
    }
    loadSessions();
  };
  input.addEventListener("keydown", e => { if (e.key === "Enter") done(true); if (e.key === "Escape") done(false); });
  input.addEventListener("blur", () => done(true), { once: true });
}

// ── настройки ───────────────────────────────────────────────────────
const fieldRow = (label, control, help) => h("div", { class: "field-row" }, h("label", {}, label), control, help ? h("div", { class: "help" }, help) : null);
const group = (title, lead, ...kids) => h("div", { class: "group" }, title ? h("h4", {}, title) : null, lead ? h("p", { class: "lead-text" }, lead) : null, ...kids);
function segmented(options, value, onChange) {
  const el = h("div", { class: "segmented", role: "radiogroup" });
  for (const [v, label] of options) {
    const b = h("button", { type: "button", role: "radio", class: v === value ? "on" : "", "aria-checked": String(v === value),
      onclick: () => { el.querySelectorAll("button").forEach(x => { x.classList.remove("on"); x.setAttribute("aria-checked", "false"); });
        b.classList.add("on"); b.setAttribute("aria-checked", "true"); onChange(v); } }, label);
    el.append(b);
  }
  return el;
}
const prefs = {
  get size() { return store.get("agent_text_size") || "normal"; },
  get send() { return store.get("agent_send_key") || "enter"; },
};
function applyPrefs() {
  document.documentElement.style.setProperty("--chat-size", prefs.size === "large" ? "16px" : prefs.size === "small" ? "13.5px" : "14.5px");
  $("#hint").textContent = prefs.send === "ctrl" ? "Ctrl+Enter — отправить · Enter — перенос" : "Enter — отправить · Shift+Enter — перенос";
}

const tabs = {
  async general(body) {
    const p = await api("/api/profile");
    const name = h("input", { value: p.name || "", placeholder: "Иван Петров" });
    const call = h("input", { value: p.call_me || "", placeholder: "Иван" });
    const work = h("input", { value: p.work || "", placeholder: "например: поверитель, выезды по Кирову и области" });
    const instr = h("textarea", { rows: 4, placeholder: "например: отвечай коротко, суммы — в рублях, даты — ДД.ММ.ГГГГ" });
    instr.value = p.instructions || "";
    const saved = h("span", { class: "saved" });
    const save = h("button", { class: "btn primary", type: "button", onclick: async () => {
      save.disabled = true;
      try {
        await api("/api/profile", { method: "PUT", body: { name: name.value, call_me: call.value, work: work.value, instructions: instr.value } });
        S.me = await api("/api/me"); renderMe();
        saved.textContent = "Сохранено"; setTimeout(() => (saved.textContent = ""), 1800);
      } catch (e) { alert(e.message); } finally { save.disabled = false; }
    } }, "Сохранить");
    body.append(
      group("Профиль", null,
        h("div", { class: "two" }, fieldRow("Полное имя", name), fieldRow("Как к вам обращаться", call)),
        fieldRow("Чем вы занимаетесь", work, "Агент учитывает это, чтобы отвечать по делу"),
        fieldRow("Пожелания к ответам", instr, "Учитываются в каждом разговоре"),
        h("div", { class: "save-bar" }, saved, save)),
      group("Внешний вид", null,
        h("div", { class: "field-row" }, h("label", {}, "Тема"), themeCards()),
        h("div", { class: "field-row" }, h("label", {}, "Размер текста в чате"),
          segmented([["small", "Мельче"], ["normal", "Обычный"], ["large", "Крупнее"]], prefs.size, v => { store.set("agent_text_size", v); applyPrefs(); }))),
      group("Ввод", null,
        h("div", { class: "field-row" }, h("label", {}, "Отправка сообщения"),
          segmented([["enter", "Enter"], ["ctrl", "Ctrl + Enter"]], prefs.send, v => { store.set("agent_send_key", v); applyPrefs(); }),
          h("div", { class: "help" }, "Ctrl + Enter удобнее, если часто пишете в несколько строк"))));
  },
  models: modelsTab,
  computer: computerTab,
  memory: (b) => panels.memory(b),
  tools: (b) => panels.tools(b),
  schedules: (b) => panels.schedules(b),
  usage: (b) => panels.usage(b),
  async plan(body) {
    const p = await api("/api/plan");
    const spent = p.usage?.cost || 0;
    const limit = p.limit_usd;
    const pct = limit ? Math.min(100, (spent / limit) * 100) : 0;
    const month = new Date(p.period_start * 1000).toLocaleDateString("ru-RU", { month: "long" });
    body.append(
      h("div", { class: "plan-card" },
        h("div", { class: "muted", style: "font-size:12px" }, "Текущий тариф"),
        h("div", { class: "plan-name" }, p.name),
        p.renews_at ? h("div", { class: "muted" }, "Продлится " + new Date(p.renews_at * 1000).toLocaleDateString("ru-RU")) : null,
        p.features?.length ? h("ul", {}, p.features.map(f => h("li", {}, f))) : null,
        p.manage_url ? h("div", {}, h("a", { class: "btn primary", href: p.manage_url, target: "_blank", rel: "noopener" }, "Управлять подпиской")) : null),
      group(`Расход за ${month}`, null,
        h("div", { style: "display:flex;justify-content:space-between;font-size:13px" }, h("span", {}, `$${spent.toFixed(2)}`),
          h("span", { class: "muted" }, limit ? `из $${Number(limit).toFixed(2)}` : "без лимита")),
        limit ? h("div", { class: "meter" + (pct >= 100 ? " over" : pct >= 80 ? " warn" : "") }, h("i", { style: `width:${pct}%` })) : null,
        h("div", { class: "muted", style: "font-size:12.5px" }, `${p.usage?.runs || 0} ${plural(p.usage?.runs || 0, "запуск", "запуска", "запусков")} агента`)));
  },
  async account(body) {
    const sessions = S.sessions.length;
    body.append(
      group("Аккаунт", null, h("dl", { class: "kv" },
        h("dt", {}, "Имя"), h("dd", {}, S.me.name),
        h("dt", {}, "Идентификатор"), h("dd", {}, h("code", {}, S.me.id)),
        h("dt", {}, "Роли"), h("dd", {}, S.me.roles?.length ? S.me.roles.join(", ") : "—"))),
      group("Сеанс", null, h("div", { class: "save-bar", style: "justify-content:flex-start" },
        h("button", { class: "btn", type: "button", onclick: logout }, icon("logout"), "Выйти на этом устройстве"))),
      group("Опасная зона", null, h("div", { class: "danger-zone" },
        h("div", {}, h("b", {}, "Удалить все разговоры"), h("p", {}, `${sessions} ${plural(sessions, "разговор", "разговора", "разговоров")} вместе с файлами. Память и настройки останутся.`)),
        h("button", { class: "btn danger", type: "button", onclick: async () => {
          if (!confirm("Удалить все разговоры без возможности восстановления?")) return;
          for (const x of S.sessions) { try { await api(`/api/sessions/${x.id}`, { method: "DELETE" }); } catch { /* идёт прогон — пропускаем */ } }
          newChat(); await loadSessions(); openSettings("account");
        } }, "Удалить"))));
  },
};
const TAB_TITLES = { general: "Общие", models: "Модели", computer: "Компьютер", memory: "Память", tools: "Инструменты", schedules: "Расписание", usage: "Расход", plan: "Подписка", account: "Аккаунт" };

function themeCards() {
  const cur = store.get("agent_theme") || "auto";
  const el = h("div", { class: "choice-cards" });
  for (const [v, label] of [["auto", "Как в системе"], ["light", "Светлая"], ["dark", "Тёмная"]]) {
    el.append(h("button", { type: "button", class: "choice-card" + (v === cur ? " on" : ""), onclick: () => {
      store.set("agent_theme", v); applyTheme(v); el.querySelectorAll(".choice-card").forEach(c => c.classList.toggle("on", c.dataset.v === v));
    }, "data-v": v }, h("div", { class: "preview pv-" + v }), h("span", {}, label)));
  }
  return el;
}

let lastFocus = null;
async function openSettings(tab = "general") {
  closeMenus(); toggleSidebar(false);
  if ($("#settings").hidden) lastFocus = document.activeElement;
  $("#settings").hidden = false;
  for (const b of document.querySelectorAll("#settings-nav [data-tab]")) b.classList.toggle("on", b.dataset.tab === tab);
  $("#tab-title").textContent = TAB_TITLES[tab];
  const body = $("#tab-body");
  if (body.dataset.tab !== tab) body.replaceChildren(h("p", { class: "muted" }, "Загрузка…"));
  body.dataset.tab = tab;
  const fresh = h("div", { style: "display:contents" });
  try { await tabs[tab](fresh); body.replaceChildren(...fresh.childNodes); } catch (e) { body.replaceChildren(h("p", { class: "error-text" }, e.message)); }
  $("#settings .modal-card").focus({ preventScroll: true });
}
function closeSettings() { $("#settings").hidden = true; lastFocus?.focus?.(); refreshDevices(); }
const openPanel = openSettings; // совместимость с кодом панелей

// ── тема ────────────────────────────────────────────────────────────
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  for (const b of document.querySelectorAll("[data-theme-set]")) b.classList.toggle("on", b.dataset.themeSet === t);
}

// ── старт ───────────────────────────────────────────────────────────
function autosize() { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, innerHeight * 0.4) + "px"; updateSend(); }
function logout() { store.set("agent_token", null); S.token = null; closeSettings(); closeMenus(); $("#app").hidden = true; $("#login").hidden = false; }
function toggleSidebar(open) { $("#sidebar").classList.toggle("open", open); $("#scrim").hidden = !open; }
function toggleTasks(open = $("#tasks").hidden) {
  if (open && S.preview) closePreview(); // сбоку — одна панель за раз
  $("#tasks").hidden = !open;
  $("#toggle-tasks").classList.toggle("on", open); $("#toggle-tasks").setAttribute("aria-expanded", String(open));
  // На узком экране панель — поверх чата: нужен фон, по которому её можно закрыть.
  if (innerWidth <= 960) $("#scrim").hidden = !open;
}
function newChat() { S.sessionId = null; S.projectId = null; setCrumb(null); showPage(false); history.replaceState(null, "", "#"); resetView(); $("#chat-title").textContent = "Новый разговор"; renderSessions(); toggleSidebar(false); $("#input").focus(); }
async function route(hash) {
  if (hash === "projects") { await loadSessions(); return openProjects(); }
  if (hash.startsWith("p/")) { await loadSessions(); return openProject(hash.slice(2)); }
  if (hash) return openSession(hash);
  resetView(); await loadSessions();
}
function renderMe() {
  $("#me-name").textContent = S.me.name;
  $("#me-avatar").textContent = (S.me.name || "?").trim().charAt(0).toUpperCase();
  $("#menu-id").textContent = S.me.id;
}

async function boot() {
  applyTheme(store.get("agent_theme") || "auto");
  applyPrefs();
  if (!S.token) { $("#login").hidden = false; return; }
  try { S.config = await api("/api/config"); } catch (e) {
    // 401 уже вернул на вход; остальное (сервер лежит, прокси) — не оставляем пустую страницу.
    $("#login").hidden = false; $("#app").hidden = true;
    $("#login-error").textContent = S.token ? `Сервер не ответил: ${e.message}. Обновите страницу.` : "";
    return;
  }
  S.me = await api("/api/me");
  const name = S.config.name;
  document.title = name;
  $("#app-name").textContent = name; $("#login-title").textContent = name;
  for (const el of document.querySelectorAll(".logo-mark")) el.textContent = name.trim().charAt(0).toUpperCase();
  renderMe();
  api("/api/plan").then(p => { $("#me-plan").textContent = p.name; $("#menu-plan").textContent = p.name; }).catch(() => {});
  if (S.config.help_url) { $("#menu-help").href = S.config.help_url; $("#menu-help").hidden = false; }
  setModel(S.config.default_model);
  loadCatalog().catch(() => {}); // имена и цены моделей — не задерживая старт
  setMode(S.config.default_mode);
  $("#login").hidden = true; $("#app").hidden = false;
  const id = location.hash.slice(1);
  await route(id);
  refreshDevices(); setInterval(refreshDevices, 30000);
  $("#input").focus();
}

$("#login-form").addEventListener("submit", async e => {
  e.preventDefault();
  S.token = $("#token-input").value.trim(); store.set("agent_token", S.token);
  $("#login-error").textContent = "";
  try { await api("/api/me"); boot(); } catch { $("#login-error").textContent = "Токен не подошёл"; logout(); }
});
$("#composer").addEventListener("submit", e => { e.preventDefault(); send(); });
$("#input").addEventListener("keydown", e => {
  if (e.key !== "Enter" || e.isComposing) return;
  const ctrl = e.ctrlKey || e.metaKey;
  if (prefs.send === "ctrl" ? ctrl : !e.shiftKey && !ctrl) { e.preventDefault(); send(); }
});
$("#input").addEventListener("input", autosize);
$("#stop").addEventListener("click", () => S.sessionId && api(`/api/sessions/${S.sessionId}/cancel`, { method: "POST" }));
$("#new-chat").addEventListener("click", newChat);
// Ссылки вида …/#p/<id> и кнопка «назад» браузера. Свои переходы идут через replaceState и сюда не попадают.
window.addEventListener("hashchange", () => S.config && route(location.hash.slice(1)));
$("#device-chip").addEventListener("click", () => openSettings("computer"));
$("#logout").addEventListener("click", logout);
$("#user-btn").addEventListener("click", e => { e.stopPropagation(); toggleUserMenu(); });
document.querySelectorAll("[data-settings]").forEach(b => b.addEventListener("click", () => openSettings(b.dataset.settings)));
document.querySelectorAll("[data-theme-set]").forEach(b => b.addEventListener("click", e => {
  e.stopPropagation(); store.set("agent_theme", b.dataset.themeSet); applyTheme(b.dataset.themeSet);
}));
document.querySelectorAll("#settings-nav [data-tab]").forEach(b => b.addEventListener("click", () => openSettings(b.dataset.tab)));
$("#settings-close").addEventListener("click", closeSettings);
$("#settings").addEventListener("mousedown", e => { if (e.target.id === "settings") closeSettings(); });
document.addEventListener("click", e => { if (!e.target.closest(".menu, #user-btn, .more")) closeMenus(); });
$("#menu").addEventListener("click", () => toggleSidebar(true));
$("#scrim").addEventListener("click", () => { toggleSidebar(false); if (!$("#tasks").hidden && innerWidth <= 960) toggleTasks(false); if (S.preview && innerWidth <= 960) closePreview(); });
$("#session-filter").addEventListener("input", renderSessions);
$("#toggle-tasks").addEventListener("click", () => { S.tasksShown = true; toggleTasks(); });
$("#tasks-close").addEventListener("click", () => toggleTasks(false));
$("#to-bottom").addEventListener("click", () => scroll(true));
$("#messages").addEventListener("scroll", () => { $("#to-bottom").hidden = nearBottom(); }, { passive: true });
$("#mode-btn").addEventListener("click", e => { e.stopPropagation(); openModePicker(); });
$("#model-btn").addEventListener("click", e => { e.stopPropagation(); openModelPicker(); });
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    if (!$("#user-menu").hidden || !$("#popover").hidden || !$("#model-pop").hidden || !$("#mode-pop").hidden) closeMenus();
    else if (!$("#settings").hidden) closeSettings();
    else if (S.preview) closePreview();
    else toggleSidebar(false);
  }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); newChat(); }
  if ((e.ctrlKey || e.metaKey) && e.key === ",") { e.preventDefault(); openSettings("general"); }
});

async function mcpConnect(name) {
  // Окно открываем сразу по клику — иначе браузер сочтёт его всплывающим и заблокирует.
  const w = window.open("about:blank", "oauth", "width=520,height=720");
  try {
    const r = await api(`/api/mcp/${encodeURIComponent(name)}/connect`, { method: "POST" });
    if (w) w.location = r.url; else location.href = r.url;
  } catch (e) { w?.close(); alert(e.message); }
}
window.addEventListener("message", e => {
  if (e.data?.type !== "agentkit-oauth") return;
  if (!$("#settings").hidden && $("#tab-body").dataset.tab === "tools") openSettings("tools");
  if (e.data.ok) note(`Подключено: ${e.data.text}`); else alertMsg(`Не удалось подключить: ${e.data.text}`);
});

$("#attach").addEventListener("click", () => $("#file").click());
$("#file").addEventListener("change", e => { attach(e.target.files); e.target.value = ""; });
$("#input").addEventListener("paste", e => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) { e.preventDefault(); attach(files); }
});
for (const ev of ["dragover", "dragenter"]) document.addEventListener(ev, e => { if (e.dataTransfer?.types?.includes("Files")) { e.preventDefault(); document.body.classList.add("dragging"); } });
document.addEventListener("dragleave", e => { if (!e.relatedTarget) document.body.classList.remove("dragging"); });
document.addEventListener("drop", e => {
  if (!e.dataTransfer?.files?.length) return;
  e.preventDefault(); document.body.classList.remove("dragging"); attach(e.dataTransfer.files);
});
boot();
