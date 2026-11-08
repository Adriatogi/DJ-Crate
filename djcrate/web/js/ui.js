// DOM + UI helpers: h(), toasts, modals, menus, popovers, formatting.

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k in el && typeof v !== "string") el[k] = v;
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// replaceChildren that accepts nested arrays and skips null/false.
export function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity)
    .filter((c) => c !== null && c !== undefined && c !== false)
    .map((c) => (c instanceof Node ? c : document.createTextNode(String(c)))));
  return el;
}

export function fmtMs(ms) {
  if (ms === null || ms === undefined || isNaN(ms)) return "–";
  const s = Math.round(ms / 1000);
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  return hh ? `${hh}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${mm}:${String(ss).padStart(2, "0")}`;
}

const CAMELOT_NAMES = {
  "1A": "Abm", "2A": "Ebm", "3A": "Bbm", "4A": "Fm", "5A": "Cm", "6A": "Gm", "7A": "Dm", "8A": "Am", "9A": "Em", "10A": "Bm", "11A": "F#m", "12A": "Dbm",
  "1B": "B", "2B": "F#", "3B": "Db", "4B": "Ab", "5B": "Eb", "6B": "Bb", "7B": "F", "8B": "C", "9B": "G", "10B": "D", "11B": "A", "12B": "E",
};

export function fmtBpm(bpm) {
  if (!bpm) return "—";
  return Number.isInteger(bpm) ? String(bpm) : bpm.toFixed(1).replace(/\.0$/, "");
}

/** "8A · Am" from a stored Camelot code. */
export function fmtKey(code) {
  return code && CAMELOT_NAMES[code] ? `${code} · ${CAMELOT_NAMES[code]}` : "—";
}

/** Sort value: Camelot number, then letter, so compatible keys sit together. */
export function keyOrder(code) {
  return code ? parseInt(code, 10) * 2 + (code.endsWith("B") ? 1 : 0) : 0;
}

export function fmtDiff(ms) {
  if (ms === null || ms === undefined) return "";
  const s = Math.round(ms / 1000);
  if (s === 0) return "same length";
  return `${s > 0 ? "+" : "−"}${fmtMs(Math.abs(ms))}`;
}

export function debounce(fn, ms) {
  let t;
  const d = (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  d.flush = (...a) => { clearTimeout(t); return fn(...a); };
  return d;
}

// ------------------------------------------------ toasts

export function toast(message, { type = "info", action = null, actions = null, timeout = 4500 } = {}) {
  const root = document.getElementById("toasts");
  const acts = actions || (action ? [action] : []);
  const el = h("div", { class: `toast ${type}` }, h("div", { class: "msg" }, message));
  const close = () => el.remove();
  for (const a of acts) {
    el.append(h("button", { class: "btn small", onclick: async () => { close(); await a.fn(); } }, a.label));
  }
  el.append(h("button", { class: "icon-btn small", title: "Dismiss", onclick: close }, "✕"));
  root.append(el);
  if (timeout) setTimeout(close, timeout);
  while (root.children.length > 5) root.firstChild.remove();
  return close;
}

export function errorToast(err) {
  return toast(err?.message || String(err), { type: "error", timeout: 8000 });
}

// ------------------------------------------------ modals

export function modal({ title, body, buttons = [], wide = false, onClose = null, dismissable = true }) {
  const root = document.getElementById("modal-root");
  const foot = h("div", { class: "modal-foot" });
  const card = h("div", { class: `modal${wide ? " wide" : ""}` },
    h("div", { class: "modal-head" }, title),
    h("div", { class: "modal-body" }, body),
    foot);
  const backdrop = h("div", { class: "backdrop" }, card);
  const close = () => { backdrop.remove(); document.removeEventListener("keydown", onKey, true); onClose && onClose(); };
  const onKey = (e) => { if (e.key === "Escape" && dismissable) { e.stopPropagation(); close(); } };
  document.addEventListener("keydown", onKey, true);
  if (dismissable) backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(); });
  const setButtons = (btns) => {
    foot.replaceChildren(...btns.map((b) => {
      const btn = h("button", { class: `btn ${b.kind || ""}`, disabled: b.disabled }, b.label);
      btn.addEventListener("click", async () => {
        if (!b.fn) return close();
        btn.disabled = true;
        try {
          const keep = await b.fn(btn);
          if (keep !== false) close();
        } catch (e) {
          errorToast(e);
        } finally {
          btn.disabled = false;
        }
      });
      return btn;
    }));
  };
  setButtons(buttons);
  root.append(backdrop);
  setTimeout(() => card.querySelector("input, textarea, select")?.focus(), 30);
  return { close, card, setBody: (b) => card.querySelector(".modal-body").replaceChildren(b), setButtons };
}

export function confirmDialog(title, message, { ok = "OK", danger = false } = {}) {
  return new Promise((resolve) => {
    let answered = false;
    modal({
      title, body: h("p", { style: { whiteSpace: "pre-line" } }, message),
      buttons: [
        { label: "Cancel", fn: () => { answered = true; resolve(false); } },
        { label: ok, kind: danger ? "danger" : "primary", fn: () => { answered = true; resolve(true); } },
      ],
      onClose: () => { if (!answered) resolve(false); },
    });
  });
}

export function promptDialog(title, { label = "", value = "", ok = "Save", placeholder = "" } = {}) {
  return new Promise((resolve) => {
    const input = h("input", { value, placeholder, style: { width: "100%" } });
    let answered = false;
    const m = modal({
      title, body: h("div", { class: "field" }, label ? h("label", {}, label) : null, input),
      buttons: [
        { label: "Cancel", fn: () => { answered = true; resolve(null); } },
        { label: ok, kind: "primary", fn: () => { answered = true; resolve(input.value.trim()); } },
      ],
      onClose: () => { if (!answered) resolve(null); },
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { answered = true; resolve(input.value.trim()); m.close(); }
    });
    setTimeout(() => input.select(), 40);
  });
}

// ------------------------------------------------ popovers & menus

let openPop = null;

export function closePopover() {
  if (openPop) { openPop.el.remove(); openPop.onClose && openPop.onClose(); openPop = null; }
}

export function popover(anchor, content, { cls = "", align = "left", onClose = null } = {}) {
  closePopover();
  const el = h("div", { class: `popover ${cls}` }, content);
  document.body.append(el);
  const r = anchor.getBoundingClientRect();
  const w = el.offsetWidth, ht = el.offsetHeight;
  let left = align === "right" ? r.right - w : r.left;
  left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
  let top = r.bottom + 4;
  if (top + ht > window.innerHeight - 8) top = Math.max(8, r.top - ht - 4);
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
  openPop = { el, anchor, onClose };
  return el;
}

export function isPopoverOpenFor(anchor) {
  return openPop && openPop.anchor === anchor;
}

document.addEventListener("mousedown", (e) => {
  if (openPop && !openPop.el.contains(e.target) && !openPop.anchor.contains(e.target)) closePopover();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && openPop) { closePopover(); e.preventDefault(); } // so Esc doesn't also close the player
});

export function menu(anchor, items, opts = {}) {
  const el = h("div", { class: "menu" }, items.map((it) => {
    if (it === "-") return h("hr");
    if (it.label && it.header) return h("div", { class: "label" }, it.label);
    return h("button", {
      class: it.danger ? "danger" : "", disabled: it.disabled,
      onclick: async () => { closePopover(); try { await it.fn(); } catch (e) { errorToast(e); } },
    }, it.label);
  }));
  return popover(anchor, el, opts);
}

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = h("textarea", { style: { position: "fixed", opacity: "0" } }, text);
    document.body.append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}
