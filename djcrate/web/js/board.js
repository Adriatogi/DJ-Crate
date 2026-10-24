// Node board: a pannable/zoomable canvas of cards with in/out ports and curved
// wires (like a visual-scripting editor). Used by Cutting and Set Setter.
//
// The host owns the data: it calls setData({nodes, edges}) and reacts to events
// (onConnect, onMove, onDelete, onSelect, onOpen). Each node supplies its own
// body element; the board adds the frame, ports, dragging and wires.

import { h } from "./ui.js";

const SVGNS = "http://www.w3.org/2000/svg";
const MIN_ZOOM = 0.25, MAX_ZOOM = 2;

function svg(tag, attrs = {}) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

export class Board {
  constructor(container, handlers = {}) {
    this.handlers = handlers;
    this.nodes = new Map();   // key -> { data, el }
    this.edges = [];          // { key, from, to, kind, main, dim, ghost }
    this.pan = { x: 0, y: 0 };
    this.zoom = 1;
    this.selected = null;     // { type: "node"|"edge", key }

    this.root = h("div", { class: "board", tabindex: "0" });
    this.layer = h("div", { class: "board-layer" });
    this.svg = svg("svg", { class: "board-wires" });
    const defs = svg("defs");
    for (const [id, cls] of [["arrow", "arrow"], ["arrow-main", "arrow main"], ["arrow-dim", "arrow dim"]]) {
      const m = svg("marker", { id: `${id}-${Board.uid}`, viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" });
      m.append(svg("path", { d: "M 0 0 L 10 5 L 0 10 z", class: cls }));
      defs.append(m);
    }
    this.markerId = Board.uid++;
    this.svg.append(defs);
    // A big canvas offset by HALF so wires anywhere are hit-testable (WebKit
    // doesn't hit-test overflow outside a zero-size <svg>).
    const shift = svg("g", { transform: `translate(${Board.HALF} ${Board.HALF})` });
    this.wireGroup = svg("g");
    this.tempWire = svg("path", { class: "wire temp" });
    shift.append(this.wireGroup, this.tempWire);
    this.svg.append(shift);
    this.layer.append(this.svg);
    this.zoomLabel = h("span", { class: "board-zoom" }, "100%");
    this.root.append(this.layer, h("div", { class: "board-controls" },
      h("button", { class: "icon-btn", title: "Zoom out", onclick: () => this.zoomBy(1 / 1.2) }, "−"),
      this.zoomLabel,
      h("button", { class: "icon-btn", title: "Zoom in", onclick: () => this.zoomBy(1.2) }, "+"),
      h("button", { class: "btn small", title: "Fit everything on screen", onclick: () => this.fit() }, "Fit")));
    container.replaceChildren(this.root);
    this._wire();
    new ResizeObserver(() => this.drawWires()).observe(this.root);
  }

  // ------------------------------------------------ data

  setData({ nodes, edges }, { keepView = true } = {}) {
    const seen = new Set();
    for (const n of nodes) {
      seen.add(n.key);
      let entry = this.nodes.get(n.key);
      if (!entry) {
        entry = { el: h("div", { class: "bnode" }) };
        this.nodes.set(n.key, entry);
        this.layer.append(entry.el);
      }
      entry.data = n;
      this._renderNode(entry);
    }
    for (const [key, entry] of this.nodes) {
      if (!seen.has(key)) { entry.el.remove(); this.nodes.delete(key); }
    }
    this.edges = edges;
    if (this.selected && !this._exists(this.selected)) this.select(null);
    this.drawWires();
    if (!keepView) this.fit();
  }

  _exists(sel) {
    return sel.type === "node" ? this.nodes.has(sel.key) : this.edges.some((e) => e.key === sel.key);
  }

  _renderNode(entry) {
    const n = entry.data;
    const el = entry.el;
    el.className = `bnode bnode-k-${n.kind}${n.ghost ? " ghost" : ""}${n.center ? " center" : ""}${n.cls ? ` ${n.cls}` : ""}` +
      `${this.selected?.type === "node" && this.selected.key === n.key ? " selected" : ""}`;
    el.dataset.key = n.key;
    el.style.left = `${n.x}px`;
    el.style.top = `${n.y}px`;
    const ports = [];
    if (n.ports?.in !== false) ports.push(h("div", { class: "port in", dataset: { port: "in" }, title: "Wire into here" }));
    if (n.ports?.out !== false) ports.push(h("div", { class: "port out", dataset: { port: "out" }, title: "Drag to connect" }));
    el.replaceChildren(n.body, ...ports);
  }

  updateNode(key, patch) {
    const entry = this.nodes.get(key);
    if (!entry) return;
    entry.data = { ...entry.data, ...patch };
    this._renderNode(entry);
    this.drawWires();
  }

  // ------------------------------------------------ geometry

  _portPos(key, side) {
    const entry = this.nodes.get(key);
    if (!entry) return null;
    const { x, y } = entry.data;
    const el = entry.el;
    const port = el.querySelector(`.port.${side}`);
    if (port) return { x: x + port.offsetLeft + port.offsetWidth / 2, y: y + port.offsetTop + port.offsetHeight / 2 };
    return { x: x + (side === "out" ? el.offsetWidth : 0), y: y + el.offsetHeight / 2 };
  }

  _center(key) {
    const entry = this.nodes.get(key);
    if (!entry) return null;
    return { x: entry.data.x + entry.el.offsetWidth / 2, y: entry.data.y + entry.el.offsetHeight / 2 };
  }

  _curve(a, b) {
    const dx = Math.max(50, Math.abs(b.x - a.x) / 2);
    return `M ${a.x} ${a.y} C ${a.x + dx} ${a.y}, ${b.x - dx} ${b.y}, ${b.x} ${b.y}`;
  }

  drawWires() {
    this.wireGroup.replaceChildren();
    for (const e of this.edges) {
      let d;
      if (e.kind === "sample") {
        const a = this._center(e.from), b = this._center(e.to);
        if (!a || !b) continue;
        d = `M ${a.x} ${a.y} Q ${(a.x + b.x) / 2 + 40} ${(a.y + b.y) / 2}, ${b.x} ${b.y}`;
      } else {
        const a = this._portPos(e.from, "out"), b = this._portPos(e.to, "in");
        if (!a || !b) continue;
        d = this._curve(a, b);
      }
      const sel = this.selected?.type === "edge" && this.selected.key === e.key;
      const cls = `wire ${e.kind || ""}${e.main ? " main" : ""}${e.dim ? " dim" : ""}${e.ghost ? " ghost" : ""}${sel ? " selected" : ""}`;
      const marker = e.kind === "sample" ? null : `url(#${e.main ? "arrow-main" : e.dim ? "arrow-dim" : "arrow"}-${this.markerId})`;
      const path = svg("path", { d, class: cls });
      if (marker) path.setAttribute("marker-end", marker);
      const hit = svg("path", { d, class: "wire-hit" });
      hit.addEventListener("mousedown", (ev) => { ev.stopPropagation(); this.select({ type: "edge", key: e.key }); });
      this.wireGroup.append(path, hit);
    }
  }

  // ------------------------------------------------ view

  _applyView() {
    this.layer.style.transform = `translate(${this.pan.x}px, ${this.pan.y}px) scale(${this.zoom})`;
    const g = 22 * this.zoom;
    this.root.style.backgroundSize = `${g}px ${g}px`;
    this.root.style.backgroundPosition = `${this.pan.x}px ${this.pan.y}px`;
    this.zoomLabel.textContent = `${Math.round(this.zoom * 100)}%`;
  }

  zoomBy(f, cx = this.root.clientWidth / 2, cy = this.root.clientHeight / 2) {
    const z = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, this.zoom * f));
    const k = z / this.zoom;
    this.pan.x = cx - (cx - this.pan.x) * k;
    this.pan.y = cy - (cy - this.pan.y) * k;
    this.zoom = z;
    this._applyView();
  }

  fit(pad = 60) {
    if (!this.nodes.size) { this.pan = { x: this.root.clientWidth / 2, y: this.root.clientHeight / 2 }; this.zoom = 1; this._applyView(); return; }
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const { data, el } of this.nodes.values()) {
      x0 = Math.min(x0, data.x); y0 = Math.min(y0, data.y);
      x1 = Math.max(x1, data.x + el.offsetWidth); y1 = Math.max(y1, data.y + el.offsetHeight);
    }
    const w = this.root.clientWidth || 800, ht = this.root.clientHeight || 500;
    this.zoom = Math.min(1.1, Math.max(MIN_ZOOM, Math.min((w - pad * 2) / (x1 - x0 || 1), (ht - pad * 2) / (y1 - y0 || 1))));
    this.pan = { x: (w - (x1 - x0) * this.zoom) / 2 - x0 * this.zoom, y: (ht - (y1 - y0) * this.zoom) / 2 - y0 * this.zoom };
    this._applyView();
  }

  toBoard(clientX, clientY) {
    const r = this.root.getBoundingClientRect();
    return { x: (clientX - r.left - this.pan.x) / this.zoom, y: (clientY - r.top - this.pan.y) / this.zoom };
  }

  // ------------------------------------------------ selection

  select(sel) {
    this.selected = sel;
    for (const entry of this.nodes.values()) {
      entry.el.classList.toggle("selected", sel?.type === "node" && sel.key === entry.data.key);
    }
    this.drawWires();
    this.handlers.onSelect?.(sel);
  }

  // ------------------------------------------------ input

  _wire() {
    const root = this.root;
    let drag = null;

    root.addEventListener("mousedown", (e) => {
      if (e.button !== 0) return;
      const port = e.target.closest(".port");
      const nodeEl = e.target.closest(".bnode");
      if (port && port.dataset.port === "out") {
        e.preventDefault();
        drag = { type: "wire", from: nodeEl.dataset.key, start: this._portPos(nodeEl.dataset.key, "out") };
        return;
      }
      if (nodeEl) {
        if (e.target.closest("input, textarea, button, select, a, [contenteditable]")) return;
        const entry = this.nodes.get(nodeEl.dataset.key);
        drag = { type: "node", key: nodeEl.dataset.key, sx: e.clientX, sy: e.clientY, ox: entry.data.x, oy: entry.data.y, moved: false };
        return;
      }
      drag = { type: "pan", sx: e.clientX, sy: e.clientY, ox: this.pan.x, oy: this.pan.y, moved: false };
      root.classList.add("panning");
    });

    window.addEventListener("mousemove", (e) => {
      if (!drag) return;
      if (drag.type === "pan") {
        this.pan.x = drag.ox + (e.clientX - drag.sx);
        this.pan.y = drag.oy + (e.clientY - drag.sy);
        drag.moved = drag.moved || Math.abs(e.clientX - drag.sx) + Math.abs(e.clientY - drag.sy) > 3;
        this._applyView();
      } else if (drag.type === "node") {
        const dx = (e.clientX - drag.sx) / this.zoom, dy = (e.clientY - drag.sy) / this.zoom;
        if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 3) return;
        drag.moved = true;
        const entry = this.nodes.get(drag.key);
        entry.data.x = Math.round(drag.ox + dx);
        entry.data.y = Math.round(drag.oy + dy);
        entry.el.style.left = `${entry.data.x}px`;
        entry.el.style.top = `${entry.data.y}px`;
        this.drawWires();
      } else if (drag.type === "wire") {
        const p = this.toBoard(e.clientX, e.clientY);
        this.tempWire.setAttribute("d", this._curve(drag.start, p));
        root.querySelectorAll(".bnode.drop-ok").forEach((n) => n.classList.remove("drop-ok"));
        const over = document.elementFromPoint(e.clientX, e.clientY)?.closest(".bnode");
        if (over && over.dataset.key !== drag.from) over.classList.add("drop-ok");
      }
    });

    window.addEventListener("mouseup", (e) => {
      if (!drag) return;
      const d = drag;
      drag = null;
      root.classList.remove("panning");
      if (d.type === "pan" && !d.moved) this.select(null);
      if (d.type === "node") {
        if (d.moved) {
          const entry = this.nodes.get(d.key);
          this.handlers.onMove?.(entry.data, entry.data.x, entry.data.y);
        } else {
          this.select({ type: "node", key: d.key });
        }
      }
      if (d.type === "wire") {
        this.tempWire.setAttribute("d", "");
        root.querySelectorAll(".bnode.drop-ok").forEach((n) => n.classList.remove("drop-ok"));
        const over = document.elementFromPoint(e.clientX, e.clientY)?.closest(".bnode");
        if (over && over.dataset.key !== d.from) this.handlers.onConnect?.(d.from, over.dataset.key, null);
        else if (!over && root.contains(document.elementFromPoint(e.clientX, e.clientY))) {
          this.handlers.onConnect?.(d.from, null, { ...this.toBoard(e.clientX, e.clientY), clientX: e.clientX, clientY: e.clientY });
        }
      }
    });

    root.addEventListener("dblclick", (e) => {
      const nodeEl = e.target.closest(".bnode");
      if (nodeEl && !e.target.closest("input, textarea, button")) this.handlers.onOpen?.(this.nodes.get(nodeEl.dataset.key).data);
    });

    root.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = root.getBoundingClientRect();
      if (e.ctrlKey || e.metaKey) {
        this.zoomBy(Math.exp(-e.deltaY * 0.01), e.clientX - r.left, e.clientY - r.top); // pinch / ⌘-scroll
      } else {
        this.pan.x -= e.deltaX;
        this.pan.y -= e.deltaY;
        this._applyView();
      }
    }, { passive: false });

    root.addEventListener("keydown", (e) => {
      if (e.target.closest("input, textarea, [contenteditable]")) return;
      if ((e.key === "Delete" || e.key === "Backspace") && this.selected) {
        e.preventDefault();
        this.handlers.onDelete?.(this.selected);
      }
      if (e.key === "Escape") this.select(null);
    });
    root.addEventListener("mousedown", () => { if (!document.activeElement?.closest?.("input, textarea")) root.focus({ preventScroll: true }); });
  }
}
Board.uid = 1;
Board.HALF = 20000;

// Shared node bodies --------------------------------------------------------

export function songBody(song, { sub = null, badges = [] } = {}) {
  return h("div", { class: "bnode-body" },
    song?.cover_url ? h("img", { class: "cover", src: song.cover_url, alt: "" }) : h("div", { class: "cover placeholder" }, "♪"),
    h("div", { class: "bnode-text" },
      h("div", { class: "t-title" }, song?.title || "(deleted song)"),
      h("div", { class: "t-sub" }, sub ?? (song?.artists || ""))),
    h("div", { class: "bnode-badges" },
      song?.ready ? h("span", { class: "dotflag ready", title: "Ready" }, "✓") : null,
      song?.broken ? h("span", { class: "dotflag broken", title: "Broken" }, "!") : null,
      badges));
}

// A note that becomes a textarea on click; `onSave(text)` when you leave it.
export function noteBody(text, { title = "Mix", placeholder = "How do you mix it? e.g. loop the last 8 bars, add reverb, bring in the instrumental first…", onSave, extra = null } = {}) {
  const view = h("div", { class: "note-text" }, text || h("span", { class: "muted" }, "Click to write the mix…"));
  const wrap = h("div", { class: "bnode-body note" }, h("div", { class: "note-head" }, title, extra), view);
  const edit = () => {
    const ta = h("textarea", { placeholder, rows: 4 }, text || "");
    view.replaceWith(ta);
    ta.focus();
    ta.addEventListener("keydown", (e) => { if (e.key === "Escape") ta.blur(); e.stopPropagation(); });
    ta.addEventListener("blur", () => {
      const v = ta.value.trim();
      ta.replaceWith(view);
      if (v !== (text || "")) onSave?.(v);
    }, { once: true });
  };
  view.addEventListener("click", edit);
  wrap.startEditing = edit;
  return wrap;
}
