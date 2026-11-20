// Clean versions: YouTube Music lists many albums twice, once with an E (explicit)
// badge and once without. When a song's link is the copy without it, and an E copy
// of the same song exists, the song gets a red struck-through E. You decide:
// swap to the explicit one (re-downloads to the same file) or keep it.

import { post, put } from "./api.js";
import { emit } from "./store.js";
import { confirmDialog, errorToast, fmtMs, h, menu, toast } from "./ui.js";

export const twinLine = (tw) => [tw.title, tw.album, fmtMs(tw.duration_ms)].filter(Boolean).join(" · ");

// The red struck-through E. Only for flagged songs; null otherwise.
export function cleanBadge(t) {
  if (!t.clean_flag) return null;
  const tw = t.clean.twin;
  const b = h("button", {
    class: "clean-e",
    title: `Clean version. YouTube Music has the explicit one:\n${twinLine(tw)}\nClick to swap or keep.`,
  }, "E");
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    b.blur();
    menu(b, [
      { label: "Swap to the explicit version", fn: () => swapExplicit([t]) },
      { label: "Keep this one", fn: () => keepClean(t, true) },
      "-",
      { label: "Open on YouTube Music ↗", fn: () => post("/api/open_url", { url: `https://music.youtube.com/watch?v=${tw.video_id}` }) },
    ]);
  });
  return b;
}

export async function swapExplicit(tracks, { ask = true } = {}) {
  // Flagged clean edits, plus user uploads where you asked for the official explicit copy.
  const list = tracks.filter((t) => t.clean_flag || (t.clean?.result === "unofficial" && t.clean.twin));
  if (!list.length) return;
  const files = list.filter((t) => ["done", "missing", "error"].includes(t.download_status)).length;
  if (ask) {
    const one = list.length === 1;
    const ok = await confirmDialog(
      one ? `Swap “${list[0].title}” to the official explicit version?` : `Swap ${list.length} songs to their explicit versions?`,
      (one ? `The link changes to:\n${twinLine(list[0].clean.twin)}\n\n` : "Each song's link changes to its explicit copy on YouTube Music.\n\n") +
      (files
        ? `${files === list.length ? (one ? "It" : "They") : `${files} of them`} will be downloaded again to the same file, so Rekordbox keeps its link. ` +
          "If you've set cues or a beatgrid in Rekordbox, check they still line up."
        : "Download when you're ready."),
      { ok: one ? "Swap" : `Swap ${list.length}` });
    if (!ok) return;
  }
  try {
    const r = await post("/api/tracks/swap_explicit", { ids: list.map((t) => t.id) });
    r.tracks.forEach((t) => emit("track-changed", t));
    toast(`Swapped ${r.swapped} song${r.swapped === 1 ? "" : "s"} to the explicit version` +
      (r.queued ? `. Downloading ${r.queued === 1 ? "it" : r.queued} again.` : "."), { type: "success" });
    emit("refresh-tracks");
    emit("refresh-sidebar");
  } catch (e) { errorToast(e); }
}

export async function keepClean(t, keep) {
  try {
    const r = await put(`/api/tracks/${t.id}/clean_keep`, { keep });
    emit("track-changed", r.track);
    if (keep) {
      toast(`Keeping the clean version of “${t.title}”. It won't be flagged again.`, {
        action: { label: "Undo", fn: () => keepClean(r.track, false) },
      });
    }
    emit("refresh-sidebar");
  } catch (e) { errorToast(e); }
}

async function checkNow(t, btn) {
  btn.disabled = true;
  btn.textContent = "Checking…";
  try {
    const r = await post(`/api/tracks/${t.id}/cleancheck`);
    emit("track-changed", r.track);
    emit("refresh-sidebar");
  } catch (e) {
    errorToast(e);
    btn.disabled = false;
    btn.textContent = "Check again";
  }
}

// Song card section. Quiet unless there's something to say.
export function cleanSection(t) {
  const c = t.clean;
  const yt = /youtu\.?be/.test(t.source_url || "");
  if (!yt) return null;
  const again = h("button", { class: "btn small" }, c ? "Check again" : "Check now");
  again.addEventListener("click", () => checkNow(t, again));
  let line;
  let actions = [again];
  if (!c) {
    line = "Not checked yet. DJ Crate checks songs in the background.";
  } else if (c.result === "clean") {
    const tw = c.twin;
    line = h("div", {},
      h("div", { class: t.clean_keep ? "" : "clean-warn" }, t.clean_keep ? "Clean version (you chose to keep it)." : "This is the clean version."),
      h("div", { class: "muted small" }, "Explicit on YouTube Music: ", twinLine(tw)));
    actions = t.clean_keep
      ? [h("button", { class: "btn small", onclick: () => keepClean(t, false) }, "Undo keep"), again]
      : [h("button", { class: "btn small primary", onclick: () => swapExplicit([t]) }, "Swap to explicit"),
        h("button", { class: "btn small", onclick: () => keepClean(t, true) }, "Keep this one"), again];
  } else if (c.result === "explicit") {
    line = c.swapped_from ? "Explicit version ✓ (swapped from the clean one)." : "Explicit version ✓ (YouTube Music's E badge).";
  } else if (c.result === "unofficial") {
    line = h("div", {},
      h("div", {}, "Not an official YouTube Music track (a user upload or music video), so its version can't be checked."),
      c.twin ? h("div", { class: "muted small" }, "Official explicit copy on YouTube Music: ", twinLine(c.twin)) : null);
    if (c.twin) actions = [h("button", { class: "btn small", onclick: () => swapExplicit([t]) }, "Use the official explicit version"), again];
  } else if (c.result === "no_explicit") {
    line = c.official
      ? "YouTube Music has no explicit version of this song."
      : "Not a YouTube Music track (a user upload), so its version can't be checked.";
  } else {
    line = "Couldn't check this link. It will try again later.";
  }
  return [
    h("div", { class: "section-title" }, "Clean or explicit"),
    h("div", { class: "small" }, line),
    h("div", { class: "row gap", style: { flexWrap: "wrap", marginTop: "6px" } }, actions),
  ];
}

// Find results: a red struck-through E on results that are the clean copy.
export function findTag(res) {
  if (!res.clean_of) return null;
  return h("div", { class: "flagline clean-line" }, h("span", { class: "clean-e static" }, "E"),
    ` Clean version. The explicit one is ${twinLine(res.clean_of)}.`);
}
