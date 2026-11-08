// Dialogs that bring songs in: Spotify import, manual add, folder import,
// plus Auto Relocate for missing files.

import { get, native, post } from "./api.js";
import { emit, flatCrates, findCrate, on, store } from "./store.js";
import { errorToast, fill, h, modal, toast } from "./ui.js";

function folderSelect(defaultId = null) {
  const sel = h("select", {}, h("option", { value: "" }, "Top level"),
    flatCrates().filter((c) => c.kind === "folder").map((c) => h("option", { value: c.id, selected: c.id === defaultId }, c.path)));
  return sel;
}

function currentFolderId() {
  if (!store.view.startsWith("crate:")) return null;
  const c = findCrate(Number(store.view.split(":")[1]));
  if (!c) return null;
  return c.kind === "folder" ? c.id : c.parent_id;
}

function skippedList(skipped) {
  if (!skipped?.length) return null;
  return h("details", {},
    h("summary", { class: "muted" }, `${skipped.length} skipped — see why`),
    h("div", { class: "skipped" }, skipped.map((s) => h("div", {}, `${s.title || s.file} — ${s.reason}`))));
}

// ------------------------------------------------ Spotify

let spotifyModal = null;

export function openSpotifyImport() {
  const sp = store.status?.spotify || {};
  const body = h("div", {});
  spotifyModal = modal({ title: "Import from Spotify", body, wide: true, buttons: [{ label: "Close" }], onClose: () => { spotifyModal = null; } });
  if (!sp.client_id_set) {
    body.append(
      h("p", {}, "First connect your own Spotify developer app (takes about 2 minutes, once). DJ Crate uses it to read your playlists — nothing is posted anywhere."),
      h("button", { class: "btn primary", onclick: () => { spotifyModal.close(); emit("show-tab", "settings"); } }, "Set up Spotify in Settings →"));
    return;
  }
  if (!sp.connected) return renderLogin(body);
  body.dataset.picker = "1";
  renderPicker(body);
}

function renderLogin(body) {
  const sp = store.status?.spotify || {};
  const waiting = sp.login?.state === "waiting";
  const msg = sp.login?.message;
  fill(body,
    h("p", {}, "Log in to Spotify in your browser. When it says “Connected”, come back here."),
    msg && sp.login.state !== "waiting" ? h("p", { style: { color: sp.login.state === "ok" ? "var(--green)" : "var(--red)" } }, msg) : null,
    waiting
      ? h("div", {},
        h("p", {}, h("span", { class: "spinner" }), " Waiting for you to log in in the browser…"),
        h("p", { class: "muted small" },
          "If Spotify shows “INVALID_CLIENT: Invalid redirect URI”, add this exact Redirect URI in your Spotify app's settings: ",
          h("code", {}, `http://127.0.0.1:${location.port}/callback`)),
        h("button", { class: "btn", onclick: async () => { await post("/api/spotify/cancel_login"); emit("poll"); } }, "Cancel"))
      : h("button", {
        class: "btn spotify",
        onclick: async () => {
          try { await post("/api/spotify/login"); emit("poll"); } catch (e) { errorToast(e); }
        },
      }, "Log in to Spotify"),
  );
}

async function renderPicker(body) {
  body.replaceChildren(h("p", {}, h("span", { class: "spinner" }), " Loading your playlists…"));
  let data;
  try {
    data = await get("/api/spotify/playlists");
  } catch (e) {
    if (["revoked", "not_logged_in"].includes(e.code)) { emit("poll"); return renderLogin(body); }
    body.replaceChildren(h("p", { style: { color: "var(--red)" } }, e.message),
      h("button", { class: "btn", onclick: () => renderPicker(body) }, "Retry"));
    return;
  }
  let chosen = null;
  const url = h("input", { placeholder: "…or paste a playlist link (Share → Copy link to playlist)", style: { width: "100%" } });
  const folder = folderSelect(currentFolderId());
  const progress = h("div", { class: "muted small", style: { minHeight: "18px" } });
  const imported = new Set(flatCrates().map((c) => c.spotify_playlist_id).filter(Boolean));
  const hideBox = h("input", { type: "checkbox", checked: localStorage.getItem("djc.hideImported") !== "0" });
  const allHidden = h("p", { class: "muted", hidden: true }, "All your playlists are already imported.");
  const items = [{ id: "liked", name: "Liked Songs", count: null, readable: true, owner: data.me?.name }, ...data.playlists];
  const list = h("div", { class: "list" }, items.map((p) => {
    const el = h("div", {
      class: `item${p.readable ? "" : " disabled"}${imported.has(p.id) ? " imported" : ""}`,
      title: p.readable ? "" : "Spotify only shares the songs of playlists you own or collaborate on. Copy its songs into your own playlist to import them.",
      onclick: () => {
        if (!p.readable) return;
        list.querySelectorAll(".item").forEach((i) => i.classList.remove("selected"));
        el.classList.add("selected");
        chosen = p.id;
        url.value = "";
      },
    },
      p.image ? h("img", { src: p.image, alt: "" }) : h("div", { class: "cover placeholder" }, p.id === "liked" ? "♥" : "♪"),
      h("div", { class: "grow" },
        h("div", { class: "t-title" }, p.name),
        h("div", { class: "t-sub" }, [p.count != null ? `${p.count} songs` : null, p.owner ? `by ${p.owner}` : null, imported.has(p.id) ? "already imported" : null, p.readable ? null : "not yours — can't import"].filter(Boolean).join(" · "))));
    return el;
  }));
  const applyHide = () => {
    const hide = hideBox.checked;
    localStorage.setItem("djc.hideImported", hide ? "1" : "0");
    list.querySelectorAll(".item").forEach((el, i) => {
      const gone = hide && imported.has(items[i].id);
      el.hidden = gone;
      if (gone && el.classList.contains("selected")) { el.classList.remove("selected"); chosen = null; }
    });
    allHidden.hidden = !(hide && items.every((p) => imported.has(p.id)));
  };
  hideBox.addEventListener("change", applyHide);
  url.addEventListener("input", () => { if (url.value) { chosen = null; list.querySelectorAll(".item").forEach((i) => i.classList.remove("selected")); } });
  body.replaceChildren(
    h("p", { class: "muted" }, `Logged in as ${data.me?.name || "you"}. Pick a playlist:`),
    h("label", { class: "row gap small", style: { margin: "4px 0" } }, hideBox, "Hide already imported playlists"),
    list, allHidden, url,
    h("div", { class: "field", style: { marginTop: "10px" } }, h("label", {}, "Put the new crate in"), folder),
    progress);
  applyHide();
  spotifyModal.setButtons([
    { label: "Cancel" },
    {
      label: "Import", kind: "primary",
      fn: async (btn) => {
        const ref = url.value.trim() || chosen;
        if (!ref) { toast("Pick a playlist or paste a link.", { type: "warn" }); return false; }
        btn.textContent = "Importing…";
        const timer = setInterval(() => { progress.textContent = store.status?.spotify?.progress || ""; }, 500);
        try {
          const r = await post("/api/spotify/import", { ref, parent_id: folder.value ? Number(folder.value) : null });
          showImportResult(r);
          emit("refresh-sidebar");
          emit("select-view", `crate:${r.crate_id}`);
        } finally {
          clearInterval(timer);
          btn.textContent = "Import";
        }
      },
    },
  ]);
}

function showImportResult(r) {
  if (r.already_imported) {
    toast(`Already imported — re-synced “${r.name}”: ${r.added} new, ${r.removed} removed on Spotify.`, { type: "success", timeout: 7000 });
    return;
  }
  modal({
    title: `Imported “${r.name}”`,
    body: h("div", {},
      h("p", {}, `${r.imported} songs are in the new crate.`),
      r.already_known ? h("p", { class: "muted" }, `${r.already_known} were already in your library — they share their links, notes and checkmarks.`) : null,
      h("p", {}, "Next: paste a YouTube link for each song (or use Find ▾), then Download All."),
      skippedList(r.skipped)),
    buttons: [{ label: "Got it", kind: "primary" }],
  });
}

export async function resyncCrate(crateId) {
  const c = findCrate(crateId);
  const close = toast(`Re-syncing “${c?.name}” from Spotify…`, { timeout: 0 });
  try {
    const r = await post(`/api/crates/${crateId}/resync`);
    close();
    const parts = [`${r.added} new`];
    if (r.removed) parts.push(`${r.removed} removed on Spotify (kept here, dimmed)`);
    if (r.restored) parts.push(`${r.restored} back on Spotify`);
    toast(`Re-synced “${r.name}”: ${parts.join(", ")}.${r.skipped.length ? ` ${r.skipped.length} skipped.` : ""}`, { type: "success", timeout: 7000 });
    emit("refresh-sidebar");
    emit("refresh-tracks");
  } catch (e) {
    close();
    errorToast(e);
    if (["revoked", "not_logged_in"].includes(e.code)) emit("poll");
  }
}

let resyncing = false;

export async function resyncAll() {
  if (resyncing) return;
  const sp = store.status?.spotify || {};
  if (!sp.client_id_set || !sp.connected) return openSpotifyImport();
  const crates = flatCrates().filter((c) => c.spotify_playlist_id);
  if (!crates.length) {
    toast("No Spotify crates yet — import a playlist first.", { type: "info" });
    return;
  }
  resyncing = true;
  const btn = document.getElementById("btn-resync-all");
  btn.disabled = true;
  let close = () => {};
  const total = { added: 0, removed: 0, restored: 0, done: 0 };
  const failed = [];
  try {
    for (const [i, c] of crates.entries()) {
      close();
      close = toast(`Re-syncing ${i + 1} of ${crates.length}: “${c.name}”…`, { timeout: 0 });
      try {
        const r = await post(`/api/crates/${c.id}/resync`);
        total.added += r.added; total.removed += r.removed; total.restored += r.restored; total.done++;
      } catch (e) {
        if (["revoked", "not_logged_in"].includes(e.code)) {
          close();
          errorToast(e);
          emit("poll");
          return;
        }
        failed.push(c.name);
      }
    }
    close();
    const parts = [`${total.added} new`];
    if (total.removed) parts.push(`${total.removed} removed on Spotify`);
    if (total.restored) parts.push(`${total.restored} back on Spotify`);
    let msg = `Re-synced ${total.done} crate${total.done === 1 ? "" : "s"}: ${parts.join(", ")}.`;
    if (failed.length) msg += ` ${failed.length} couldn't re-sync: ${failed.map((n) => `“${n}”`).join(", ")}.`;
    toast(msg, { type: failed.length ? "warn" : "success", timeout: 8000 });
  } finally {
    close();
    resyncing = false;
    btn.disabled = false;
    emit("refresh-sidebar");
    emit("refresh-tracks");
  }
}

// ------------------------------------------------ manual add

export function openManualAdd() {
  const title = h("input", { placeholder: "Song title" });
  const artists = h("input", { placeholder: "All artists, comma separated" });
  const album = h("input", { placeholder: "Optional" });
  const link = h("input", { placeholder: "YouTube / SoundCloud / Bandcamp link (optional)" });
  const crateId = store.view.startsWith("crate:") && findCrate(Number(store.view.split(":")[1]))?.kind !== "folder"
    ? Number(store.view.split(":")[1]) : null;
  const f = (label, el) => h("div", { class: "field" }, h("label", {}, label), el);
  modal({
    title: "Add a track",
    body: h("div", {},
      h("p", { class: "muted" }, "For bootlegs, edits and anything that isn't on Spotify."),
      f("Title", title), f("Artists", artists), f("Album", album), f("Link", link),
      crateId ? h("p", { class: "muted small" }, `It'll also be added to “${findCrate(crateId).name}”.`) : null),
    buttons: [
      { label: "Cancel" },
      {
        label: "Add", kind: "primary",
        fn: async () => {
          const r = await post("/api/tracks", { title: title.value, artists: artists.value, album: album.value, source_url: link.value, crate_id: crateId });
          toast(r.created ? `Added “${r.track.title}”.` : `“${r.track.title}” was already in your library — linked to it instead.`, { type: "success" });
          emit("refresh-sidebar");
          emit("refresh-tracks");
        },
      },
    ],
  });
}

// ------------------------------------------------ folder import

export async function importFolder() {
  const path = await native.pickFolder("Folder of MP3s to import:");
  if (!path) return;
  const close = toast("Reading your MP3s…", { timeout: 0 });
  try {
    const r = await post("/api/library/import_folder", { path, parent_id: currentFolderId() });
    close();
    modal({
      title: "Folder imported",
      body: h("div", {},
        h("p", {}, `${r.imported} new song${r.imported === 1 ? "" : "s"} added${r.already_known ? `, ${r.already_known} already known` : ""}.`),
        h("p", { class: "muted small" }, "Files stay where they are. They're marked as Downloaded."),
        skippedList(r.skipped)),
      buttons: [{ label: "OK", kind: "primary" }],
    });
    emit("refresh-sidebar");
    if (r.crate_id) emit("select-view", `crate:${r.crate_id}`);
  } catch (e) {
    close();
    errorToast(e);
  }
}

// ------------------------------------------------ auto relocate

export async function autoRelocate() {
  const choice = await new Promise((resolve) => {
    let picked = null;
    modal({
      title: "Auto Relocate missing files",
      body: h("p", {}, "DJ Crate looks for files with the same name as your missing songs, then shows you what it found before changing anything."),
      buttons: [
        { label: "Cancel" },
        { label: "Choose another folder…", fn: () => { picked = "pick"; } },
        { label: "Search the library folder", kind: "primary", fn: () => { picked = "library"; } },
      ],
      onClose: () => resolve(picked),
    });
  });
  if (!choice) return;
  let folder = null;
  if (choice === "pick") {
    folder = await native.pickFolder("Folder to search:");
    if (!folder) return;
  }
  try {
    const { matches } = await post("/api/library/auto_relocate/preview", { folder });
    if (!matches.length) {
      toast("No matching files found. Use Relocate… on each song to point at it by hand.", { type: "warn", timeout: 7000 });
      return;
    }
    const checks = matches.map(() => h("input", { type: "checkbox", class: "sel", checked: true }));
    modal({
      title: `Found ${matches.length} file${matches.length === 1 ? "" : "s"}`,
      wide: true,
      body: h("div", { class: "list" }, matches.map((m, i) => h("label", { class: "item" }, checks[i],
        h("div", { class: "grow" },
          h("div", { class: "t-title" }, `${m.title} — ${m.artists}`),
          h("div", { class: "t-sub" }, `was ${m.old_path}`),
          h("div", { class: "t-sub" }, `now ${m.new_path}`))))),
      buttons: [
        { label: "Cancel" },
        {
          label: "Relink selected", kind: "primary",
          fn: async () => {
            const chosen = matches.filter((_, i) => checks[i].checked);
            const r = await post("/api/library/auto_relocate/apply", { matches: chosen });
            toast(`Relinked ${r.relinked} file${r.relinked === 1 ? "" : "s"}.`, { type: "success" });
            emit("refresh-sidebar");
            emit("refresh-tracks");
          },
        },
      ],
    });
  } catch (e) { errorToast(e); }
}

export function initImporters() {
  on("import-spotify", openSpotifyImport);
  on("add-manual", openManualAdd);
  on("import-folder", importFolder);
  on("resync", resyncCrate);
  on("resync-all", resyncAll);
  on("status", (s) => {
    // Live-update the login step of the Spotify dialog.
    if (!spotifyModal) return;
    const body = spotifyModal.card.querySelector(".modal-body > div");
    if (!body) return;
    const sp = s.spotify;
    if (sp.connected && !body.dataset.picker) { body.dataset.picker = "1"; renderPicker(body); }
    else if (!sp.connected && sp.client_id_set) {
      const key = `${sp.login?.state}|${sp.login?.message}`;
      if (body.dataset.loginKey !== key) { body.dataset.loginKey = key; renderLogin(body); }
    }
  });
}
