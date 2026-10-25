// Визуализации в чате: KPI, графики, таблицы, дашборды — по описанию из инструмента show_visual.
// Свой маленький рисовальщик на SVG вместо библиотеки: без сборки и CDN, в общих цветах и в обеих темах.
// Правила (из гайда по графикам): одна ось Y, тонкие линии 2px, скругления 4px у концов столбцов,
// зазор 2px между заливками, легенда от двух рядов, подсказка при наведении, рядом всегда «Таблица».
"use strict";

const VIZ = (() => {
  const SVGNS = "http://www.w3.org/2000/svg";
  const sv = (tag, attrs = {}, ...kids) => {
    const el = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v != null && v !== false) el.setAttribute(k, v);
    for (const k of kids.flat()) if (k != null) el.append(k.nodeType ? k : document.createTextNode(String(k)));
    return el;
  };
  const el = (tag, attrs = {}, ...kids) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") e.className = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else if (v != null && v !== false) e.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k)));
    return e;
  };
  const color = i => `var(--viz-${(i % 8) + 1})`;
  const isNum = v => typeof v === "number" && isFinite(v);

  // ── числа по-русски: 1 234,5 · 12,3 тыс. · 4,5 млн ─────────────────
  const nf = (v, digits) => v.toLocaleString("ru-RU", { maximumFractionDigits: digits });
  function fmt(v, unit = "", compact = false) {
    if (!isNum(v)) return v == null ? "—" : String(v);
    const a = Math.abs(v);
    let s;
    if (compact && a >= 1e9) s = nf(v / 1e9, 1) + " млрд";
    else if (compact && a >= 1e6) s = nf(v / 1e6, 1) + " млн";
    else if (compact && a >= 1e4) s = nf(v / 1e3, a >= 1e5 ? 0 : 1) + " тыс.";
    else s = nf(v, a >= 100 ? 0 : a >= 10 ? 1 : 2);
    return unit ? (unit === "%" ? s + "%" : `${s} ${unit}`) : s;
  }
  function niceStep(range, count) {
    const raw = range / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const n = raw / mag;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * mag;
  }
  function scaleY(min, max, count = 5) {
    if (min === max) { max = min === 0 ? 1 : min + Math.abs(min) * 0.5; min = Math.min(0, min); }
    const step = niceStep(max - min, count);
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(10));
    return { lo, hi, ticks };
  }

  // ── подсказка при наведении: одна на страницу ───────────────────────
  let tipEl = null;
  function tip(content, x, y) {
    if (!tipEl) { tipEl = el("div", { class: "viz-tip", role: "tooltip" }); document.body.append(tipEl); }
    tipEl.replaceChildren(...content);
    tipEl.hidden = false;
    const r = tipEl.getBoundingClientRect();
    const left = Math.min(innerWidth - r.width - 8, Math.max(8, x + 14));
    const top = y - r.height - 12 < 8 ? y + 16 : y - r.height - 12;
    tipEl.style.transform = `translate(${left}px, ${top}px)`;
  }
  const hideTip = () => { if (tipEl) tipEl.hidden = true; };
  const tipRow = (i, name, value) => el("div", { class: "tr" }, el("i", { style: `background:${color(i)}` }), el("span", {}, name), el("b", {}, value));

  function legend(names, onHover) {
    return el("div", { class: "viz-legend" }, names.map((n, i) => el("span", {
      onmouseenter: () => onHover?.(i), onmouseleave: () => onHover?.(null),
    }, el("i", { style: `background:${color(i)}` }), n)));
  }

  // Скругление 4px только у «свободного» конца столбца — к оси он прилегает прямо.
  function barPath(x, y, w, h, dir = "up", r = 4) {
    r = Math.max(0, Math.min(r, w / 2, Math.abs(h)));
    if (h <= 0 || w <= 0) return "";
    if (dir === "up") return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
    if (dir === "down") return `M${x},${y}V${y + h - r}Q${x},${y + h} ${x + r},${y + h}H${x + w - r}Q${x + w},${y + h} ${x + w},${y + h - r}V${y}Z`;
    // right: x — у оси
    return `M${x},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h - r}Q${x + w},${y + h} ${x + w - r},${y + h}H${x}Z`;
  }

  // ── декартовы графики: line, area, bar, stacked_bar ─────────────────
  function cartesian(svg, W, H, b, dim) {
    const kind = b.kind, x = b.x || [], series = b.series || [], n = x.length, unit = b.unit || "";
    const stacked = kind === "stacked_bar", isBar = kind === "bar" || stacked;
    let vals = series.flatMap(s => (s.values || []).filter(isNum));
    if (stacked) {
      vals = [];
      for (let i = 0; i < n; i++) {
        let pos = 0, neg = 0;
        for (const s of series) { const v = s.values[i]; if (isNum(v)) v >= 0 ? (pos += v) : (neg += v); }
        vals.push(pos, neg);
      }
    }
    let min = Math.min(...vals, 0), max = Math.max(...vals, 0);
    if (!isBar && kind !== "area") {
      const dmin = Math.min(...vals), dmax = Math.max(...vals);
      // Линия «прилипает» к нулю, только если нуль недалеко — иначе вся динамика сплющится наверху.
      min = dmin >= 0 && dmin < dmax * 0.55 ? 0 : dmin; max = dmax;
    }
    const ys = scaleY(min, max, H < 220 ? 3 : 5);
    const tickLabels = ys.ticks.map(t => fmt(t, "", true));
    const ml = 10 + Math.max(...tickLabels.map(t => t.length)) * 6.6, mr = 12, mt = 10, mb = 26;
    const pw = W - ml - mr, ph = H - mt - mb;
    const Y = v => mt + ph - ((v - ys.lo) / (ys.hi - ys.lo)) * ph;
    const band = pw / Math.max(1, n);
    const X = i => isBar ? ml + band * i + band / 2 : ml + (n === 1 ? pw / 2 : (pw * i) / (n - 1));
    const g = sv("g");
    // сетка и ось Y
    ys.ticks.forEach((t, i) => g.append(
      sv("line", { x1: ml, x2: W - mr, y1: Y(t), y2: Y(t), class: t === 0 ? "viz-zero" : "viz-grid" }),
      sv("text", { x: ml - 8, y: Y(t) + 4, "text-anchor": "end", class: "viz-tick" }, tickLabels[i])));
    // подписи X: сколько влезает
    const every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(pw / 56))));
    x.forEach((lab, i) => {
      if (i % every) return;
      const t = String(lab);
      g.append(sv("text", { x: X(i), y: H - 8, "text-anchor": n > 1 && !isBar && i === 0 ? "start" : n > 1 && !isBar && i === n - 1 ? "end" : "middle", class: "viz-tick" },
        t.length > 14 ? t.slice(0, 13) + "…" : t));
    });
    svg.append(g);
    const marks = sv("g");
    svg.append(marks);
    const hoverLayer = sv("g", { class: "viz-hover" });
    const hit = sv("rect", { x: ml, y: mt, width: pw, height: ph, fill: "transparent" });

    if (isBar) {
      const groups = stacked ? 1 : series.length;
      const inner = band * 0.72, bw = Math.max(2, (inner - (groups - 1) * 2) / groups);
      const bars = [];
      for (let i = 0; i < n; i++) {
        let pos = 0, neg = 0;
        // в стопке скругляется только крайний сегмент (сверху у плюса, снизу у минуса)
        const lastPos = series.reduce((a, s, si) => isNum(s.values[i]) && s.values[i] > 0 ? si : a, -1);
        const lastNeg = series.reduce((a, s, si) => isNum(s.values[i]) && s.values[i] < 0 ? si : a, -1);
        series.forEach((s, si) => {
          const v = s.values[i];
          if (!isNum(v)) return;
          const x0 = ml + band * i + (band - inner) / 2 + (stacked ? 0 : si * (bw + 2));
          let y1, y2;
          if (stacked) { if (v >= 0) { y1 = Y(pos + v); y2 = Y(pos); pos += v; } else { y1 = Y(neg); y2 = Y(neg + v); neg += v; } }
          else { y1 = Y(Math.max(v, 0)); y2 = Y(Math.min(v, 0)); }
          const last = !stacked || si === (v >= 0 ? lastPos : lastNeg);
          const d = last ? barPath(x0, y1, stacked ? inner : bw, y2 - y1, v >= 0 ? "up" : "down") : `M${x0},${y1}h${stacked ? inner : bw}v${y2 - y1}h${-(stacked ? inner : bw)}Z`;
          const p = sv("path", { d, fill: color(si), class: "viz-bar", "data-s": si });
          bars.push(p);
          p.addEventListener("mousemove", e => {
            bars.forEach(o => o.classList.toggle("dim", o !== p));
            tip([el("div", { class: "th" }, String(x[i])), tipRow(si, s.name, fmt(v, unit))], e.clientX, e.clientY);
          });
          p.addEventListener("mouseleave", () => { bars.forEach(o => o.classList.remove("dim")); hideTip(); });
          marks.append(p);
        });
      }
      dim.on = si => bars.forEach(o => o.classList.toggle("dim", si != null && +o.dataset.s !== si));
      return;
    }
    // line / area
    const paths = series.map((s, si) => {
      let d = "", started = false;
      (s.values || []).forEach((v, i) => { if (!isNum(v)) { started = false; return; } d += `${started ? "L" : "M"}${X(i)},${Y(v)}`; started = true; });
      const grp = sv("g", { "data-s": si });
      if (kind === "area") {
        const pts = (s.values || []).map((v, i) => isNum(v) ? [X(i), Y(v)] : null).filter(Boolean);
        if (pts.length) grp.append(sv("path", { d: `M${pts[0][0]},${Y(Math.max(ys.lo, 0))}` + pts.map(p => `L${p[0]},${p[1]}`).join("") + `L${pts.at(-1)[0]},${Y(Math.max(ys.lo, 0))}Z`, fill: color(si), class: "viz-area" }));
      }
      grp.append(sv("path", { d, stroke: color(si), class: "viz-line" }));
      // одиночные точки без соседей иначе не видны
      (s.values || []).forEach((v, i) => {
        const prev = s.values[i - 1], next = s.values[i + 1];
        if (isNum(v) && !isNum(prev) && !isNum(next)) grp.append(sv("circle", { cx: X(i), cy: Y(v), r: 3, fill: color(si) }));
      });
      marks.append(grp);
      return grp;
    });
    dim.on = si => paths.forEach((p, i) => p.classList.toggle("dim", si != null && i !== si));
    svg.append(hoverLayer, hit);
    hit.addEventListener("mousemove", e => {
      const r = svg.getBoundingClientRect();
      const px = (e.clientX - r.left) * (W / r.width);
      const i = Math.max(0, Math.min(n - 1, Math.round(n === 1 ? 0 : ((px - ml) / pw) * (n - 1))));
      hoverLayer.replaceChildren(sv("line", { x1: X(i), x2: X(i), y1: mt, y2: mt + ph, class: "viz-cross" }),
        ...series.map((s, si) => isNum(s.values[i]) ? sv("circle", { cx: X(i), cy: Y(s.values[i]), r: 4.5, fill: color(si), class: "viz-dot" }) : null));
      tip([el("div", { class: "th" }, String(x[i])), ...series.map((s, si) => tipRow(si, s.name, fmt(s.values[i], unit)))], e.clientX, e.clientY);
    });
    hit.addEventListener("mouseleave", () => { hoverLayer.replaceChildren(); hideTip(); });
  }

  // ── горизонтальные столбцы: рейтинги и длинные подписи ─────────────
  function hbar(svg, W, H, b, dim) {
    const x = b.x || [], series = b.series || [], unit = b.unit || "", n = x.length;
    const vals = series.flatMap(s => s.values.filter(isNum));
    const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
    const labW = Math.min(W * 0.38, 10 + Math.max(...x.map(l => Math.min(String(l).length, 26))) * 6.8);
    const valW = 10 + Math.max(...vals.map(v => fmt(v, unit).length)) * 6.8;
    const ml = labW, mr = valW, pw = W - ml - mr, rowH = (H - 8) / Math.max(1, n);
    const scale = v => ((v - lo) / ((hi - lo) || 1)) * pw;
    const groups = series.length, inner = Math.min(rowH * 0.72, 18 * groups + 2 * (groups - 1)), bh = (inner - (groups - 1) * 2) / groups;
    const bars = [];
    x.forEach((lab, i) => {
      const y0 = 4 + rowH * i + (rowH - inner) / 2;
      const t = String(lab);
      svg.append(sv("text", { x: ml - 8, y: y0 + inner / 2 + 4, "text-anchor": "end", class: "viz-label" }, t.length > 26 ? t.slice(0, 25) + "…" : t));
      series.forEach((s, si) => {
        const v = s.values[i];
        if (!isNum(v)) return;
        const xa = ml + scale(Math.min(0, v)), w = Math.abs(scale(v) - scale(0)), y = y0 + si * (bh + 2);
        const p = sv("path", { d: v >= 0 ? barPath(xa, y, w, bh, "right") : `M${xa},${y}h${w}v${bh}h${-w}Z`, fill: color(si), class: "viz-bar", "data-s": si });
        p.addEventListener("mousemove", e => { bars.forEach(o => o.classList.toggle("dim", o !== p)); tip([el("div", { class: "th" }, t), tipRow(si, s.name, fmt(v, unit))], e.clientX, e.clientY); });
        p.addEventListener("mouseleave", () => { bars.forEach(o => o.classList.remove("dim")); hideTip(); });
        bars.push(p);
        svg.append(p);
        if (groups === 1) svg.append(sv("text", { x: xa + w + 6, y: y + bh / 2 + 4, class: "viz-value" }, fmt(v, unit)));
      });
    });
    if (lo < 0) svg.append(sv("line", { x1: ml + scale(0), x2: ml + scale(0), y1: 0, y2: H, class: "viz-zero" }));
    dim.on = si => bars.forEach(o => o.classList.toggle("dim", si != null && +o.dataset.s !== si));
  }

  // ── доли: кольцо или круг ───────────────────────────────────────────
  function pie(svg, W, H, b, dim) {
    const x = b.x || [], vals = (b.series?.[0]?.values || []).map(v => isNum(v) ? v : 0), unit = b.unit || "";
    const total = vals.reduce((a, v) => a + v, 0) || 1;
    const R = Math.min(W, H) / 2 - 4, cx = W / 2, cy = H / 2, r0 = b.kind === "donut" ? R * 0.62 : 0;
    let a = -Math.PI / 2;
    const arcs = [];
    vals.forEach((v, i) => {
      const a1 = a + (v / total) * Math.PI * 2;
      const large = a1 - a > Math.PI ? 1 : 0;
      const p = (rr, ang) => `${cx + rr * Math.cos(ang)},${cy + rr * Math.sin(ang)}`;
      const d = v / total >= 0.9999 ? (r0 ? `M${cx - R},${cy}a${R},${R} 0 1,0 ${2 * R},0a${R},${R} 0 1,0 ${-2 * R},0M${cx - r0},${cy}a${r0},${r0} 0 1,1 ${2 * r0},0a${r0},${r0} 0 1,1 ${-2 * r0},0` : `M${cx - R},${cy}a${R},${R} 0 1,0 ${2 * R},0a${R},${R} 0 1,0 ${-2 * R},0`)
        : r0 ? `M${p(R, a)}A${R},${R} 0 ${large} 1 ${p(R, a1)}L${p(r0, a1)}A${r0},${r0} 0 ${large} 0 ${p(r0, a)}Z`
          : `M${cx},${cy}L${p(R, a)}A${R},${R} 0 ${large} 1 ${p(R, a1)}Z`;
      const path = sv("path", { d, fill: color(i), class: "viz-slice", "data-s": i, "fill-rule": "evenodd" });
      path.addEventListener("mousemove", e => { arcs.forEach(o => o.classList.toggle("dim", o !== path)); tip([el("div", { class: "th" }, String(x[i])), tipRow(i, fmt(v, unit), `${nf((v / total) * 100, 1)}%`)], e.clientX, e.clientY); });
      path.addEventListener("mouseleave", () => { arcs.forEach(o => o.classList.remove("dim")); hideTip(); });
      arcs.push(path); svg.append(path);
      a = a1;
    });
    if (r0) svg.append(sv("text", { x: cx, y: cy - 2, "text-anchor": "middle", class: "viz-center" }, fmt(total, unit, true)),
      sv("text", { x: cx, y: cy + 16, "text-anchor": "middle", class: "viz-tick" }, "всего"));
    dim.on = si => arcs.forEach(o => o.classList.toggle("dim", si != null && +o.dataset.s !== si));
  }

  // ── точки: связь двух величин ───────────────────────────────────────
  function scatter(svg, W, H, b, dim) {
    const series = b.series || [], unit = b.unit || "";
    const pts = series.flatMap(s => s.points || []);
    const xs = scaleY(Math.min(...pts.map(p => p[0])), Math.max(...pts.map(p => p[0])), 5);
    const ys = scaleY(Math.min(...pts.map(p => p[1])), Math.max(...pts.map(p => p[1])), H < 220 ? 3 : 5);
    const yl = ys.ticks.map(t => fmt(t, "", true));
    const ml = 10 + Math.max(...yl.map(t => t.length)) * 6.6, mr = 12, mt = 10, mb = b.x_label ? 40 : 26, pw = W - ml - mr, ph = H - mt - mb;
    const X = v => ml + ((v - xs.lo) / (xs.hi - xs.lo)) * pw, Y = v => mt + ph - ((v - ys.lo) / (ys.hi - ys.lo)) * ph;
    ys.ticks.forEach((t, i) => svg.append(sv("line", { x1: ml, x2: W - mr, y1: Y(t), y2: Y(t), class: "viz-grid" }), sv("text", { x: ml - 8, y: Y(t) + 4, "text-anchor": "end", class: "viz-tick" }, yl[i])));
    xs.ticks.forEach(t => svg.append(sv("text", { x: X(t), y: mt + ph + 18, "text-anchor": "middle", class: "viz-tick" }, fmt(t, "", true))));
    if (b.x_label) svg.append(sv("text", { x: ml + pw / 2, y: H - 4, "text-anchor": "middle", class: "viz-tick" }, b.x_label));
    const dots = [];
    series.forEach((s, si) => (s.points || []).forEach(([px, py]) => {
      const c = sv("circle", { cx: X(px), cy: Y(py), r: 4.5, fill: color(si), class: "viz-dot", "data-s": si });
      c.addEventListener("mousemove", e => tip([el("div", { class: "th" }, s.name), el("div", { class: "tr" }, el("span", {}, b.x_label || "x"), el("b", {}, fmt(px))), el("div", { class: "tr" }, el("span", {}, b.y_label || "y"), el("b", {}, fmt(py, unit)))], e.clientX, e.clientY));
      c.addEventListener("mouseleave", hideTip);
      dots.push(c); svg.append(c);
    }));
    dim.on = si => dots.forEach(o => o.classList.toggle("dim", si != null && +o.dataset.s !== si));
  }

  function dataTable(b) {
    if (b.kind === "scatter") {
      return el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, "Ряд"), el("th", {}, b.x_label || "x"), el("th", {}, b.y_label || "y"))),
        el("tbody", {}, (b.series || []).flatMap(s => (s.points || []).map(p => el("tr", {}, el("td", {}, s.name), el("td", {}, fmt(p[0])), el("td", {}, fmt(p[1], b.unit)))))));
    }
    return el("table", {}, el("thead", {}, el("tr", {}, el("th", {}, ""), (b.series || []).map(s => el("th", {}, s.name)))),
      el("tbody", {}, (b.x || []).map((lab, i) => el("tr", {}, el("td", {}, String(lab)), (b.series || []).map(s => el("td", {}, fmt(s.values?.[i], b.unit)))))));
  }

  function chart(b, opts) {
    const kind = b.kind || "line";
    const names = kind === "pie" || kind === "donut" ? (b.x || []).map(String) : (b.series || []).map(s => s.name);
    const dim = { on: null };
    const plot = el("div", { class: "viz-plot" + (kind === "pie" || kind === "donut" ? " round" : "") });
    const table = el("div", { class: "viz-table md", hidden: true }, dataTable(b));
    const tBtn = el("button", { class: "viz-mini", type: "button", "aria-pressed": "false", onclick: () => {
      const on = table.hidden; table.hidden = !on; plot.hidden = on; tBtn.setAttribute("aria-pressed", String(on)); tBtn.textContent = on ? "График" : "Таблица";
    } }, "Таблица");
    const head = el("div", { class: "viz-head" }, el("div", { class: "viz-title" }, b.title || "", b.y_label ? el("small", {}, b.y_label) : null), tBtn);
    const lg = names.length >= 2 ? legend(names, i => dim.on?.(i)) : null;
    const box = el("div", { class: "viz-block chart" + (b.span === 2 || opts.full ? " wide" : "") }, head, lg, plot, table);
    const draw = () => {
      const round = kind === "pie" || kind === "donut";
      // Кольцо рисуем в натуральную величину: если растянуть viewBox, подписи внутри мельчают.
      const W = round ? Math.min(280, Math.floor(plot.clientWidth)) : Math.max(220, Math.floor(plot.clientWidth));
      if (!plot.isConnected || !W) return;
      const H = round ? W : kind === "hbar" ? Math.max(120, (b.x || []).length * Math.max(26, 14 * (b.series || []).length + 10) + 8) : (opts.expanded ? 420 : W < 420 ? 200 : 240);
      const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": b.title || "график" });
      try {
        ({ line: cartesian, area: cartesian, bar: cartesian, stacked_bar: cartesian, hbar, pie, donut: pie, scatter })[kind](svg, W, H, b, dim);
      } catch (e) { plot.replaceChildren(el("div", { class: "viz-err" }, "Не получилось нарисовать: " + e.message)); return; }
      plot.replaceChildren(svg);
    };
    new ResizeObserver(() => { if (plot.clientWidth && plot._w !== plot.clientWidth) { plot._w = plot.clientWidth; draw(); } }).observe(plot);
    return box;
  }

  function kpi(b) {
    const d = b.delta, good = b.good === "down" ? d < 0 : d > 0;
    return el("div", { class: "viz-block kpi" + (b.span === 2 ? " wide" : "") },
      el("div", { class: "kpi-label" }, b.label || b.title || ""),
      el("div", { class: "kpi-value" }, isNum(b.value) ? fmt(b.value, b.unit) : String(b.value)),
      isNum(d) || b.hint ? el("div", { class: "kpi-foot" },
        isNum(d) ? el("span", { class: "kpi-delta " + (d === 0 ? "flat" : good ? "good" : "bad") }, d > 0 ? "▲ " : d < 0 ? "▼ " : "", `${d > 0 ? "+" : ""}${nf(d, 1)}%`) : null,
        b.hint ? el("span", {}, b.hint) : null) : null);
  }

  function table(b) {
    const right = (b.columns || []).map((_, ci) => (b.rows || []).every(r => r[ci] == null || isNum(r[ci])));
    return el("div", { class: "viz-block table" + (b.span === 1 ? "" : " wide") },
      b.title ? el("div", { class: "viz-head" }, el("div", { class: "viz-title" }, b.title)) : null,
      el("div", { class: "viz-table md" }, el("table", {},
        el("thead", {}, el("tr", {}, (b.columns || []).map((c, ci) => el("th", { class: right[ci] ? "num" : "" }, c)))),
        el("tbody", {}, (b.rows || []).map(r => el("tr", {}, r.map((c, ci) => el("td", { class: right[ci] ? "num" : "" }, isNum(c) ? fmt(c) : c ?? "—"))))))));
  }

  function text(b) {
    const d = el("div", { class: "viz-block text md" + (b.span === 1 ? "" : " wide") });
    if (typeof renderMd === "function") renderMd(d, b.markdown || ""); else d.textContent = b.markdown || "";
    return d;
  }

  // ── свой HTML: изолированный iframe без доступа к странице и сети ───
  const TOKENS = ["--bg", "--surface", "--surface-2", "--text", "--text-2", "--muted", "--faint", "--line", "--line-2", "--accent", "--ok", "--warn", "--err",
    ...Array.from({ length: 8 }, (_, i) => `--viz-${i + 1}`)];
  let frameSeq = 0;
  function htmlFrame(spec, opts) {
    const cs = getComputedStyle(document.documentElement);
    const vars = TOKENS.map(t => `${t}:${cs.getPropertyValue(t).trim()};`).join("");
    const id = "f" + ++frameSeq;
    const csp = "default-src 'none'; script-src 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; " +
      "style-src 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; img-src data: blob:; font-src data:; connect-src 'none'";
    const doc = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}">
<style>:root{${vars}color-scheme:${document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")}}
html,body{margin:0;background:transparent;color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
body{padding:4px}*{box-sizing:border-box}</style>
<script>(()=>{const m=()=>{const d={};return{getItem:k=>k in d?d[k]:null,setItem:(k,v)=>{d[k]=String(v)},removeItem:k=>{delete d[k]},clear:()=>{for(const k in d)delete d[k]},key:i=>Object.keys(d)[i]??null,get length(){return Object.keys(d).length}}};
/* в песочнице без allow-same-origin обращение к localStorage бросает исключение — подставляем память, чтобы чужой код не падал */
for(const n of["localStorage","sessionStorage"])try{window[n]}catch{Object.defineProperty(window,n,{value:m()})}})();<\/script></head><body>${spec.html}
<script>(()=>{const p=()=>parent.postMessage({type:"viz-h",id:"${id}",h:Math.ceil(document.body.getBoundingClientRect().bottom+6)},"*");
addEventListener("load",p);new ResizeObserver(p).observe(document.body);setTimeout(p,300);})();<\/script></body></html>`;
    const f = el("iframe", { class: "viz-frame", sandbox: "allow-scripts", srcdoc: doc, title: spec.title || "визуализация", loading: "lazy" });
    f.style.height = (spec.height || (opts.expanded ? 520 : 320)) + "px";
    if (!spec.height) {
      const on = e => { if (e.source === f.contentWindow && e.data?.type === "viz-h" && e.data.id === id) f.style.height = Math.min(opts.expanded ? 4000 : 1400, Math.max(60, e.data.h)) + "px"; };
      addEventListener("message", on);
    }
    return el("div", { class: "viz-block wide html" }, f);
  }

  function render(spec, opts = {}) {
    const blocks = spec.html ? [htmlFrame(spec, opts)] : (spec.blocks || []).map(b =>
      b.type === "kpi" ? kpi(b) : b.type === "chart" ? chart(b, opts) : b.type === "table" ? table(b) : b.type === "text" ? text(b) : null);
    const allKpi = !spec.html && (spec.blocks || []).every(b => b.type === "kpi");
    // Один блок — во всю ширину ответа; нечётный последний график — тоже, чтобы не висел половинкой.
    const nonKpi = blocks.filter(x => x && !x.classList.contains("kpi"));
    if (nonKpi.length === 1 || (nonKpi.length % 2 === 1 && nonKpi.at(-1)?.classList.contains("chart"))) nonKpi.at(-1).classList.add("wide");
    const card = el("div", { class: "viz" + (opts.expanded ? " expanded" : "") },
      spec.title ? el("div", { class: "viz-top" }, el("b", {}, spec.title)) : null,
      opts.expanded ? null : el("button", { class: "viz-mini icon viz-expand", type: "button", title: "Во весь экран", "aria-label": "Во весь экран", onclick: () => expand(spec) },
        sv("svg", { viewBox: "0 0 24 24" }, sv("path", { d: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5" }))),
      el("div", { class: "viz-grid" + (allKpi ? " kpis" : "") }, blocks));
    // Без общего заголовка кнопка «на весь экран» встаёт рядом с «Таблица» первого графика, а не поверх неё.
    const ex = card.querySelector(":scope > .viz-expand"), head = card.querySelector(".viz-head");
    if (ex && !spec.title && head) { ex.classList.remove("viz-expand"); head.append(ex); }
    return card;
  }

  function expand(spec) {
    const close = () => { m.remove(); hideTip(); };
    const m = el("div", { class: "modal viz-modal", onmousedown: e => { if (e.target === m) close(); } },
      el("div", { class: "modal-card viz-modal-card", role: "dialog", "aria-modal": "true", tabindex: "-1" },
        el("div", { class: "dialog-head" }, el("h3", {}, spec.title || "Визуализация"),
          el("button", { class: "icon-btn", "aria-label": "Закрыть", onclick: close }, sv("svg", { viewBox: "0 0 24 24" }, sv("path", { d: "M6 6l12 12M18 6 6 18" })))),
        render({ ...spec, title: "" }, { expanded: true })));
    m.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); close(); } });
    document.body.append(m);
    m.querySelector(".modal-card").focus();
  }

  return { render, fmt };
})();
