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
  for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
};

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* приватный режим */ } },
};

const S = {
  token: store.get("agent_token"),
  config: null,
  sessionId: null,
  lastSeq: 0,
  stream: null,      // AbortController текущего SSE
  running: false,
  bubble: null,      // текущий пузырь стрима
  bubbleText: "",
  tools: new Map(),  // tool_call_id → элемент
  subs: new Map(),   // id дочерней сессии → контейнер в блоке task
  cards: new Map(),  // approval_id → карточка
  cost: 0,
  attachments: [],   // загруженные файлы для следующего сообщения
  uploading: 0,
  queued: [],        // пузыри сообщений, отправленных во время работы агента
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

// ── Markdown (минимум: заголовки, списки, таблицы, код, ссылки, жирный) ─
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

// ── рендер ──────────────────────────────────────────────────────────
const box = () => $("#messages");
function scroll(force) {
  const el = box();
  if (force || el.scrollHeight - el.scrollTop - el.clientHeight < 160) el.scrollTop = el.scrollHeight;
}
function add(el) { $("#empty")?.remove(); box().append(el); scroll(); return el; }

function splitContent(content) {
  if (typeof content === "string") return { text: content, files: [] };
  const parts = Array.isArray(content) ? content : [];
  return { text: parts.filter(p => p.type === "text").map(p => p.text).join("\n"), files: parts.filter(p => p.type === "file") };
}

function userMsg(content, seq, { queued = false } = {}) {
  const { text, files } = splitContent(content);
  const rewind = seq ? h("button", { class: "link-btn small", title: "Откатить разговор и файлы к моменту до этого сообщения",
    onclick: () => doRewind(seq, text) }, "↺ изменить") : null;
  const badge = queued ? h("span", { class: "badge queued" }, "агент прочтёт после текущего шага") : null;
  const el = add(h("div", { class: "msg user" + (queued ? " queued" : "") },
    files.length ? h("div", { class: "files" }, files.map(fileView)) : null,
    text ? h("div", { class: "bubble" }, text) : null,
    h("div", { class: "tools-row" }, badge, rewind)));
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
  return h("button", { class: "file-chip", title: "Скачать", onclick: () => blobURL(f.path).then(u => {
    const a = h("a", { href: u, download: f.name }); document.body.append(a); a.click(); a.remove(); }) },
    "📄 ", f.name, h("small", {}, " " + humanSize(f.size)));
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
function humanSize(n) { return !n ? "" : n < 1024 ? `${n} Б` : n < 1048576 ? `${(n / 1024).toFixed(0)} КБ` : `${(n / 1048576).toFixed(1)} МБ`; }

function assistantBubble() {
  const b = h("div", { class: "bubble md streaming" });
  add(h("div", { class: "msg assistant" }, b));
  return b;
}

function brief(args) {
  const s = JSON.stringify(args || {});
  return s.length > 120 ? s.slice(0, 120) + "…" : s;
}

function toolBlock(id, name, args, agent) {
  if (S.tools.has(id)) return S.tools.get(id);
  const state = h("span", { class: "state run" });
  const body = h("div", { class: "body" }, h("pre", {}, JSON.stringify(args || {}, null, 2)));
  const el = h("details", { class: "tool" },
    h("summary", {}, state, h("span", { class: "name" }, prettyName(name)), h("span", { class: "brief" }, brief(args))), body);
  el._state = state; el._body = body;
  S.tools.set(id, el);
  if (agent && S.subs.has(agent)) S.subs.get(agent).append(h("div", {}, `→ ${prettyName(name)} ${brief(args)}`));
  else add(el);
  return el;
}
function prettyName(n) { return n.startsWith("mcp__") ? n.slice(5).replace("__", " · ") : n; }

function toolResult(id, content, isError, ui) {
  const el = S.tools.get(id);
  if (!el) return;
  el._state.className = "state " + (isError ? "err" : "ok");
  el._state.textContent = isError ? "✕" : "✓";
  el._body.append(h("pre", {}, content || ""));
  for (const w of ui || []) el.after(widget(w));
}

function widget(w) {
  if (w.type === "table") {
    const t = h("table", {}, h("thead", {}, h("tr", {}, (w.columns || []).map(c => h("th", {}, c)))),
      h("tbody", {}, (w.rows || []).map(r => h("tr", {}, r.map(c => h("td", {}, c ?? ""))))));
    return h("div", { class: "msg assistant" }, h("div", { class: "bubble md" }, w.title ? h("strong", {}, w.title) : null, t));
  }
  if (w.type === "kv") {
    return h("div", { class: "msg assistant" }, h("div", { class: "bubble md" },
      h("table", {}, h("tbody", {}, (w.items || []).map(([k, v]) => h("tr", {}, h("td", {}, k), h("td", {}, v)))))));
  }
  if (w.type === "attachments") {
    return h("div", { class: "msg assistant" }, (w.items || []).filter(a => a.type === "image").map(a =>
      h("img", { src: `data:${a.mime};base64,${a.data}`, style: "max-width:100%;border-radius:10px" })));
  }
  if (w.type === "choices") return choices(w.question, w.options || []);
  return h("div", {});
}

function choices(question, options) {
  const wrap = h("div", { class: "choices" });
  for (const o of options) {
    wrap.append(h("button", { class: "choice", onclick: () => { wrap.remove(); send(o.label); } },
      o.label, o.description ? h("small", {}, o.description) : null));
  }
  return wrap;
}

function approvalCard(a) {
  if (S.cards.has(a.approval_id)) return;
  const comment = h("input", { placeholder: "Комментарий (необязательно)" });
  const yes = h("button", { class: "btn primary", onclick: () => decide(a.approval_id, true, comment.value, card) }, "Подтвердить");
  const no = h("button", { class: "btn danger", onclick: () => decide(a.approval_id, false, comment.value, card) }, "Отклонить");
  const rows = (a.view || []).map(([k, v]) => h("tr", {}, h("td", {}, k), h("td", {}, v)));
  const card = h("div", { class: "card" + (a.destructive ? " destructive" : "") },
    h("div", { class: "title" }, "Нужно подтверждение: ", prettyName(a.tool || a.tool_name)),
    a.reason ? h("div", { class: "muted" }, a.reason) : null,
    h("table", {}, h("tbody", {}, rows)),
    h("div", { class: "actions" }, comment, no, yes));
  S.cards.set(a.approval_id, card);
  add(card);
  const t = S.tools.get(a.tool_call_id);
  if (t) { t._state.className = "state wait"; t._state.textContent = "⏸"; }
}

function closeCard(id, approved, expired) {
  const c = S.cards.get(id);
  if (!c) return;
  c.classList.add("done");
  c.querySelector(".actions")?.replaceWith(h("div", { class: "muted" }, expired ? "Не выполнено" : approved ? "Подтверждено" : "Отклонено"));
}

function note(text, err) { add(h("div", { class: "note" + (err ? " err" : "") }, text)); }

function renderTodos(todos) {
  const ol = $("#todo-list");
  ol.replaceChildren(...(todos?.length ? todos.map(t => h("li", { class: t.status },
    h("span", { class: "st" }, t.status === "completed" ? "✓" : t.status === "in_progress" ? "▸" : "○"), t.content))
    : [h("li", { class: "muted" }, "Пока нет")]));
  if (todos?.length) $("#tasks").hidden = false;
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
      if (el) { el.classList.remove("queued"); el._badge?.remove(); }
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
        S.bubble.innerHTML = md(d.content);
      } else S.bubble?.parentElement.remove();
      S.bubble?.classList.remove("streaming");
      S.bubble = null; S.bubbleText = "";
      for (const tc of d.tool_calls || []) toolBlock(tc.id, tc.function.name, safeJSON(tc.function.arguments));
      break;
    case "tool_call": toolBlock(d.id, d.name, d.args, ev.agent); break;
    case "tool_result":
      if (ev.agent && S.subs.has(ev.agent)) { if (d.is_error) S.subs.get(ev.agent).append(h("div", {}, `  ✕ ${d.content.slice(0, 160)}`)); }
      else toolResult(d.id, d.content, d.is_error, d.ui);
      break;
    case "subagent_started": {
      const t = S.tools.get(d.tool_call_id);
      const sub = h("div", { class: "sub" }, h("div", {}, `субагент ${d.type}: ${d.description}`));
      S.subs.set(d.session_id, sub);
      t?._body.append(sub); break;
    }
    case "approval_required": approvalCard(d); break;
    case "approval_resolved": closeCard(d.approval_id, d.approved, d.expired); break;
    case "ask_user": add(choices(d.question, d.options)); break;
    case "todo": if (!ev.agent) renderTodos(d.todos); break;
    case "compacted": note("История сжата, чтобы поместиться в контекст"); break;
    case "notice": note(d.message); break;
    case "error": note(d.message, true); break;
    case "usage": setCost(S.cost + (d.cost || 0)); break;
    case "run_finished": {
      setRunning(false);
      S.bubble?.classList.remove("streaming"); S.bubble = null;
      const st = d.status;
      if (st === "max_steps") note("Достигнут лимит шагов — напишите «продолжай», чтобы агент продолжил");
      else if (st === "cancelled") note("Остановлено");
      else if (st === "error" && d.error) note(d.error, true);
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
  $("#input").placeholder = on ? "Дописать агенту на ходу…" : "Сообщение…";
}

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
  try {
    const r = await api(`/api/sessions/${sid}/messages`, { method: "POST", body: { text, files, model: $("#model").value, mode: $("#mode").value } });
    if (!r.queued) { setRunning(true); follow(); }
    else if (!wasRunning) { S.queued.push(el); el.classList.add("queued"); setRunning(true); follow(); }
  } catch (e) { note(e.message, true); }
  loadSessions();
}

// ── вложения ────────────────────────────────────────────────────────
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
      if (!r.ok) { note(`${f.name}: ${d.detail || r.statusText}`, true); continue; }
      S.attachments.push(d);
    }
  } finally { S.uploading--; renderAttachments(); }
}

function renderAttachments() {
  const box = $("#attachments");
  box.replaceChildren(...S.attachments.map((a, i) => h("span", { class: "chip" }, a.kind === "image" ? "🖼 " : "📄 ", a.name,
    a.note ? h("small", { class: "muted" }, " — " + a.note) : null,
    h("button", { class: "chip-x", "aria-label": "Убрать", onclick: () => { S.attachments.splice(i, 1); renderAttachments(); } }, "×"))),
    S.uploading ? h("span", { class: "chip muted" }, "загрузка…") : null);
  box.hidden = !S.attachments.length && !S.uploading;
}

async function decide(id, approve, comment, card) {
  card.querySelectorAll("button").forEach(b => (b.disabled = true));
  try {
    await api(`/api/approvals/${id}`, { method: "POST", body: { approve, comment } });
    setRunning(true); follow();
  } catch (e) { note(e.message, true); card.querySelectorAll("button").forEach(b => (b.disabled = false)); }
}

async function doRewind(seq, text) {
  if (S.running) return;
  if (!confirm("Откатить разговор (и файлы агента) к моменту до этого сообщения?")) return;
  await api(`/api/sessions/${S.sessionId}/rewind`, { method: "POST", body: { seq: seq - 1 } });
  await openSession(S.sessionId);
  $("#input").value = text; autosize(); $("#input").focus();
}

// ── сессии ──────────────────────────────────────────────────────────
async function loadSessions() {
  const items = await api("/api/sessions");
  const cur = items.find(s => s.id === S.sessionId);
  if (cur) $("#chat-title").textContent = cur.title;
  $("#sessions").replaceChildren(...items.map(s => h("div", {
    class: "session" + (s.id === S.sessionId ? " active" : ""), onclick: () => { openSession(s.id); $("#sidebar").classList.remove("open"); },
  }, s.running ? h("i", { class: "dot" }) : null, h("span", {}, s.title))));
}

function resetView() {
  S.stream?.abort();
  S.tools.clear(); S.subs.clear(); S.cards.clear(); S.bubble = null; S.lastSeq = 0; setCost(0);
  box().replaceChildren(h("div", { class: "empty", id: "empty" }, h("h3", {}, "Чем помочь?"),
    h("p", { class: "muted" }, "Агент сам вызывает инструменты. Изменения — через карточку подтверждения.")));
  renderTodos([]); $("#tasks").hidden = true; setRunning(false);
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
  for (const m of s.messages) {
    if (m.meta?.hidden) continue;
    if (m.role === "user") userMsg(m.content, m.seq);
    else if (m.role === "assistant") {
      if (m.content) { const b = assistantBubble(); b.classList.remove("streaming"); b.innerHTML = md(m.content); }
      for (const tc of m.tool_calls || []) toolBlock(tc.id, tc.function.name, safeJSON(tc.function.arguments));
    } else if (m.role === "tool") toolResult(m.tool_call_id, m.content, m.meta?.is_error, m.meta?.ui);
  }
  // Варианты ответа живы, только пока вопрос — последнее, что было в разговоре.
  const last = s.messages.filter(m => !m.meta?.hidden).at(-1);
  if (!(last?.role === "tool" && last.name === "ask_user")) box().querySelectorAll(".choices").forEach(e => e.remove());
  for (const a of s.approvals) approvalCard({ ...a, approval_id: a.id, tool: a.tool_name });
  renderTodos(s.state?.todos);
  setCost((s.runs || []).reduce((acc, r) => acc + (r.usage?.cost || 0), 0));
  S.lastSeq = s.last_seq;
  scroll(true);
  if (s.running_run) { setRunning(true); S.lastSeq = Math.max(0, s.last_seq - 0); follow(); }
  loadSessions();
}

// ── панели ──────────────────────────────────────────────────────────
const panels = {
  async memory(body) {
    const items = await api("/api/memory");
    const input = h("textarea", { rows: 2, placeholder: "Что запомнить (правило или факт)" });
    const scope = S.config.is_admin ? h("select", {}, h("option", { value: "user" }, "для меня"), h("option", { value: "global" }, "для всех")) : null;
    body.append(h("div", { class: "form" }, input, h("div", { class: "actions" }, scope,
      h("button", { class: "btn primary", onclick: async () => { if (!input.value.trim()) return; await api("/api/memory", { method: "POST", body: { text: input.value, scope: scope?.value || "user" } }); openPanel("memory"); } }, "Добавить"))));
    if (!items.length) body.append(h("p", { class: "muted" }, "Память пуста. Скажите агенту «запомни: …» или добавьте здесь."));
    for (const m of items) body.append(h("div", { class: "row" }, h("div", { class: "grow" }, m.text),
      h("span", { class: "badge" }, m.scope === "global" ? "все" : "моё"),
      h("button", { class: "link-btn small", onclick: async () => { await api(`/api/memory/${m.id}`, { method: "DELETE" }); openPanel("memory"); } }, "удалить")));
  },
  async tools(body) {
    const d = await api("/api/tools");
    for (const s of d.mcp) {
      let action = null;
      if (S.config.is_admin && s.needs_auth) action = h("button", { class: "btn primary small", onclick: () => mcpConnect(s.name) }, "Подключить");
      else if (S.config.is_admin && s.oauth) action = h("button", { class: "link-btn small", onclick: async () => {
        if (!confirm(`Отключить ${s.name}? Токены будут отозваны.`)) return;
        await api(`/api/mcp/${encodeURIComponent(s.name)}/connect`, { method: "DELETE" }); openPanel("tools"); } }, "отключить");
      body.append(h("div", { class: "row" }, h("div", { class: "grow" }, h("strong", {}, "MCP · " + s.name),
        s.oauth ? h("span", { class: "badge ok" }, " OAuth") : null, s.error && !s.ok ? h("div", { class: "muted" }, s.error) : null),
        h("span", { class: "badge " + (s.ok ? "ok" : s.needs_auth ? "warn" : "err") }, s.ok ? `${s.tools} инстр.` : s.needs_auth ? "нужен вход" : "нет связи"),
        action));
    }
    for (const k of d.skills) body.append(h("div", { class: "row" }, h("div", { class: "grow" }, h("strong", {}, "skill · " + k.name), h("div", { class: "muted" }, k.description))));
    for (const t of d.tools) body.append(h("div", { class: "row" }, h("div", { class: "grow" }, h("code", {}, prettyName(t.name)), h("div", { class: "muted" }, t.description)),
      h("span", { class: "badge " + (t.read_only ? "ok" : t.destructive ? "err" : "warn") }, t.read_only ? "чтение" : t.destructive ? "опасно" : "запись"),
      t.deferred ? h("span", { class: "badge" }, "отложен") : null));
    if (S.config.is_admin) body.prepend(h("button", { class: "btn", onclick: async () => { await api("/api/reload", { method: "POST" }); openPanel("tools"); } }, "Перечитать mcp.json и skills"));
  },
  async schedules(body) {
    const items = await api("/api/schedules");
    const name = h("input", { placeholder: "Название" }), cron = h("input", { placeholder: "cron, напр. 45 8 * * 1-5 (будни 8:45)" });
    const prompt = h("textarea", { rows: 3, placeholder: "Что сделать агенту" });
    body.append(h("div", { class: "form" }, name, cron, prompt, h("button", { class: "btn primary", onclick: async () => {
      try { await api("/api/schedules", { method: "POST", body: { name: name.value, cron: cron.value, prompt: prompt.value } }); openPanel("schedules"); }
      catch (e) { alert(e.message); } } }, "Добавить")));
    for (const s of items) body.append(h("div", { class: "row" }, h("div", { class: "grow" }, h("strong", {}, s.name), h("div", { class: "muted" }, `${s.cron} · ${s.prompt}`),
      s.next_run_at ? h("div", { class: "muted" }, "следующий: " + new Date(s.next_run_at * 1000).toLocaleString("ru-RU")) : null),
      h("button", { class: "link-btn small", onclick: async () => { await api(`/api/schedules/${s.id}`, { method: "DELETE" }); openPanel("schedules"); } }, "удалить")));
  },
  async usage(body) {
    const u = await api("/api/usage?days=30");
    body.append(h("div", { class: "row" }, h("div", { class: "grow" }, "Прогонов за 30 дней"), h("strong", {}, u.runs)),
      h("div", { class: "row" }, h("div", { class: "grow" }, "Токенов: вход / выход / из кэша"), h("strong", {}, `${u.input_tokens} / ${u.output_tokens} / ${u.cached_tokens}`)),
      h("div", { class: "row" }, h("div", { class: "grow" }, "Стоимость"), h("strong", {}, `$${(u.cost || 0).toFixed(4)}`)),
      ...Object.entries(u.cost_by_model || {}).map(([m, c]) => h("div", { class: "row" }, h("div", { class: "grow muted" }, m), h("span", {}, `$${c.toFixed(4)}`))));
  },
};
const titles = { memory: "Память", tools: "Инструменты", schedules: "Расписание", usage: "Расход" };
async function openPanel(name) {
  $("#panel").hidden = false; $("#panel-title").textContent = titles[name];
  const body = $("#panel-body"); body.replaceChildren(h("p", { class: "muted" }, "Загрузка…"));
  const fresh = h("div", { class: "panel-body", style: "padding:0" });
  try { await panels[name](fresh); body.replaceChildren(...fresh.childNodes); } catch (e) { body.replaceChildren(h("p", { class: "error-text" }, e.message)); }
}

// ── старт ───────────────────────────────────────────────────────────
function autosize() { const t = $("#input"); t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, innerHeight * 0.4) + "px"; }
function logout() { store.set("agent_token", null); S.token = null; $("#app").hidden = true; $("#login").hidden = false; }

async function boot() {
  if (!S.token) { $("#login").hidden = false; return; }
  try { S.config = await api("/api/config"); } catch { return; }
  const me = await api("/api/me");
  document.title = S.config.name; $("#app-name").textContent = S.config.name; $("#login-title").textContent = S.config.name;
  $("#me-name").textContent = me.name;
  $("#model").replaceChildren(...S.config.models.map(m => h("option", { value: m }, m.replace(/^[^:]+:/, ""))));
  $("#model").value = S.config.default_model;
  const modeNames = { default: "Спрашивать", accept_edits: "Правки файлов — сразу", plan: "Только план", readonly: "Только чтение", auto: "Авто", bypass: "Без вопросов" };
  $("#mode").replaceChildren(...S.config.modes.map(m => h("option", { value: m }, modeNames[m] || m)));
  $("#mode").value = S.config.default_mode;
  $("#login").hidden = true; $("#app").hidden = false;
  const id = location.hash.slice(1);
  if (id) await openSession(id); else await loadSessions();
  $("#input").focus();
}

$("#login-form").addEventListener("submit", async e => {
  e.preventDefault();
  S.token = $("#token-input").value.trim(); store.set("agent_token", S.token);
  $("#login-error").textContent = "";
  try { await api("/api/me"); boot(); } catch (err) { $("#login-error").textContent = "Токен не подошёл"; logout(); }
});
$("#composer").addEventListener("submit", e => { e.preventDefault(); send(); });
$("#input").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } });
$("#input").addEventListener("input", autosize);
$("#stop").addEventListener("click", () => S.sessionId && api(`/api/sessions/${S.sessionId}/cancel`, { method: "POST" }));
$("#new-chat").addEventListener("click", () => { S.sessionId = null; history.replaceState(null, "", "#"); resetView(); $("#chat-title").textContent = "Новый разговор"; loadSessions(); $("#input").focus(); });
$("#logout").addEventListener("click", logout);
$("#menu").addEventListener("click", () => $("#sidebar").classList.toggle("open"));
$("#toggle-tasks").addEventListener("click", () => ($("#tasks").hidden = !$("#tasks").hidden));
$("#panel-close").addEventListener("click", () => ($("#panel").hidden = true));
$("#panel").addEventListener("click", e => { if (e.target.id === "panel") $("#panel").hidden = true; });
document.querySelectorAll("[data-panel]").forEach(b => b.addEventListener("click", () => openPanel(b.dataset.panel)));
for (const id of ["model", "mode"]) $("#" + id).addEventListener("change", () => S.sessionId && api(`/api/sessions/${S.sessionId}`, { method: "PATCH", body: { [id]: $("#" + id).value } }));
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
  if (!$("#panel").hidden) openPanel("tools");
  note(e.data.ok ? `Подключено: ${e.data.text}` : `Не удалось подключить: ${e.data.text}`, !e.data.ok);
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
