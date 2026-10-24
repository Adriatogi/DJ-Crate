// Crates tab controller: which view is selected, toolbar, loading data.

import { get, post } from "./api.js";
import { autoRelocate, initImporters } from "./importers.js";
import { closeDetail, initDetail } from "./detail.js";
import { crateMenu, deleteCrate, initSidebar, renderSidebar } from "./sidebar.js";
import { emit, findCrate, on, store } from "./store.js";
import { initTracks, renderTracks, resetTable } from "./tracks.js";
import { debounce, errorToast, menu, toast } from "./ui.js";

let loadSeq = 0;

const SORTS = {
  default: "Default order", title: "Title", artists: "Artist", album: "Album", duration: "Length",
  added: "Date added", status: "Status", edited: "Edited", ready: "Ready", broken: "Broken",
};
const STATUS_ORDER = { none: 1, queued: 2, downloading: 3, error: 4, missing: 5, done: 6 };
store.sort = JSON.parse(localStorage.getItem("djc.sort") || '{"key":"default","dir":1}');

function sortValue(t, key) {
  switch (key) {
    case "duration": return t.duration_ms ?? t.file_duration_ms ?? 0;
    case "added": return t.created_at || "";
    case "status": return t.download_status === "none" && !t.source_url ? 0 : STATUS_ORDER[t.download_status] ?? 9;
    case "edited": case "ready": case "broken": return t[key] ? 1 : 0;
    default: return (t[key] || "").toLowerCase();
  }
}

function applySort() {
  const { key, dir } = store.sort;
  store.tracks.sort((a, b) => {
    if (key === "default") return a._order - b._order;
    const va = sortValue(a, key), vb = sortValue(b, key);
    const c = typeof va === "number" ? va - vb : va.localeCompare(vb, undefined, { numeric: true });
    return c * dir || a._order - b._order;
  });
  document.querySelectorAll("#tracks th.sortable").forEach((th) => {
    const on = th.dataset.sort === key;
    th.classList.toggle("sorted", on);
    th.dataset.label ||= th.textContent;
    th.textContent = th.dataset.label + (on ? (dir > 0 ? " ▲" : " ▼") : "");
  });
  document.getElementById("btn-sort").textContent = key === "default" ? "Sort ▾" : `Sort: ${SORTS[key]} ${dir > 0 ? "▲" : "▼"}`;
}

function setSort(key, dir) {
  store.sort = { key, dir };
  localStorage.setItem("djc.sort", JSON.stringify(store.sort));
  applySort();
  renderTracks();
}

function headerSort(key) {
  // click: ascending → descending → back to default order
  const { key: k, dir } = store.sort;
  if (k !== key) setSort(key, 1);
  else if (dir > 0) setSort(key, -1);
  else setSort("default", 1);
}

function sortMenu(anchor) {
  const { key, dir } = store.sort;
  menu(anchor, [
    ...Object.entries(SORTS).map(([k, label]) => ({ label: `${k === key ? "● " : ""}${label}`, fn: () => setSort(k, k === key ? dir : 1) })),
    "-",
    { label: "Ascending ▲", fn: () => setSort(store.sort.key, 1) },
    { label: "Descending ▼", fn: () => setSort(store.sort.key, -1) },
  ], { align: "right" });
}

export async function refreshSidebar() {
  try {
    store.sidebar = await get("/api/sidebar");
    if (store.view.startsWith("crate:") && !findCrate(Number(store.view.split(":")[1]))) {
      return selectView("smart:all");
    }
    renderSidebar();
    renderToolbar();
  } catch (e) {
    if (e.code !== "library_unavailable") console.warn(e);
  }
}

export async function refreshTracks() {
  const seq = ++loadSeq;
  const q = document.getElementById("search").value;
  try {
    const r = await get(`/api/tracks?view=${encodeURIComponent(store.view)}&q=${encodeURIComponent(q)}`);
    if (seq !== loadSeq) return; // a newer request won
    r.tracks.forEach((t, i) => { t._order = i; });
    store.tracks = r.tracks;
    applySort();
    renderTracks();
    renderToolbar();
    emit("tracks-loaded");
  } catch (e) {
    if (e.status === 404 && store.view !== "smart:all") return selectView("smart:all");
    if (e.code !== "library_unavailable") errorToast(e);
  }
}

export function selectView(view) {
  if (view !== store.view) {
    store.view = view;
    localStorage.setItem("djc.view", view);
    resetTable();
    closeDetail();
    store.tracks = [];
  }
  renderSidebar();
  renderToolbar();
  refreshTracks();
}

function viewInfo() {
  const v = store.view;
  if (v.startsWith("crate:")) {
    const c = findCrate(Number(v.split(":")[1]));
    return { name: c?.name || "…", crate: c };
  }
  const key = v.split(":")[1];
  const s = store.sidebar?.smart.find((x) => x.key === key);
  return { name: s?.name || "All Tracks", crate: null };
}

function renderToolbar() {
  const { name, crate } = viewInfo();
  document.getElementById("view-name").textContent = name;
  const ts = store.tracks;
  const done = ts.filter((t) => t.download_status === "done").length;
  const ready = ts.filter((t) => t.ready).length;
  const broken = ts.filter((t) => t.broken).length;
  const meta = [`${ts.length} song${ts.length === 1 ? "" : "s"}`, `${done} downloaded`, `${ready} ready`];
  if (broken) meta.push(`${broken} broken`);
  if (crate?.kind === "spotify" && crate.last_synced) meta.push(`synced ${crate.last_synced.slice(0, 16)} UTC`);
  if (crate?.kind === "spotify" && crate.spotify_playlist_id === "liked") meta.push("Liked Songs");
  document.getElementById("view-meta").textContent = meta.join(" · ");
  document.getElementById("btn-resync").hidden = crate?.kind !== "spotify";
  const toDownload = ts.filter((t) => t.source_url && ["none", "error"].includes(t.download_status)).length;
  const dl = document.getElementById("btn-download-all");
  dl.textContent = toDownload ? `⬇ Download All (${toDownload})` : "⬇ Download All";
  dl.title = toDownload ? "Download every song here that has a link and isn't downloaded yet" : "Nothing here has a link waiting to download";
}

async function downloadAll() {
  try {
    const r = await post("/api/downloads/all", { view: store.view });
    toast(r.queued ? `Queued ${r.queued} download${r.queued === 1 ? "" : "s"}.` : "Nothing to download here.", { type: r.queued ? "success" : "info" });
    refreshTracks();
  } catch (e) { errorToast(e); }
}

function viewMenu(anchor) {
  const { crate } = viewInfo();
  const items = [
    { label: "Rescan for missing files", fn: rescan },
    { label: "Auto Relocate missing files…", fn: autoRelocate },
    { label: "Show library folder in Finder", fn: () => post("/api/reveal", { target: "library" }) },
  ];
  if (crate) {
    items.push("-", { label: "Crate options…", fn: () => crateMenu(anchor, crate) });
    items.push({ label: crate.kind === "folder" ? "Delete folder…" : "Delete crate…", danger: true, fn: () => deleteCrate(crate) });
  }
  menu(anchor, items, { align: "right" });
}

async function rescan() {
  try {
    const r = await post("/api/library/rescan");
    toast(r.missing
      ? `${r.missing} file${r.missing === 1 ? " is" : "s are"} missing. See “Missing Files” — use Relocate or Auto Relocate.`
      : "All files found. ✓", { type: r.missing ? "warn" : "success", timeout: 7000 });
    refreshSidebar();
    refreshTracks();
  } catch (e) { errorToast(e); }
}

export function initCrates() {
  initSidebar();
  initTracks();
  initDetail();
  initImporters();
  document.getElementById("btn-import-spotify").onclick = () => emit("import-spotify");
  document.getElementById("btn-add-manual").onclick = () => emit("add-manual");
  document.getElementById("btn-import-folder").onclick = () => emit("import-folder");
  document.getElementById("btn-download-all").onclick = downloadAll;
  document.getElementById("btn-resync").onclick = () => emit("resync", Number(store.view.split(":")[1]));
  const more = document.getElementById("btn-view-menu");
  more.onclick = () => viewMenu(more);
  const sortBtn = document.getElementById("btn-sort");
  sortBtn.onclick = () => sortMenu(sortBtn);
  document.querySelectorAll("#tracks th.sortable").forEach((th) => th.addEventListener("click", () => headerSort(th.dataset.sort)));
  applySort();
  const search = document.getElementById("search");
  search.addEventListener("input", debounce(refreshTracks, 200));
  on("select-view", selectView);
  on("rescan", rescan);
  on("refresh-sidebar", debounce(refreshSidebar, 150));
  on("refresh-tracks", debounce(refreshTracks, 100));
}
