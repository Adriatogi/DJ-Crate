// Left sidebar: smart crates, folder/crate tree, drag to nest, drop songs onto crates.

import { del, patch, post } from "./api.js";
import { emit, findCrate, store } from "./store.js";
import { confirmDialog, errorToast, h, menu, promptDialog, toast } from "./ui.js";

const SMART_ICONS = {
  all: "◉", needs_link: "🔗", to_download: "⬇", downloaded: "✓", ready: "🟢", broken: "🔴", check_version: "⚠", missing: "!",
};

const collapsed = new Set(JSON.parse(localStorage.getItem("djc.collapsed") || "[]"));
export const drag = { type: null, ids: null, id: null };

function saveCollapsed() {
  localStorage.setItem("djc.collapsed", JSON.stringify([...collapsed]));
}

export function renderSidebar() {
  const data = store.sidebar;
  if (!data) return;
  const smart = document.getElementById("smart-list");
  smart.replaceChildren(...data.smart.map((s) => h("div", {
    class: `nav-item smart-${s.key}${store.view === `smart:${s.key}` ? " active" : ""}`,
    onclick: () => emit("select-view", `smart:${s.key}`),
  },
    h("span", { class: "icon" }, SMART_ICONS[s.key] || "•"),
    h("span", { class: "name" }, s.name),
    h("span", { class: "count" }, ["check_version", "missing", "broken"].includes(s.key) && !s.count ? "" : s.count),
  )));
  document.getElementById("tree").replaceChildren(...data.tree.map(renderNode));
}

function statsLine(c) {
  const s = c.stats;
  if (!s.total) return "empty";
  return `${s.downloaded}/${s.total} ↓ · ${s.ready} ready${s.broken ? ` · ${s.broken} broken` : ""}`;
}

function renderNode(c) {
  const isFolder = c.kind === "folder";
  const isCollapsed = collapsed.has(c.id);
  const icon = isFolder ? (isCollapsed ? "📁" : "📂") : c.kind === "spotify" ? h("span", { class: "spotify-dot", title: "Imported from Spotify" }) : "♪";
  const item = h("div", {
    class: `nav-item${store.view === `crate:${c.id}` ? " active" : ""}`,
    draggable: "true",
    dataset: { id: c.id, kind: c.kind },
    onclick: () => emit("select-view", `crate:${c.id}`),
  },
    h("span", {
      class: "caret",
      onclick: (e) => {
        if (!isFolder) return;
        e.stopPropagation();
        isCollapsed ? collapsed.delete(c.id) : collapsed.add(c.id);
        saveCollapsed();
        renderSidebar();
      },
    }, isFolder ? (isCollapsed ? "▶" : "▼") : ""),
    h("span", { class: "icon" }, icon),
    h("div", { class: "name" },
      h("div", { class: "name" }, c.name),
      h("div", { class: "stats" }, statsLine(c))),
    h("button", {
      class: "icon-btn small more", title: "More",
      onclick: (e) => { e.stopPropagation(); crateMenu(e.currentTarget, c); },
    }, "⋯"),
  );
  wireDnD(item, c);
  const wrap = h("div", {}, item);
  if (isFolder && !isCollapsed && c.children.length) {
    wrap.append(h("div", { class: "tree-children" }, c.children.map(renderNode)));
  }
  return wrap;
}

export function crateMenu(anchor, c) {
  const items = [{ label: "Rename…", fn: () => renameCrate(c) }];
  if (c.kind === "folder") {
    items.push({ label: "New crate inside…", fn: () => newCrate("crate", c.id) });
    items.push({ label: "New folder inside…", fn: () => newCrate("folder", c.id) });
  }
  if (c.kind === "spotify") items.push({ label: "↻ Re-sync from Spotify", fn: () => emit("resync", c.id) });
  items.push("-", { label: c.kind === "folder" ? "Delete folder…" : "Delete crate…", danger: true, fn: () => deleteCrate(c) });
  menu(anchor, items);
}

export async function newCrate(kind = "crate", parentId = null) {
  const name = await promptDialog(kind === "folder" ? "New folder" : "New crate", {
    label: "Name", ok: "Create", placeholder: kind === "folder" ? "e.g. Weddings" : "e.g. Friday warmup",
  });
  if (!name) return;
  try {
    const { id } = await post("/api/crates", { name, kind, parent_id: parentId });
    if (parentId) collapsed.delete(parentId);
    saveCollapsed();
    emit("refresh-sidebar");
    if (kind !== "folder") emit("select-view", `crate:${id}`);
  } catch (e) { errorToast(e); }
}

async function renameCrate(c) {
  const name = await promptDialog("Rename", { value: c.name, ok: "Rename" });
  if (!name || name === c.name) return;
  try {
    await patch(`/api/crates/${c.id}`, { name });
    emit("refresh-sidebar");
  } catch (e) { errorToast(e); }
}

export async function deleteCrate(c) {
  const what = c.kind === "folder" ? "folder and every crate inside it" : "crate";
  const ok = await confirmDialog(`Delete “${c.name}”?`,
    `This deletes the ${what}. Your songs and files stay in All Tracks — nothing is removed from disk.`,
    { ok: "Delete", danger: true });
  if (!ok) return;
  try {
    await del(`/api/crates/${c.id}`);
    if (store.view === `crate:${c.id}`) emit("select-view", "smart:all");
    emit("refresh-sidebar");
    toast(`Deleted “${c.name}”.`);
  } catch (e) { errorToast(e); }
}

// ------------------------------------------------ drag & drop

function clearDropMarks() {
  document.querySelectorAll(".drop-target, .drop-before").forEach((el) => el.classList.remove("drop-target", "drop-before"));
}

function wireDnD(item, c) {
  item.addEventListener("dragstart", (e) => {
    drag.type = "crate";
    drag.id = c.id;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", c.name);
    document.body.classList.add("dragging-crate");
    e.stopPropagation();
  });
  item.addEventListener("dragend", () => {
    drag.type = null;
    document.body.classList.remove("dragging-crate");
    clearDropMarks();
  });
  item.addEventListener("dragover", (e) => {
    if (drag.type === "tracks" && c.kind !== "folder") {
      e.preventDefault();
      clearDropMarks();
      item.classList.add("drop-target");
    } else if (drag.type === "crate" && drag.id !== c.id) {
      e.preventDefault();
      clearDropMarks();
      item.classList.add(c.kind === "folder" ? "drop-target" : "drop-before");
    }
  });
  item.addEventListener("dragleave", () => item.classList.remove("drop-target", "drop-before"));
  item.addEventListener("drop", async (e) => {
    e.preventDefault();
    e.stopPropagation();
    clearDropMarks();
    try {
      if (drag.type === "tracks" && c.kind !== "folder") {
        const r = await post(`/api/crates/${c.id}/tracks`, { ids: drag.ids });
        toast(r.added ? `Added ${r.added} song${r.added === 1 ? "" : "s"} to “${c.name}”${r.already ? ` (${r.already} already there)` : ""}.`
          : `Already in “${c.name}”.`, { type: r.added ? "success" : "info" });
        emit("refresh-sidebar");
      } else if (drag.type === "crate" && drag.id !== c.id) {
        if (c.kind === "folder") {
          await patch(`/api/crates/${drag.id}`, { move: true, parent_id: c.id, index: null });
          collapsed.delete(c.id);
          saveCollapsed();
        } else {
          const siblings = siblingsOf(c);
          const index = siblings.filter((s) => s.id !== drag.id).findIndex((s) => s.id === c.id);
          await patch(`/api/crates/${drag.id}`, { move: true, parent_id: c.parent_id, index });
        }
        emit("refresh-sidebar");
      }
    } catch (err) { errorToast(err); }
    drag.type = null;
  });
}

function siblingsOf(c) {
  if (c.parent_id == null) return store.sidebar.tree;
  return findCrate(c.parent_id)?.children || [];
}

export function initSidebar() {
  document.getElementById("btn-new-crate").onclick = () => newCrate("crate");
  document.getElementById("btn-new-folder").onclick = () => newCrate("folder");
  const root = document.getElementById("tree-root-drop");
  root.addEventListener("dragover", (e) => { if (drag.type === "crate") { e.preventDefault(); root.classList.add("drop-target"); } });
  root.addEventListener("dragleave", () => root.classList.remove("drop-target"));
  root.addEventListener("drop", async (e) => {
    e.preventDefault();
    root.classList.remove("drop-target");
    if (drag.type !== "crate") return;
    try {
      await patch(`/api/crates/${drag.id}`, { move: true, parent_id: null, index: null });
      emit("refresh-sidebar");
    } catch (err) { errorToast(err); }
  });
}
