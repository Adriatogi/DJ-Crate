// App shell: tabs, status polling, status strip, banners, startup/crash
// screens, Activity panel, keyboard shortcuts.

import { get, native, post } from "./api.js";
import { swapVersion } from "./cleanver.js";
import { autofillProgress, initCrates, refreshSidebar, refreshTracks, selectView } from "./crates.js";
import { closeDetail } from "./detail.js";
import { showCutting } from "./cutting.js";
import { showDocs } from "./docs.js";
import { renderOnboarding } from "./onboarding.js";
import { showSets } from "./sets.js";
import { showSettings, updateYtdlp } from "./settings.js";
import { emit, on, store } from "./store.js";
import { closePlayer, playerOpen, resetTable, togglePlayPause } from "./tracks.js";
import { copyText, errorToast, fill, h, toast, toolOutput } from "./ui.js";

let tab = "crates";
let failures = 0;
let lastLibraryStatus = null;
let interruptedPrompted = false;
let lastQueueKey = "";
let activityTimer = null;
let everConnected = false;
let openedPath = null;
let searchPaused = false;
let lastRekordboxAt;
let lastCleanFlagAt;
let shownBatchEnd;   // the finished run we've already shown (by its time); set on the first poll
let batchEndTimer = null;
let batchEndShowing = false;
let lastCleanNotice = 0;
let cleanSeen = 0;
let pendingTrackRefresh = false;

// ------------------------------------------------ tabs

function showTab(name) {
  tab = name;
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "sets") showSets();
  if (name === "cutting") showCutting();
  if (name === "docs") showDocs();
  if (name === "settings") showSettings();
  if (name !== "crates") closePlayer();
  if (name === "crates" && pendingTrackRefresh) { pendingTrackRefresh = false; refreshTracks(); }
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
  // The My Tags column and filter only show once some song has My Tags from Rekordbox (or a filter is on).
  document.body.classList.toggle("no-mytags", !(s.rekordbox?.mytags || store.mytag));
  if (s.spotify.connected) everConnected = true;
  const libStatus = s.library.status;
  if (libStatus !== lastLibraryStatus) {
    lastLibraryStatus = libStatus;
    if (libStatus === "ok") {
      hideOverlay();
      if (openedPath && openedPath !== s.library.path) {
        // Switched to another library: nothing from the old one should linger.
        resetTable();
        store.tracks = [];
        store.view = "smart:all";
        interruptedPrompted = false;
        everConnected = false;
        if (tab !== "crates") showTab(tab);
      }
      openedPath = s.library.path;
      await refreshSidebar();
      selectView(store.view);
    } else {
      showStartup(s);
    }
  } else if (libStatus !== "ok") {
    showStartup(s);
  }
  renderStrip(s);
  renderProgress(s);
  renderBanners(s);
  renderOnboarding(s);
  if (libStatus === "ok") {
    const q = s.queue;
    const key = `${q.queued}|${q.running}|${q.failed}|${q.interrupted}`;
    // While downloads run, keep the table + counts live.
    if (q.queued || q.running || key !== lastQueueKey) {
      if (tab === "crates") refreshTracks();
      if (key !== lastQueueKey) refreshSidebar();
    }
    lastQueueKey = key;
    // Rekordbox re-export picked up by the watcher: refresh BPM & key.
    const rb = s.rekordbox;
    if (rb && lastRekordboxAt !== undefined && rb.at && rb.at !== lastRekordboxAt) {
      if (tab === "crates") refreshTracks(); else pendingTrackRefresh = true;
      if (tab === "settings") showSettings();
      toast(rb.error || `Updated BPM & key for ${rb.matched} songs from Rekordbox`, { type: rb.error ? "warn" : "success" });
    }
    lastRekordboxAt = rb?.at ?? null;
    // The background clean check found a clean version: say so once, with the choice right there.
    // While it works through your existing songs, finds are summed up (at most one notice a minute).
    const cf = s.clean?.last_flag;
    if (lastCleanFlagAt === undefined) { lastCleanFlagAt = cf?.at ?? null; cleanSeen = s.clean?.flagged ?? 0; }
    else if (cf && cf.at !== lastCleanFlagAt && Date.now() - lastCleanNotice > 60000) {
      lastCleanFlagAt = cf.at;
      lastCleanNotice = Date.now();
      const found = Math.max(1, (s.clean.flagged ?? 0) - cleanSeen);
      cleanSeen = s.clean.flagged ?? 0;
      if (tab === "crates") refreshTracks(); else pendingTrackRefresh = true;
      emit("refresh-sidebar");
      announceClean(cf, found);
    }
    if (tab === "crates") autofillProgress(s.autofill);
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
  // One notice when YouTube rate-limits the background searches.
  const paused = !!s.search?.paused_until;
  if (paused && !searchPaused) {
    toast("YouTube is rate-limiting searches, so searching ahead is paused for 3 minutes. Find still works — it's just not pre-loaded.",
      { type: "warn", timeout: 9000 });
  }
  searchPaused = paused;
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

// Just information (some gigs want clean): say what was found, with a swap one click away.
async function announceClean(cf, found) {
  if (found > 1) {
    toast(`${found} songs are clean versions. YouTube Music also has them as explicit.`, {
      timeout: 12000,
      action: { label: "Show them", fn: () => { emit("show-tab", "crates"); selectView("smart:clean_versions"); } },
    });
    return;
  }
  toast(`“${cf.title}” is the clean version. YouTube Music also has the explicit one.`, {
    timeout: 12000,
    action: { label: "Swap to explicit", fn: async () => { const r = await get(`/api/tracks/${cf.track_id}`); await swapVersion([r.track], "explicit"); } },
  });
}

// ------------------------------------------------ overall download progress

const PAUSED = {
  ffmpeg_missing: "ffmpeg is missing", library_missing: "library folder not reachable", disk_full: "disk is full",
  offline: "you're offline", rate_limited: "YouTube is rate-limiting",
};

function renderProgress(s) {
  const el = document.getElementById("dl-progress");
  const b = s.library?.status === "ok" ? s.queue?.batch : null;
  const bar = (fraction, cls = "") => h("div", { class: `progress ${cls}` }, h("div", { style: { width: `${Math.round(fraction * 100)}%` } }));
  if (b) {
    clearTimeout(batchEndTimer);
    batchEndShowing = false;
    const paused = s.queue.paused_reason;
    const text = paused
      ? `Paused: ${PAUSED[paused] || paused.replace(/_/g, " ")}`
      : `${b.done}/${b.total} downloaded${b.failed ? ` · ${b.failed} failed` : ""}`;
    el.title = [...b.running.map((r) => `${r.title} — ${r.artists} · ${r.stage === "downloading" ? `${Math.round(r.progress * 100)}%` : r.stage}`),
      b.queued ? `${b.queued} waiting` : null, b.failed ? `${b.failed} failed` : null, "Click for details"].filter(Boolean).join("\n");
    el.className = paused ? "paused" : b.failed ? "failed" : "";
    fill(el, bar(b.total ? b.done / b.total : 0), h("span", {}, text));   // simple: songs finished out of the total
    el.hidden = false;
    return;
  }
  // The run just finished: say how it went for a few seconds, once.
  const last = s.queue?.last_batch;
  if (shownBatchEnd === undefined) shownBatchEnd = last?.at ?? null;   // don't replay an old run after a reload
  if (last && last.at !== shownBatchEnd) {
    shownBatchEnd = last.at;
    el.className = last.failed ? "failed" : "finished";
    el.title = last.failed ? "Click to see what failed" : "";
    fill(el, bar(1), h("span", {}, last.failed ? `Downloaded ${last.done} · ${last.failed} failed` : `Downloaded ${last.done} ✓`));
    el.hidden = false;
    clearTimeout(batchEndTimer);
    batchEndShowing = true;
    batchEndTimer = setTimeout(() => { batchEndShowing = false; el.hidden = true; }, last.failed ? 12000 : 6000);
    return;
  }
  if (!batchEndShowing) el.hidden = true;
}

// ------------------------------------------------ status strip & banners

function renderStrip(s) {
  const sp = s.spotify;
  const lib = s.library;
  const spDot = sp.connected ? "ok" : sp.client_id_set ? "off" : "off";
  const spText = sp.connected ? "Spotify connected" : sp.client_id_set ? "Spotify disconnected" : "Spotify not set up";
  const libDot = lib.status === "ok" ? (lib.read_only ? "warn" : "ok") : "bad";
  const libText = lib.status === "ok" ? `${lib.name || "Library"}${lib.read_only ? " (read-only)" : ""} ▾` : "No library open";
  const toolsOk = s.tools.ffmpeg && s.tools.ytdlp && s.tools.js && !s.queue.outdated;
  const toolsText = !s.tools.ffmpeg ? "ffmpeg missing" : !s.tools.ytdlp ? "yt-dlp broken" : s.queue.outdated ? "yt-dlp needs update"
    : !s.tools.js ? "JS runtime missing" : "Downloader OK";
  document.getElementById("status-strip").replaceChildren(
    h("span", { class: "pill clickable", title: sp.connected ? "Logged in" : "Click to set up / log in", onclick: () => showTab("settings") },
      h("span", { class: `dot ${spDot}` }), spText),
    h("span", { class: "pill clickable", title: s.genius?.configured ? "Used by Find samples in Cutting" : "Click to set up (free) for Find samples", onclick: () => showTab("settings") },
      h("span", { class: `dot ${s.genius?.configured ? "ok" : "off"}` }), s.genius?.configured ? "Genius connected" : "Genius not set up"),
    h("span", { class: "pill clickable", title: `${lib.path || ""}\nClick to switch library`, onclick: switchLibrary }, h("span", { class: `dot ${libDot}` }), libText),
    h("span", { class: "pill clickable", title: `ffmpeg ${s.tools.ffmpeg ? "found" : "missing"} · yt-dlp ${s.tools.ytdlp || "?"} · JS runtime ${s.tools.js || "missing"}`, onclick: () => showTab("settings") },
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
  const recheck = () => h("button", { class: "btn small", onclick: async () => { await post("/api/tools/recheck"); poll(); } }, "Re-check");
  // The downloadable app carries ffmpeg and deno inside it, so missing ones mean a damaged copy.
  const fix = (tool) => s.install === "app" ? ["Download DJ Crate again and replace the app (your music and crates are kept), then"]
    : ["In Terminal run ", h("code", {}, `brew install ${tool}`), ", then"];
  if (s.translocated) {
    b.push(h("div", { class: "banner warn" }, "DJ Crate is running from your Downloads. Drag it into your Applications folder and open it from there, so it can find its own files and update cleanly."));
  }
  if (!s.tools.ffmpeg) {
    b.push(h("div", { class: "banner bad" }, "ffmpeg is missing, so downloads are paused. ", ...fix("ffmpeg"), recheck()));
  }
  if (!s.tools.js) {
    b.push(h("div", { class: "banner warn" }, "yt-dlp has no JavaScript runtime, so YouTube downloads may miss formats or fail. ", ...fix("deno"), recheck()));
  }
  const up = s.update;
  if (up?.available && localStorage.getItem("djcrate.dismissed_update") !== up.latest) {
    const dismiss = h("button", { class: "btn small", title: "Hide until the next version" }, "Later");
    dismiss.onclick = () => { localStorage.setItem("djcrate.dismissed_update", up.latest); renderBanners(s); };
    b.push(h("div", { class: "banner info" }, `DJ Crate ${up.latest} is out (you have ${up.current}).`,
      up.install === "app"
        ? h("button", { class: "btn small primary", onclick: () => post("/api/open_url", { url: up.url }) }, "Download")
        : h("span", {}, " Update with ", h("code", {}, "scripts/update.sh"), " ",
          h("button", { class: "btn small", onclick: () => post("/api/open_url", { url: up.url }) }, "What's new")),
      dismiss));
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
  if (lib.status === "needs_library" || lib.status === "choose_library") {
    const box = h("div", {}, h("p", {}, h("span", { class: "spinner" }), " Loading libraries…"));
    renderPicker(box);
    content = [box];
  } else if (lib.status === "library_missing") {
    content = [
      h("h1", {}, "Library not found"),
      h("p", {}, "DJ Crate can't find your library at:"),
      h("p", {}, h("code", {}, lib.path || "?")),
      lib.error ? h("p", { class: "muted" }, lib.error) : null,
      h("p", {}, "If it's on an external drive, plug it in and click Retry. If you moved it, choose its new location."),
      h("div", { class: "actions" }, act("Retry", () => post("/api/library/retry"), "primary"), act("Choose folder…", choose),
        act("Open a different library…", () => post("/api/library/switch"))),
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

const tilde = (p) => (p || "").replace(/^\/Users\/[^/]+/, "~");

function timeAgo(iso) {
  if (!iso) return "";
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
  return days <= 0 ? "opened today" : days === 1 ? "opened yesterday" : `opened ${days} days ago`;
}

// Which library? Several people can share DJ Crate on one Mac — each library is
// its own music, crates, notes and Spotify login.
async function renderPicker(box) {
  let d;
  try { d = await get("/api/libraries"); } catch (e) { fill(box, h("p", {}, e.message)); return; }
  const done = () => { startupKey = ""; poll(); };
  const open = async (path) => {
    try { await post("/api/library/choose", { path }); done(); } catch (e) { errorToast(e); }
  };
  const first = !d.libraries.length;
  const name = h("input", { placeholder: first ? "Library name — e.g. your name" : "New library name — e.g. Sam", style: { flex: "1" } });
  const create = async () => {
    try { await post("/api/library/new", { name: name.value }); done(); } catch (e) { errorToast(e); }
  };
  name.addEventListener("keydown", (e) => { if (e.key === "Enter") create(); });
  const ask = h("input", { type: "checkbox", class: "sel", checked: !d.ask_on_launch });
  ask.addEventListener("change", () => post("/api/library/prefs", { ask_on_launch: !ask.checked }).catch(errorToast));
  const list = d.libraries.map((l) => h("div", {
    class: `item${l.exists ? "" : " disabled"}`,
    onclick: () => l.exists && open(l.path),
    title: l.exists ? `Open ${l.path}` : "Folder not found — reconnect the drive, or remove it from this list",
  },
    h("div", { class: "cover placeholder" }, "◉"),
    h("div", { class: "grow" },
      h("div", { class: "t-title" }, l.name),
      h("div", { class: "t-sub" }, [tilde(l.path), l.exists ? `${l.tracks ?? "?"} songs` : "not found", timeAgo(l.last_opened)].filter(Boolean).join(" · "))),
    h("button", {
      class: "icon-btn small", title: "Remove from this list (doesn't delete anything)",
      onclick: async (e) => {
        e.stopPropagation();
        try { await post("/api/library/forget", { path: l.path }); renderPicker(box); } catch (err) { errorToast(err); }
      },
    }, "✕")));
  fill(box,
    h("h1", {}, first ? "Welcome to DJ Crate" : "Which library?"),
    h("p", {}, first
      ? "Create your library. It's one folder holding your MP3s and DJ Crate's data — the folder Rekordbox imports, and the one you copy to move to a new Mac."
      : "Each library has its own music, crates, notes and Spotify login — so several people can share DJ Crate on this Mac."),
    list.length ? h("div", { class: "list" }, list) : null,
    h("div", { class: "section-title" }, first ? "Name it" : "Or start a new one"),
    h("div", { class: "row gap" }, name, h("button", { class: `btn ${first ? "primary" : ""}`, onclick: create }, "+ Create library")),
    h("p", { class: "muted small" }, `New libraries go in ${tilde(d.libraries_dir)}/<name>.`),
    h("div", { class: "row gap", style: { marginTop: "6px" } },
      h("button", {
        class: "btn", onclick: async () => {
          const p = await native.pickFolder("Library folder path:");
          if (p) { try { await post("/api/library/choose", { path: p, create: true }); done(); } catch (e) { errorToast(e); } }
        },
      }, "Open a folder…"),
      h("span", { class: "muted small" }, "an existing library, e.g. on an external SSD")),
    first ? null : h("label", { class: "row gap", style: { marginTop: "16px", cursor: "pointer" } }, ask,
      "Don't ask again — always open the last library"),
    first ? null : h("p", { class: "muted small" }, "You can switch any time by clicking the library name at the top."),
  );
  if (first) setTimeout(() => name.focus(), 50);
}

async function switchLibrary() {
  try {
    closePlayer();
    await post("/api/library/switch");
    startupKey = "";
    poll();
  } catch (e) { errorToast(e); }
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
    j.status === "failed" && j.error_detail ? toolOutput(j.error_detail, `job:${j.id}`) : null,
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
        btn("Copy error details", async () => {
          await copyText(`${j.title} — ${j.artists}\n${j.error_code}: ${j.error_msg}${j.error_detail ? `\n\nyt-dlp said:\n${j.error_detail}` : ""}`);
          toast("Copied.");
        }),
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
  const pausedUntil = store.status?.search?.paused_until;
  if (pausedUntil) {
    sections.unshift(h("div", { class: "job" }, h("div", { class: "jt" }, "Searching ahead is paused"),
      h("div", { class: "jm" }, `YouTube rate-limited searches. It resumes at ${new Date(pausedUntil * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}.`)));
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
  document.getElementById("dl-progress").addEventListener("click", (e) => { e.stopPropagation(); toggleActivity(true); });
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
