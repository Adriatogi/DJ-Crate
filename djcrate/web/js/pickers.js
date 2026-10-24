// "Pick a song from your library" popover, used by the boards.

import { get } from "./api.js";
import { closePopover, debounce, errorToast, fmtMs, h, popover } from "./ui.js";

// anchor: an element, or {clientX, clientY} for a point on screen.
export function songPicker(anchor, { title = "Add a song", onPick, extra = null, allowText = false, textHint = "" } = {}) {
  let el = anchor;
  if (!(anchor instanceof Element)) {
    el = h("div", { style: { position: "fixed", left: `${anchor.clientX}px`, top: `${anchor.clientY}px`, width: "1px", height: "1px" } });
    document.body.append(el);
  }
  const input = h("input", { placeholder: allowText ? "Search your library, or type and press Enter" : "Search your library…", style: { width: "100%" } });
  const results = h("div", { class: "menu", style: { maxHeight: "300px", overflowY: "auto" } });
  const box = h("div", { style: { width: "380px", padding: "8px" } },
    h("div", { class: "section-title", style: { margin: "2px 2px 6px" } }, title), extra, input, results,
    allowText ? h("div", { class: "muted small", style: { margin: "6px 2px 0" } }, textHint) : null);
  let tracks = [];
  const pick = (t) => { closePopover(); onPick(t); };
  const render = () => results.replaceChildren(...tracks.slice(0, 30).map((t) => h("button", { onclick: () => pick(t) },
    t.cover_url ? h("img", { class: "cover", src: t.cover_url, alt: "", style: { width: "26px", height: "26px" } }) : h("span", { class: "cover placeholder", style: { width: "26px", height: "26px" } }, "♪"),
    h("span", { class: "grow", style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, `${t.title} — ${t.artists}`),
    h("span", { class: "muted small" }, fmtMs(t.duration_ms ?? t.file_duration_ms)))));
  const search = debounce(async () => {
    try {
      const q = input.value.trim();
      tracks = (await get(`/api/tracks?view=smart:all&q=${encodeURIComponent(q)}`)).tracks;
      render();
      if (!tracks.length) results.replaceChildren(h("div", { class: "label" }, allowText && q ? "No song matches — press Enter to add it as text" : "No matches in your library"));
    } catch (e) { errorToast(e); }
  }, 150);
  input.addEventListener("input", search);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (allowText && input.value.trim() && (!tracks.length || e.metaKey || e.shiftKey)) { closePopover(); onPick(null, input.value.trim()); }
      else if (tracks[0]) pick(tracks[0]);
    }
  });
  popover(el, box, { onClose: () => { if (el !== anchor) el.remove(); } });
  search();
  setTimeout(() => input.focus(), 30);
}
