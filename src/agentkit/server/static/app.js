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
function add(el, { keepGroup = false } = {}) {
  $("#empty")?.remove();
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
    img.addEventListener("click", () => blobURL(f.path).then(u => window.open(u)));
    return img;
  }
  return h("button", { class: "file-chip", title: "Скачать", type: "button", onclick: () => blobURL(f.path).then(u => {
    const a = h("a", { href: u, download: f.name }); document.body.append(a); a.click(); a.remove(); }) },
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

function stepRow(id, name, args, title, agent) {
  if (S.steps.has(id)) return S.steps.get(id);
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
  if (list.length && !S.tasksShown && innerWidth > 960) { $("#tasks").hidden = false; S.tasksShown = true; }
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
        renderMd(S.bubble, d.content);
      } else S.bubble?.parentElement.remove();
      S.bubble?.classList.remove("streaming");
      S.bubble = null; S.bubbleText = "";
      for (const tc of d.tool_calls || []) stepRow(tc.id, tc.function.name, safeJSON(tc.function.arguments));
      break;
    case "tool_call": stepRow(d.id, d.name, d.args, d.title, ev.agent); break;
    case "tool_result": stepResult(d.id, d.content, d.is_error, ev.agent ? [] : d.ui, d.elapsed_ms); break;
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
  const s = await api("/api/sessions", { method: "POST", body: { model: $("#model").value, mode: $("#mode").value } });
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
    const r = await api(`/api/sessions/${sid}/messages`, { method: "POST", body: { text, files, model: $("#model").value, mode: $("#mode").value } });
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
  S.sessions = await api("/api/sessions");
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
      }, h("span", {}, s.title), s.running ? h("i", { class: "dot", title: "Агент работает" }) : null, more);
      more.addEventListener("click", e => { e.stopPropagation(); sessionMenu(s, item, more); });
      out.push(item);
    }
  }
  if (!out.length) out.push(h("div", { class: "group-label" }, q ? "Ничего не найдено" : "Разговоров пока нет"));
  $("#sessions").replaceChildren(...out);
}

function greeting() {
  const hr = new Date().getHours();
  const part = hr < 5 ? "Доброй ночи" : hr < 12 ? "Доброе утро" : hr < 18 ? "Добрый день" : "Добрый вечер";
  const name = S.me?.call_me || (S.me?.name || "").split(/\s+/)[0];
  return name && name !== S.me?.id ? `${part}, ${name}` : part;
}
function emptyState() {
  const sugg = S.config?.suggestions || [];
  return h("div", { class: "empty", id: "empty" },
    h("h1", {}, greeting()),
    h("p", {}, S.config?.description || "Опишите задачу — агент сам найдёт, посчитает и подготовит."),
    sugg.length ? h("div", { class: "suggestions" }, sugg.map(t => h("button", { class: "suggestion", type: "button", onclick: () => send(t) }, t))) : null,
    h("div", { class: "facts" },
      h("span", {}, icon("shield"), "Изменения — только после вашего подтверждения"),
      h("span", {}, icon("clip"), "Можно приложить файлы и фото"),
      h("span", {}, icon("send"), "Можно дописывать, пока агент работает")));
}

function resetView() {
  S.stream?.abort();
  S.steps.clear(); S.subs.clear(); S.cards.clear(); S.bubble = null; S.group = null; S.lastSeq = 0; setCost(0);
  box().replaceChildren(emptyState());
  S.tasksShown = false; renderTodos([]); $("#tasks").hidden = true; setRunning(false);
  S.queued = []; S.attachments = []; renderAttachments();
}

async function openSession(id) {
  resetView();
  S.sessionId = id;
  history.replaceState(null, "", `#${id}`);
  let s;
  try { s = await api(`/api/sessions/${id}`); } catch { S.sessionId = null; history.replaceState(null, "", "#"); return; }
  $("#chat-title").textContent = s.title || "Новый разговор";
  if (s.state?.model) $("#model").value = s.state.model;
  if (s.state?.mode) $("#mode").value = s.state.mode;
  if (!s.messages.length) box().replaceChildren(emptyState());
  for (const m of s.messages) {
    if (m.meta?.hidden) continue;
    if (m.role === "user") userMsg(m.content, m.seq);
    else if (m.role === "assistant") {
      if (m.content) { const b = assistantBubble(); b.classList.remove("streaming"); renderMd(b, m.content); }
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

// ── панели ──────────────────────────────────────────────────────────
const row = (main, ...right) => h("div", { class: "row" }, h("div", { class: "grow" }, main), ...right);
const panels = {
  async memory(body) {
    const items = await api("/api/memory");
    const input = h("textarea", { rows: 2, placeholder: "Правило или факт, например: «отчёты присылай по пятницам»" });
    const scope = S.config.is_admin ? h("select", {}, h("option", { value: "user" }, "Только для меня"), h("option", { value: "global" }, "Для всех")) : null;
    body.append(h("div", { class: "form" }, input, h("div", { class: "actions" }, scope,
      h("button", { class: "btn primary", onclick: async () => { if (!input.value.trim()) return; await api("/api/memory", { method: "POST", body: { text: input.value, scope: scope?.value || "user" } }); openSettings("memory"); } }, "Добавить"))));
    if (!items.length) body.append(h("p", { class: "muted" }, "Пока пусто. Скажите агенту «запомни: …» — правило появится здесь и будет учитываться в каждом разговоре."));
    for (const m of items) body.append(row(m.text,
      h("span", { class: "badge" }, m.scope === "global" ? "для всех" : "моё"),
      h("button", { class: "icon-btn", title: "Удалить", onclick: async () => { await api(`/api/memory/${m.id}`, { method: "DELETE" }); openSettings("memory"); } }, icon("x"))));
  },
  async tools(body) {
    const d = await api("/api/tools");
    if (S.config.is_admin) body.append(h("div", { class: "form" }, h("div", { class: "actions" },
      h("span", { class: "muted", style: "flex:1;font-size:12.5px" }, "mcp.json и skills читаются при старте"),
      h("button", { class: "btn sm", onclick: async () => { await api("/api/reload", { method: "POST" }); openSettings("tools"); } }, "Перечитать"))));
    if (d.mcp.length) {
      body.append(h("div", { class: "section-title" }, "MCP-серверы"));
      for (const s of d.mcp) {
        let action = null;
        if (S.config.is_admin && s.needs_auth) action = h("button", { class: "btn primary sm", onclick: () => mcpConnect(s.name) }, "Подключить");
        else if (S.config.is_admin && s.oauth) action = h("button", { class: "btn ghost sm", onclick: async () => {
          if (!confirm(`Отключить ${s.name}? Токены будут отозваны.`)) return;
          await api(`/api/mcp/${encodeURIComponent(s.name)}/connect`, { method: "DELETE" }); openSettings("tools"); } }, "Отключить");
        body.append(row([h("div", {}, h("b", {}, s.name), s.oauth ? h("span", { class: "muted" }, " · OAuth") : null),
          !s.ok && s.error ? h("div", { class: "sub-text" }, s.error) : null],
          h("span", { class: "badge " + (s.ok ? "ok" : s.needs_auth ? "warn" : "err") }, s.ok ? `${s.tools} ${plural(s.tools, "инструмент", "инструмента", "инструментов")}` : s.needs_auth ? "нужен вход" : "нет связи"),
          action));
      }
    }
    if (d.skills.length) {
      body.append(h("div", { class: "section-title" }, "Skills"));
      for (const k of d.skills) body.append(row([h("b", {}, k.name), h("div", { class: "sub-text" }, k.description)]));
    }
    const filter = h("input", { class: "field filter", placeholder: `Найти среди ${d.tools.length} инструментов` });
    const list = h("div");
    const draw = () => {
      const q = filter.value.trim().toLowerCase();
      list.replaceChildren(...d.tools.filter(t => !q || (t.name + t.title + t.description).toLowerCase().includes(q)).map(t => row(
        [h("div", {}, h("b", {}, toolTitle(t.name, t.title)), " ", h("code", {}, t.name)), h("div", { class: "sub-text" }, t.description)],
        t.deferred ? h("span", { class: "badge", title: "Схема подгружается по запросу агента" }, "по запросу") : null,
        h("span", { class: "badge " + (t.read_only ? "ok" : t.destructive ? "err" : "warn") }, t.read_only ? "чтение" : t.destructive ? "необратимо" : "запись"))));
    };
    filter.addEventListener("input", draw);
    body.append(h("div", { class: "section-title" }, "Инструменты"), filter, list);
    draw();
  },
  async schedules(body) {
    const items = await api("/api/schedules");
    const name = h("input", { placeholder: "Название, например «Утренний отчёт»" });
    const cron = h("input", { placeholder: "Когда: cron, например 45 8 * * 1-5 — по будням в 8:45" });
    const prompt = h("textarea", { rows: 3, placeholder: "Что сделать агенту" });
    body.append(h("div", { class: "form" }, name, cron, prompt, h("div", { class: "actions" }, h("button", { class: "btn primary", onclick: async () => {
      try { await api("/api/schedules", { method: "POST", body: { name: name.value, cron: cron.value, prompt: prompt.value } }); openSettings("schedules"); }
      catch (e) { alert(e.message); } } }, "Добавить"))));
    if (!items.length) body.append(h("p", { class: "muted" }, "Задач по расписанию нет. Результат каждого запуска появится отдельным разговором."));
    for (const s of items) body.append(row([h("b", {}, s.name), h("div", { class: "sub-text" }, s.prompt),
      h("div", { class: "sub-text" }, h("code", {}, s.cron), s.next_run_at ? " · следующий запуск " + new Date(s.next_run_at * 1000).toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" }) : "")],
      h("button", { class: "icon-btn", title: "Удалить", onclick: async () => { await api(`/api/schedules/${s.id}`, { method: "DELETE" }); openSettings("schedules"); } }, icon("x"))));
  },
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
      for (const [m, c] of models) body.append(row(h("code", {}, m), h("span", {}, `$${c.toFixed(4)}`)));
    }
  },
};

// ── всплывающие меню ────────────────────────────────────────────────
function closeMenus() {
  $("#user-menu").hidden = true; $("#user-btn").setAttribute("aria-expanded", "false");
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
const TAB_TITLES = { general: "Общие", memory: "Память", tools: "Инструменты", schedules: "Расписание", usage: "Расход", plan: "Подписка", account: "Аккаунт" };

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
function closeSettings() { $("#settings").hidden = true; lastFocus?.focus?.(); }
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
function newChat() { S.sessionId = null; history.replaceState(null, "", "#"); resetView(); $("#chat-title").textContent = "Новый разговор"; renderSessions(); toggleSidebar(false); $("#input").focus(); }
function renderMe() {
  $("#me-name").textContent = S.me.name;
  $("#me-avatar").textContent = (S.me.name || "?").trim().charAt(0).toUpperCase();
  $("#menu-id").textContent = S.me.id;
}

async function boot() {
  applyTheme(store.get("agent_theme") || "auto");
  applyPrefs();
  if (!S.token) { $("#login").hidden = false; return; }
  try { S.config = await api("/api/config"); } catch { return; }
  S.me = await api("/api/me");
  const name = S.config.name;
  document.title = name;
  $("#app-name").textContent = name; $("#login-title").textContent = name;
  for (const el of document.querySelectorAll(".logo-mark")) el.textContent = name.trim().charAt(0).toUpperCase();
  renderMe();
  api("/api/plan").then(p => { $("#me-plan").textContent = p.name; $("#menu-plan").textContent = p.name; }).catch(() => {});
  if (S.config.help_url) { $("#menu-help").href = S.config.help_url; $("#menu-help").hidden = false; }
  $("#model").replaceChildren(...S.config.models.map(m => h("option", { value: m }, m.replace(/^[^:]+:/, "").replace(/^[^/]+\//, ""))));
  $("#model").value = S.config.default_model;
  const modeNames = { default: "Спрашивать", accept_edits: "Правки без вопросов", plan: "Только план", readonly: "Только чтение", auto: "Авто", bypass: "Без вопросов" };
  const modeHelp = { default: "Любое изменение данных — через карточку подтверждения", accept_edits: "Файлы рабочей папки агент правит сам, остальное — с подтверждением",
    plan: "Агент только изучает и предлагает план, ничего не меняет", readonly: "Только чтение", auto: "Изменения без подтверждения, кроме отмеченных как опасные",
    bypass: "Всё без подтверждения — только для доверенных задач" };
  $("#mode").replaceChildren(...S.config.modes.map(m => h("option", { value: m, title: modeHelp[m] }, modeNames[m] || m)));
  $("#mode").value = S.config.default_mode;
  $("#login").hidden = true; $("#app").hidden = false;
  const id = location.hash.slice(1);
  if (id) await openSession(id); else { resetView(); await loadSessions(); }
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
$("#scrim").addEventListener("click", () => toggleSidebar(false));
$("#session-filter").addEventListener("input", renderSessions);
$("#toggle-tasks").addEventListener("click", () => ($("#tasks").hidden = !$("#tasks").hidden));
for (const id of ["model", "mode"]) $("#" + id).addEventListener("change", () => S.sessionId && api(`/api/sessions/${S.sessionId}`, { method: "PATCH", body: { [id]: $("#" + id).value } }));
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    if (!$("#user-menu").hidden || !$("#popover").hidden) closeMenus();
    else if (!$("#settings").hidden) closeSettings();
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
