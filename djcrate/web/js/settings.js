// Settings tab: Spotify, library, downloads/tools, backups, logs.

import { del, get, native, patch, post, put } from "./api.js";
import { emit, store } from "./store.js";
import { confirmDialog, copyText, errorToast, h, modal, toast } from "./ui.js";

const root = () => document.getElementById("view-settings");

export async function showSettings() {
  let s;
  try {
    s = await get("/api/settings");
  } catch (e) {
    root().replaceChildren(h("div", { class: "settings" }, h("p", {}, e.message)));
    return;
  }
  const sp = store.status?.spotify || {};
  const card = (title, ...children) => h("div", { class: "card" }, h("h3", {}, title), ...children);

  // ---- Spotify
  const cid = h("input", { value: s.spotify_client_id, placeholder: "Paste your Client ID", spellcheck: false });
  const saveCid = h("button", {
    class: "btn",
    onclick: async () => {
      try {
        await patch("/api/settings", { spotify_client_id: cid.value });
        toast("Client ID saved.", { type: "success" });
        emit("poll");
        setTimeout(showSettings, 300);
      } catch (e) { errorToast(e); }
    },
  }, "Save");
  const loginBtn = sp.connected
    ? h("button", {
      class: "btn", onclick: async () => {
        if (!(await confirmDialog("Log out of Spotify?", "Your crates, links and notes stay. You'll just need to log in again to import or re-sync.", { ok: "Log out" }))) return;
        await post("/api/spotify/logout");
        emit("spotify-logout");
        emit("poll");
        setTimeout(showSettings, 300);
      },
    }, "Log out")
    : h("button", {
      class: "btn spotify", disabled: !s.spotify_client_id,
      onclick: async () => { try { await post("/api/spotify/login"); toast("Finish logging in in your browser, then come back.", { timeout: 8000 }); emit("poll"); } catch (e) { errorToast(e); } },
    }, "Log in to Spotify");

  const spotifyCard = card("Spotify",
    h("p", {}, sp.connected ? "● Connected." : s.spotify_client_id ? "○ Not logged in." : "Not set up yet."),
    h("ol", {},
      h("li", {}, "Go to ", h("a", { href: "#", onclick: (e) => { e.preventDefault(); post("/api/open_url", { url: "https://developer.spotify.com/dashboard" }); } }, "developer.spotify.com/dashboard"),
        " and log in (your account needs Premium)."),
      h("li", {}, "Create app → any name (e.g. “DJ Crate”), tick ", h("b", {}, "Web API"), "."),
      h("li", {}, "Redirect URI: ", h("code", {}, s.redirect_uri), " ",
        h("button", { class: "btn small", onclick: async () => { await copyText(s.redirect_uri); toast("Copied."); } }, "Copy")),
      h("li", {}, "Open the app's Settings, copy the ", h("b", {}, "Client ID"), ", paste it here, Save, then Log in.")),
    h("div", { class: "line" }, cid, saveCid, loginBtn),
    h("p", { class: "muted small" }, "Your login is stored in the macOS Keychain, not in the library folder."));

  // ---- Genius (sample lookup)
  const gConnected = !!store.status?.genius?.configured;
  const gToken = h("input", { placeholder: gConnected ? "Saved — paste a new one to replace it" : "Paste your access token", spellcheck: false });
  const saveToken = h("button", { class: "btn" }, "Save");
  saveToken.onclick = async () => {
    saveToken.disabled = true;
    try {
      await put("/api/genius/token", { token: gToken.value });
      toast("Genius connected.", { type: "success" });
      emit("poll");
      setTimeout(showSettings, 300);
    } catch (e) { errorToast(e); } finally { saveToken.disabled = false; }
  };
  const geniusCard = card("Genius (sample lookup)",
    h("p", {}, gConnected ? "● Connected." : "Not set up yet."),
    h("ol", {},
      h("li", {}, "Go to ", h("a", { href: "#", onclick: (e) => { e.preventDefault(); post("/api/open_url", { url: "https://genius.com/api-clients" }); } }, "genius.com/api-clients"),
        " and log in (a free Genius account is fine)."),
      h("li", {}, "New API Client → any name (e.g. “DJ Crate”), App Website URL ", h("code", {}, "http://127.0.0.1"), ", Save."),
      h("li", {}, "Click ", h("b", {}, "Generate Access Token"), ", copy it, paste it here and Save.")),
    h("div", { class: "line" }, gToken, saveToken,
      gConnected ? h("button", {
        class: "btn", onclick: async () => {
          try { await del("/api/genius/token"); emit("poll"); setTimeout(showSettings, 300); } catch (e) { errorToast(e); }
        },
      }, "Disconnect") : null),
    h("p", { class: "muted small" }, "Used by Find samples in Cutting: Genius lists what a song samples and what sampled it. WhoSampled has no public API, so its button just opens the website."),
    h("p", { class: "muted small" }, "Your token is stored in the macOS Keychain, not in the library folder."));

  // ---- Rekordbox
  const rbCard = rekordboxCard(card, store.status?.rekordbox || {});

  // ---- Library
  const libName = h("input", { value: s.library_name, style: { width: "260px" } });
  libName.addEventListener("change", async () => {
    try { await patch("/api/settings", { library_name: libName.value }); toast("Renamed.", { type: "success" }); emit("poll"); } catch (e) { errorToast(e); }
  });
  const askBox = h("input", { type: "checkbox", class: "sel", checked: s.ask_on_launch });
  askBox.addEventListener("change", () => post("/api/library/prefs", { ask_on_launch: askBox.checked }).catch(errorToast));
  const libraryCard = card("Library",
    h("p", {}, "One folder holds every MP3 plus DJ Crate's data (", h("code", {}, ".djcrate/"), "). Import this folder in Rekordbox. To move to a new Mac, copy this folder."),
    h("div", { class: "line" }, "Name: ", libName),
    h("div", { class: "line" }, h("code", {}, s.library_path)),
    h("div", { class: "line" },
      h("button", { class: "btn", onclick: () => post("/api/reveal", { target: "library" }) }, "Show in Finder"),
      h("button", {
        class: "btn", onclick: async () => {
          try { await post("/api/library/switch"); emit("show-tab", "crates"); emit("poll"); } catch (e) { errorToast(e); }
        },
      }, "Switch library…"),
      h("button", { class: "btn", onclick: () => emit("rescan") }, "Rescan for missing files")),
    h("label", { class: "row gap", style: { cursor: "pointer", marginTop: "6px" } }, askBox, "Ask which library to open when DJ Crate starts"),
    h("p", { class: "muted small" }, "Each library has its own music, crates, notes and Spotify login — handy when several people share this Mac."));

  // ---- Downloads
  const browser = h("select", {}, s.cookie_browsers.map((b) => h("option", { value: b, selected: b === s.cookies_browser }, b ? b[0].toUpperCase() + b.slice(1) : "Off")));
  browser.addEventListener("change", async () => {
    try { await patch("/api/settings", { cookies_browser: browser.value }); toast("Saved.", { type: "success" }); } catch (e) { errorToast(e); }
  });
  const aheadInput = h("input", { type: "number", min: "0", max: String(s.search_ahead_max), value: String(s.search_ahead), style: { width: "70px" } });
  aheadInput.addEventListener("change", async () => {
    const n = Number(aheadInput.value);
    if (!Number.isInteger(n) || n < 0 || n > s.search_ahead_max) {
      aheadInput.classList.add("invalid");
      toast(`${s.search_ahead_max} max — searching further ahead makes YouTube rate-limit you (it can block downloads too). Not saved.`, { type: "error", timeout: 8000 });
      aheadInput.value = String(s.search_ahead);
      setTimeout(() => aheadInput.classList.remove("invalid"), 2500);
      return;
    }
    try { await patch("/api/settings", { search_ahead: n }); s.search_ahead = n; toast(n ? `Searching ${n} song${n === 1 ? "" : "s"} ahead.` : "Searching ahead is off.", { type: "success" }); }
    catch (e) { errorToast(e); aheadInput.value = String(s.search_ahead); }
  });
  const updateBtn = h("button", { class: "btn" }, "Update yt-dlp");
  updateBtn.onclick = () => updateYtdlp(updateBtn).then(showSettings);
  const downloadsCard = card("Downloads",
    h("div", { class: "line" }, "ffmpeg: ", s.ffmpeg ? h("span", { style: { color: "var(--green)" } }, `found (${s.ffmpeg})`) : h("span", { style: { color: "var(--red)" } }, "not found — run brew install ffmpeg"),
      h("button", { class: "btn small", onclick: async () => { await post("/api/tools/recheck"); showSettings(); } }, "Re-check")),
    h("div", { class: "line" }, "yt-dlp: ", h("code", {}, s.ytdlp || "not working"), updateBtn),
    h("div", { class: "line" }, "JavaScript runtime: ", s.js_runtime
      ? h("span", { style: { color: "var(--green)" } }, `${s.js_runtime} (${s.js_runtime_path})`)
      : h("span", { style: { color: "var(--red)" } }, "not found — run brew install deno"),
      h("button", { class: "btn small", onclick: async () => { await post("/api/tools/recheck"); showSettings(); } }, "Re-check")),
    h("p", { class: "muted small" }, "YouTube needs yt-dlp to run a bit of JavaScript. Without Deno or Node, some audio formats go missing and downloads fail more often."),
    h("p", { class: "muted small" }, "YouTube changes things often. If downloads start failing, Update yt-dlp usually fixes it."),
    h("div", { class: "line" }, "Search ahead: ", aheadInput, " songs", h("span", { class: "muted small" }, `(0 = off, max ${s.search_ahead_max})`)),
    h("p", { class: "muted small" }, "When you open Find on a song, DJ Crate quietly searches the next few songs too, so theirs open instantly. More than 10 gets you rate-limited by YouTube."),
    h("div", { class: "line" }, "Use cookies from browser: ", browser),
    h("p", { class: "muted small" }, "Only needed for age-restricted videos or “confirm you're not a bot” errors. Uses your logged-in YouTube session from that browser."),
    h("p", { class: "muted small" }, "Files are saved as “Title - Artist.mp3” (MP3, constant 192 kbps — steady cue points on CDJs) in the library folder."));

  // ---- Backups
  const music = h("input", { type: "checkbox", class: "sel" });
  const backupsCard = card("Backups",
    h("p", {}, "DJ Crate saves a copy of its database every day (last 14 kept). Rekordbox has its own backup (File → Library → Backup Library) — do both when moving computers."),
    h("div", { class: "line" },
      h("button", {
        class: "btn primary", onclick: async () => {
          const close = toast("Exporting…", { timeout: 0 });
          try { const r = await post("/api/backup/export", { include_music: music.checked }); close(); toast(`Exported to ${r.path}`, { type: "success", timeout: 8000 }); } catch (e) { close(); errorToast(e); }
        },
      }, "Export crates…"),
      h("label", { class: "row gap" }, music, "include music files"),
      h("button", { class: "btn", onclick: importBackup }, "Import crates…"),
      h("button", { class: "btn", onclick: async () => { try { const r = await post("/api/backup/now"); toast(`Backup saved: ${r.name}`, { type: "success" }); showSettings(); } catch (e) { errorToast(e); } } }, "Back up now")),
    s.backups.length ? h("p", { class: "muted small" }, `Latest: ${s.backups[0].name} (${s.backups[0].modified}) · ${s.backups.length} backups in .djcrate/backups`) : null);

  // ---- Troubleshooting
  const troubleCard = card("Troubleshooting",
    h("div", { class: "line" },
      h("button", { class: "btn", onclick: () => post("/api/reveal", { target: "logs" }) }, "Open logs"),
      h("span", { class: "muted small" }, s.log_dir)),
    h("div", { class: "line" }, `Port: ${s.port}`, h("button", { class: "btn small", onclick: changePort }, "Change…")),
    h("p", { class: "muted small" }, `DJ Crate ${store.status?.version || ""}`));

  root().replaceChildren(h("div", { class: "settings" }, spotifyCard, geniusCard, rbCard, libraryCard, downloadsCard, backupsCard, troubleCard));
}

export async function updateYtdlp(btn) {
  if (btn) { btn.disabled = true; btn.textContent = "Updating…"; }
  const close = toast("Updating yt-dlp…", { timeout: 0 });
  try {
    const r = await post("/api/tools/update_ytdlp");
    close();
    if (r.ok) {
      toast(r.before === r.after ? `yt-dlp is already the latest (${r.after}).` : `Updated yt-dlp ${r.before} → ${r.after}.${r.retried ? ` Retrying ${r.retried} failed download(s).` : ""}`, { type: "success", timeout: 7000 });
    } else {
      toast(`Update failed — the old version still works.\n${(r.output || "").slice(-300)}`, { type: "error", timeout: 10000 });
    }
    emit("poll");
  } catch (e) {
    close();
    errorToast(e);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Update yt-dlp"; }
  }
}

async function importBackup() {
  const path = await native.pickFile("zip", "Path to a DJ Crate export (.zip):");
  if (!path) return;
  const ok = await confirmDialog("Import crates?",
    "This replaces DJ Crate's crates, links, notes, checkmarks and sets in this library with the ones in the export. " +
    "A backup of the current data is saved first. Music files in the export are added without overwriting existing files.",
    { ok: "Import", danger: true });
  if (!ok) return;
  try {
    const r = await post("/api/backup/import", { path });
    toast(`Imported.${r.music_extracted ? ` ${r.music_extracted} music files added.` : ""}`, { type: "success" });
    setTimeout(() => location.reload(), 600);
  } catch (e) { errorToast(e); }
}

async function changePort() {
  const input = h("input", { type: "number", value: location.port, min: "1024", max: "65535" });
  modal({
    title: "Change port",
    body: h("div", {}, h("p", {}, "Only needed if another app uses this port. After changing it, add the new redirect URI in your Spotify dashboard and restart DJ Crate."), input),
    buttons: [{ label: "Cancel" }, {
      label: "Save", kind: "primary",
      fn: async () => {
        const r = await patch("/api/settings", { port: Number(input.value) });
        r.notes.forEach((n) => toast(n, { type: "warn", timeout: 12000 }));
      },
    }],
  });
}

// ------------------------------------------------ Rekordbox (BPM & key in, crates & sets out)

function ago(seconds) {
  if (!seconds) return "never";
  const d = Math.max(0, Date.now() / 1000 - seconds);
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  return `${Math.round(d / 86400)} d ago`;
}

function rekordboxCard(card, rb) {
  const ex = rb.export || {};
  const again = () => { emit("poll"); setTimeout(showSettings, 300); };
  const run = (fn) => async () => { try { await fn(); again(); } catch (e) { errorToast(e); } };

  const choose = async () => {
    const path = await native.pickFile("xml", "Path to your Rekordbox XML (File → Export Collection in xml format):");
    if (!path) return;
    await put("/api/rekordbox/xml", { path });
  };
  const inLine = rb.path
    ? [h("p", {}, rb.error ? `⚠ ${rb.error}` : `● ${rb.matched} of ${rb.total} downloaded songs matched · updated ${ago(rb.at)}`),
      h("div", { class: "line" }, h("code", {}, rb.path)),
      h("div", { class: "line" },
        h("button", { class: "btn", onclick: run(() => post("/api/rekordbox/import")) }, "Import now"),
        h("button", { class: "btn", onclick: run(choose) }, "Choose a different file…"),
        h("button", { class: "btn", onclick: run(() => del("/api/rekordbox/xml")) }, "Stop using this file")),
      h("p", { class: "muted small" }, "Re-export over the same file in Rekordbox and DJ Crate picks up the change within about 10 seconds.")]
    : [h("div", { class: "line" }, h("button", { class: "btn primary", onclick: run(choose) }, "Choose XML file…"))];

  const toggle = h("input", { type: "checkbox", class: "sel", checked: !!ex.on });
  toggle.addEventListener("change", run(() => put("/api/rekordbox/export", { on: toggle.checked })));
  const outStatus = !ex.on ? null : ex.error ? `⚠ ${ex.error}`
    : `Updated ${ago(ex.at)} · ${ex.playlists} playlists · ${ex.songs} songs${ex.skipped ? ` · ${ex.skipped} not downloaded yet (left out)` : ""}`;

  return card("Rekordbox",
    h("p", {}, "DJ Crate and Rekordbox talk through files Rekordbox officially reads and writes. DJ Crate never touches Rekordbox's database."),
    h("h4", {}, "BPM & key from Rekordbox"),
    h("ol", {},
      h("li", {}, "In Rekordbox: File → ", h("b", {}, "Export Collection in xml format"), ", e.g. into your library folder."),
      h("li", {}, "Choose that file here. Re-export whenever you analyse or fix songs.")),
    ...inLine,
    h("h4", {}, "Send crates & sets to Rekordbox"),
    h("label", { class: "row gap", style: { cursor: "pointer" } }, toggle, "Send my crates & sets to Rekordbox"),
    h("p", { class: "muted small" }, "When this is on, DJ Crate creates ", h("code", {}, "DJ Crate for Rekordbox.xml"),
      " in your library folder and keeps it up to date by itself, every time you change a crate or set. You don't make, export or edit that file. The steps below happen in Rekordbox."),
    outStatus ? h("p", {}, outStatus) : null,
    ex.on ? h("div", { class: "line" },
      h("button", { class: "btn", onclick: () => post("/api/rekordbox/export/reveal").catch(errorToast) }, "Show in Finder"),
      h("button", { class: "btn", onclick: async () => { await copyText(ex.path); toast("Path copied."); } }, "Copy path")) : null,
    h("p", {}, "One-time setup in Rekordbox:"),
    h("ol", {},
      h("li", {}, "Rekordbox → Preferences → Advanced → Database → rekordbox xml → Imported Library: choose that file."),
      h("li", {}, "Preferences → View → Layout: tick “rekordbox xml” so it shows in the sidebar."),
      h("li", {}, "In that section, right-click a playlist → Import Playlist. Do it again after changing a crate.")),
    h("p", { class: "muted small" }, "Menu names are from Rekordbox's documentation — to be confirmed once tried. Your crates become playlists, sets become playlists in main-path order, and notes become each song's Comments. BPM, key, beat grids and cues are never sent, so nothing in Rekordbox can be overwritten. Songs are matched by file path, so you won't get duplicates."));
}
