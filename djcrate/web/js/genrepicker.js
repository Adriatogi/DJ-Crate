// Genres on songs: chips + a picker over your Genre Rack. The rack is the only
// list of genres, so you can't misspell one into existence — a new genre is
// always added to the rack on purpose ("+ Add … to the Genre Rack").

import { get, post, put } from "./api.js";
import { emit, store } from "./store.js";
import { closePopover, errorToast, h, isPopoverOpenFor, menu, modal, popover, toast } from "./ui.js";

// Same spelling-proofing as the server (djcrate/genres.py norm()).
export function normGenre(s) {
  return (s || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[-_]+/g, " ").replace(/\s+/g, " ").trim();
}

export async function loadGenres() {
  store.genres = await get("/api/genres");
  return store.genres;
}

export function flattenGenres(tree = store.genres?.tree || [], depth = 0, trail = [], idTrail = []) {
  const out = [];
  for (const g of tree) {
    const path = [...trail, g.name];
    const pathIds = [...idTrail, g.id];
    out.push({ ...g, depth, path, pathIds });
    out.push(...flattenGenres(g.children, depth + 1, path, pathIds));
  }
  return out;
}

export function titleCase(name) {
  const shout = new Set(["edm", "uk", "us", "idm", "dnb", "ebm", "r&b"]);
  return (name || "").split(/\s+/).map((w) => (shout.has(w) ? w.toUpperCase() : w.charAt(0).toUpperCase() + w.slice(1))).join(" ");
}

export function parentSelect(selected = null) {
  return h("select", {}, h("option", { value: "" }, "Top level"),
    flattenGenres().map((g) => h("option", { value: g.id, selected: g.id === selected }, `${" ".repeat(g.depth)}${g.name}`)));
}

// ------------------------------------------------ adding to the rack

export function addToRackDialog(name, onCreated, { parentId = null, title = "Add to the Genre Rack" } = {}) {
  const input = h("input", { value: name, style: { width: "100%" } });
  const parent = parentSelect(parentId);
  modal({
    title,
    body: h("div", {},
      h("p", { class: "muted" }, "Genres live in your Genre Rack so “House” and “house” can never become two genres."),
      h("div", { class: "field" }, h("label", {}, "Name"), input),
      h("div", { class: "field" }, h("label", {}, "Inside (optional — for a sub-genre)"), parent)),
    buttons: [
      { label: "Cancel" },
      {
        label: "Add to rack", kind: "primary",
        fn: async () => {
          try {
            const r = await post("/api/genres", { name: input.value, parent_id: parent.value ? Number(parent.value) : null });
            await loadGenres();
            emit("genres-changed");
            await onCreated?.({ id: r.id, name: input.value.trim() });
          } catch (e) {
            if (e.code === "genre_exists" && e.extra?.existing_id) {
              toast(e.message + " Using that one.", { type: "info" });
              await loadGenres();
              await onCreated?.({ id: e.extra.existing_id, name: input.value.trim() });
              return;
            }
            errorToast(e);
            return false;
          }
        },
      },
    ],
  });
  setTimeout(() => input.select(), 40);
}

// ------------------------------------------------ the picker

// opts: selected (Set of genre ids), onToggle(genre, on, ids) — toggling mode;
//       pickOne + onPick(genre, {remove}) — choose a single genre (bulk, alias);
//       removeToggle — show an "Remove instead" checkbox (bulk);
//       suggestions + onSuggestion(s) / onUnknown(s, "add"|"alias"|"ignore") — a "Suggested" section on top.
export async function openGenrePicker(anchor, opts) {
  if (isPopoverOpenFor(anchor)) return closePopover();
  try { await loadGenres(); } catch (e) { return errorToast(e); }
  const sel = new Set(opts.selected || []);
  const search = h("input", { placeholder: "Search your genres…", style: { width: "100%" } });
  const list = h("div", { class: "glist" });
  const remove = h("input", { type: "checkbox" });
  const box = h("div", { class: "genre-picker" }, search, list,
    opts.removeToggle ? h("label", { class: "row gap small", style: { padding: "6px 8px" } }, remove, "Remove instead of add") : null);

  const render = () => {
    const q = normGenre(search.value);
    const all = flattenGenres();
    const rows = q ? all.filter((g) => normGenre(g.name).includes(q) || g.aliases.some((a) => a.includes(q))) : all;
    const exact = all.some((g) => normGenre(g.name) === q || g.aliases.includes(q));
    const items = rows.map((g) => h("button", {
      class: `gitem${sel.has(g.id) ? " on" : ""}`,
      style: { paddingLeft: `${8 + (q ? 0 : g.depth * 16)}px` },
      onclick: async () => {
        if (opts.pickOne) {
          closePopover();
          await opts.onPick(g, { remove: remove.checked });
          return;
        }
        const on = !sel.has(g.id);
        on ? sel.add(g.id) : sel.delete(g.id);
        render();
        try { await opts.onToggle(g, on, [...sel]); } catch (e) { on ? sel.delete(g.id) : sel.add(g.id); render(); errorToast(e); }
      },
    },
    opts.pickOne ? null : h("span", { class: "gcheck" }, sel.has(g.id) ? "✓" : ""),
    h("span", {}, g.name),
    q && g.depth ? h("span", { class: "muted small" }, ` · ${g.path.slice(0, -1).join(" › ")}`) : null));
    const add = search.value.trim() && !exact
      ? h("button", {
        class: "gitem add",
        onclick: () => {
          closePopover();
          addToRackDialog(search.value.trim(), async (g) => {
            if (opts.pickOne) await opts.onPick(g, { remove: remove.checked });
            else { sel.add(g.id); await opts.onToggle(g, true, [...sel]); }
          });
        },
      }, `+ Add “${search.value.trim()}” to the Genre Rack…`)
      : null;
    const suggested = !q && opts.suggestions?.length ? [
      h("div", { class: "gsec" }, "Suggested — click to use"),
      ...opts.suggestions.map((s) => {
        const src = h("span", { class: "muted small" }, ` ${s.source}${s.level === "artist" ? " · artist" : ""}`);
        if (s.genre_id) {
          return h("button", { class: "gitem sug", onclick: () => { closePopover(); opts.onSuggestion(s); } }, h("span", {}, `+ ${s.label}`), src);
        }
        const act = (name, label) => h("button", {
          class: "gmini", onclick: (e) => { e.stopPropagation(); closePopover(); opts.onUnknown(s, name); },
        }, label);
        return h("div", { class: "gitem unk", title: "Not in your Genre Rack yet" },
          h("span", {}, `${s.label} ?`), src, h("span", { class: "gacts" }, act("add", "Add"), act("alias", "Alias of…"), act("ignore", "Ignore")));
      }),
      h("div", { class: "gsec" }, "Your Genre Rack"),
    ] : [];
    list.replaceChildren(...suggested, ...items, ...(add ? [add] : []),
      ...(!items.length && !add ? [h("div", { class: "muted small", style: { padding: "8px" } }, "Your Genre Rack is empty. Type a name to add the first genre.")] : []));
  };
  search.addEventListener("input", render);
  search.addEventListener("keydown", (e) => {
    if (e.key === "Enter") list.querySelector(".gitem")?.click();
  });
  render();
  popover(anchor, box, { cls: "genre-pop" });
  setTimeout(() => search.focus(), 30);
}

// ------------------------------------------------ saving

export async function assignGenres(t, genreIds, onSaved = null) {
  const r = await put(`/api/tracks/${t.id}/genres`, { genre_ids: genreIds });
  (r.tag_warnings || []).forEach((w) => toast(w, { type: "warn", timeout: 8000 }));
  Object.assign(t, r.track);
  emit("track-genres", r.track);
  onSaved?.(r.track);
  emit("refresh-sidebar");
  emit("genres-changed");
  return r;
}

export async function bulkGenre(ids, genre, remove) {
  try {
    const r = await post("/api/tracks/genres", { ids, genre_id: genre.id, action: remove ? "remove" : "add" });
    toast(`${remove ? "Removed" : "Added"} “${genre.name}” ${remove ? "from" : "to"} ${ids.length} song${ids.length === 1 ? "" : "s"}.` +
      (r.retagged ? ` Updated the genre tag on ${r.retagged} file${r.retagged === 1 ? "" : "s"}.` : ""), { type: "success" });
    (r.tag_warnings || []).forEach((w) => toast(w, { type: "warn", timeout: 8000 }));
    emit("refresh-tracks");
    emit("refresh-sidebar");
    emit("genres-changed");
  } catch (e) { errorToast(e); }
}

export function openBulkGenre(anchor, ids) {
  openGenrePicker(anchor, { pickOne: true, removeToggle: true, onPick: (g, { remove }) => bulkGenre(ids, g, remove) });
}

// ------------------------------------------------ the control (table cell + song card)

function resolveUnknown(anchor, t, s, action, onSaved) {
  if (action === "add") return addToRackDialog(s.label, (g) => assignGenres(t, [g.id], onSaved));
  if (action === "ignore") return post("/api/genres/ignore", { name: s.label }).then(() => emit("refresh-tracks")).catch(errorToast);
  return openGenrePicker(anchor, {
    pickOne: true,
    onPick: async (g) => {
      await post(`/api/genres/${g.id}/aliases`, { name: s.label });
      toast(`“${s.label}” now counts as “${g.name}”.`, { type: "success" });
      await assignGenres(t, [g.id], onSaved);
      emit("refresh-tracks");
    },
  });
}

export function genreControl(t, { onSaved = null, max = 3, inline = 2 } = {}) {
  const shown = t.genres.slice(0, max);
  const sug = t.genres.length ? [] : (t.genre_suggestions || []);
  // The [+] square opens the full dropdown (your rack, plus every suggestion).
  const btn = h("button", {
    class: "gplus",
    title: sug.length ? `Choose a genre — ${sug.length} suggestion${sug.length === 1 ? "" : "s"}` : "Choose genres from your Genre Rack",
  }, "+");
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    openGenrePicker(btn, {
      selected: new Set(t.genres.map((g) => g.id)),
      onToggle: async (_g, _on, ids) => { await assignGenres(t, ids, onSaved); },
      suggestions: sug,
      onSuggestion: (s) => assignGenres(t, [s.genre_id], onSaved).catch(errorToast),
      onUnknown: (s, action) => resolveUnknown(btn, t, s, action, onSaved),
    });
  });
  // Hover a genre to get an ✕ that removes it from this song.
  const parts = shown.map((g) => {
    const x = h("button", { class: "gx", title: `Remove “${g.name}” from this song` }, "✕");
    x.addEventListener("click", (e) => {
      e.stopPropagation();
      x.blur(); // the table won't redraw a row that has focus
      assignGenres(t, t.genres.filter((o) => o.id !== g.id).map((o) => o.id), onSaved).catch(errorToast);
    });
    return h("span", { class: "gchip", title: g.path.join(" › ") }, g.name, x);
  });
  if (t.genres.length > max) {
    parts.push(h("span", { class: "gchip", title: t.genres.slice(max).map((g) => g.path.join(" › ")).join("\n") }, `+${t.genres.length - max}`));
  }
  if (t.rekordbox_genre_differs) {
    parts.push(h("span", {
      class: "gwarn",
      title: `Rekordbox has: ${t.rekordbox_genre}\nDJ Crate writes: ${t.genre_tag}\nIn Rekordbox, right-click the song → Reload Tag to update it.`,
    }, "⚠"));
  }
  // Inline suggestions for songs with no genre: one click to use. They never wrap onto a second line.
  for (const s of sug.slice(0, inline)) {
    const chip = h("button", {
      class: `gchip suggest${s.genre_id ? "" : " unknown"}${s.level === "artist" ? " artist" : ""}`,
      title: `${s.source}${s.level === "artist" ? " — the artist's genre, not necessarily this song's" : ""}\n` +
        (s.genre_id ? "Click to use it." : "Not in your Genre Rack yet — click for options."),
    }, s.genre_id ? `+ ${s.label}` : `${s.label} ?`);
    chip.addEventListener("click", (e) => {
      e.stopPropagation();
      chip.blur();
      if (s.genre_id) return assignGenres(t, [s.genre_id], onSaved).catch(errorToast);
      menu(chip, [
        { label: `Add “${s.label}” to the Genre Rack…`, fn: () => resolveUnknown(chip, t, s, "add", onSaved) },
        { label: "Make it an alias of…", fn: () => resolveUnknown(chip, t, s, "alias", onSaved) },
        "-",
        { label: `Ignore “${s.label}”`, fn: () => resolveUnknown(chip, t, s, "ignore", onSaved) },
      ]);
    });
    parts.push(chip);
  }
  return h("div", { class: "genre-cell" }, parts, btn);
}
