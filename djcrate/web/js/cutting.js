// Cutting: one board per song. The song sits in the middle; songs it mixes
// into (right) and from (left) appear as ghost nodes, 1–3 steps out. Mixes are
// note nodes wired song → note → song. Samples are dotted links.

import { del, get, patch, post, put } from "./api.js";
import { Board, noteBody, songBody } from "./board.js";
import { songCard } from "./detail.js";
import { songPicker } from "./pickers.js";
import { emit, on, readOnly } from "./store.js";
import { confirmDialog, debounce, errorToast, fill, h, toast } from "./ui.js";

const COL_W = 560, ROW_H = 112, MIX_ROW_H = 150;

let center = Number(localStorage.getItem("djc.cut.center")) || null;
let depth = Number(localStorage.getItem("djc.cut.depth")) || 1;
let onlyConnected = localStorage.getItem("djc.cut.connected") === "1";
let data = null;        // last /api/cutting/board
let board = null;
let sideSong = null;    // song shown in the right panel
let pendingEdit = null; // mix id whose note should open for editing after reload
const root = () => document.getElementById("view-cutting");

// Another tab asked for a song's board (e.g. a linked sample on a Crates card).
on("cutting-focus", (id) => { if (root().querySelector(".cutting-main")) focus(id); else { center = id; localStorage.setItem("djc.cut.center", id); } });

export async function showCutting() {
  if (!root().querySelector(".cutting-main")) build();
  await loadList();
  if (center) await loadBoard({ fit: true });
  else renderEmpty();
}

function build() {
  const search = h("input", { type: "search", placeholder: "Search songs…" });
  search.addEventListener("input", debounce(() => loadList(search.value), 200));
  const filter = h("div", { class: "seg" },
    h("button", { class: onlyConnected ? "on" : "", onclick: () => setFilter(true) }, "Has mixes"),
    h("button", { class: onlyConnected ? "" : "on", onclick: () => setFilter(false) }, "All songs"));
  fill(root(),
    h("aside", { class: "cutting-list" },
      h("div", { class: "head" }, search, filter),
      h("div", { class: "songs", id: "cut-songs" })),
    h("section", { class: "cutting-main" },
      h("div", { class: "cutting-bar", id: "cut-bar" }),
      h("div", { class: "board-wrap", id: "cut-board-wrap" },
        h("div", { id: "cut-board", style: { flex: "1", display: "flex", minHeight: "0" } }),
        h("div", { class: "board-toolbar", id: "cut-toolbar" }),
        h("div", { class: "board-help" }, "Drag from a song's ● to connect · double-click a song to open its board · Delete removes the selected wire or note"))),
    h("aside", { class: "cutting-side", id: "cut-side" }));
  board = new Board(document.getElementById("cut-board"), {
    onConnect, onMove: queueLayout, onDelete, onSelect, onOpen,
  });
}

function setFilter(v) {
  onlyConnected = v;
  localStorage.setItem("djc.cut.connected", v ? "1" : "0");
  root().querySelectorAll(".cutting-list .seg button").forEach((b, i) => b.classList.toggle("on", (i === 0) === v));
  loadList(root().querySelector(".cutting-list input").value);
}

async function loadList(q = "") {
  try {
    const { songs } = await get(`/api/cutting/songs?q=${encodeURIComponent(q)}&connected=${onlyConnected ? 1 : 0}`);
    const list = document.getElementById("cut-songs");
    if (!songs.length) {
      fill(list, h("div", { class: "muted small", style: { padding: "12px" } },
        onlyConnected ? "No mixes yet. Switch to All songs, pick one, and drag from its ● to another song." : "No songs."));
      return;
    }
    fill(list, songs.map((s) => h("div", {
      class: `nav-item${s.id === center ? " active" : ""}`, onclick: () => focus(s.id),
    },
      s.cover_url ? h("img", { class: "cover", src: s.cover_url, alt: "" }) : h("div", { class: "cover placeholder" }, "♪"),
      h("div", { class: "name" },
        h("div", { class: "name" }, s.title),
        h("div", { class: "stats" }, [s.artists, s.mixes ? `${s.mixes} mix${s.mixes === 1 ? "" : "es"}` : null, s.samples ? `${s.samples} sample${s.samples === 1 ? "" : "s"}` : null].filter(Boolean).join(" · "))))));
  } catch (e) {
    if (e.code !== "library_unavailable") errorToast(e);
  }
}

export function focus(trackId) {
  center = trackId;
  localStorage.setItem("djc.cut.center", trackId);
  sideSong = null;
  root().querySelectorAll("#cut-songs .nav-item").forEach((n) => n.classList.remove("active"));
  loadBoard({ fit: true });
  loadList(root().querySelector(".cutting-list input")?.value || "");
}

function renderEmpty() {
  fill(document.getElementById("cut-bar"), h("h2", { style: { margin: 0, fontSize: "16px" } }, "Cutting"));
  board.setData({ nodes: [], edges: [] });
  fill(document.getElementById("cut-side"),
    h("div", { class: "cutting-empty" }, h("div", {},
      h("h3", {}, "Pick a song"),
      h("p", {}, "Every song has its own board: what it mixes into, what mixes into it, and what it samples."))));
}

// ------------------------------------------------ board data → nodes

async function loadBoard({ fit = false } = {}) {
  try {
    data = await get(`/api/cutting/board/${center}?depth=${depth}`);
  } catch (e) {
    if (e.status === 404) { center = null; localStorage.removeItem("djc.cut.center"); return renderEmpty(); }
    return errorToast(e);
  }
  renderBar();
  const positions = layout(data);
  const touchesCenter = (m) => [...m.ins, ...m.outs].some((s) => s.track_id === center);
  const nodes = [
    ...data.songs.map((s) => ({
      key: `song:${s.id}`, kind: "song", ...positions[`song:${s.id}`], center: s.id === center, ghost: s.id !== center,
      body: songBody(s, { sub: s.id === center ? s.artists : `${s.artists}` }), song: s,
    })),
    ...data.mixes.map((m) => ({
      key: `mix:${m.id}`, kind: "mix", ...positions[`mix:${m.id}`], ghost: !touchesCenter(m), mix: m,
      body: noteBody(m.note, { title: m.ins.length + m.outs.length > 2 ? `Mix · ${m.ins.length + m.outs.length} songs` : "Mix",
        onSave: (note) => saveNote(m.id, note) }),
    })),
  ];
  const edges = [];
  for (const m of data.mixes) {
    const ghost = !touchesCenter(m);
    for (const s of m.ins) if (s.track_id) edges.push({ key: `in:${m.id}:${s.track_id}`, from: `song:${s.track_id}`, to: `mix:${m.id}`, ghost });
    for (const s of m.outs) if (s.track_id) edges.push({ key: `out:${m.id}:${s.track_id}`, from: `mix:${m.id}`, to: `song:${s.track_id}`, ghost });
  }
  for (const s of data.samples) edges.push({ key: `sample:${s.id}`, from: `song:${s.track_id}`, to: `song:${s.sample_track_id}`, kind: "sample" });
  board.setData({ nodes, edges }, { keepView: !fit });
  if (pendingEdit) {
    const entry = board.nodes.get(`mix:${pendingEdit}`);
    pendingEdit = null;
    entry?.data.body.startEditing?.();
  }
  renderSide(sideSong && sideSong !== center ? sideSong : center);
}

// Center at column 0; mixes sit between their songs' columns. Saved positions win.
function layout(d) {
  const pos = {};
  const byCol = {};
  for (const s of d.songs) (byCol[s.col] ||= []).push(s);
  for (const [col, songs] of Object.entries(byCol)) {
    songs.sort((a, b) => (a.id === center ? -1 : b.id === center ? 1 : a.hop - b.hop || a.title.localeCompare(b.title)));
    const n = songs.length;
    songs.forEach((s, i) => { pos[`song:${s.id}`] = { x: Number(col) * COL_W, y: (i - (n - 1) / 2) * ROW_H }; });
  }
  const taken = [];
  for (const m of d.mixes) {
    const ys = [...m.ins, ...m.outs].filter((s) => s.track_id && pos[`song:${s.track_id}`]).map((s) => pos[`song:${s.track_id}`].y);
    const x = m.col * COL_W + (Number.isInteger(m.col) ? 0 : 10);
    let y = ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : 0;
    while (taken.some((t) => Math.abs(t.x - x) < 200 && Math.abs(t.y - y) < MIX_ROW_H - 20)) y += MIX_ROW_H;
    taken.push({ x, y });
    pos[`mix:${m.id}`] = { x, y: y - 20 };
  }
  for (const [key, p] of Object.entries(d.layout || {})) if (pos[key]) pos[key] = { x: p.x, y: p.y };
  return pos;
}

function renderBar() {
  const c = data.center;
  const seg = h("div", { class: "seg", title: "How many steps out from this song to show" },
    [1, 2, 3].map((n) => h("button", { class: n === depth ? "on" : "", onclick: () => { depth = n; localStorage.setItem("djc.cut.depth", n); loadBoard({ fit: true }); } }, `${n}`)));
  const connect = h("button", { class: "btn primary small", disabled: readOnly() }, "+ Connect a song…");
  connect.onclick = () => connectDialog(connect);
  fill(document.getElementById("cut-bar"),
    h("div", { style: { minWidth: 0 } },
      h("h2", { style: { margin: 0, fontSize: "16px", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" } }, c.title),
      h("div", { class: "muted small" }, `${c.artists} · ${data.mixes.length} mix${data.mixes.length === 1 ? "" : "es"} shown`)),
    h("div", { class: "spacer" }),
    h("span", { class: "muted small" }, "Steps"), seg,
    connect,
    h("button", { class: "btn small ghost", title: "Forget dragged positions", onclick: resetLayout }, "Tidy up"));
}

function connectDialog(anchor) {
  let dir = "out";
  const seg = h("div", { class: "seg", style: { marginBottom: "8px" } },
    h("button", { class: "on", onclick: (e) => setDir("out", e) }, `${data.center.title} → song`),
    h("button", { onclick: (e) => setDir("in", e) }, `song → ${data.center.title}`));
  function setDir(d, e) {
    dir = d;
    seg.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === e.target));
  }
  songPicker(anchor, {
    title: "Connect a song with a mix", extra: seg,
    onPick: (t) => newMix(dir === "out" ? [center] : [t.id], dir === "out" ? [t.id] : [center]),
  });
}

// ------------------------------------------------ actions

async function newMix(inIds, outIds, at = null) {
  try {
    const m = await post("/api/mixes", { in_ids: inIds, out_ids: outIds, note: "" });
    if (at) await put(`/api/cutting/board/${center}/layout`, { positions: [{ kind: "mix", id: m.id, x: at.x - 110, y: at.y - 40 }] });
    pendingEdit = m.id;
    await loadBoard();
    loadList(root().querySelector(".cutting-list input").value);
    emit("refresh-sidebar");
  } catch (e) { errorToast(e); }
}

async function saveNote(mixId, note) {
  try {
    await patch(`/api/mixes/${mixId}`, { note });
    const m = data.mixes.find((x) => x.id === mixId);
    if (m) m.note = note;
    loadBoard();
  } catch (e) { errorToast(e); }
}

const parse = (key) => { const [kind, id] = key.split(":"); return { kind, id: Number(id) }; };

async function onConnect(fromKey, toKey, at) {
  if (readOnly()) return toast("This library is read-only.", { type: "warn" });
  const from = parse(fromKey);
  if (!toKey) {
    songPicker({ clientX: at.clientX, clientY: at.clientY }, {
      title: from.kind === "song" ? "Mix into which song?" : "Add a song coming out of this mix",
      onPick: async (t) => {
        if (from.kind === "song") return newMix([from.id], [t.id], at);
        const m = data.mixes.find((x) => x.id === from.id);
        await editSides(m, null, [...m.outs.map((s) => s.track_id).filter(Boolean), t.id]);
      },
    });
    return;
  }
  const to = parse(toKey);
  if (from.kind === "song" && to.kind === "song") return newMix([from.id], [to.id]);
  if (from.kind === "mix" && to.kind === "mix") return toast("Connect notes through a song.", { type: "warn" });
  const m = data.mixes.find((x) => x.id === (from.kind === "mix" ? from.id : to.id));
  const ins = m.ins.map((s) => s.track_id).filter(Boolean), outs = m.outs.map((s) => s.track_id).filter(Boolean);
  if (from.kind === "song") {
    if (ins.includes(from.id)) return;
    await editSides(m, [...ins, from.id], null);
  } else {
    if (outs.includes(to.id)) return;
    await editSides(m, null, [...outs, to.id]);
  }
}

async function editSides(m, ins, outs) {
  try {
    await patch(`/api/mixes/${m.id}`, { ...(ins ? { in_ids: ins } : {}), ...(outs ? { out_ids: outs } : {}) });
    await loadBoard();
  } catch (e) { errorToast(e); }
}

async function deleteMix(m) {
  try {
    const { snapshot } = await del(`/api/mixes/${m.id}`);
    await loadBoard();
    loadList(root().querySelector(".cutting-list input").value);
    toast("Mix deleted.", { timeout: 10000, action: { label: "Undo", fn: async () => { await post("/api/mixes/restore", { snapshot }); loadBoard(); } } });
  } catch (e) { errorToast(e); }
}

async function onDelete(sel) {
  if (readOnly()) return;
  if (sel.type === "node") {
    const { kind, id } = parse(sel.key);
    if (kind === "mix") return deleteMix(data.mixes.find((m) => m.id === id));
    return toast("Songs stay in your library. To unhook one, select its wire and press Delete.", { type: "info" });
  }
  const [kind, a, b] = sel.key.split(":");
  if (kind === "sample") {
    await del(`/api/samples/${a}`).catch(errorToast);
    return loadBoard();
  }
  const m = data.mixes.find((x) => x.id === Number(a));
  const side = kind === "in" ? m.ins : m.outs;
  const left = side.map((s) => s.track_id).filter((t) => t && t !== Number(b));
  if (!left.length) {
    const ok = await confirmDialog("Delete this mix?", "That's the only song on that side of the note, so removing it removes the whole mix.", { ok: "Delete mix", danger: true });
    if (ok) deleteMix(m);
    return;
  }
  await editSides(m, kind === "in" ? left : null, kind === "out" ? left : null);
}

function onOpen(node) {
  if (node.kind === "song" && node.song.id !== center) focus(node.song.id);
  if (node.kind === "mix") node.body.startEditing?.();
}

function onSelect(sel) {
  const bar = document.getElementById("cut-toolbar");
  if (!sel) { bar.replaceChildren(); if (sideSong !== center) renderSide(center); return; }
  if (sel.type === "node") {
    const { kind, id } = parse(sel.key);
    if (kind === "song") {
      renderSide(id);
      fill(bar, id === center ? null : [
        h("span", { class: "muted small" }, board.nodes.get(sel.key).data.song.title),
        h("button", { class: "btn small primary", onclick: () => focus(id) }, "Open its board")]);
    } else {
      const m = data.mixes.find((x) => x.id === id);
      fill(bar,
        h("button", { class: "btn small", onclick: () => board.nodes.get(sel.key)?.data.body.startEditing?.() }, "Edit note"),
        h("button", { class: "btn small danger", onclick: () => deleteMix(m) }, "Delete mix"));
    }
  } else {
    const kind = sel.key.split(":")[0];
    fill(bar, h("button", { class: "btn small danger", onclick: () => onDelete(sel) }, kind === "sample" ? "Remove sample link" : "Unhook from mix"));
  }
}

const queueLayout = (() => {
  const pending = new Map();
  const flush = debounce(async () => {
    const positions = [...pending.values()];
    pending.clear();
    try { await put(`/api/cutting/board/${center}/layout`, { positions }); } catch (e) { errorToast(e); }
  }, 400);
  return (node, x, y) => {
    const { kind, id } = parse(node.key);
    pending.set(node.key, { kind, id, x, y });
    flush();
  };
})();

async function resetLayout() {
  try { await del(`/api/cutting/board/${center}/layout`); loadBoard({ fit: true }); } catch (e) { errorToast(e); }
}

// ------------------------------------------------ side panel: the song card (same one as Crates)

async function renderSide(trackId) {
  const panel = document.getElementById("cut-side");
  sideSong = trackId;
  let t = data?.center;
  if (trackId !== center) {
    try { t = (await get(`/api/cutting/board/${trackId}?depth=1`)).center; } catch (e) { return errorToast(e); }
  }
  if (!t || sideSong !== trackId) return;
  // Don't wipe the card while you're typing in it.
  if (panel.dataset.id === String(t.id) && panel.contains(document.activeElement) && document.activeElement.matches("input, textarea")) return;
  panel.dataset.id = t.id;
  fill(panel, songCard(t, {
    extra: trackId === center ? null : h("button", { class: "btn small", style: { marginTop: "6px" }, onclick: () => focus(trackId) }, "Open its board"),
    openSong: focus, openTitle: "Open its board",
    onSaved: () => loadBoard(),
    onSamplesChange: async () => {
      await loadBoard();
      loadList(root().querySelector(".cutting-list input").value);
    },
  }));
}
