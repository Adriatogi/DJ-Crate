// Samples on the song card: what a song samples and what it was sampled in.
// Shared by the Crates song card and Cutting. Linked samples are library
// songs; the rest are text you can link later. "Find samples" asks Genius.

import { del, get, patch, post } from "./api.js";
import { songPicker } from "./pickers.js";
import { emit, readOnly } from "./store.js";
import { errorToast, h, menu, modal, toast } from "./ui.js";

const LIBRARY_HINT = "Want a sample in your library? Add it to a Spotify playlist, then import it (or Resync). " +
  "Your library always comes from Spotify, so everything stays in one place. Then link the sample here.";

const helpTip = (text) => h("span", { class: "help-q", tabindex: "0", dataset: { tip: text } }, "?");
const firstArtist = (artists) => (artists || "").split(",")[0].trim();
const openYouTube = (title, artist) => post("/api/open_url", {
  url: `https://www.youtube.com/results?search_query=${encodeURIComponent(`${firstArtist(artist)} ${plainTitle(title)}`)}`,
});
const openWhoSampled = (t) => post("/api/open_url", {
  url: `https://www.whosampled.com/search/?q=${encodeURIComponent(`${firstArtist(t.artists)} ${plainTitle(t.title)}`)}`,
});
const plainTitle = (title) => (title || "").replace(/\s*[([](feat|ft|with)\.?\s[^)\]]*[)\]]/i, "").replace(/\s+-\s+.*$/, "");

const splitLabel = (label) => { const [title, ...rest] = label.split(" — "); return [title, rest.join(" — ")]; };

async function patchSample(ctx, id, body) {
  try { await patch(`/api/samples/${id}`, body); await ctx.changed(); } catch (e) { errorToast(e); }
}

function sampleChip(ctx, s) {
  const linked = s.sample_track_id;
  const label = [s.song ? `${s.song.title} — ${s.song.artists}` : s.sample_label, s.text, s.relation === "interpolation" ? "interpolation" : null]
    .filter(Boolean).join(" · ");
  const stop = (fn) => (e) => { e.stopPropagation(); fn(e); };
  const more = h("button", { title: "More", onclick: stop(() => chipMenu(ctx, more, s)) }, "⋯");
  return h("span", {
    class: `sample-chip${linked ? " link" : ""}`, title: linked ? ctx.openTitle : "",
    onclick: linked ? () => ctx.openSong(linked) : null,
  },
    label,
    !linked && s.url ? h("button", { title: "Open on Genius", onclick: stop(() => post("/api/open_url", { url: s.url })) }, "↗") : null,
    !linked && s.match && !readOnly() ? h("button", {
      class: "chip-link", title: `It's in your library: link to “${s.match.title} — ${s.match.artists}”`,
      onclick: stop(() => patchSample(ctx, s.id, { sample_track_id: s.match.id })),
    }, `Link → ${s.match.title}`) : null,
    readOnly() ? null : more);
}

function chipMenu(ctx, anchor, s) {
  menu(anchor, [
    s.sample_track_id
      ? { label: "Unlink (keep as text)", fn: () => patchSample(ctx, s.id, { unlink: true }) }
      : {
        label: "Link to a library song…", fn: () => songPicker(anchor, {
          title: `Which song in your library is “${s.sample_label || s.text}”?`,
          onPick: (song) => patchSample(ctx, s.id, { sample_track_id: song.id }),
        }),
      },
    s.url ? { label: "Open on Genius ↗", fn: () => post("/api/open_url", { url: s.url }) } : null,
    !s.sample_track_id && s.sample_label ? { label: "Search YouTube ▶", fn: () => openYouTube(...splitLabel(s.sample_label)) } : null,
    "-",
    { label: "Remove", danger: true, fn: async () => { await del(`/api/samples/${s.id}`); await ctx.changed(); } },
  ].filter(Boolean));
}

const KINDS = [["samples", "Samples"], ["interpolates", "Interpolates"], ["sampled_in", "Sampled in"], ["interpolated_by", "Interpolated by"]];

function findSamples(ctx, t) {
  let data = null;
  const m = modal({ title: `Samples · ${t.title}`, wide: true, body: h("div", { class: "muted" }, "Asking Genius…"), buttons: [{ label: "Cancel" }] });
  const notConnected = (message) => {
    m.setBody(h("div", {},
      h("p", {}, message),
      h("p", { class: "muted small" }, "Genius is free: make an API client, copy its access token, and paste it in Settings → Genius. It takes a minute.")));
    m.setButtons([{ label: "Cancel" }, { label: "Open Settings", kind: "primary", fn: () => emit("show-tab", "settings") }]);
  };
  const load = async (query = "") => {
    m.setBody(h("div", { class: "muted" }, "Asking Genius…"));
    try {
      data = await get(`/api/tracks/${t.id}/genius${query}`);
    } catch (e) {
      if (e.code === "genius_not_set_up" || e.code === "genius_auth") return notConnected(e.message);
      m.setBody(h("p", {}, e.message));
      return m.setButtons([{ label: "Close" }]);
    }
    render();
  };
  const render = () => {
    const boxes = [];
    const g = data.genius;
    const others = data.alternatives.filter((a) => a.id !== g?.id);
    const pick = h("select", {},
      h("option", { value: "" }, g ? "Wrong song? Pick another…" : "Pick the right song…"),
      others.map((a) => h("option", { value: String(a.id) }, `${a.title} — ${a.artist}${a.year ? ` (${a.year})` : ""}`)));
    pick.addEventListener("change", () => pick.value && load(`?genius_id=${pick.value}`));
    const head = h("div", { class: "genius-head" },
      g ? h("div", { class: "grow" }, "Genius: ",
        h("a", { href: "#", onclick: (e) => { e.preventDefault(); post("/api/open_url", { url: g.url }); } },
          `${g.title} — ${g.artist}${g.year ? ` (${g.year})` : ""} ↗`))
        : h("div", { class: "grow" }, "Genius didn't find a clear match for this song."),
      others.length ? pick : null,
      h("button", { class: "btn small ghost", title: "Ask Genius again instead of using saved results", onclick: () => load("?refresh=1") }, "Search again"));
    const groups = KINDS.map(([kind, title]) => {
      const items = data.items.filter((i) => i.kind === kind)
        .sort((a, b) => !!b.library_song - !!a.library_song);
      if (!items.length) return null;
      return h("div", { class: "genius-group" },
        h("div", { class: "section-title" }, `${title} (${items.length})`),
        items.map((it) => {
          const box = h("input", {
            type: "checkbox", class: "sel", disabled: it.already_added,
            // "Sampled in" lists can be long, so only songs you have start ticked there.
            checked: it.already_added || kind === "samples" || kind === "interpolates" || !!it.library_song,
          });
          if (!box.disabled) boxes.push([box, it]);
          const badge = it.already_added ? h("span", { class: "gtag" }, "Added")
            : it.library_song ? h("span", { class: "gtag green" }, "In library")
            : h("span", { class: "gtag", title: "Not in your library, so it's saved as text. Add it to a Spotify playlist to get it in your library, then link it." }, "Text");
          const yt = h("button", {
            class: "btn small ghost", title: "Search YouTube for this song",
            onclick: (e) => { e.preventDefault(); e.stopPropagation(); openYouTube(it.title, it.artist); },
          }, "▶ YouTube");
          return h("label", { class: "genius-row" }, box,
            h("span", { class: "grow" }, `${it.title} — ${it.artist}`, it.year ? h("span", { class: "muted small" }, ` · ${it.year}`) : null),
            yt, badge);
        }));
    }).filter(Boolean);
    m.setBody(h("div", {},
      head,
      groups.length ? h("div", { class: "genius-list" }, groups)
        : h("p", { class: "muted" }, g ? ["Genius doesn't list any samples for this song. Try ",
          h("a", { href: "#", onclick: (e) => { e.preventDefault(); openWhoSampled(t); } }, "WhoSampled ↗"), "."] : "Pick the right song above."),
      h("p", { class: "muted small", style: { marginTop: "10px" } }, helpTip(LIBRARY_HINT),
        " Songs in your library get linked (dotted line on the board). The rest are saved as text, and you can link them later.")));
    m.setButtons([
      { label: "Cancel" },
      {
        label: "Add selected", kind: "primary", disabled: !boxes.length, fn: async () => {
          const items = boxes.filter(([b]) => b.checked).map(([, it]) => it);
          if (!items.length) return;
          const r = await post(`/api/tracks/${t.id}/genius/apply`, { items });
          toast(r.added ? `Added ${r.added} sample${r.added === 1 ? "" : "s"}.` : "Nothing new to add.", { type: "success" });
          await ctx.changed();
        },
      },
    ]);
  };
  load();
}

// t: the song. opts.openSong(id) opens a linked song; opts.onChange() runs
// after anything is added, linked or removed (e.g. to redraw the board).
export function samplesSection(t, { openSong, openTitle = "Open this song", onChange } = {}) {
  const wrap = h("div", { class: "samples-section" });
  const ctx = {
    openSong, openTitle,
    changed: async () => { await load(); await onChange?.(); },
  };
  const load = async () => {
    try { render(await get(`/api/tracks/${t.id}/samples`)); } catch (e) { if (e.code !== "library_unavailable") errorToast(e); }
  };
  const render = (d) => {
    const add = h("button", { class: "btn small", disabled: readOnly() }, "+ Add sample");
    add.onclick = () => songPicker(add, {
      title: `What does “${t.title}” sample?`, allowText: true,
      textHint: "Pick a song from your library to link it, or type anything (e.g. “drum break from Impeach the President”) and press Enter.",
      onPick: async (song, text) => {
        try {
          await post(`/api/tracks/${t.id}/samples`, song ? { sample_track_id: song.id } : { text });
          await ctx.changed();
        } catch (e) { errorToast(e); }
      },
    });
    const find = h("button", { class: "btn small", disabled: readOnly(), title: "Look up what this song samples on Genius", onclick: () => findSamples(ctx, t) }, "Find samples");
    const who = h("button", { class: "btn small ghost", title: "Search WhoSampled in your browser", onclick: () => openWhoSampled(t) }, "WhoSampled ↗");
    wrap.replaceChildren(...[
      h("div", { class: "section-title" }, "Samples ", helpTip(LIBRARY_HINT)),
      h("div", {}, d.samples.length ? d.samples.map((s) => sampleChip(ctx, s))
        : h("div", { class: "muted small", style: { marginBottom: "6px" } }, "Nothing yet.")),
      h("div", { class: "row gap", style: { flexWrap: "wrap" } }, add, find, who),
      d.sampled_by.length ? h("div", { class: "section-title" }, "Sampled by") : null,
      d.sampled_by.length ? h("div", {}, d.sampled_by.map((s) => s.song
        ? h("span", { class: "sample-chip link", title: openTitle, onclick: () => openSong(s.track_id) }, `${s.song.title} — ${s.song.artists}`)
        : sampleChip(ctx, s))) : null,
    ].filter(Boolean));
  };
  if (t.samples && t.sampled_by) render(t); else load();
  return wrap;
}
