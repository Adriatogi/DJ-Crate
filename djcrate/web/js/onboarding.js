// Getting started: a small checklist for a new library. Each step ticks itself off from what's
// really done (Spotify connected, a playlist imported, a link picked, a song downloaded).
// A brand-new library is asked once whether it wants the walkthrough. Hide it any time;
// Settings → About → Setup guide brings it back.

import { post } from "./api.js";
import { openDocsAt } from "./docs.js";
import { emit } from "./store.js";
import { errorToast, h, modal } from "./ui.js";

let lastKey = "";
let forced = false;   // reopened from Settings
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
      text: "About 2 minutes, once. Needs Spotify Premium: you make a free developer app and paste its Client ID.",
      action: ["Set up", () => emit("show-tab", "settings")],
    },
    {
      done: ob.playlists > 0, title: "Import a playlist",
      text: "It becomes a crate with every song's title, artists, cover and length.",
      action: ["Import", () => { emit("show-tab", "crates"); emit("import-spotify"); }],
    },
    {
      done: ob.linked > 0, title: "Pick the YouTube links",
      text: "In Needs Link, click Find ▾ on a song (the green result matches Spotify's length), or Auto-fill links to fill the sure ones.",
      action: ["Needs Link", () => { emit("show-tab", "crates"); emit("select-view", "smart:needs_link"); }],
    },
    {
      done: ob.downloaded > 0, title: "Download",
      text: "Ready to Download → Download All. Each song becomes a tagged MP3 in your library folder, ready for Rekordbox.",
      action: ["Ready to Download", () => { emit("show-tab", "crates"); emit("select-view", "smart:to_download"); }],
    },
    {
      done: !!s.rekordbox?.path, optional: true, title: "Connect Rekordbox",
      text: "Optional: brings BPM, key and My Tags in, and sends your crates out as playlists.",
      action: ["How", () => openDocsAt("faq-xml")],
    },
  ];
}

export function showOnboarding() {
  forced = true;
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
  const required = list.filter((x) => !x.optional);
  const allDone = required.length && required.every((x) => x.done);
  const show = ok && (!s.onboarding?.dismissed) && (!allDone || forced);
  const key = JSON.stringify([show, list.map((x) => x.done)]);
  if (key === lastKey) return;
  lastKey = key;
  if (!show) { el?.remove(); return; }
  if (!el) {
    el = h("aside", { id: "onboarding", class: "onboarding" });
    document.body.append(el);
  }
  const next = list.find((x) => !x.done && !x.optional);
  const hide = async () => {
    forced = false;
    try { await post("/api/onboarding", { dismissed: true }); el.remove(); lastKey = ""; emit("poll"); } catch (e) { errorToast(e); }
  };
  el.replaceChildren(
    h("div", { class: "ob-head" },
      h("b", {}, allDone ? "You're set up" : "Getting started"),
      h("span", { class: "muted small" }, `${required.filter((x) => x.done).length} of ${required.length}`),
      h("button", { class: "icon-btn small", title: "Hide (Settings → About → Setup guide brings it back)", onclick: hide }, "✕")),
    h("ol", { class: "ob-steps" }, list.map((x) => h("li", { class: `ob-step${x.done ? " done" : ""}${x === next ? " next" : ""}` },
      h("span", { class: "ob-check" }, x.done ? "✓" : ""),
      h("div", { class: "ob-body" },
        h("div", { class: "ob-title" }, x.title, x.optional ? h("span", { class: "muted small" }, " · optional") : null),
        x === next || (x.optional && !x.done && !next) ? h("div", { class: "muted small" }, x.text) : null,
        (x === next || (x.optional && !x.done && !next)) && x.action
          ? h("button", { class: `btn small${x === next ? " primary" : ""}`, onclick: x.action[1] }, x.action[0]) : null)))),
    h("div", { class: "ob-foot" },
      h("a", { href: "#", onclick: (e) => { e.preventDefault(); openDocsAt("start"); } }, "Full guide in Docs"),
      allDone ? h("button", { class: "btn small", onclick: hide }, "Done") : null));
}
