// The song card: edit metadata, notes (written into the MP3's tags), flags and
// samples. One card for the whole app: the Crates side panel, and Cutting's
// right panel (via songCard).

import { patch, post } from "./api.js";
import { samplesSection } from "./samples.js";
import { emit, on, store } from "./store.js";
import { errorToast, fill, fmtBpm, fmtDiff, fmtKey, fmtMs, h, toast } from "./ui.js";
import { relocateTrack, updateTrack } from "./tracks.js";

function current() {
  return store.tracks.find((t) => t.id === store.detailId) || null;
}

export function openDetail(id) {
  store.detailId = id;
  render();
  emit("detail-changed");
}

export function closeDetail() {
  if (!store.detailId) return false;
  store.detailId = null;
  document.getElementById("detail").hidden = true;
  emit("detail-changed");
  return true;
}

function render() {
  const panel = document.getElementById("detail");
  const t = current();
  if (!t) { closeDetail(); return; }
  panel.hidden = false;
  if (panel.contains(document.activeElement) && panel.dataset.id === String(t.id)) return; // don't clobber typing
  panel.dataset.id = t.id;
  fill(panel, songCard(t, {
    onClose: closeDetail,
    // A linked sample: open its card if it's in this list, otherwise its Cutting board.
    openSong: (id) => store.tracks.some((x) => x.id === id) ? openDetail(id) : (emit("cutting-focus", id), emit("show-tab", "cutting")),
    openTitle: "Open this song",
  }));
}

// opts: onClose (shows ✕), extra (nodes under the title), openSong/openTitle
// (for linked samples), onSaved(track), onSamplesChange().
export function songCard(t, { onClose = null, extra = null, openSong, openTitle, onSaved = null, onSamplesChange = null } = {}) {
  const save = (changes, revert) => saveTrack(t, changes, revert, onSaved);
  const retag = () => retagTrack(t, onSaved);
  const field = (label, key, { multiline = false, placeholder = "" } = {}) => {
    const input = multiline
      ? h("textarea", { rows: 5, placeholder }, t[key] || "")
      : h("input", { value: t[key] || "", placeholder });
    input.addEventListener("change", () => save({ [key]: input.value }));
    if (!multiline) input.addEventListener("keydown", (e) => { if (e.key === "Enter") input.blur(); });
    return h("div", { class: "field" }, h("label", {}, label), input);
  };

  const flag = (key, label) => {
    const cb = h("input", { type: "checkbox", class: `flag ${key}`, checked: !!t[key] });
    cb.addEventListener("change", () => save({ [key]: cb.checked }, () => { cb.checked = !cb.checked; }));
    return h("label", {}, cb, label);
  };

  const hasFile = t.file_rel_path && t.download_status === "done";
  const lengthLine = t.file_duration_ms && t.duration_ms
    ? `${fmtMs(t.file_duration_ms)} (${fmtDiff(t.file_duration_ms - t.duration_ms)} vs Spotify)`
    : fmtMs(t.file_duration_ms);

  return [
    h("div", { class: "row", style: { justifyContent: "space-between", marginBottom: "8px" } },
      h("div", { class: "section-title", style: { margin: 0 } }, "Song"),
      onClose ? h("button", { class: "icon-btn", title: "Close (Esc)", onclick: onClose }, "✕") : null),
    h("div", { class: "detail-head" },
      t.cover_url ? h("img", { src: t.cover_url, alt: "" }) : h("div", { class: "cover placeholder" }, "♪"),
      h("div", {},
        h("div", { class: "t-title", style: { fontSize: "15px", whiteSpace: "normal" } }, t.title),
        h("div", { class: "muted" }, t.artists),
        t.spotify_url ? h("a", { href: "#", class: "small", onclick: (e) => { e.preventDefault(); post("/api/open_url", { url: t.spotify_url }); } }, "Open in Spotify ↗") : null,
        extra)),
    h("div", { class: "flags-row" }, flag("edited", "Edited"), flag("ready", "Ready"), flag("broken", "Broken")),
    field("Notes", "notes", { multiline: true, placeholder: "Anything about this song — drop at 1:04, vocal comes in late, pairs well with…" }),
    h("div", { class: "muted small", style: { marginTop: "-6px", marginBottom: "10px" } },
      "Notes are also written into the MP3's comment tag, so they show in Rekordbox's Comments column (use Reload Tag there after edits)."),
    samplesSection(t, { openSong, openTitle, onChange: onSamplesChange }),
    h("div", { class: "section-title" }, "Metadata"),
    field("Title", "title"),
    field("Artists (all, including features)", "artists"),
    field("Album", "album"),
    field("Release date", "release_date", { placeholder: "YYYY-MM-DD" }),
    h("div", { class: "muted small" }, "Editing these updates the MP3's tags. The filename stays the same so Rekordbox keeps its link."),
    h("div", { class: "section-title" }, "File"),
    h("div", { class: "kv" },
      h("div", { class: "k" }, "Status"), h("div", { class: "v" }, statusText(t)),
      h("div", { class: "k" }, "Spotify length"), h("div", { class: "v" }, fmtMs(t.duration_ms)),
      h("div", { class: "k" }, "BPM"), h("div", { class: "v", title: t.bpm ? "" : "Not in your Rekordbox export yet" }, fmtBpm(t.bpm)),
      h("div", { class: "k" }, "Key"), h("div", { class: "v", title: t.musical_key ? "" : "Not in your Rekordbox export yet" }, fmtKey(t.musical_key)),
      h("div", { class: "k" }, "File length"), h("div", { class: "v" }, t.file_duration_ms ? lengthLine : "–"),
      h("div", { class: "k" }, "File"), h("div", { class: "v" }, t.file_rel_path || "–"),
      t.isrc ? [h("div", { class: "k" }, "ISRC"), h("div", { class: "v" }, t.isrc)] : null,
      t.source_url ? [h("div", { class: "k" }, "Link"), h("div", { class: "v" },
        h("a", { href: "#", onclick: (e) => { e.preventDefault(); post("/api/open_url", { url: t.source_url }); } }, t.source_url))] : null),
    t.error_msg && t.download_status === "error" ? h("div", { class: "banner bad", style: { borderRadius: "6px", margin: "6px 0" } }, t.error_msg) : null,
    t.tag_error ? h("div", { class: "banner warn", style: { borderRadius: "6px", margin: "6px 0" } }, t.tag_error) : null,
    h("div", { class: "row gap", style: { flexWrap: "wrap", marginTop: "8px" } },
      t.file_rel_path && t.download_status !== "missing"
        ? h("button", { class: "btn small", onclick: () => post("/api/reveal", { track_id: t.id }).catch(errorToast) }, "Show in Finder") : null,
      t.file_rel_path ? h("button", { class: "btn small", onclick: () => relocateTrack(t) }, "Relocate…") : null,
      hasFile ? h("button", { class: "btn small", onclick: retag }, t.tag_error ? "Retry tags" : "Rewrite tags") : null),
  ];
}

function statusText(t) {
  return {
    none: t.source_url ? "Ready to download" : "Needs a link",
    queued: "Queued", downloading: "Downloading…", done: t.check_version ? "Downloaded — ⚠ length differs from Spotify" : "Downloaded",
    error: "Failed", missing: "File missing",
  }[t.download_status] || t.download_status;
}

async function saveTrack(t, changes, revert, onSaved) {
  try {
    const r = await patch(`/api/tracks/${t.id}`, changes);
    r.warnings.forEach((w) => toast(w, { type: "warn", timeout: 8000 }));
    Object.assign(t, r.track);
    updateTrack(r.track);
    onSaved?.(r.track);
    emit("refresh-sidebar");
  } catch (e) {
    revert && revert();
    errorToast(e);
  }
}

async function retagTrack(t, onSaved) {
  try {
    const r = await post(`/api/tracks/${t.id}/retag`);
    updateTrack(r.track);
    onSaved?.(r.track);
    toast("Tags written.", { type: "success" });
  } catch (e) { errorToast(e); }
}

export function initDetail() {
  on("open-detail", openDetail);
  on("track-updated", (t) => { if (t && t.id === store.detailId) render(); });
  on("tracks-loaded", () => { if (store.detailId) render(); });
}
