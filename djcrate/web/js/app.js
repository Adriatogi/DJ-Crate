// App shell: tabs, status polling, status strip, banners, startup/crash
// screens, Activity panel, keyboard shortcuts.

import { get, native, post } from "./api.js";
import { initCrates, refreshSidebar, refreshTracks, selectView } from "./crates.js";
import { closeDetail } from "./detail.js";
import { showSets } from "./sets.js";
import { showSettings, updateYtdlp } from "./settings.js";
import { emit, on, store } from "./store.js";
import { closePlayer, playerOpen, togglePlayPause } from "./tracks.js";
import { copyText, errorToast, fill, h, toast } from "./ui.js";

let tab = "crates";
let failures = 0;
let lastLibraryStatus = null;
let interruptedPrompted = false;
let lastQueueKey = "";
let activityTimer = null;
let everConnected = false;

// ------------------------------------------------ tabs

function showTab(name) {
  tab = name;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "sets") showSets();
  if (name === "settings") showSettings();
  if (name !== "crates") closePlayer();
}

// ------------------------------------------------ status polling

async function poll() {
  let s;
  try {
    s = await get("/api/status");
  } catch {
    failures += 1;
    if (failures >= 3) showCrash();
    return;
  }
  if (failures >= 3) hideOverlay();
  failures = 0;
  const prev = store.status;
  store.status = s;
  if (s.spotify.connected) everConnected = true;
  const libStatus = s.library.status;
  if (libStatus !== lastLibraryStatus) {
    lastLibraryStatus = libStatus;
    if (libStatus === "ok") {
      hideOverlay();
      await refreshSidebar();
      selectView(store.view);
    } else {
      showStartup(s);
    }
  } else if (libStatus !== "ok") {
    showStartup(s);
  }
  renderStrip(s);
  renderBanners(s);
  if (libStatus === "ok") {
    const q = s.queue;
    const key = `${q.queued}|${q.running}|${q.failed}|${q.interrupted}`;
    // While downloads run, keep the table + counts live.
    if (q.queued || q.running || key !== lastQueueKey) {
      if (tab === "crates") refreshTracks();
      if (key !== lastQueueKey) refreshSidebar();
    }
    lastQueueKey = key;
    if (q.interrupted && !interruptedPrompted) promptInterrupted(q.interrupted);
  }
  const badge = document.getElementById("activity-count");
  const n = s.queue.failed + s.errors_count + s.queue.interrupted;
  const active = s.queue.queued + s.queue.running;
  badge.hidden = !n && !active;
  badge.textContent = n || active;
  badge.classList.toggle("info", !n && !!active);
  if (prev?.spotify?.login?.state === "waiting" && s.spotify.login.state === "ok") {
    toast("Connected to Spotify. ✓", { type: "success" });
    if (tab === "settings") showSettings();
  }
  emit("status", s);
}

function promptInterrupted(n) {
  interruptedPrompted = true;
  toast(`${n} download${n === 1 ? " was" : "s were"} interrupted when DJ Crate closed. Resume?`, {
    timeout: 0, type: "warn",
    actions: [
      { label: "Resume", fn: async () => { await post("/api/jobs/interrupted", { resume: true }); poll(); } },
      { label: "Discard", fn: async () => { await post("/api/jobs/interrupted", { resume: false }); poll(); } },
    ],
  });
}

// ------------------------------------------------ status strip & banners

function renderStrip(s) {
  const sp = s.spotify;
  const lib = s.library;
  const spDot = sp.connected ? "ok" : sp.client_id_set ? "off" : "off";
  const spText = sp.connected ? "Spotify connected" : sp.client_id_set ? "Spotify disconnected" : "Spotify not set up";
  const libDot = lib.status === "ok" ? (lib.read_only ? "warn" : "ok") : "bad";
  const libText = lib.status === "ok" ? (lib.read_only ? "Library read-only" : "Library OK") : "Library not found";
  const toolsOk = s.tools.ffmpeg && s.tools.ytdlp && !s.queue.outdated;
  const toolsText = !s.tools.ffmpeg ? "ffmpeg missing" : !s.tools.ytdlp ? "yt-dlp broken" : s.queue.outdated ? "yt-dlp needs update" : "Downloader OK";
  document.getElementById("status-strip").replaceChildren(
    h("span", { class: "pill clickable", title: sp.connected ? "Logged in" : "Click to set up / log in", onclick: () => showTab("settings") },
      h("span", { class: `dot ${spDot}` }), spText),
    h("span", { class: "pill", title: lib.path || "" }, h("span", { class: `dot ${libDot}` }), libText),
    h("span", { class: "pill clickable", title: `ffmpeg ${s.tools.ffmpeg ? "found" : "missing"} · yt-dlp ${s.tools.ytdlp || "?"}`, onclick: () => showTab("settings") },
      h("span", { class: `dot ${toolsOk ? "ok" : "warn"}` }), toolsText),
  );
}

function renderBanners(s) {
  const el = document.getElementById("banners");
  const b = [];
  if (s.library.status !== "ok") { el.replaceChildren(); return; }
  if (s.library.read_only) {
    b.push(h("div", { class: "banner warn" },
      `Read-only: this library is also open on ${s.library.lock_info?.host || "another Mac"}. Changes are off so nothing gets corrupted.`,
      h("button", { class: "btn small", onclick: async () => { await post("/api/library/take_over"); location.reload(); } }, "Take over")));
  }
  if (!s.tools.ffmpeg) {
    b.push(h("div", { class: "banner bad" }, "ffmpeg isn't installed, so downloads are paused. In Terminal run ", h("code", {}, "brew install ffmpeg"), ", then",
      h("button", { class: "btn small", onclick: async () => { await post("/api/tools/recheck"); poll(); } }, "Re-check")));
  }
  if (s.queue.outdated) {
    const btn = h("button", { class: "btn small primary" }, "Update yt-dlp");
    btn.onclick = () => updateYtdlp(btn);
    b.push(h("div", { class: "banner warn" }, `YouTube changed something — ${s.queue.outdated} download${s.queue.outdated === 1 ? "" : "s"} failed. Updating the downloader usually fixes it.`, btn));
  }
  const paused = s.queue.paused_reason;
  if (paused === "offline") b.push(h("div", { class: "banner info" }, "You're offline — downloads are paused and will resume automatically."));
  if (paused === "disk_full") b.push(h("div", { class: "banner bad" }, "Not enough disk space — downloads are paused. Free up some space; they resume automatically."));
  if (paused === "library_missing") b.push(h("div", { class: "banner bad" }, "Library folder not reachable — downloads paused."));
  if (s.spotify.client_id_set && !s.spotify.connected && everConnected) {
    b.push(h("div", { class: "banner info" }, "Logged out of Spotify. Your crates are safe — log in again to import or re-sync."));
  }
  el.replaceChildren(...b);
}

// ------------------------------------------------ startup & crash screens

function overlay(card) {
  const el = document.getElementById("overlay");
  el.hidden = false;
  el.replaceChildren(h("div", { class: "overlay-card" }, card));
}

function hideOverlay() {
  document.getElementById("overlay").hidden = true;
}

let startupKey = "";

function showStartup(s) {
  const lib = s.library;
  const key = `${lib.status}|${lib.path}|${lib.error}`;
  if (key === startupKey && !document.getElementById("overlay").hidden) return;
  startupKey = key;
  const act = (label, fn, kind = "") => h("button", {
    class: `btn ${kind}`,
    onclick: async (e) => {
      e.target.disabled = true;
      try { await fn(); } catch (err) { errorToast(err); } finally { e.target.disabled = false; }
      startupKey = "";
      poll();
    },
  }, label);
  const choose = async () => {
    const p = await native.pickFolder("Library folder path:");
    if (p) await post("/api/library/choose", { path: p, create: true });
  };
  const logs = act("Open logs", () => post("/api/reveal", { target: "logs" }));
  let content;
  if (lib.status === "needs_library") {
    content = [
      h("h1", {}, "Welcome to DJ Crate"),
      h("p", {}, "Pick where your library lives. This one folder holds all your MP3s and DJ Crate's data — it's the folder Rekordbox will import from, and the folder you copy to move to a new Mac."),
      h("div", { class: "actions" },
        act(`Use ${lib.default_path.replace(/^\/Users\/[^/]+/, "~")}`, () => post("/api/library/default"), "primary"),
        act("Choose a folder…", choose)),
    ];
  } else if (lib.status === "library_missing") {
    content = [
      h("h1", {}, "Library not found"),
      h("p", {}, "DJ Crate can't find your library at:"),
      h("p", {}, h("code", {}, lib.path || "?")),
      lib.error ? h("p", { class: "muted" }, lib.error) : null,
      h("p", {}, "If it's on an external drive, plug it in and click Retry. If you moved it, choose its new location."),
      h("div", { class: "actions" }, act("Retry", () => post("/api/library/retry"), "primary"), act("Choose folder…", choose)),
    ];
  } else if (lib.status === "lock_conflict") {
    content = [
      h("h1", {}, "This library is open somewhere else"),
      h("p", {}, `It's open on “${lib.lock_info?.host || "another Mac"}” (since ${lib.lock_info?.since || "?"}). Opening it in two places at once can corrupt it.`),
      h("p", { class: "muted" }, "If that Mac isn't actually using it anymore (e.g. it crashed), Take over is safe."),
      h("div", { class: "actions" },
        act("Open read-only", () => post("/api/library/open_read_only"), "primary"),
        act("Take over", () => post("/api/library/take_over"), "danger"),
        act("Choose another folder…", choose)),
    ];
  } else if (lib.status === "db_corrupt") {
    content = [
      h("h1", {}, "The library's database is damaged"),
      h("p", {}, "Your MP3s are fine. Restore DJ Crate's data from a backup — the damaged file is kept aside, not deleted."),
      s.backups?.length
        ? h("div", { class: "backup-list" }, s.backups.slice(0, 8).map((b, i) => h("div", { class: "item" },
          h("span", {}, `${b.name} `, h("span", { class: "muted" }, b.modified)),
          act(i === 0 ? "Restore latest" : "Restore", () => post("/api/library/restore", { name: b.name }), i === 0 ? "primary" : ""))))
        : h("p", { class: "muted" }, "No backups found in .djcrate/backups."),
      h("div", { class: "actions" }, logs, act("Choose another folder…", choose)),
    ];
  } else if (lib.status === "migration_failed") {
    content = [
      h("h1", {}, "Couldn't upgrade the library"),
      h("p", {}, "DJ Crate tried to update its database for this version and stopped. Nothing was half-changed — a backup was made first."),
      h("p", {}, h("code", {}, lib.error)),
      h("div", { class: "actions" }, act("Retry", () => post("/api/library/retry"), "primary"), logs),
    ];
  } else {
    content = [h("h1", {}, "Opening library…")];
  }
  overlay(content);
}

function showCrash() {
  overlay([
    h("h1", {}, "DJ Crate hit an error"),
    h("p", {}, "The engine behind the window stopped responding. Your data is safe — restart to continue."),
    h("div", { class: "actions" },
      h("button", { class: "btn primary", onclick: () => native.restart() }, native.available ? "Restart DJ Crate" : "Reload"),
      h("p", { class: "muted small" }, "Logs: ~/Library/Logs/DJ Crate/")),
  ]);
}

// ------------------------------------------------ activity panel

async function renderActivity() {
  const panel = document.getElementById("activity-panel");
  let d;
  try { d = await get("/api/jobs"); } catch (e) { panel.replaceChildren(h("div", { class: "job" }, e.message)); return; }
  const jobView = (j, extra = []) => h("div", { class: "job" },
    h("div", { class: "jt" }, `${j.title} — ${j.artists}`),
    h("div", { class: `jm ${j.status === "failed" ? "err" : ""}` },
      j.status === "running" ? `${j.stage || "downloading"} ${j.stage === "downloading" ? Math.round(j.progress * 100) + "%" : ""}` :
        j.status === "queued" ? (j.error_msg || "Queued") : j.error_msg || j.status),
    extra.length ? h("div", { class: "ja" }, extra) : null);
  const btn = (label, fn, kind = "small") => h("button", { class: `btn ${kind}`, onclick: async () => { try { await fn(); } catch (e) { errorToast(e); } renderActivity(); poll(); } }, label);
  const sections = [];
  if (d.interrupted.length) {
    sections.push(h("h4", {}, `Interrupted (${d.interrupted.length})`),
      h("div", { class: "actions" },
        btn("Resume all", () => post("/api/jobs/interrupted", { resume: true }), "small primary"),
        btn("Discard", () => post("/api/jobs/interrupted", { resume: false }))));
  }
  if (d.active.length) {
    sections.push(h("h4", {}, `Downloading (${d.active.length})`),
      h("div", { class: "actions" }, btn("Cancel all", () => post("/api/jobs/cancel_all"))),
      d.active.map((j) => jobView(j, [btn("Cancel", () => post(`/api/jobs/${j.id}/cancel`))])));
  }
  if (d.failed.length) {
    sections.push(h("h4", {}, `Failed (${d.failed.length})`),
      h("div", { class: "actions" }, btn("Retry all failed", () => post("/api/jobs/retry", {}), "small primary")),
      d.failed.map((j) => jobView(j, [
        btn("Retry", () => post("/api/jobs/retry", { ids: [j.id] })),
        btn("Copy error details", async () => { await copyText(`${j.title} — ${j.artists}\n${j.error_code}: ${j.error_msg}`); toast("Copied."); }),
        btn("Dismiss", () => post(`/api/jobs/${j.id}/dismiss`)),
      ])));
  }
  if (d.app_errors.length) {
    sections.push(h("h4", {}, `App errors (${d.app_errors.length})`),
      h("div", { class: "actions" },
        btn("Copy details", async () => { await copyText(d.app_errors.map((e) => `[${e.time}] ${e.message}\n${e.detail}`).join("\n\n")); toast("Copied — paste it into a GitHub issue."); }),
        btn("Open logs", () => post("/api/reveal", { target: "logs" })),
        btn("Clear", () => post("/api/errors/clear"))),
      d.app_errors.slice(0, 10).map((e) => h("div", { class: "job" }, h("div", { class: "jt" }, e.message), h("div", { class: "jm" }, e.time))));
  }
  if (!sections.length) sections.push(h("div", { class: "job" }, h("div", { class: "jm" }, "All quiet. Downloads and problems show up here.")));
  fill(panel, sections);
}

function toggleActivity(force) {
  const panel = document.getElementById("activity-panel");
  const open = force ?? panel.hidden;
  panel.hidden = !open;
  clearInterval(activityTimer);
  if (open) {
    renderActivity();
    activityTimer = setInterval(renderActivity, 2000);
  }
}

// ------------------------------------------------ boot

function init() {
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  document.getElementById("activity-btn").addEventListener("click", () => toggleActivity());
  document.addEventListener("mousedown", (e) => {
    const panel = document.getElementById("activity-panel");
    if (!panel.hidden && !panel.contains(e.target) && !document.getElementById("activity-btn").contains(e.target)) toggleActivity(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented) return;
    const typing = e.target.closest("input:not([type=range]):not([type=checkbox]), textarea, select, [contenteditable]");
    // Only real open overlays block shortcuts (the Activity panel is in the DOM but hidden).
    if (document.querySelector("#modal-root .backdrop") || document.querySelector(".popover:not([hidden])")) return;
    if (e.key === "Escape") {
      if (playerOpen()) closePlayer();
      else closeDetail();
    }
    if (e.key === " " && !typing && playerOpen()) {
      e.preventDefault();
      togglePlayPause();
    }
  });
  on("show-tab", showTab);
  on("open-activity", () => toggleActivity(true));
  on("poll", poll);
  on("spotify-logout", () => { everConnected = false; });
  window.addEventListener("error", (e) => console.error(e.error || e.message));
  window.addEventListener("unhandledrejection", (e) => { console.error(e.reason); });
  initCrates();
  poll();
  setInterval(poll, 2000);
}

init();
