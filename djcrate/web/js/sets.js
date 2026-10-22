// Set Setter: a scratchpad for planning sets — ordered songs + free-text rows,
// cue times, transition notes, running time, export as text.

import { del, get, patch, post, put } from "./api.js";
import { readOnly } from "./store.js";
import { closePopover, copyText, debounce, errorToast, fmtMs, h, menu, popover, toast } from "./ui.js";

let sets = [];
let currentId = Number(localStorage.getItem("djc.set")) || null;
let current = null; // { set, items }
let dragIndex = null;

const root = () => document.getElementById("view-sets");

export async function showSets() {
  await loadList();
  if (currentId && sets.some((s) => s.id === currentId)) await openSet(currentId);
  else if (sets.length) await openSet(sets[0].id);
  else { current = null; render(); }
}

async function loadList() {
  try {
    sets = (await get("/api/sets")).sets;
  } catch (e) {
    if (e.code !== "library_unavailable") errorToast(e);
    sets = [];
  }
}

async function openSet(id) {
  try {
    current = await get(`/api/sets/${id}`);
    currentId = id;
    localStorage.setItem("djc.set", id);
  } catch (e) {
    errorToast(e);
    current = null;
  }
  render();
}

async function newSet() {
  try {
    const { id } = await post("/api/sets", { name: "New set" });
    await loadList();
    await openSet(id);
    root().querySelector(".set-name")?.select();
  } catch (e) { errorToast(e); }
}

async function deleteSet() {
  const name = current.set.name;
  try {
    const { snapshot } = await del(`/api/sets/${current.set.id}`);
    current = null;
    currentId = null;
    await showSets();
    toast(`Deleted “${name}”.`, {
      timeout: 10000,
      action: {
        label: "Undo",
        fn: async () => {
          const { id } = await post("/api/sets/restore", { snapshot });
          await loadList();
          await openSet(id);
        },
      },
    });
  } catch (e) { errorToast(e); }
}

// ------------------------------------------------ saving

const saveMeta = debounce(async (changes) => {
  try {
    await patch(`/api/sets/${current.set.id}`, changes);
    await loadList();
    renderList();
  } catch (e) { errorToast(e); }
}, 500);

const saveItems = debounce(async () => {
  try {
    const r = await put(`/api/sets/${current.set.id}/items`, {
      items: current.items.map((i) => ({
        track_id: i.track_id, free_text: i.free_text, label: i.label || "", cue_time: i.cue_time || "", transition_note: i.transition_note || "",
        length_ms: i.length_ms || null,
      })),
    });
    // Keep local edits; only pick up server-side labels.
    r.items.forEach((srv, idx) => { if (current.items[idx]) current.items[idx].label = srv.label; });
  } catch (e) { errorToast(e); }
}, 500);

function changed({ rerender = true } = {}) {
  saveItems();
  if (rerender) renderTimeline(); else renderStats();
}

// ------------------------------------------------ render

function render() {
  const listPane = h("aside", { class: "sets-list" },
    h("button", { class: "btn primary block", onclick: newSet, disabled: readOnly() }, "+ New set"),
    h("div", { id: "sets-nav", style: { marginTop: "10px" } }));
  const editor = h("section", { class: "set-editor", id: "set-editor" });
  root().replaceChildren(listPane, editor);
  renderList();
  if (!current) {
    editor.append(h("div", { class: "empty" },
      h("h3", {}, "Plan a set"),
      h("p", {}, "Line up songs from your library, add notes for transitions, and export it as text for your phone."),
      h("button", { class: "btn primary", onclick: newSet }, "+ New set")));
    return;
  }
  const name = h("input", { class: "set-name", value: current.set.name });
  name.addEventListener("input", () => { current.set.name = name.value; saveMeta({ name: name.value }); });
  const notes = h("textarea", { placeholder: "Set notes — venue, vibe, start time, what to avoid…", rows: 3 }, current.set.notes);
  notes.addEventListener("input", () => saveMeta({ notes: notes.value }));
  const exportBtn = h("button", { class: "btn" }, "Export as text ▾");
  exportBtn.onclick = () => menu(exportBtn, [
    { label: "Copy to clipboard", fn: async () => { await saveItems.flush(); const r = await get(`/api/sets/${current.set.id}/export`); await copyText(r.text); toast("Copied the set as text.", { type: "success" }); } },
    { label: "Save .txt to Downloads", fn: async () => { await saveItems.flush(); const r = await post(`/api/sets/${current.set.id}/export_file`); toast(`Saved ${r.path.split("/").pop()} to Downloads.`, { type: "success" }); } },
  ], { align: "right" });
  editor.append(
    h("div", { class: "set-head" }, name, h("div", { class: "spacer" }), exportBtn,
      h("button", { class: "btn danger", onclick: deleteSet }, "Delete set")),
    h("div", { class: "set-stats", id: "set-stats" }),
    notes,
    h("table", { class: "timeline" },
      h("colgroup", {},
        h("col", { style: { width: "26px" } }), h("col", { style: { width: "34px" } }), h("col", { style: { width: "62px" } }),
        h("col", {}), h("col", { style: { width: "92px" } }), h("col", { style: { width: "130px" } }),
        h("col", { style: { width: "32%" } }), h("col", { style: { width: "34px" } })),
      h("thead", {}, h("tr", {},
        h("th", {}), h("th", {}, "#"), h("th", {}, "Starts"), h("th", {}, "Song"), h("th", { title: "How long it plays in the set — editable" }, "Plays for"),
        h("th", {}, "Cue / time"), h("th", {}, "Transition notes"), h("th", {}))),
      h("tbody", { id: "timeline-body" })),
    addRow());
  renderTimeline();
}

function renderList() {
  const nav = document.getElementById("sets-nav");
  if (!nav) return;
  nav.replaceChildren(...sets.map((s) => h("div", {
    class: `nav-item${s.id === currentId ? " active" : ""}`, onclick: () => openSet(s.id),
  }, h("span", { class: "icon" }, "☰"), h("span", { class: "name" }, s.name), h("span", { class: "count" }, s.items))));
}

function renderStats() {
  const el = document.getElementById("set-stats");
  if (!el) return;
  const songs = current.items.filter((i) => i.free_text == null);
  const total = songs.reduce((a, i) => a + played(i), 0);
  const notReady = songs.filter((i) => i.track_id && i.download_status !== "done").length;
  el.textContent = `${songs.length} song${songs.length === 1 ? "" : "s"} · ${fmtMs(total)} total` +
    (notReady ? ` · ${notReady} not downloaded yet` : "");
}

function renderTimeline() {
  const body = document.getElementById("timeline-body");
  if (!body) return;
  let elapsed = 0, n = 0;
  body.replaceChildren(...current.items.map((it, idx) => {
    const isFree = it.free_text != null;
    const tr = h("tr", { class: isFree ? "free" : "", draggable: "false" });
    const handle = h("td", { class: "handle", title: "Drag to reorder" }, "⋮⋮");
    handle.addEventListener("mousedown", () => { tr.draggable = true; });
    tr.addEventListener("dragstart", (e) => { dragIndex = idx; tr.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", String(idx)); });
    tr.addEventListener("dragend", () => { tr.draggable = false; tr.classList.remove("dragging"); dragIndex = null; body.querySelectorAll(".drop-before").forEach((r) => r.classList.remove("drop-before")); });
    tr.addEventListener("dragover", (e) => { if (dragIndex === null) return; e.preventDefault(); body.querySelectorAll(".drop-before").forEach((r) => r.classList.remove("drop-before")); tr.classList.add("drop-before"); });
    tr.addEventListener("drop", (e) => {
      e.preventDefault();
      if (dragIndex === null || dragIndex === idx) return;
      const [moved] = current.items.splice(dragIndex, 1);
      current.items.splice(dragIndex < idx ? idx - 1 : idx, 0, moved);
      changed();
    });

    const cue = h("input", { value: it.cue_time || "", placeholder: "e.g. drop 1:04" });
    cue.addEventListener("input", () => { it.cue_time = cue.value; changed({ rerender: false }); });
    const note = h("textarea", { placeholder: isFree ? "" : "How to mix in/out…" }, it.transition_note || "");
    note.addEventListener("input", () => { it.transition_note = note.value; changed({ rerender: false }); });
    const remove = h("button", { class: "icon-btn", title: "Remove row", onclick: () => { current.items.splice(idx, 1); changed(); } }, "✕");

    let songCell, start = "", len = null;
    if (isFree) {
      const text = h("input", { value: it.free_text, placeholder: "Free text — mic break, B2B with…, encore" });
      text.addEventListener("input", () => { it.free_text = text.value; changed({ rerender: false }); });
      songCell = text;
    } else {
      n += 1;
      start = fmtMs(elapsed);
      len = lengthInput(it);
      elapsed += played(it);
      const gone = !it.title;
      songCell = h("div", { class: "song" },
        it.cover_url ? h("img", { class: "cover", src: it.cover_url, alt: "" }) : h("div", { class: "cover placeholder" }, "♪"),
        h("div", { style: { minWidth: 0 } },
          h("div", { class: "t-title" }, gone ? (it.label || "(deleted song)") : it.title),
          h("div", { class: "t-sub" }, gone ? "No longer in your library" :
            [it.artists, it.download_status !== "done" ? "not downloaded" : null, it.cleaned ? "cleaned" : null, it.verified ? "verified ✓" : null].filter(Boolean).join(" · "))));
    }
    tr.append(handle, h("td", { class: "num" }, isFree ? "" : n), h("td", { class: "start" }, start), h("td", {}, songCell),
      h("td", {}, len), h("td", {}, cue), h("td", {}, note), h("td", {}, remove));
    return tr;
  }));
  if (!current.items.length) {
    body.append(h("tr", {}, h("td", { colspan: "8", class: "muted", style: { padding: "20px", textAlign: "center" } },
      "Empty set — add songs from your library below.")));
  }
  renderStats();
}

// How long a song actually plays in the set (e.g. you mix in at 1:04).
function played(it) {
  return it.length_ms || it.duration_ms || 0;
}

export function parseTime(text) {
  const t = (text || "").trim();
  if (!t) return null;
  if (!/^\d+(:\d{1,2}){0,2}$/.test(t)) return NaN;
  const parts = t.split(":").map(Number);
  if (parts.slice(1).some((p) => p >= 60)) return NaN;
  const secs = parts.reduce((acc, p) => acc * 60 + p, 0);
  return secs > 0 ? secs * 1000 : NaN;
}

function lengthInput(it) {
  const input = h("input", {
    value: it.length_ms ? fmtMs(it.length_ms) : "",
    placeholder: fmtMs(it.duration_ms),
    title: "How long this song plays in the set. Leave empty for the full length.\nE.g. you mix in at 1:04 of a 3:03 song → 1:59.",
    style: { fontVariantNumeric: "tabular-nums" },
  });
  input.addEventListener("change", () => {
    const ms = parseTime(input.value);
    if (Number.isNaN(ms)) {
      input.classList.add("invalid");
      toast("Use m:ss, like 1:59.", { type: "error" });
      return;
    }
    input.classList.remove("invalid");
    it.length_ms = ms;
    changed();
  });
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); });
  const full = it.length_ms && it.duration_ms && it.length_ms !== it.duration_ms;
  return h("div", {}, input, full ? h("div", { class: "t-sub" }, `of ${fmtMs(it.duration_ms)}`) : null);
}

function addRow() {
  const input = h("input", { placeholder: "Add a song from your library — type a title or artist…" });
  const wrap = h("div", { class: "add-row" }, input,
    h("button", { class: "btn", onclick: () => { current.items.push({ track_id: null, free_text: "", cue_time: "", transition_note: "" }); changed(); } }, "+ Free-text row"));
  const search = debounce(async () => {
    const q = input.value.trim();
    if (!q) return closePopover();
    try {
      const { tracks } = await get(`/api/tracks?view=smart:all&q=${encodeURIComponent(q)}`);
      const items = tracks.slice(0, 15).map((t) => h("button", {
        onclick: () => {
          current.items.push({
            track_id: t.id, free_text: null, title: t.title, artists: t.artists, cover_url: t.cover_url,
            duration_ms: t.file_duration_ms ?? t.duration_ms, download_status: t.download_status, cleaned: t.cleaned, verified: t.verified,
            cue_time: "", transition_note: "",
          });
          input.value = "";
          closePopover();
          changed();
          input.focus();
        },
      }, h("span", { class: "grow" }, `${t.title} — ${t.artists}`), h("span", { class: "muted small" }, fmtMs(t.file_duration_ms ?? t.duration_ms))));
      popover(input, h("div", { class: "menu", style: { width: "480px" } },
        items.length ? items : h("div", { class: "label" }, "No matches in your library")));
    } catch (e) { errorToast(e); }
  }, 200);
  input.addEventListener("input", search);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.querySelector(".popover .menu button")?.click();
  });
  return wrap;
}
