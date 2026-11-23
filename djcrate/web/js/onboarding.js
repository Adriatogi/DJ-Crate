// Getting started: a small checklist for a new library. Each step ticks itself off from what's
// really done (Spotify connected, a playlist imported, a link picked, a song downloaded).
// A brand-new library is asked once whether it wants the walkthrough. Hide it any time;
// Settings → About → Setup guide brings it back.

import { post } from "./api.js";
import { openDocsAt } from "./docs.js";
import { emit } from "./store.js";
import { errorToast, h, modal } from "./ui.js";

let lastKey = "";
let asking = false;   // the "want a walkthrough?" popup is open

function ask() {
  asking = true;
  const answer = (dismissed) => post("/api/onboarding", { asked: true, dismissed })
    .then(() => { lastKey = ""; emit("poll"); }).catch(errorToast).finally(() => { asking = false; });
  let answered = false;
  modal({
    title: "Welcome to DJ Crate",
    body: h("div", {},
      h("p", {}, "Want a quick walkthrough? A small checklist takes you through connecting Spotify, importing your first playlist, picking links and downloading. Each step ticks itself off as you go."),
      h("p", { class: "muted small" }, "You can start it any time from Settings → About → Setup guide.")),
    buttons: [
      { label: "No thanks", fn: () => { answered = true; return answer(true); } },
      { label: "Show me", kind: "primary", fn: () => { answered = true; return answer(false); } },
    ],
    onClose: () => { if (!answered) answer(true); },
  });
}

function steps(s) {
  const ob = s.onboarding || {};
  return [
    { done: true, title: "Create a library", text: "Done. Your music and crates live in this folder." },
    {
      done: !!s.spotify?.connected, title: "Connect Spotify",
      text: "About 2 minutes, once (needs Spotify Premium). You make your own free Spotify developer app and paste its Client ID: Spotify only lets a hobby app approve ~25 people, so everyone gets their own. Settings walks you through it.",
      actions: [["Set up", () => emit("show-tab", "settings"), true]],
    },
    {
      done: ob.playlists > 0, title: "Import a playlist",
      text: "It becomes a crate with every song's title, artists, cover and length.",
      actions: [["Import", () => { emit("show-tab", "crates"); emit("import-spotify"); }, true]],
    },
    {
      done: ob.linked > 0, title: "Pick the YouTube links",
      text: "In Needs Link, click Find ▾ on a song (the green result matches Spotify's length), or Auto-fill links to fill the sure ones.",
      actions: [["Needs Link", () => { emit("show-tab", "crates"); emit("select-view", "smart:needs_link"); }, true]],
    },
    {
      done: ob.downloaded > 0, title: "Download",
      text: "Ready to Download → Download All. Each song becomes a tagged MP3 in your library folder, ready for Rekordbox.",
      actions: [["Ready to Download", () => { emit("show-tab", "crates"); emit("select-view", "smart:to_download"); }, true]],
    },
    {
      done: !!s.rekordbox?.path || !!ob.skipped_rekordbox, title: "Connect Rekordbox",
      text: "Brings Rekordbox's BPM, key and My Tags into DJ Crate, and can send your crates back as playlists.",
      sub: [
        "In Rekordbox, drag your library folder into Collection (that imports the MP3s).",
        "Rekordbox: File → Export Collection in xml format. Save it anywhere, e.g. in your library folder.",
        "DJ Crate: Settings → Rekordbox → pick that file. Export again whenever you change things in Rekordbox.",
        "Optional: turn on Send crates & sets there to get your crates in Rekordbox as playlists.",
      ],
      actions: [
        ["Open Settings", () => emit("show-tab", "settings"), true],
        ["Step by step", () => openDocsAt("faq-xml")],
        ["Skip, I don't use Rekordbox", () => post("/api/onboarding", { skipped_rekordbox: true }).then(() => emit("poll")).catch(errorToast)],
      ],
    },
  ];
}

export function showOnboarding() {
  lastKey = "";
  post("/api/onboarding", { asked: true, dismissed: false }).then(() => emit("poll")).catch(errorToast);
}

export function renderOnboarding(s) {
  let el = document.getElementById("onboarding");
  const ok = s.library?.status === "ok" && !s.library.read_only;
  if (ok && s.onboarding && !s.onboarding.asked && !asking && !document.querySelector(".backdrop")) {
    ask();
    return;
  }
  const list = ok ? steps(s) : [];
  const allDone = list.length && list.every((x) => x.done);
  const show = ok && !s.onboarding?.dismissed;
  const key = JSON.stringify([show, list.map((x) => x.done)]);
  if (key === lastKey) return;
  lastKey = key;
  if (!show) { el?.remove(); return; }
  if (!el) {
    el = h("aside", { id: "onboarding", class: "onboarding" });
    document.body.append(el);
  }
  const next = list.find((x) => !x.done);
  const hide = async () => {
    try { await post("/api/onboarding", { dismissed: true }); el.remove(); lastKey = ""; emit("poll"); } catch (e) { errorToast(e); }
  };
  el.replaceChildren(
    h("div", { class: "ob-head" },
      h("b", {}, allDone ? "You're set up" : "Getting started"),
      h("span", { class: "muted small" }, `${list.filter((x) => x.done).length} of ${list.length}`),
      h("button", { class: "icon-btn small", title: "Hide (Settings → About → Setup guide brings it back)", onclick: hide }, "✕")),
    h("ol", { class: "ob-steps" }, list.map((x) => h("li", { class: `ob-step${x.done ? " done" : ""}${x === next ? " next" : ""}` },
      h("span", { class: "ob-check" }, x.done ? "✓" : ""),
      h("div", { class: "ob-body" },
        h("div", { class: "ob-title" }, x.title),
        x === next ? h("div", { class: "muted small" }, x.text) : null,
        x === next && x.sub ? h("ol", { class: "ob-sub small" }, x.sub.map((t) => h("li", {}, t))) : null,
        x === next && x.actions ? h("div", { class: "ob-actions" }, x.actions.map(([label, fn, primary]) =>
          h("button", { class: `btn small${primary ? " primary" : ""}`, onclick: fn }, label))) : null)))),
    allDone ? h("p", { class: "small" }, "That's the whole loop: import, pick links, download, and Rekordbox. The Docs tab has everything else.") : null,
    h("div", { class: "ob-foot" },
      h("a", { href: "#", onclick: (e) => { e.preventDefault(); openDocsAt("start"); } }, "Full guide in Docs"),
      allDone ? h("button", { class: "btn small primary", onclick: hide }, "Done") : null));
}
