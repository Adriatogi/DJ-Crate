// Genre Rack tab. Not a second crate list: genres are cards you drill into
// (House › Deep House), each showing its sub-genres, artists and songs. The
// song table is the Crates one, moved in here while this tab is open.

import { del, get, patch, post } from "./api.js";
import { refreshTracks, selectView } from "./crates.js";
import { addToRackDialog, flattenGenres, loadGenres, openGenrePicker, titleCase } from "./genrepicker.js";
import { drag } from "./sidebar.js";
import { closeDetail } from "./detail.js";
import { emit, flatCrates, on, store } from "./store.js";
import { confirmDialog, debounce, errorToast, fill, h, menu, modal, promptDialog, toast } from "./ui.js";

let sel = localStorage.getItem("djc.rackSel") || "root";   // "root", "none" (Unsorted), or a genre id
let dom = null;
let active = false;
let savedView = null;
let mbQuery = "";

const selId = () => (/^\d+$/.test(sel) ? Number(sel) : null);
const viewFor = (s) => (s === "none" ? "genre:none" : `genre:${s}`);
const findGenre = (id) => flattenGenres().find((g) => g.id === id) || null;

function ensureDom() {
  if (dom) return dom;
  const root = document.getElementById("view-rack");
  dom = {
    mine: h("div", { id: "my-genres" }),
    mb: h("details", { id: "mb-genres" }),
    head: h("div", { id: "rack-head" }),
    cards: h("div", { id: "rack-cards" }),
    artists: h("div", { id: "rack-artists" }),
    addPanel: h("div", { id: "rack-add" }),
    addFoot: h("div", { id: "rack-addfoot" }),
    body: h("div", { id: "rack-body" }),
    rootDrop: h("div", { id: "rack-root-drop" }, "Drop here to move it to the top level"),
  };
  dom.rootDrop.addEventListener("dragover", (e) => { if (drag.type === "genre") { e.preventDefault(); dom.rootDrop.classList.add("drop-target"); } });
  dom.rootDrop.addEventListener("dragleave", () => dom.rootDrop.classList.remove("drop-target"));
  dom.rootDrop.addEventListener("drop", async (e) => {
    e.preventDefault();
    dom.rootDrop.classList.remove("drop-target");
    if (drag.type === "genre") await moveGenre(drag.id, null);
  });
  dom.mb.open = true; // always expanded; the summary still folds it away
  dom.mb.append(h("summary", {}, "MusicBrainz genres"));
  dom.mb.addEventListener("toggle", () => { if (dom.mb.open) loadMb(""); });
  root.append(
    h("aside", { id: "rack-side" },
      h("div", { class: "side-actions" },
        h("button", { class: "btn primary block", onclick: () => addToRackDialog("", async (g) => { await afterChange(); go(g.id); }, { parentId: selId() }) }, "+ Genre"),
        h("button", { class: "btn small block", onclick: () => bringIn() }, "Bring in genres…")),
      h("div", { class: "muted small", style: { padding: "10px 14px" } },
        "Your Genre Rack is the only list of genres. Songs get genres from it, and DJ Crate writes them into the MP3 for Rekordbox."),
      dom.mine, dom.mb),
    h("section", { id: "rack-center" }, dom.head, dom.rootDrop, dom.cards, dom.artists, dom.addPanel, dom.body, dom.addFoot));
  return dom;
}

// ------------------------------------------------ entering / leaving

export async function showRack() {
  ensureDom();
  if (!active) {
    active = true;
    savedView = store.view.startsWith("genre:") ? savedView : store.view;
    dom.body.append(document.getElementById("main-pane"), document.getElementById("detail"));
    closeDetail();
  }
  try {
    await loadGenres();
  } catch (e) {
    if (e.code !== "library_unavailable") errorToast(e);
    return;
  }
  if (selId() !== null && !findGenre(selId())) sel = "root";
  if (dom.mb.open) loadMb(mbQuery);
  await go(sel);
}

export function leaveRack() {
  if (!active) return;
  active = false;
  document.getElementById("view-crates").append(document.getElementById("main-pane"), document.getElementById("detail"));
  closeDetail();
  selectView(savedView && !savedView.startsWith("genre:") ? savedView : "smart:all");
}

// Go to the rack root, Unsorted, or a genre.
async function go(target) {
  sel = String(target);
  localStorage.setItem("djc.rackSel", sel);
  store.rack.artist = "";
  const g = selId() !== null ? findGenre(selId()) : null;
  store.rack.others = !!g && g.count === 0; // an empty genre opens straight on "songs you could add"
  store.rack.name = sel === "none" ? "Unsorted — songs with no genre" : g?.path.join(" › ") || "Genre";
  renderMine();
  renderHead();
  renderCards();
  const showSongs = sel === "none" || !!g;
  applyMode();
  dom.artists.hidden = true;
  if (showSongs) {
    selectView(viewFor(sel));
    loadArtists();
  }
}

async function afterChange() {
  try { await loadGenres(); } catch (e) { return errorToast(e); }
  if (selId() !== null && !findGenre(selId())) sel = "root";
  emit("refresh-sidebar");
  emit("genres-changed");
  await go(sel);
}

// ------------------------------------------------ left rail: my genres + MusicBrainz genres

// A compact outline of your whole rack, so any genre is one click away.
function renderMine() {
  const data = store.genres;
  if (!data) return;
  const row = (g, depth) => [
    h("div", {
      class: `mine-row${sel === String(g.id) ? " active" : ""}`, style: { paddingLeft: `${10 + depth * 14}px` },
      title: g.path ? g.path.join(" › ") : g.name, onclick: () => go(g.id),
    }, h("span", { class: "grow" }, g.name), h("span", { class: "count" }, g.count),
    h("button", { class: "icon-btn small mine-add", title: `Add a sub-genre inside ${g.name}`, onclick: (e) => { e.stopPropagation(); newSubGenre(g); } }, "+"),
    h("button", { class: "icon-btn small mine-add", title: "More (move, rename, merge…)", onclick: (e) => { e.stopPropagation(); genreMenu(e.currentTarget, g); } }, "⋯")),
    ...g.children.flatMap((c) => row(c, depth + 1)),
  ];
  dom.mine.replaceChildren(
    h("div", { class: "rail-title" }, "My genres"),
    ...(data.tree.length ? data.tree.flatMap((g) => row(g, 0)) : [h("div", { class: "muted small", style: { padding: "4px 14px" } }, "None yet — add one with + Genre.")]),
    h("div", { class: `mine-row unsorted${sel === "none" ? " active" : ""}`, onclick: () => go("none") },
      h("span", { class: "grow" }, "⚑ Unsorted"), h("span", { class: "count" }, data.unsorted)));
}

async function loadMb(q) {
  mbQuery = q;
  if (!dom.mb.querySelector(".mb-body")) {
    dom.mb.append(h("div", { class: "mb-body" },
      h("input", { class: "mb-search", placeholder: "Search all of MusicBrainz's genres…", oninput: debounce((e) => loadMb(e.target.value.trim()), 200) }),
      h("div", { class: "mb-meta muted small" }),
      h("div", { class: "mb-list" })));
  }
  const body = dom.mb.querySelector(".mb-body");
  const meta = body.querySelector(".mb-meta");
  const list = body.querySelector(".mb-list");
  meta.textContent = "Loading…";
  try {
    const r = await get(`/api/genres/musicbrainz?q=${encodeURIComponent(q)}`);
    if (q !== mbQuery) return; // a newer search is on its way
    if (r.error) { meta.textContent = r.error; list.replaceChildren(); return; }
    if (r.mode === "needed") {
      const gl = store.status?.genres;
      meta.textContent = r.genres.length
        ? "Genres MusicBrainz put your songs or their artists under that aren't in your rack yet. Search to find any other."
        : gl?.on && gl?.pending ? `Nothing needed yet — still looking up songs (${gl.pending} to go).` : "Nothing needed right now. Search to browse all of MusicBrainz's genres.";
      list.replaceChildren(...r.genres.map((g) => h("div", { class: "mb-row" },
        h("span", { class: "grow" }, g.name),
        h("span", { class: "muted small", title: g.level === "artist" ? "Only as the artist's genre" : "Listed for the song itself" },
          `${g.songs} song${g.songs === 1 ? "" : "s"}${g.level === "artist" ? " · artist" : ""}`),
        h("button", { class: "btn small", title: "Add it to your Genre Rack", onclick: () => addToRackDialog(g.name, async () => { await afterChange(); loadMb(mbQuery); }) }, "+ Add"),
        h("button", {
          class: "icon-btn small", title: "Not needed — hide it",
          onclick: async () => { await post("/api/genres/ignore", { name: g.name }); loadMb(mbQuery); emit("refresh-tracks"); },
        }, "✕"))));
      return;
    }
    meta.textContent = `${r.matches} of ${r.total} match${r.matches > 150 ? " (showing 150)" : ""}`;
    list.replaceChildren(...r.genres.map((g) => h("div", { class: "mb-row" },
      h("span", { class: "grow" }, g.name),
      g.genre_id
        ? h("span", { class: "muted small", title: "Already in your Genre Rack (by name or alias)" }, "✓ in rack")
        : h("button", {
          class: "btn small", title: "Add it to your Genre Rack",
          onclick: () => addToRackDialog(titleCase(g.name), async () => { await afterChange(); loadMb(q); }),
        }, "+ Add"))));
  } catch (e) { meta.textContent = e.message; }
}

// ------------------------------------------------ header (breadcrumb) + cards

function crumbs() {
  const out = [h("a", { href: "#", onclick: (e) => { e.preventDefault(); go("root"); } }, "Genre Rack")];
  if (sel === "none") {
    out.push(h("span", { class: "sep" }, "›"), h("span", { class: "cur" }, "Unsorted"));
  } else if (selId() !== null) {
    const g = findGenre(selId());
    g?.pathIds.forEach((id, i) => {
      out.push(h("span", { class: "sep" }, "›"));
      out.push(id === g.id ? h("span", { class: "cur" }, g.path[i]) : h("a", { href: "#", onclick: (e) => { e.preventDefault(); go(id); } }, g.path[i]));
    });
  }
  return h("div", { class: "crumbs" }, out);
}

function renderHead() {
  const g = selId() !== null ? findGenre(selId()) : null;
  const crate = g ? genreCrate(g) : null;
  fill(dom.head,
    h("div", { class: "rack-title" }, crumbs(),
      h("div", { class: "row gap", style: { marginTop: "4px" } },
        g ? h("button", { class: "btn small", title: `Add a genre inside ${g.name}`, onclick: () => newSubGenre(g) }, "+ Sub-genre") : null,
        h("div", { class: "muted small", id: "rack-meta" },
          sel === "root" ? "Click a genre to open it. Drag genres onto each other to nest them." : ""))),
    h("div", { class: "spacer" }),
    g && g.children.length ? h("label", { class: "row gap small", title: "Show songs of its sub-genres too" },
      h("input", { type: "checkbox", checked: store.rack.sub, onchange: (e) => { store.rack.sub = e.target.checked; refreshTracks(); loadArtists(); } }),
      "Include sub-genres") : null,
    g ? (crate
      ? h("button", { class: "btn", title: "This crate follows the genre", onclick: () => { emit("show-tab", "crates"); emit("select-view", `crate:${crate.id}`); } }, "🏷 Show its crate")
      : h("button", { class: "btn", title: "A crate that fills itself with this genre's songs", onclick: () => makeCrate(g) }, "Make crate")) : null,
    g ? h("button", { class: "icon-btn", title: "More", onclick: (e) => genreMenu(e.currentTarget, g) }, "⋯") : null);
}

// A short how-to on the rack's home page, so none of this has to be discovered by accident.
function tips() {
  return h("details", { class: "rack-tips", open: true },
    h("summary", {}, "How the Genre Rack works"),
    h("ul", {},
      h("li", {}, h("b", {}, "Open a genre"), " by clicking its card (or its name under My genres on the left)."),
      h("li", {}, h("b", {}, "Add a sub-genre:"), " open the genre and press ", h("b", {}, "+ Sub-genre"), ", or hover its name under My genres and click ", h("b", {}, "+"), "."),
      h("li", {}, h("b", {}, "Make an existing genre a sub-genre of another:"), " drag its card onto the other card, or use its ", h("b", {}, "⋯"),
        " menu → ", h("b", {}, "Move inside another genre…"), ". ⋯ → ", h("b", {}, "Move to the top level"), " takes it back out."),
      h("li", {}, h("b", {}, "Put songs in a genre:"), " open it and press ", h("b", {}, "+ Add songs"), " and click songs to add them (filter by genre with “Show”), drag songs from Crates onto its card, or use the ",
        h("b", {}, "+"), " square in a song's Genre column."),
      h("li", {}, h("b", {}, "Take a genre off a song:"), " hover the genre in its Genre column and click the ✕."),
      h("li", {}, h("b", {}, "Two spellings of one genre?"), " ⋯ → ", h("b", {}, "Merge into…"), ", or ⋯ → ", h("b", {}, "Other spellings"), " so names from Rekordbox and MusicBrainz match it."),
      h("li", {}, h("b", {}, "Make a crate from a genre:"), " open it and press ", h("b", {}, "Make crate"), " — it fills itself as you tag songs.")));
}

function renderCards() {
  const data = store.genres;
  if (!data) return;
  let nodes = [];
  if (sel === "root") nodes = data.tree;
  else if (selId() !== null) nodes = findGenre(selId())?.children || [];
  const cards = nodes.map(card);
  if (sel === "root") {
    if (data.unsorted) cards.push(unsortedCard(data.unsorted));
    if (!data.tree.length) {
      dom.cards.className = "";
      dom.cards.hidden = false;
      dom.cards.replaceChildren(h("div", { class: "rack-empty" },
        h("h3", {}, "Your Genre Rack is empty"),
        h("p", { class: "muted" }, "Add your first genre with + Genre, browse MusicBrainz's list on the left, or use Bring in genres… to start from the ones already in Rekordbox and your MP3s."),
        data.unsorted ? unsortedCard(data.unsorted) : null, tips()));
      return;
    }
  }
  if (sel === "root") cards.push(tips());
  dom.cards.hidden = !cards.length;
  dom.cards.className = sel === "root" ? "grid-root" : "grid-sub";
  dom.cards.replaceChildren(...cards);
}

function unsortedCard(n) {
  return h("div", { class: "gcard unsorted", onclick: () => go("none") },
    h("div", { class: "gname" }, "⚑ Unsorted"),
    h("div", { class: "gmeta" }, `${n} song${n === 1 ? "" : "s"} with no genre`));
}

function card(g) {
  const sub = g.children.length;
  const el = h("div", { class: "gcard", draggable: "true", dataset: { id: g.id }, onclick: () => go(g.id) },
    g.covers.length ? h("div", { class: "gcovers" }, g.covers.map((u) => h("img", { src: u, alt: "", loading: "lazy" }))) : h("div", { class: "gcovers empty" }, "♪"),
    h("div", { class: "gname" }, g.name),
    h("div", { class: "gmeta" }, `${g.count} song${g.count === 1 ? "" : "s"}${sub ? ` · ${sub} sub-genre${sub === 1 ? "" : "s"}` : ""}`),
    g.top_artists.length ? h("div", { class: "gartists", title: g.top_artists.join(", ") }, g.top_artists.join(" · ")) : null,
    h("button", { class: "icon-btn small more", title: "More", onclick: (e) => { e.stopPropagation(); genreMenu(e.currentTarget, g); } }, "⋯"));
  el.addEventListener("contextmenu", (e) => { e.preventDefault(); genreMenu(el, g); });
  wireDnD(el, g);
  return el;
}

function wireDnD(el, g) {
  el.addEventListener("dragstart", (e) => {
    drag.type = "genre";
    drag.id = g.id;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", g.name);
    e.stopPropagation();
    if (selId() !== null) dom.rootDrop.style.display = "block";
  });
  el.addEventListener("dragend", () => {
    drag.type = null;
    dom.rootDrop.style.display = "none";
    document.querySelectorAll("#rack-cards .drop-target").forEach((x) => x.classList.remove("drop-target"));
  });
  el.addEventListener("dragover", (e) => {
    if ((drag.type === "genre" && drag.id !== g.id) || drag.type === "tracks") {
      e.preventDefault();
      el.classList.add("drop-target");
    }
  });
  el.addEventListener("dragleave", () => el.classList.remove("drop-target"));
  el.addEventListener("drop", async (e) => {
    e.preventDefault();
    e.stopPropagation();
    el.classList.remove("drop-target");
    try {
      if (drag.type === "genre" && drag.id !== g.id) {
        await moveGenre(drag.id, g.id);
      } else if (drag.type === "tracks") {
        const ids = drag.ids;
        const r = await post("/api/tracks/genres", { ids, genre_id: g.id, action: "add" });
        toast(`Added “${g.name}” to ${ids.length} song${ids.length === 1 ? "" : "s"}.` +
          (r.retagged ? ` Updated the genre tag on ${r.retagged} file${r.retagged === 1 ? "" : "s"}.` : ""), { type: "success" });
        await afterChange();
      }
    } catch (err) { errorToast(err); }
    drag.type = null;
  });
}

async function moveGenre(id, parentId) {
  try {
    tagToast(await patch(`/api/genres/${id}`, { move: true, parent_id: parentId, index: null }));
    await afterChange();
  } catch (e) { errorToast(e); }
}

function tagToast(r) {
  if (r?.retagged) toast(`Updated the genre tag on ${r.retagged} file${r.retagged === 1 ? "" : "s"}.`, { type: "info" });
  (r?.tag_warnings || []).forEach((w) => toast(w, { type: "warn", timeout: 8000 }));
}

// ------------------------------------------------ genre actions

function genreCrate(g) {
  return flatCrates().find((c) => c.genre_id === g.id) || null;
}

function newSubGenre(g) {
  addToRackDialog("", async (n) => { await afterChange(); go(n.id); }, { parentId: g.id, title: `New sub-genre of ${g.name}` });
}

function genreMenu(anchor, g) {
  const crate = genreCrate(g);
  menu(anchor, [
    { label: "New sub-genre…", fn: () => newSubGenre(g) },
    { label: "Rename…", fn: () => renameGenre(g) },
    { label: "Move inside another genre…", fn: () => openGenrePicker(anchor, {
      pickOne: true,
      onPick: async (target) => {
        if (target.id === g.id) return;
        await moveGenre(g.id, target.id);
      },
    }) },
    ...(g.parent_id != null ? [{ label: "Move to the top level", fn: () => moveGenre(g.id, null) }] : []),
    { label: "Other spellings (aliases)…", fn: () => aliasesDialog(g) },
    "-",
    crate
      ? { label: "Show its crate", fn: () => { emit("show-tab", "crates"); emit("select-view", `crate:${crate.id}`); } }
      : { label: "Make a live crate from this genre", fn: () => makeCrate(g) },
    { label: "Merge into…", fn: () => mergeInto(g, anchor) },
    "-",
    { label: "Delete…", danger: true, fn: () => deleteGenre(g) },
  ], { align: "right" });
}

async function renameGenre(g) {
  const name = await promptDialog("Rename genre", { value: g.name, ok: "Rename" });
  if (!name || name === g.name) return;
  try {
    tagToast(await patch(`/api/genres/${g.id}`, { name }));
    await afterChange();
  } catch (e) {
    if (e.code === "genre_exists" && e.extra?.existing_id && e.extra.existing_id !== g.id) {
      const target = findGenre(e.extra.existing_id);
      const ok = await confirmDialog(`Merge into “${target?.name}”?`,
        `${e.message}\n\nMerge “${g.name}” into it? Its songs and sub-genres move over, and “${g.name}” becomes another spelling of it.`, { ok: "Merge" });
      if (ok) await doMerge(g, e.extra.existing_id);
    } else errorToast(e);
  }
}

async function doMerge(g, into) {
  try {
    tagToast(await post(`/api/genres/${g.id}/merge`, { into }));
    if (sel === String(g.id)) sel = String(into);
    await afterChange();
  } catch (e) { errorToast(e); }
}

function mergeInto(g, anchor) {
  openGenrePicker(anchor, {
    pickOne: true,
    onPick: async (target) => {
      if (target.id === g.id) return;
      const ok = await confirmDialog(`Merge “${g.name}” into “${target.name}”?`,
        `Its songs and sub-genres move to “${target.name}”, and “${g.name}” becomes another spelling of it.`, { ok: "Merge" });
      if (ok) await doMerge(g, target.id);
    },
  });
}

async function deleteGenre(g) {
  const ok = await confirmDialog(`Delete “${g.name}”?`,
    `${g.own} song${g.own === 1 ? "" : "s"} will lose this genre (their MP3 genre tag is updated).` +
    `${g.children.length ? "\nIts sub-genres move up one level." : ""}\nSongs and files are never deleted.`,
    { ok: "Delete", danger: true });
  if (!ok) return;
  try {
    tagToast(await del(`/api/genres/${g.id}`));
    await afterChange();
    toast(`Deleted “${g.name}”.`);
  } catch (e) { errorToast(e); }
}

async function makeCrate(g) {
  try {
    await post(`/api/genres/${g.id}/crate`, { parent_id: null });
    emit("refresh-sidebar");
    toast(`Made a crate for “${g.name}”. It fills itself as you tag songs, and goes to Rekordbox like your other crates.`, { type: "success", timeout: 8000 });
    renderHead();
  } catch (e) { errorToast(e); }
}

function aliasesDialog(g) {
  const list = h("div", {});
  const input = h("input", { placeholder: "Another spelling, e.g. for “Hip-Hop/Rap” add “Hip-Hop”, then “Rap”", style: { width: "100%" } });
  const render = () => {
    const node = findGenre(g.id);
    list.replaceChildren(...(node.aliases.length
      ? node.aliases.map((a) => h("div", { class: "row", style: { justifyContent: "space-between", padding: "3px 0" } },
        h("span", {}, a),
        h("button", { class: "icon-btn small", title: "Remove", onclick: async () => { await del(`/api/genres/aliases/${encodeURIComponent(a)}`); await loadGenres(); render(); } }, "✕")))
      : [h("div", { class: "muted small" }, "None yet.")]));
  };
  const add = async () => {
    if (!input.value.trim()) return;
    try {
      await post(`/api/genres/${g.id}/aliases`, { name: input.value });
      input.value = "";
      await loadGenres();
      render();
      emit("refresh-tracks");
    } catch (e) { errorToast(e); }
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
  modal({
    title: `Other spellings of “${g.name}”`,
    body: h("div", {},
      h("p", { class: "muted" }, "Names from Rekordbox, your MP3s or MusicBrainz that mean this genre. Case and hyphens never matter (“Hip-Hop” = “hip hop”)."),
      list, h("div", { class: "field", style: { marginTop: "10px" } }, input)),
    buttons: [{ label: "Done", kind: "primary", fn: async () => { await afterChange(); } }],
  });
  render();
}

// ------------------------------------------------ artists of the open genre

// "+ Add songs": a light list of the songs that are NOT in this genre. Click a song to add it
// (click again to undo). No downloads, links or flags — just enough to recognise the song.
const add = { q: "", filter: localStorage.getItem("djc.addFilter") || "none", seq: 0, built: false }; // filter: "all", "none", or a genre id

function applyMode() {
  const g = selId() !== null ? findGenre(selId()) : null;
  const adding = !!g && store.rack.others;
  dom.addPanel.hidden = !adding;
  dom.body.hidden = !(sel === "none" || !!g) || adding;
  dom.addFoot.hidden = !g || adding;
  if (g && !adding) {
    dom.addFoot.replaceChildren(h("button", {
      class: "btn primary big", title: "Pick songs from your library to add to this genre",
      onclick: () => setAdding(true),
    }, `+ Add songs to ${g.name}`));
  }
  if (adding) openAddPanel(g);
}

function setAdding(on) {
  store.rack.others = on;
  store.rack.artist = "";
  applyMode();
  refreshTracks();
  loadArtists();
}

function openAddPanel(g) {
  if (!add.built) {
    add.built = true;
    add.search = h("input", { type: "search", placeholder: "Search your songs…", style: { width: "260px" } });
    add.search.addEventListener("input", debounce(() => { add.q = add.search.value; loadAddList(); }, 200));
    add.filterSel = h("select", { title: "Which songs to list" });
    add.filterSel.addEventListener("change", () => {
      add.filter = add.filterSel.value;
      localStorage.setItem("djc.addFilter", add.filter);
      loadAddList();
    });
    add.hint = h("span", { class: "muted small" });
    add.back = h("button", { class: "btn", onclick: () => setAdding(false) }, "← Back to its songs");
    add.list = h("div", { class: "add-list" });
    dom.addPanel.append(
      h("div", { class: "add-controls" }, add.back, add.search,
        h("label", { class: "row gap small" }, "Show", add.filterSel),
        h("div", { class: "spacer" }), add.hint),
      add.list);
  }
  add.hint.textContent = `Click a song to add it to “${g.name}”`;
  fillFilter(g);
  add.search.value = add.q = "";
  loadAddList();
}

// Which songs the list shows: songs with no genre yet, every song, or the songs of any one genre.
function fillFilter(g) {
  const opts = [["none", "Songs with no genre yet"], ["all", "All songs not in this genre"],
    ...flattenGenres().filter((x) => x.id !== g.id).map((x) => [String(x.id), `Songs in ${x.path.join(" › ")}`])];
  if (!opts.some(([v]) => v === add.filter)) add.filter = "none";
  add.filterSel.replaceChildren(...opts.map(([v, label]) => h("option", { value: v, selected: v === add.filter }, label)));
}

async function loadAddList() {
  const g = selId() !== null ? findGenre(selId()) : null;
  if (!g || !store.rack.others) return;
  const seq = ++add.seq;
  try {
    const r = await get(`/api/tracks?view=genre:${g.id}&others=1&nogenre=${add.filter === "none" ? 1 : 0}&ingenre=${/^\d+$/.test(add.filter) ? add.filter : 0}&q=${encodeURIComponent(add.q)}`);
    if (seq !== add.seq) return;
    add.list.replaceChildren(...(r.tracks.length
      ? r.tracks.map((t) => addRow(t, g))
      : [h("div", { class: "rack-empty" }, h("h3", {}, add.q ? "No songs match" : add.filter === "none" ? "No songs without a genre left" : "Nothing to add from here"),
        !add.q && add.filter !== "all" ? h("p", { class: "muted" }, "Change “Show” above to see other songs.") : null)]));
  } catch (e) {
    if (e.code !== "library_unavailable") errorToast(e);
  }
}

function addRow(t, g) {
  let on = false;
  const mark = h("span", { class: "add-mark" }, "+ Add");
  const row = h("div", { class: "add-row", title: `Add to ${g.name}` },
    t.cover_url ? h("img", { class: "cover", src: t.cover_url, alt: "", loading: "lazy" }) : h("div", { class: "cover placeholder" }, "♪"),
    h("div", { class: "grow add-text" }, h("div", { class: "t-title" }, t.title), h("div", { class: "muted small" }, t.artists)),
    t.genres.length ? h("div", { class: "add-genres" }, t.genres.slice(0, 3).map((x) => h("span", { class: "gchip", title: x.path.join(" › ") }, x.name))) : null,
    mark);
  row.addEventListener("click", async () => {
    row.classList.add("busy");
    try {
      const r = await post("/api/tracks/genres", { ids: [t.id], genre_id: g.id, action: on ? "remove" : "add" });
      on = !on;
      row.classList.toggle("added", on);
      row.title = on ? "Added — click to undo" : `Add to ${g.name}`;
      mark.textContent = on ? "✓ Added" : "+ Add";
      (r.tag_warnings || []).forEach((w) => toast(w, { type: "warn", timeout: 8000 }));
      emit("refresh-sidebar");
      emit("genres-changed");
    } catch (e) { errorToast(e); } finally { row.classList.remove("busy"); }
  });
  return row;
}

async function loadArtists() {
  if (!active || !dom || !(sel === "none" || selId() !== null)) return;
  if (store.rack.others) { dom.artists.hidden = true; return; }
  const view = viewFor(sel);
  try {
    const r = await get(`/api/genre_artists?view=${encodeURIComponent(view)}&sub=${store.rack.sub ? 1 : 0}`);
    if (viewFor(sel) !== view) return;
    const meta = document.getElementById("rack-meta");
    if (meta) meta.textContent = `${r.songs} song${r.songs === 1 ? "" : "s"} · ${r.artists.length} artist${r.artists.length === 1 ? "" : "s"}`;
    dom.artists.hidden = !r.artists.length;
    dom.artists.replaceChildren(
      h("span", { class: "muted small", style: { marginRight: "6px", flexShrink: 0 } }, "Artists"),
      ...r.artists.slice(0, 200).map((a) => h("button", {
        class: `artist-chip${store.rack.artist === a.name ? " on" : ""}`,
        title: store.rack.artist === a.name ? "Click to show all songs again" : `Show only ${a.name}'s songs here`,
        onclick: () => {
          store.rack.artist = store.rack.artist === a.name ? "" : a.name;
          refreshTracks();
          loadArtists();
        },
      }, a.name, h("span", { class: "muted" }, ` ${a.count}`))));
  } catch (e) {
    if (e.code !== "library_unavailable") console.warn(e);
  }
}

// ------------------------------------------------ bring in genres (from Rekordbox / MP3s)

async function bringIn() {
  const body = h("div", {});
  const render = async () => {
    let data;
    try { data = await get("/api/genres/incoming"); } catch (e) { return errorToast(e); }
    await loadGenres();
    if (!data.items.length) {
      body.replaceChildren(h("p", {}, "No genre names found yet."),
        h("p", { class: "muted small" }, "They come from the Genre field in your Rekordbox XML (Settings → Rekordbox → Choose XML file…) and from MP3s you import from a folder."));
      return;
    }
    const todo = data.items.filter((i) => !i.genre_id && !i.ignored).length;
    body.replaceChildren(
      h("p", { class: "muted" }, "Every genre name found in Rekordbox and your MP3s. Match each to your Genre Rack, then apply — nothing changes until you press Apply."),
      h("div", { class: "muted small", style: { marginBottom: "6px" } }, todo ? `${todo} not in your rack yet` : "All matched or ignored ✓"),
      h("div", { class: "list incoming" }, data.items.slice(0, 300).map((it) => {
        const status = it.genre_id
          ? h("span", { class: "gchip" }, `→ ${it.path.join(" › ")}`)
          : it.ignored ? h("span", { class: "muted" }, "ignored") : null;
        const actions = it.genre_id ? [] : it.ignored
          ? [h("button", { class: "btn small", onclick: async () => { await post("/api/genres/unignore", { name: it.name }); render(); } }, "Undo")]
          : (() => {
            const aliasBtn = h("button", { class: "btn small" }, "Alias of…");
            aliasBtn.onclick = () => openGenrePicker(aliasBtn, {
              pickOne: true,
              onPick: async (g) => { await post(`/api/genres/${g.id}/aliases`, { name: it.name }); render(); },
            });
            return [
              h("button", { class: "btn small", onclick: () => addToRackDialog(it.name, () => render()) }, "Add to rack…"),
              aliasBtn,
              h("button", { class: "btn small", onclick: async () => { await post("/api/genres/ignore", { name: it.name }); render(); } }, "Ignore"),
            ];
          })();
        return h("div", { class: "item" },
          h("div", { class: "grow" },
            h("div", { class: "t-title" }, it.name),
            h("div", { class: "t-sub" }, `${it.songs} song${it.songs === 1 ? "" : "s"} · ${it.unsorted} without a genre · ${it.sources.join(" + ")}`)),
          status, actions);
      })));
  };
  modal({
    title: "Bring in genres", wide: true, body,
    buttons: [
      { label: "Close", fn: async () => { await afterChange(); } },
      {
        label: "Apply to songs without a genre", kind: "primary",
        fn: async () => {
          const r = await post("/api/genres/incoming/apply");
          toast(r.assigned ? `Gave genres to ${r.assigned} song${r.assigned === 1 ? "" : "s"}.` : "No songs needed a genre from these names.",
            { type: r.assigned ? "success" : "info" });
          tagToast(r);
          await afterChange();
        },
      },
    ],
  });
  render();
}

// ------------------------------------------------ wiring

export function initRack() {
  on("genres-changed", debounce(async () => {
    if (!active) return;
    try { await loadGenres(); } catch { return; }
    if (dom.mb.open) loadMb(mbQuery);
    renderMine();
    renderHead();
    renderCards();
    loadArtists();
  }, 200));
  on("tracks-loaded", () => { if (active) loadArtists(); });
  on("rack-show", (id) => {
    sel = String(id);
    localStorage.setItem("djc.rackSel", sel);
    emit("show-tab", "rack");
  });
}
