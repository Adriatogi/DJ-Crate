// Set Setter: build a set as a board of song / mix-note / text nodes (branches
// welcome). The main path — the bright wires — is the set as you'd play it, and
// is what the List view, running time and text export show.

import { del, get, patch, post, put } from "./api.js";
import { Board, noteBody, songBody } from "./board.js";
import { songPicker } from "./pickers.js";
import { readOnly } from "./store.js";
import { copyText, debounce, errorToast, fill, fmtMs, h, menu, toast } from "./ui.js";

let sets = [];
let currentId = Number(localStorage.getItem("djc.set")) || null;
let current = null;       // last server board {set, nodes, edges, main_path, total_ms}
let nodes = [], edges = []; // local, editable copy
let mode = localStorage.getItem("djc.setmode") || "board";
let board = null;
let tempId = -1;
let version = 0, savedVersion = 0, saving = null;
let editAfterSave = null;

const root = () => document.getElementById("view-sets");
const STEP_SONG = 300, STEP_NOTE = 280;

export async function showSets() {
  await loadList();
  if (currentId && sets.some((s) => s.id === currentId)) await openSet(currentId);
  else if (sets.length) await openSet(sets[0].id);
  else { current = null; render(); }
}

async function loadList() {
  try { sets = (await get("/api/sets")).sets; } catch (e) {
    if (e.code !== "library_unavailable") errorToast(e);
    sets = [];
  }
}

async function openSet(id) {
  await flush();
  try {
    adopt(await get(`/api/sets/${id}`));
    currentId = id;
    localStorage.setItem("djc.set", id);
  } catch (e) {
    errorToast(e);
    current = null;
  }
  render();
}

function adopt(data) {
  current = data;
  nodes = data.nodes.map((n) => ({ ...n }));
  edges = data.edges.map((e) => ({ from_node: e.from_node, to_node: e.to_node, main: e.main }));
  version = savedVersion = 0;
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
    await flush();
    const { snapshot } = await del(`/api/sets/${current.set.id}`);
    current = null;
    currentId = null;
    await showSets();
    toast(`Deleted “${name}”.`, {
      timeout: 10000,
      action: { label: "Undo", fn: async () => { const { id } = await post("/api/sets/restore", { snapshot }); await loadList(); await openSet(id); } },
    });
  } catch (e) { errorToast(e); }
}

// ------------------------------------------------ saving the board

const saveMeta = debounce(async (changes) => {
  try { await patch(`/api/sets/${current.set.id}`, changes); await loadList(); renderList(); } catch (e) { errorToast(e); }
}, 500);

const queueSave = debounce(() => save(), 600);

function changed() {
  version += 1;
  if (mode === "list") queueSave.flush(); else queueSave();
  renderBoard();
  renderStats();
}

async function save() {
  if (!current || readOnly() || version === savedVersion) return;
  if (saving) { await saving; if (version === savedVersion) return; }
  const sent = version;
  const setId = current.set.id;
  saving = (async () => {
    try {
      const r = await put(`/api/sets/${setId}/board`, {
        nodes: nodes.map((n) => ({ id: n.id, kind: n.kind, track_id: n.track_id, mix_id: n.mix_id, text: n.text, label: n.label,
          cue_time: n.cue_time, length_ms: n.length_ms, x: n.x, y: n.y })),
        edges,
      });
      if (!current || current.set.id !== setId) return;
      // New nodes got real ids: remap the local copy (it may have changed meanwhile).
      const map = r.idmap;
      for (const n of nodes) if (n.id < 0 && map[String(n.id)]) n.id = map[String(n.id)];
      for (const e of edges) {
        e.from_node = map[String(e.from_node)] ?? e.from_node;
        e.to_node = map[String(e.to_node)] ?? e.to_node;
      }
      const fresh = new Map(r.nodes.map((n) => [n.id, n]));
      for (const n of nodes) {
        const s = fresh.get(n.id);
        if (s) Object.assign(n, { title: s.title, artists: s.artists, cover_url: s.cover_url, ready: s.ready, broken: s.broken,
          duration_ms: s.duration_ms, download_status: s.download_status, saved: s.saved, label: s.label });
      }
      current = { ...r, nodes: current.nodes };
      savedVersion = sent;
      if (editAfterSave) { const id = map[String(editAfterSave)] ?? editAfterSave; editAfterSave = null; renderBoard(); startEditing(id); }
      else renderBoard();
      if (mode === "list") renderListView();
      renderStats();
    } catch (e) { errorToast(e); }
  })();
  await saving;
  saving = null;
  if (version !== savedVersion) queueSave();
}

async function flush() {
  if (!current) return;
  queueSave.flush ? await queueSave.flush() : null;
  if (saving) await saving;
}

// ------------------------------------------------ graph helpers

const byId = (id) => nodes.find((n) => n.id === id);

function mainChain() {
  const main = edges.filter((e) => e.main && byId(e.from_node) && byId(e.to_node));
  if (!main.length) {
    const first = [...nodes].sort((a, b) => a.x - b.x)[0];
    return first ? [first] : [];
  }
  const hasIn = new Set(main.map((e) => e.to_node));
  const start = main.map((e) => byId(e.from_node)).filter((n) => !hasIn.has(n.id)).sort((a, b) => a.x - b.x)[0] || byId(main[0].from_node);
  const chain = [start], seen = new Set([start.id]);
  let cur = start;
  for (;;) {
    const e = main.find((x) => x.from_node === cur.id && !seen.has(x.to_node));
    if (!e) break;
    cur = byId(e.to_node);
    chain.push(cur);
    seen.add(cur.id);
  }
  return chain;
}

function mainEnd() {
  const chain = mainChain();
  return chain[chain.length - 1] || null;
}

function joinsMain(fromId, toId) {
  // A new wire joins the main path if it continues from its end and the target isn't already on it.
  const end = mainEnd();
  return (!end || end.id === fromId || !edges.some((e) => e.main)) && !edges.some((e) => e.main && e.to_node === toId) &&
    !edges.some((e) => e.main && e.from_node === fromId);
}

function addNode(kind, props, { at = null, wire = true } = {}) {
  const end = wire ? mainEnd() : null;
  const pos = at || (end ? { x: end.x + (kind === "mix" || end.kind === "mix" ? STEP_NOTE : STEP_SONG), y: end.y } : { x: 0, y: 0 });
  const node = { id: tempId--, kind, track_id: null, mix_id: null, text: "", label: "", cue_time: "", length_ms: null, ...pos, ...props };
  nodes.push(node);
  if (end && wire) edges.push({ from_node: end.id, to_node: node.id, main: true });
  return node;
}

function connect(fromId, toId) {
  const a = byId(fromId), b = byId(toId);
  if (!a || !b || a.id === b.id) return;
  if (edges.some((e) => e.from_node === a.id && e.to_node === b.id)) return;
  if (a.kind !== "mix" && b.kind !== "mix") {
    // song → song: put a mix note between them, ready to write.
    const main = joinsMain(a.id, b.id);
    const mix = addNode("mix", {}, { at: { x: (a.x + b.x) / 2 + 20, y: (a.y + b.y) / 2 + 70 }, wire: false });
    edges.push({ from_node: a.id, to_node: mix.id, main }, { from_node: mix.id, to_node: b.id, main });
    editAfterSave = mix.id;
  } else {
    edges.push({ from_node: a.id, to_node: b.id, main: joinsMain(a.id, b.id) });
  }
  changed();
}

function makeMain(edge) {
  for (const e of edges) {
    if (e === edge) continue;
    if (e.from_node === edge.from_node || e.to_node === edge.to_node) e.main = false;
  }
  edge.main = true;
  changed();
}

function splitEdge(edge) {
  const a = byId(edge.from_node), b = byId(edge.to_node);
  const mix = addNode("mix", {}, { at: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 + 60 }, wire: false });
  edges.splice(edges.indexOf(edge), 1,
    { from_node: a.id, to_node: mix.id, main: edge.main }, { from_node: mix.id, to_node: b.id, main: edge.main });
  editAfterSave = mix.id;
  changed();
}

function removeNode(id) {
  nodes = nodes.filter((n) => n.id !== id);
  // Keep the main path whole: if a main node is removed, join its neighbours.
  const inMain = edges.find((e) => e.main && e.to_node === id), outMain = edges.find((e) => e.main && e.from_node === id);
  edges = edges.filter((e) => e.from_node !== id && e.to_node !== id);
  if (inMain && outMain) edges.push({ from_node: inMain.from_node, to_node: outMain.to_node, main: true });
  changed();
}

function startEditing(id) {
  setTimeout(() => board?.nodes.get(`n:${id}`)?.data.body.startEditing?.(), 30);
}

// ------------------------------------------------ render

function render() {
  const listPane = h("aside", { class: "sets-list" },
    h("button", { class: "btn primary block", onclick: newSet, disabled: readOnly() }, "+ New set"),
    h("div", { id: "sets-nav", style: { marginTop: "10px" } }));
  const editor = h("section", { class: `set-editor${mode === "board" ? " board-mode" : ""}`, id: "set-editor" });
  fill(root(), listPane, editor);
  board = null;
  renderList();
  if (!current) {
    editor.append(h("div", { class: "empty" },
      h("h3", {}, "Plan a set"),
      h("p", {}, "Lay out songs on a board, wire them through mix notes, branch out backups — the bright path is your set. Export it as text for your phone."),
      h("button", { class: "btn primary", onclick: newSet }, "+ New set")));
    return;
  }
  const name = h("input", { class: "set-name", value: current.set.name });
  name.addEventListener("input", () => { current.set.name = name.value; saveMeta({ name: name.value }); });
  const notes = h("textarea", { placeholder: "Set notes — venue, vibe, start time, what to avoid…", rows: 2 }, current.set.notes);
  notes.addEventListener("input", () => saveMeta({ notes: notes.value }));
  const exportBtn = h("button", { class: "btn" }, "Export as text ▾");
  exportBtn.onclick = () => menu(exportBtn, [
    { label: "Copy to clipboard", fn: async () => { await flush(); const r = await get(`/api/sets/${current.set.id}/export`); await copyText(r.text); toast("Copied the set as text.", { type: "success" }); } },
    { label: "Save .txt to Downloads", fn: async () => { await flush(); const r = await post(`/api/sets/${current.set.id}/export_file`); toast(`Saved ${r.path.split("/").pop()} to Downloads.`, { type: "success" }); } },
  ], { align: "right" });
  const addSong = h("button", { class: "btn small primary", disabled: readOnly() }, "+ Song");
  addSong.onclick = () => songPicker(addSong, {
    title: "Add a song to the end of the set",
    onPick: (t) => { addNode("song", songProps(t)); changed(); },
  });
  const seg = h("div", { class: "seg" },
    h("button", { class: mode === "board" ? "on" : "", onclick: () => setMode("board") }, "Board"),
    h("button", { class: mode === "list" ? "on" : "", onclick: () => setMode("list") }, "List"));
  editor.append(
    h("div", { class: "set-head" }, name, h("div", { class: "spacer" }), exportBtn, h("button", { class: "btn danger", onclick: deleteSet }, "Delete set")),
    notes,
    h("div", { class: "set-mode" }, seg, addSong,
      h("button", { class: "btn small", disabled: readOnly(), onclick: () => { const n = addNode("text", { text: "" }); changed(); startEditing(n.id); } }, "+ Text"),
      h("div", { class: "spacer" }), h("span", { class: "set-stats", id: "set-stats", style: { margin: 0 } })),
    mode === "board" ? boardPane() : h("div", { id: "set-list" }));
  renderStats();
  if (mode !== "board") renderListView(); // the board builds itself once it's on screen
}

function songProps(t) {
  return { track_id: t.id, title: t.title, artists: t.artists, cover_url: t.cover_url, ready: t.ready, broken: t.broken,
    duration_ms: t.file_duration_ms ?? t.duration_ms, download_status: t.download_status, label: `${t.title} — ${t.artists}` };
}

async function setMode(m) {
  mode = m;
  localStorage.setItem("djc.setmode", m);
  await flush();
  if (current) adopt(await get(`/api/sets/${current.set.id}`));
  render();
}

function renderList() {
  const nav = document.getElementById("sets-nav");
  if (!nav) return;
  fill(nav, sets.map((s) => h("div", {
    class: `nav-item${s.id === currentId ? " active" : ""}`, onclick: () => openSet(s.id),
  }, h("span", { class: "icon" }, "☰"), h("span", { class: "name" }, s.name), h("span", { class: "count" }, s.items))));
}

function played(n) {
  return n.length_ms || n.duration_ms || 0;
}

function renderStats() {
  const el = document.getElementById("set-stats");
  if (!el) return;
  const chain = mainChain().filter((n) => n.kind === "song");
  const total = chain.reduce((a, n) => a + played(n), 0);
  const notReady = chain.filter((n) => n.track_id && n.download_status !== "done").length;
  const branches = edges.filter((e) => !e.main).length ? " · has branches" : "";
  el.textContent = `${chain.length} song${chain.length === 1 ? "" : "s"} on the main path · ${fmtMs(total)}${branches}` +
    (notReady ? ` · ${notReady} not downloaded` : "");
}

// ------------------------------------------------ board view

function boardPane() {
  const wrap = h("div", { class: "board-wrap" },
    h("div", { id: "set-board", style: { flex: "1", display: "flex", minHeight: "0" } }),
    h("div", { class: "board-toolbar", id: "set-toolbar" }),
    h("div", { class: "suggest-panel", id: "set-suggest", hidden: true }),
    h("div", { class: "board-help" }, "Drag from ● to wire · bright wires = your set, dim = backups · select a song for saved mixes · Delete removes"));
  setTimeout(() => {
    board = new Board(document.getElementById("set-board"), { onConnect, onMove, onDelete, onSelect, onOpen });
    renderBoard();
    board.fit();
  });
  return wrap;
}

function renderBoard() {
  if (!board) return;
  const onMain = new Set(mainChain().map((n) => n.id));
  board.setData({
    nodes: nodes.map((n) => ({
      key: `n:${n.id}`, kind: n.kind, x: n.x, y: n.y, ghost: !onMain.has(n.id) && edges.some((e) => e.main), raw: n,
      body: n.kind === "song"
        ? songBody({ title: n.title || n.label || "(deleted song)", artists: n.artists || "", cover_url: n.cover_url, ready: n.ready, broken: n.broken },
          { sub: [n.artists, n.length_ms ? `plays ${fmtMs(n.length_ms)}` : null, n.cue_time || null].filter(Boolean).join(" · ") })
        : noteBody(n.text, {
          title: n.kind === "text" ? "Note" : "Mix",
          placeholder: n.kind === "text" ? "mic break, B2B with…, encore" : undefined,
          extra: n.kind === "mix" ? (n.saved ? h("span", { class: "saved-tag", title: "Saved in Cutting — editing updates it everywhere" }, "✓ SAVED")
            : h("button", { class: "btn small", title: "Save this transition so it shows up in Cutting and as a suggestion", onclick: () => saveAsMix(n) }, "Save as mix")) : null,
          onSave: (text) => { n.text = text; changed(); },
        }),
    })),
    edges: edges.map((e, i) => ({ key: `e:${i}`, from: `n:${e.from_node}`, to: `n:${e.to_node}`, main: e.main, dim: !e.main })),
  });
}

const keyId = (key) => Number(key.split(":")[1]);

function onConnect(fromKey, toKey, at) {
  if (readOnly()) return;
  if (toKey) return connect(keyId(fromKey), keyId(toKey));
  songPicker({ clientX: at.clientX, clientY: at.clientY }, {
    title: "Wire to which song?",
    onPick: (t) => {
      const n = addNode("song", songProps(t), { at: { x: at.x, y: at.y - 30 }, wire: false });
      connect(keyId(fromKey), n.id);
    },
  });
}

function onMove(data, x, y) {
  const n = byId(keyId(data.key));
  if (n) { n.x = x; n.y = y; version += 1; queueSave(); }
}

function onDelete(sel) {
  if (readOnly()) return;
  if (sel.type === "node") removeNode(keyId(sel.key));
  else { edges.splice(keyId(sel.key), 1); changed(); }
  document.getElementById("set-suggest").hidden = true;
}

function onOpen(data) {
  if (data.kind !== "song") data.body.startEditing?.();
}

async function onSelect(sel) {
  const bar = document.getElementById("set-toolbar");
  const panel = document.getElementById("set-suggest");
  if (!bar) return;
  panel.hidden = true;
  if (!sel) return bar.replaceChildren();
  if (sel.type === "edge") {
    const e = edges[keyId(sel.key)];
    return fill(bar,
      h("button", { class: "btn small", onclick: () => splitEdge(e) }, "Add mix note here"),
      e.main ? null : h("button", { class: "btn small primary", onclick: () => makeMain(e) }, "Make main path"),
      h("button", { class: "btn small danger", onclick: () => onDelete(sel) }, "Delete wire"));
  }
  const n = byId(keyId(sel.key));
  fill(bar,
    n.kind === "mix" && !n.saved ? h("button", { class: "btn small", onclick: () => saveAsMix(n) }, "Save as mix") : null,
    n.kind !== "song" ? h("button", { class: "btn small", onclick: () => startEditing(n.id) }, "Edit") : null,
    h("button", { class: "btn small danger", onclick: () => onDelete(sel) }, "Remove"));
  if (n.kind === "song" && n.track_id) showSuggestions(n);
}

async function showSuggestions(n) {
  const panel = document.getElementById("set-suggest");
  let mixes;
  try { mixes = (await get(`/api/mixes/from/${n.track_id}`)).mixes; } catch (e) { return errorToast(e); }
  if (board?.selected?.key !== `n:${n.id}`) return;
  panel.hidden = false;
  fill(panel,
    h("div", { class: "section-title", style: { marginTop: 0 } }, `Saved mixes from ${n.title}`),
    mixes.length ? mixes.map((m) => h("button", { class: "item", onclick: () => useMix(n, m) },
      h("div", { class: "t-title" }, "→ " + m.outs.map((s) => s.song?.title || s.label).join(", ")),
      m.ins.length > 1 ? h("div", { class: "t-sub" }, "with " + m.ins.filter((s) => s.track_id !== n.track_id).map((s) => s.song?.title || s.label).join(", ")) : null,
      h("div", { class: "note" }, m.note || "(no note yet)")))
      : h("div", { class: "muted small" }, "No saved mixes from this song yet. Write one in Cutting, or wire it here and click “Save as mix”."));
}

function useMix(from, m) {
  const main = joinsMain(from.id, null);
  const mix = addNode("mix", { mix_id: m.id, text: m.note, saved: true }, { at: { x: from.x + STEP_NOTE, y: from.y }, wire: false });
  edges.push({ from_node: from.id, to_node: mix.id, main });
  m.outs.filter((s) => s.track_id).forEach((s, i) => {
    const song = addNode("song", songProps({ ...s.song, id: s.track_id }), { at: { x: mix.x + STEP_NOTE, y: from.y + i * 110 }, wire: false });
    edges.push({ from_node: mix.id, to_node: song.id, main: main && i === 0 });
  });
  document.getElementById("set-suggest").hidden = true;
  changed();
}

async function saveAsMix(n) {
  try {
    await flush();
    const real = byId(n.id);
    if (!real || real.id < 0) return toast("Still saving — try again in a second.", { type: "warn" });
    adopt(await post(`/api/sets/${current.set.id}/nodes/${real.id}/save_mix`));
    renderBoard();
    toast("Saved — it now shows up in Cutting and as a suggestion.", { type: "success" });
  } catch (e) { errorToast(e); }
}

// ------------------------------------------------ list view (the main path)

export function parseTime(text) {
  const t = (text || "").trim();
  if (!t) return null;
  if (!/^\d+(:\d{1,2}){0,2}$/.test(t)) return NaN;
  const parts = t.split(":").map(Number);
  if (parts.slice(1).some((p) => p >= 60)) return NaN;
  const secs = parts.reduce((acc, p) => acc * 60 + p, 0);
  return secs > 0 ? secs * 1000 : NaN;
}

async function apply(promise) {
  try { adopt(await promise); renderListView(); renderStats(); } catch (e) { errorToast(e); }
}

function renderListView() {
  const box = document.getElementById("set-list");
  if (!box) return;
  const path = current.main_path;
  const setId = current.set.id;
  let n = 0;
  const rows = path.map((r, idx) => {
    const next = path[idx + 1];
    const isSong = r.kind === "song";
    if (isSong) n += 1;
    const cue = h("input", { value: r.cue_time || "", placeholder: "e.g. drop 1:04" });
    cue.addEventListener("change", () => apply(patch(`/api/sets/${setId}/nodes/${r.node_id}`, { cue_time: cue.value })));
    const plays = h("input", { value: r.length_ms ? fmtMs(r.length_ms) : "", placeholder: fmtMs(r.duration_ms), style: { fontVariantNumeric: "tabular-nums" },
      title: "How long it plays in the set — e.g. you mix in at 1:04 of a 3:03 song → 1:59. Empty = full length." });
    plays.addEventListener("change", () => {
      const ms = parseTime(plays.value);
      if (Number.isNaN(ms)) { plays.classList.add("invalid"); return toast("Use m:ss, like 1:59.", { type: "error" }); }
      apply(patch(`/api/sets/${setId}/nodes/${r.node_id}`, ms ? { length_ms: ms } : { clear_length: true }));
    });
    const trans = h("textarea", { placeholder: next ? "How to mix into the next song…" : "Outro / ending…" }, r.transition?.text || "");
    trans.addEventListener("change", () => apply(r.transition
      ? patch(`/api/sets/${setId}/nodes/${r.transition.node_id}`, { text: trans.value })
      : post(`/api/sets/${setId}/transition`, { after_node: r.node_id, text: trans.value })));
    let savedCell = null;
    if (isSong && next?.kind === "song" && r.track_id && next.track_id) {
      savedCell = r.transition?.saved || r.saved_mix_id
        ? h("span", { class: "saved-tag", title: "This transition is saved in Cutting" }, "✓ saved")
        : h("button", {
          class: "btn small", title: "Save this transition as a mix (shows up in Cutting)",
          onclick: () => apply(r.transition
            ? post(`/api/sets/${setId}/nodes/${r.transition.node_id}/save_mix`)
            : post(`/api/sets/${setId}/save_pair`, { a_node: r.node_id, b_node: next.node_id, text: trans.value })),
        }, "Save as mix");
    }
    const song = isSong
      ? h("div", { class: "song" },
        r.cover_url ? h("img", { class: "cover", src: r.cover_url, alt: "" }) : h("div", { class: "cover placeholder" }, "♪"),
        h("div", { style: { minWidth: 0 } },
          h("div", { class: "t-title" }, r.title || r.label || "(deleted song)"),
          h("div", { class: "t-sub" }, r.title ? [r.artists, r.download_status !== "done" ? "not downloaded" : null, r.ready ? "ready ✓" : null, r.broken ? "⚠ broken" : null].filter(Boolean).join(" · ") : "No longer in your library")))
      : h("div", { class: "t-title" }, `— ${r.text || "note"} —`);
    return h("tr", { class: isSong ? "" : "free" },
      h("td", { class: "num" }, isSong ? n : ""),
      h("td", { class: "start" }, isSong ? fmtMs(r.start_ms) : ""),
      h("td", {}, song),
      h("td", {}, isSong ? h("div", {}, plays, r.length_ms && r.duration_ms && r.length_ms !== r.duration_ms ? h("div", { class: "t-sub" }, `of ${fmtMs(r.duration_ms)}`) : null) : null),
      h("td", {}, cue),
      h("td", {}, trans),
      h("td", {}, savedCell));
  });
  fill(box,
    h("p", { class: "muted small" }, "This is your main path. Reorder, branch and add backups on the Board."),
    h("table", { class: "timeline" },
      h("colgroup", {}, h("col", { style: { width: "34px" } }), h("col", { style: { width: "62px" } }), h("col", {}),
        h("col", { style: { width: "96px" } }), h("col", { style: { width: "130px" } }), h("col", { style: { width: "30%" } }), h("col", { style: { width: "96px" } })),
      h("thead", {}, h("tr", {}, h("th", {}, "#"), h("th", {}, "Starts"), h("th", {}, "Song"), h("th", {}, "Plays for"),
        h("th", {}, "Cue / time"), h("th", {}, "Transition notes"), h("th", {}))),
      h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { colspan: "7", class: "muted", style: { padding: "20px", textAlign: "center" } },
        "Empty set — add songs with + Song.")))));
}
