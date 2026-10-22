// Track table: rows, link pasting, Find YouTube link, inline player, flags,
// multi-select (checkboxes only — no Shift-click ranges) and bulk actions.

import { get, native, patch, post } from "./api.js";
import { drag } from "./sidebar.js";
import { LINK_RE, emit, flatCrates, findCrate, on, store } from "./store.js";
import {
  closePopover, confirmDialog, errorToast, fill, fmtDiff, fmtMs, h, isPopoverOpenFor, menu, popover, toast,
} from "./ui.js";

const rows = new Map(); // track id -> { tr, sig }
export const selected = new Set();
let player = null;      // { trackId, row, playBtn }
const audio = () => document.getElementById("audio");

// ------------------------------------------------ rendering

export function renderTracks() {
  const body = document.getElementById("track-body");
  const tracks = store.tracks;
  const ids = new Set(tracks.map((t) => t.id));

  for (const [id, entry] of rows) {
    if (!ids.has(id)) { entry.tr.remove(); rows.delete(id); selected.delete(id); }
  }
  if (player && !ids.has(player.trackId)) closePlayer();

  let prev = null;
  for (const t of tracks) {
    const sig = JSON.stringify(t);
    let entry = rows.get(t.id);
    if (!entry) {
      entry = { tr: rowFor(t), sig };
      rows.set(t.id, entry);
    } else if (entry.sig !== sig && entry.tr.contains(document.activeElement)) {
      if (!entry.pending) {
        // Don't clobber typing; redraw as soon as focus leaves the row.
        entry.pending = true;
        entry.tr.addEventListener("focusout", () => setTimeout(() => {
          entry.pending = false;
          if (!entry.tr.contains(document.activeElement)) renderTracks();
        }, 0), { once: true });
      }
    } else if (entry.sig !== sig) {
      const tr = rowFor(t);
      entry.tr.replaceWith(tr);
      if (player?.trackId === t.id) {
        player.playBtn = tr.querySelector(".play-btn");
        player.playBtn?.classList.add("active");
        player.trackRow = tr;
      }
      entry.tr = tr;
      entry.sig = sig;
    }
    const expectedPrev = prev ? (player?.trackId === prev ? player.row : rows.get(prev).tr) : null;
    if (entry.tr.previousElementSibling !== expectedPrev || entry.tr.parentNode !== body) {
      if (expectedPrev) expectedPrev.after(entry.tr); else body.prepend(entry.tr);
    }
    if (player?.trackId === t.id && entry.tr.nextElementSibling !== player.row) entry.tr.after(player.row);
    prev = t.id;
  }
  syncSelection();
  renderEmpty();
}

function renderEmpty() {
  const el = document.getElementById("empty-state");
  if (store.tracks.length) { el.hidden = true; return; }
  el.hidden = false;
  const q = document.getElementById("search").value.trim();
  const v = store.view;
  let content;
  if (q) content = [h("h3", {}, `No matches for “${q}”`)];
  else if (v === "smart:all") {
    content = [
      h("h3", {}, "Your library is empty"),
      h("p", {}, "Import a Spotify playlist to fill a crate with song info, then paste a YouTube link for each song."),
      h("div", { class: "row gap", style: { justifyContent: "center" } },
        h("button", { class: "btn primary", onclick: () => emit("import-spotify") }, "+ Import Spotify playlist"),
        h("button", { class: "btn", onclick: () => emit("add-manual") }, "+ Add a track manually"),
        h("button", { class: "btn", onclick: () => emit("import-folder") }, "Import a folder of MP3s")),
    ];
  } else if (v.startsWith("crate:")) {
    const c = findCrate(Number(v.split(":")[1]));
    content = c?.kind === "folder"
      ? [h("h3", {}, "This folder has no songs yet"), h("p", {}, "Make crates inside it, or drag crates in from the sidebar.")]
      : [h("h3", {}, "This crate is empty"), h("p", {}, "Go to All Tracks, tick some songs and use “Add to crate”, or drag rows onto this crate.")];
  } else {
    const msgs = {
      "smart:needs_link": "Every song has a link. 🎉",
      "smart:ready": "Nothing waiting to download.",
      "smart:downloaded": "No downloads yet.",
      "smart:cleaned": "Nothing marked Cleaned yet.",
      "smart:verified": "Nothing marked Verified yet.",
      "smart:check_version": "No length mismatches — every download matches its Spotify length (or is marked Edited).",
      "smart:missing": "No missing files.",
    };
    content = [h("h3", {}, msgs[v] || "Nothing here")];
  }
  el.replaceChildren(...content);
}

function coverEl(t) {
  return t.cover_url
    ? h("img", { class: "cover", src: t.cover_url, loading: "lazy", alt: "" })
    : h("div", { class: "cover placeholder" }, "♪");
}

function rowFor(t) {
  const tr = h("tr", { class: `track${t.removed_from_source ? " removed" : ""}`, dataset: { id: t.id } });
  const sel = h("input", { type: "checkbox", class: "sel", checked: selected.has(t.id), title: "Select" });
  sel.addEventListener("change", () => {
    sel.checked ? selected.add(t.id) : selected.delete(t.id);
    syncSelection();
  });
  sel.addEventListener("click", (e) => { if (e.shiftKey) { /* no range select, on purpose */ } e.stopPropagation(); });

  tr.append(
    h("td", { class: "c-sel" }, sel),
    h("td", { class: "c-cover" }, coverEl(t)),
    h("td", { title: t.title },
      h("div", { class: "t-title" }, t.title),
      t.removed_from_source ? h("span", { class: "chip removed", title: "Removed from the Spotify playlist. Kept here with your notes and file." }, "Removed on Spotify") : null),
    h("td", { title: t.artists }, t.artists),
    h("td", { class: "c-album muted", title: t.album }, t.album),
    h("td", { class: "c-time muted" }, fmtMs(t.duration_ms ?? t.file_duration_ms)),
    h("td", { class: "c-link" }, linkCell(t)),
    h("td", { class: "c-play" }, playButton(t, tr)),
    h("td", { class: "c-flag" }, flag(t, "edited")),
    h("td", { class: "c-flag" }, flag(t, "cleaned")),
    h("td", { class: "c-flag" }, flag(t, "verified")),
    h("td", { class: "c-status" }, statusCell(t)),
    h("td", { class: "c-action" }, actionButton(t)),
  );

  tr.addEventListener("mousedown", (e) => {
    tr.draggable = !e.target.closest("input, textarea, button, select, a");
  });
  tr.addEventListener("dragstart", (e) => {
    drag.type = "tracks";
    drag.ids = selected.has(t.id) ? [...selected] : [t.id];
    e.dataTransfer.effectAllowed = "copy";
    e.dataTransfer.setData("text/plain", drag.ids.length > 1 ? `${drag.ids.length} songs` : t.title);
  });
  tr.addEventListener("dragend", () => { drag.type = null; });
  tr.addEventListener("click", (e) => {
    if (e.target.closest("input, button, a, select, textarea, label")) return;
    emit("open-detail", t.id);
  });
  if (store.detailId === t.id) tr.classList.add("open");
  return tr;
}

function linkCell(t) {
  let saved = t.source_url || "";
  const input = h("input", { value: saved, placeholder: "Paste YouTube link…", spellcheck: false });
  const save = async () => {
    const v = input.value.trim();
    if (v === saved) { input.classList.remove("invalid"); return; }
    if (v && !LINK_RE.test(v)) {
      input.classList.add("invalid");
      input.title = "Not a YouTube/SoundCloud/Bandcamp link — not saved";
      toast("That's not a YouTube/SoundCloud/Bandcamp link, so it wasn't saved.", { type: "error" });
      return;
    }
    input.classList.remove("invalid");
    input.title = "";
    try {
      const r = await patch(`/api/tracks/${t.id}`, { source_url: v });
      saved = v;
      r.warnings.forEach((w) => toast(w, { type: "warn", timeout: 7000 }));
      updateTrack(r.track);
      emit("refresh-sidebar");
    } catch (e) { errorToast(e); }
  };
  input.addEventListener("change", save);
  input.addEventListener("paste", () => setTimeout(save, 0));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") input.blur();
    if (e.key === "Escape") { input.value = saved; input.classList.remove("invalid"); input.blur(); }
  });
  const find = h("button", { class: "find-btn", title: "Find YouTube link" }, "Find ▾");
  find.addEventListener("click", (e) => { e.stopPropagation(); openFind(t, find, input); });
  return h("div", { class: "link-cell" }, input, find);
}

function flag(t, field) {
  const titles = {
    edited: "Edited — you changed the audio on purpose (skips the length check)",
    cleaned: "Cleaned — BPM and grid line up in Rekordbox",
    verified: "Verified — learned and gig-ready",
  };
  const cb = h("input", { type: "checkbox", class: `flag ${field}`, checked: !!t[field], title: titles[field] });
  cb.addEventListener("change", async () => {
    const value = cb.checked;
    try {
      const r = await patch(`/api/tracks/${t.id}`, { [field]: value });
      updateTrack(r.track);
      emit("refresh-sidebar");
    } catch (e) {
      cb.checked = !value; // revert the optimistic change
      errorToast(e);
    }
  });
  return cb;
}

function statusCell(t) {
  const chips = [];
  const s = t.download_status;
  if (t.job_status === "interrupted") {
    chips.push(h("span", { class: "chip warn", title: "Interrupted when DJ Crate closed. Resume it from Activity." }, "Interrupted"));
  } else if (s === "none") {
    chips.push(t.source_url ? h("span", { class: "chip" }, "Ready") : h("span", { class: "chip" }, "Needs link"));
  } else if (s === "queued") {
    const retrying = t.job_error && t.job_status === "queued";
    chips.push(h("span", { class: "chip busy", title: retrying ? t.job_error : "Waiting for a free download slot" }, retrying ? "Retrying soon" : "Queued"));
  } else if (s === "downloading") {
    const pct = Math.round((t.job_progress || 0) * 100);
    const stage = { converting: "Converting", tagging: "Tagging", starting: "Starting" }[t.job_stage] || `${pct}%`;
    chips.push(h("div", { class: "status-stack", style: { width: "100%" } },
      h("span", { class: "chip busy" }, stage),
      h("div", { class: "progress" }, h("div", { style: { width: `${pct}%` } }))));
  } else if (s === "done") {
    chips.push(h("span", { class: "chip ok" }, "✓ Downloaded"));
  } else if (s === "error") {
    chips.push(h("span", { class: "chip bad", title: t.error_msg || "Failed" }, "✕ " + shortError(t)));
  } else if (s === "missing") {
    chips.push(h("span", { class: "chip bad", title: `File not found: ${t.file_rel_path}\nRelocate it, or use ⋯ → Auto Relocate.` }, "! Missing"));
  }
  if (t.check_version) {
    chips.push(h("span", {
      class: "chip warn",
      title: `The file is ${fmtMs(t.file_duration_ms)} but Spotify says ${fmtMs(t.duration_ms)} (${fmtDiff(t.file_duration_ms - t.duration_ms)}).\n` +
        "Check you got the right version — or tick Edited if you cut it on purpose.",
    }, "⚠ Check version"));
  }
  if (t.tag_error) chips.push(h("span", { class: "chip warn", title: t.tag_error + "\nOpen the song and click Retry tags." }, "Tags failed"));
  return h("div", { class: "status-stack" }, chips);
}

function shortError(t) {
  return {
    unavailable: "Unavailable", needs_cookies: "Needs sign-in", ytdlp_outdated: "Update yt-dlp", invalid_url: "Bad link",
    transient: "Network", disk_full: "Disk full", ffmpeg_missing: "No ffmpeg",
  }[t.error_code] || "Failed";
}

function actionButton(t) {
  const s = t.download_status;
  if (t.job_status === "interrupted") {
    return h("button", { class: "btn small", onclick: (e) => { e.stopPropagation(); emit("open-activity"); } }, "Resume…");
  }
  if (s === "queued" || s === "downloading") {
    return h("button", {
      class: "btn small ghost",
      onclick: async (e) => {
        e.stopPropagation();
        try { await post(`/api/jobs/${t.job_id}/cancel`); emit("refresh-tracks"); } catch (err) { errorToast(err); }
      },
    }, "Cancel");
  }
  if (s === "missing") {
    return h("button", { class: "btn small", onclick: (e) => { e.stopPropagation(); relocateTrack(t); } }, "Relocate…");
  }
  if (s === "done") {
    return h("button", {
      class: "btn small ghost", title: "Download again (e.g. with a better link)",
      onclick: async (e) => {
        e.stopPropagation();
        const ok = await confirmDialog("Replace the audio?",
          "This downloads the link again and replaces the file at the same path (the old file goes to the Trash).\n\n" +
          "If this song is already in Rekordbox, its cues and beatgrid may no longer line up.", { ok: "Replace audio" });
        if (ok) startDownloads([t.id], true);
      },
    }, "Re-download");
  }
  return h("button", {
    class: `btn small ${s === "error" ? "" : "primary"}${t.source_url ? "" : " no-link"}`,
    onclick: (e) => {
      e.stopPropagation();
      if (!t.source_url) {
        toast(`No link yet for “${t.title}” — paste a YouTube link (or use Find ▾) first.`, { type: "warn" });
        rows.get(t.id)?.tr.querySelector(".link-cell input")?.focus();
        return;
      }
      startDownloads([t.id]);
    },
  }, s === "error" ? "Retry" : "Download");
}

export async function relocateTrack(t) {
  const path = await native.pickFile("mp3", "Path to the MP3:");
  if (!path) return;
  try {
    const r = await post("/api/library/relocate", { track_id: t.id, path });
    updateTrack(r);
    emit("refresh-sidebar");
    toast("Relinked.", { type: "success" });
  } catch (e) { errorToast(e); }
}

export async function startDownloads(ids, replace = false) {
  try {
    const r = await post("/api/downloads", { ids, replace });
    const parts = [];
    if (r.queued) parts.push(`Queued ${r.queued} download${r.queued === 1 ? "" : "s"}.`);
    const noLink = r.skipped.filter((s) => s.reason === "No link").length;
    if (noLink) parts.push(`${noLink} skipped — no link yet.`);
    const dupes = r.skipped.filter((s) => s.reason === "Already queued").length;
    if (dupes) parts.push(`${dupes} already queued.`);
    toast(parts.join(" ") || "Nothing to download.", { type: r.queued ? "success" : "info" });
    emit("refresh-tracks");
  } catch (e) { errorToast(e); }
}

export function updateTrack(track) {
  const i = store.tracks.findIndex((t) => t.id === track.id);
  if (i >= 0) {
    // keep per-view fields (crate position, removed flag, job info)
    store.tracks[i] = { ...store.tracks[i], ...track };
    renderTracks();
  }
  emit("track-updated", store.tracks[i] || track);
}

// ------------------------------------------------ selection & bulk

export function syncSelection() {
  for (const [id, { tr }] of rows) {
    const on = selected.has(id);
    tr.classList.toggle("selected", on);
    const cb = tr.querySelector("input.sel");
    if (cb && cb.checked !== on) cb.checked = on;
  }
  const all = document.getElementById("select-all");
  const n = store.tracks.filter((t) => selected.has(t.id)).length;
  all.checked = n > 0 && n === store.tracks.length;
  all.indeterminate = n > 0 && n < store.tracks.length;
  renderBulkbar();
}

function renderBulkbar() {
  const bar = document.getElementById("bulkbar");
  const ids = store.tracks.filter((t) => selected.has(t.id)).map((t) => t.id);
  if (!ids.length) { bar.hidden = true; return; }
  bar.hidden = false;
  const inCrate = store.view.startsWith("crate:") && findCrate(Number(store.view.split(":")[1]))?.kind !== "folder";
  const markBtn = h("button", { class: "btn small" }, "Mark ▾");
  markBtn.onclick = () => menu(markBtn, [
    { label: "Edited ✓", fn: () => bulk(ids, "edited", true) },
    { label: "Edited ✕", fn: () => bulk(ids, "edited", false) },
    "-",
    { label: "Cleaned ✓", fn: () => bulk(ids, "cleaned", true) },
    { label: "Cleaned ✕", fn: () => bulk(ids, "cleaned", false) },
    "-",
    { label: "Verified ✓", fn: () => bulk(ids, "verified", true) },
    { label: "Verified ✕", fn: () => bulk(ids, "verified", false) },
  ]);
  const addBtn = h("button", { class: "btn small" }, "Add to crate ▾");
  addBtn.onclick = () => {
    const crates = flatCrates().filter((c) => c.kind !== "folder");
    menu(addBtn, crates.length
      ? crates.map((c) => ({ label: c.path, fn: () => addToCrate(ids, c) }))
      : [{ label: "No crates yet — make one with +♪", disabled: true, fn: () => {} }]);
  };
  fill(bar,
    h("span", { class: "count" }, `${ids.length} selected`),
    markBtn, addBtn,
    h("button", { class: "btn small", onclick: () => startDownloads(ids) }, "⬇ Download"),
    inCrate ? h("button", { class: "btn small", onclick: () => removeFromCrate(ids) }, "Remove from crate") : null,
    h("button", { class: "btn small danger", onclick: () => deleteFromLibrary(ids) }, "Delete from library…"),
    h("div", { class: "spacer" }),
    h("button", { class: "btn small ghost", onclick: () => { selected.clear(); syncSelection(); } }, "Clear selection"),
  );
}

async function bulk(ids, field, value) {
  try {
    await post("/api/tracks/bulk", { ids, field, value });
    emit("refresh-tracks");
    emit("refresh-sidebar");
  } catch (e) { errorToast(e); }
}

async function addToCrate(ids, c) {
  try {
    const r = await post(`/api/crates/${c.id}/tracks`, { ids });
    toast(`Added ${r.added} to “${c.name}”${r.already ? ` (${r.already} already there)` : ""}.`, { type: "success" });
    emit("refresh-sidebar");
  } catch (e) { errorToast(e); }
}

async function removeFromCrate(ids) {
  const crateId = Number(store.view.split(":")[1]);
  const c = findCrate(crateId);
  try {
    const r = await post(`/api/crates/${crateId}/tracks/remove`, { ids });
    ids.forEach((id) => selected.delete(id));
    emit("refresh-tracks");
    emit("refresh-sidebar");
    toast(`Removed ${r.removed.length} from “${c?.name}”. Songs are still in All Tracks.`, {
      timeout: 10000,
      action: {
        label: "Undo",
        fn: async () => {
          await post(`/api/crates/${crateId}/tracks/restore`, { rows: r.removed });
          emit("refresh-tracks");
          emit("refresh-sidebar");
        },
      },
    });
  } catch (e) { errorToast(e); }
}

async function deleteFromLibrary(ids) {
  const files = store.tracks.filter((t) => ids.includes(t.id) && t.file_rel_path).length;
  const ok = await confirmDialog(`Delete ${ids.length} song${ids.length === 1 ? "" : "s"} from the library?`,
    `They'll be removed from every crate${files ? `, and ${files} MP3 file${files === 1 ? "" : "s"} will be moved to the Trash` : ""}.\n\n` +
    "If they're in Rekordbox, Rekordbox will show them as missing.\nTo only take songs out of this crate, use “Remove from crate” instead.",
    { ok: "Delete", danger: true });
  if (!ok) return;
  try {
    const r = await post("/api/tracks/delete", { ids });
    ids.forEach((id) => selected.delete(id));
    if (r.failed.length) toast(`Couldn't trash ${r.failed.length} file(s): ${r.failed[0].reason}`, { type: "error", timeout: 9000 });
    toast(`Deleted ${r.deleted} song${r.deleted === 1 ? "" : "s"}${r.trashed_files ? ` (${r.trashed_files} file${r.trashed_files === 1 ? "" : "s"} in the Trash)` : ""}.`);
    emit("refresh-tracks");
    emit("refresh-sidebar");
  } catch (e) { errorToast(e); }
}

// ------------------------------------------------ Find YouTube link

async function openFind(t, anchor, input) {
  if (isPopoverOpenFor(anchor)) return closePopover();
  const box = h("div", { class: "find" });
  popover(anchor, box, { align: "right" });
  const load = async () => {
    box.replaceChildren(h("div", { class: "head" }, h("span", { class: "spinner" }), ` Searching YouTube for “${t.artists} - ${t.title}”…`));
    try {
      const r = await get(`/api/tracks/${t.id}/yt_search`);
      if (!box.isConnected) return;
      renderFind(box, t, r, input);
    } catch (e) {
      if (!box.isConnected) return;
      fill(box,
        h("div", { class: "head", style: { color: "var(--red)" } }, `Search failed: ${e.message}`),
        h("div", { class: "foot" },
          h("button", { class: "btn small", onclick: load }, "Retry"),
          e.extra?.search_url ? h("button", { class: "btn small", onclick: () => post("/api/open_url", { url: e.extra.search_url }) }, "Open YouTube search ↗") : null));
    }
  };
  load();
}

function renderFind(box, t, r, input) {
  const items = r.results.map((res) => {
    let lengthNote = null;
    if (res.diff_ms !== null && res.diff_ms !== undefined) {
      lengthNote = res.close
        ? h("span", { class: "match" }, "✓ length matches Spotify")
        : h("span", { class: "off" }, `${fmtDiff(res.diff_ms)} vs Spotify`);
    }
    return h("button", {
      class: `result${res.close ? " close" : ""}`,
      onclick: async () => {
        input.value = res.url;
        input.dispatchEvent(new Event("change"));
        closePopover();
      },
    },
      h("div", { class: "rt" }, res.title),
      h("div", { class: "rm" }, [res.channel, fmtMs(res.duration_ms)].filter(Boolean).join(" · "), lengthNote ? " · " : "", lengthNote),
      res.flags.map((f) => h("div", { class: "flagline" }, "⚠ " + f)));
  });
  fill(box,
    h("div", { class: "head" }, `Results for “${r.query}” · Spotify length ${fmtMs(t.duration_ms)}. Pick the right one — green means the length matches.`),
    items.length ? items : h("div", { class: "head" }, "No results."),
    h("div", { class: "foot" },
      h("span", { class: "muted small" }, "Not here?"),
      h("button", { class: "btn small", onclick: () => post("/api/open_url", { url: r.search_url }) }, "Open YouTube search ↗")),
  );
}

// ------------------------------------------------ inline player

function playButton(t, tr) {
  const btn = h("button", {
    class: `play-btn${player?.trackId === t.id ? " active" : ""}`,
    disabled: t.download_status !== "done", title: t.download_status === "done" ? "Preview" : "Download first",
  }, player?.trackId === t.id ? "■" : "▶");
  btn.addEventListener("click", (e) => { e.stopPropagation(); btn.blur(); togglePlayer(t, rows.get(t.id)?.tr || tr); });
  return btn;
}

function togglePlayer(t, tr) {
  if (player && player.trackId === t.id) return closePlayer();
  closePlayer();
  const a = audio();
  const pp = h("button", { class: "pp", title: "Play/pause (Space)" }, "❚❚");
  const cur = h("span", { class: "time" }, "0:00");
  const dur = h("span", { class: "time" }, fmtMs(t.file_duration_ms));
  const range = h("input", { type: "range", min: "0", max: "1000", step: "1", value: "0", title: "Drag to skip through the song" });
  let seeking = false;
  pp.onclick = () => { pp.blur(); a.paused ? a.play() : a.pause(); };
  range.addEventListener("input", () => {
    seeking = true;
    if (a.duration) cur.textContent = fmtMs((range.value / 1000) * a.duration * 1000);
  });
  range.addEventListener("change", () => {
    if (a.duration) a.currentTime = (range.value / 1000) * a.duration;
    seeking = false;
    range.blur(); // so Space/Esc go back to the player shortcuts
  });
  a.ontimeupdate = () => {
    if (!a.duration) return;
    cur.textContent = fmtMs(a.currentTime * 1000);
    if (!seeking) range.value = String(Math.round((a.currentTime / a.duration) * 1000));
  };
  a.onloadedmetadata = () => { dur.textContent = fmtMs(a.duration * 1000); };
  a.onplay = () => { pp.textContent = "❚❚"; };
  a.onpause = () => { pp.textContent = "▶"; };
  a.onended = () => { pp.textContent = "▶"; };
  a.onerror = () => {
    if (!player) return;
    toast("Couldn't play this file — it may be missing or damaged. Try Rescan or Relocate.", { type: "error" });
    closePlayer();
  };
  const row = h("tr", { class: "player-row" },
    h("td", { colspan: "13" }, h("div", { class: "player" },
      pp, cur, range, dur, h("span", { class: "hint" }, "Space play/pause · Esc close"))));
  tr.after(row);
  const playBtn = tr.querySelector(".play-btn");
  playBtn?.classList.add("active");
  if (playBtn) playBtn.textContent = "■";
  player = { trackId: t.id, row, playBtn, trackRow: tr };
  a.src = `/api/tracks/${t.id}/audio`;
  a.play().catch(() => {});
}

export function closePlayer() {
  if (!player) return false;
  const a = audio();
  a.pause();
  a.removeAttribute("src");
  a.load();
  player.row.remove();
  if (player.playBtn) { player.playBtn.classList.remove("active"); player.playBtn.textContent = "▶"; }
  player = null;
  return true;
}

export function playerOpen() {
  return !!player;
}

export function togglePlayPause() {
  if (!player) return false;
  const a = audio();
  a.paused ? a.play() : a.pause();
  return true;
}

export function resetTable() {
  closePlayer();
  rows.forEach(({ tr }) => tr.remove());
  rows.clear();
  selected.clear();
  document.getElementById("table-wrap").scrollTop = 0;
}

export function initTracks() {
  document.getElementById("select-all").addEventListener("change", (e) => {
    if (e.target.checked) store.tracks.forEach((t) => selected.add(t.id));
    else selected.clear();
    syncSelection();
  });
  on("detail-changed", () => {
    for (const [id, { tr }] of rows) tr.classList.toggle("open", store.detailId === id);
  });
}
