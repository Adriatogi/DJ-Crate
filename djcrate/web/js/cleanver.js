// Clean and explicit versions: YouTube Music lists many albums twice, once marked
// explicit and once not. DJ Crate knows both copies of a song and which one your
// link is. A clean song gets a neutral “Clean” label (some gigs want clean), and
// either way you can swap to the other copy (re-downloads to the same file).

import { post } from "./api.js";
import { emit } from "./store.js";
import { confirmDialog, errorToast, fmtMs, h, menu, toast } from "./ui.js";

export const twinLine = (tw) => [tw.title, tw.album, fmtMs(tw.duration_ms)].filter(Boolean).join(" · ");

// The other copy a song can swap to: { to, copy } or null.
export function otherCopy(t) {
  const c = t.clean;
  if (!c) return null;
  if ((c.result === "clean" || c.result === "unofficial") && c.twin) return { to: "explicit", copy: c.twin };
  if (c.result === "explicit" && c.clean_twin) return { to: "clean", copy: c.clean_twin };
  return null;
}

// The neutral “Clean” label next to a title. Only for clean copies; null otherwise.
export function cleanBadge(t) {
  if (!t.clean_flag) return null;
  const tw = t.clean.twin;
  const b = h("button", {
    class: "clean-tag",
    title: `Clean version.${tw ? ` YouTube Music also has the explicit one:\n${twinLine(tw)}` : ""}\nClick for options.`,
  }, "Clean");
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    b.blur();
    menu(b, [
      tw ? { label: "Swap to the explicit version", fn: () => swapVersion([t], "explicit") } : null,
      tw ? { label: "Open the explicit one on YouTube Music ↗", fn: () => post("/api/open_url", { url: `https://music.youtube.com/watch?v=${tw.video_id}` }) } : null,
      { label: "Open song details", fn: () => emit("open-detail", t.id) },
    ].filter(Boolean));
  });
  return b;
}

// Swap songs to their explicit (or clean) copy. Songs without one are skipped.
export async function swapVersion(tracks, to, { ask = true } = {}) {
  const list = tracks.filter((t) => otherCopy(t)?.to === to);
  if (!list.length) return;
  const files = list.filter((t) => ["done", "missing", "error"].includes(t.download_status)).length;
  if (ask) {
    const one = list.length === 1;
    const ok = await confirmDialog(
      one ? `Swap “${list[0].title}” to the ${to} version?` : `Swap ${list.length} songs to their ${to} versions?`,
      (one ? `The link changes to:\n${twinLine(otherCopy(list[0]).copy)}\n\n` : `Each song's link changes to its ${to} copy on YouTube Music.\n\n`) +
      (files
        ? `${files === list.length ? (one ? "It" : "They") : `${files} of them`} will be downloaded again to the same file, so Rekordbox keeps its link. ` +
          "If you've set cues or a beatgrid in Rekordbox, check they still line up."
        : "Download when you're ready."),
      { ok: one ? "Swap" : `Swap ${list.length}` });
    if (!ok) return;
  }
  try {
    const r = await post("/api/tracks/swap_version", { ids: list.map((t) => t.id), to });
    r.tracks.forEach((t) => emit("track-changed", t));
    toast(`Swapped ${r.swapped} song${r.swapped === 1 ? "" : "s"} to the ${to} version` +
      (r.queued ? `. Downloading ${r.queued === 1 ? "it" : r.queued} again.` : "."), { type: "success" });
    emit("refresh-tracks");
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

// Song card section: which version this is, and a swap to the other copy if there is one.
export function cleanSection(t) {
  if (!/youtu\.?be/.test(t.source_url || "")) return null;
  const c = t.clean;
  const again = h("button", { class: "btn small" }, c ? "Check again" : "Check now");
  again.addEventListener("click", () => checkNow(t, again));
  const other = otherCopy(t);
  let text;
  if (!c) text = "Not checked yet. DJ Crate checks songs in the background.";
  else if (c.result === "clean") text = c.swapped_from ? "Clean version (swapped from the explicit one)." : "Clean version.";
  else if (c.result === "explicit") text = c.swapped_from ? "Explicit version (swapped from the clean one)." : "Explicit version.";
  else if (c.result === "unofficial") text = "Not an official YouTube Music track (a user upload or music video), so its version can't be checked.";
  else if (c.result === "no_explicit") text = c.official ? "YouTube Music has no explicit version of this song." : "Not a YouTube Music track, so its version can't be checked.";
  else text = "Couldn't check this link. It will try again later.";
  const label = other ? `${other.to === "explicit" ? (c.result === "unofficial" ? "Official explicit" : "Explicit") : "Clean"} copy on YouTube Music: ` : null;
  return [
    h("div", { class: "section-title" }, "Clean or explicit"),
    h("div", { class: "small" }, text),
    other ? h("div", { class: "muted small" }, label, twinLine(other.copy)) : null,
    h("div", { class: "row gap", style: { flexWrap: "wrap", marginTop: "6px" } },
      other ? h("button", { class: "btn small", onclick: () => swapVersion([t], other.to) }, `Swap to ${other.to}`) : null,
      again),
  ];
}

// Find results: a neutral note on results that are the clean copy.
export function findTag(res) {
  if (!res.clean_of) return null;
  return h("div", { class: "flagline clean-note" }, `Clean version. The explicit one is ${twinLine(res.clean_of)}.`);
}
