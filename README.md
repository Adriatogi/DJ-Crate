# DJ Crate

**Spotify playlists → crates → the right audio, every time.**

Tools like spotdl download your Spotify playlists by guessing the YouTube video. They often get the wrong one: the radio edit, a live version, a music-video intro. DJ Crate does the guessing differently: **you** pick the link.

1. Import a Spotify playlist. It becomes a **crate** with every song's title, artists, album, cover and length.
2. Paste a YouTube link for each song, or use **Find ▾**. It shows the top results, highlights the one whose length matches Spotify's, and warns about "Radio Edit", "Live", "Sped up" and other versions.
3. Hit **Download All**. Each song becomes a tagged `Title - Artist.mp3` in one library folder, which Rekordbox imports.
4. Track your prep: **Edited**, **Cleaned** (BPM and grid line up in Rekordbox), **Verified** (learned and gig-ready), plus notes.
5. Plan sets in **Set Setter**: an ordered running list with cue times, transition notes and running time, exportable as text.

Everything stays on your Mac. DJ Crate runs as a normal app window; no browser tab or terminal is needed.

---

## Install (macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/Adriatogi/DJ-Crate/main/scripts/install.sh | bash
```

This needs [Homebrew](https://brew.sh). The installer adds whatever is missing (`ffmpeg`, `uv`, and `deno` if you have neither Deno nor Node). It clones the repo to `~/DJ-Crate`, installs the dependencies, and builds **DJ Crate.app** in `~/Applications`. Open it from Spotlight or Launchpad.

<details>
<summary>Prefer to do it by hand?</summary>

```bash
brew install ffmpeg uv deno       # deno: skip if you already have Node
git clone https://github.com/Adriatogi/DJ-Crate.git ~/DJ-Crate
cd ~/DJ-Crate
uv sync
scripts/make_app.sh              # builds ~/Applications/DJ Crate.app
# or run it directly:  uv run python -m djcrate.main
```
</details>

**Update:** `~/DJ-Crate/scripts/update.sh`. It pulls the latest version, reinstalls and rebuilds the app. If anything fails, it rolls back to the version you had. Your music and crates are never touched.

---

## First launch

1. **Pick your library folder.** The default is `~/Music/DJ Crate/Library`. This one folder holds every MP3 *and* DJ Crate's data (`.djcrate/`). It's what Rekordbox imports, and what you copy to move to a new Mac.
2. **Connect Spotify** (once, about 2 minutes). Everyone uses their own free Spotify developer app, so there's no shared app and no user limit:
   1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) and log in. **Your account needs Spotify Premium.**
   2. **Create app.** Use any name (e.g. "DJ Crate") and description, and tick **Web API**.
   3. **Redirect URI:** `http://127.0.0.1:8765/callback`. Enter it exactly like that; Settings has a copy button.
   4. Open the app's settings, copy the **Client ID**, and paste it into **DJ Crate → Settings → Spotify**. Then click **Save** and **Log in to Spotify**.

   Your login is stored in the macOS Keychain, never in the library folder.

> **Spotify limitation:** since 2026, Spotify only lets apps read the songs in playlists **you own or collaborate on**, and Liked Songs. For anyone else's playlist (including Spotify's own), select all its songs in Spotify, choose **Add to playlist → New playlist**, and import that.

---

## Using it

### Crates
- **All Tracks** holds every song you have. **Smart crates** fill themselves: Needs Link, Ready to Download, Downloaded, Cleaned, Verified, ⚠ Check Version, Missing Files.
- **Your crates and folders:** create them with **+♪** and **+📁**. Drag crates into folders, and drag songs onto crates. Every crate shows its progress (`12/30 ↓ · 8 cleaned · 3 verified`).
- **One song, one record.** If a song is in five crates, it's still one song. Its link, file, checkmarks and notes are shared everywhere.
- **Re-sync** a Spotify crate to pick up new songs. Songs removed on Spotify are **kept**, dimmed with a "Removed on Spotify" badge, so you never lose a file or your notes.
- **Select songs** with the checkboxes, or the header box to select all. Then **Mark**, **Add to crate**, **Download**, **Remove from crate** (only takes it out of that crate), or **Delete from library** (asks first; the MP3 goes to the Trash).
- **Sort** by clicking a column header (first click ascending, second descending, third back to default), or use **Sort ▾**, which also has Date added.
- **Add track** handles songs that aren't on Spotify, such as bootlegs and edits; SoundCloud and Bandcamp links work too. **Import folder** adds MP3s you already have.

### Links and downloads
- Paste a link into the row, or use **Find ▾**. Green means the result's length matches Spotify's, and ⚠ marks a different version.
- Files are saved as **`Title - Artist.mp3`**, with all artists, features included. They're tagged with title, artists, album, year, ISRC and cover art, and your **notes go into the comment tag**.
- **Format: MP3, constant 192 kbps.** YouTube's audio is about 130 kbps, so higher bitrates don't add quality. Constant bitrate on purpose: variable-bitrate MP3s can make cue points drift between Rekordbox and CDJs.
- **⚠ Check Version:** if a download is more than 5 seconds off Spotify's length, you probably got a different version. If you cut the song on purpose, tick **Edited** and the warning goes away.
- **▶** previews a downloaded song in a player under its row. Drag the slider to skip around; **Space** plays and pauses, **Esc** closes.
- **The bell (Activity)** shows downloads in progress and failures, with the reason and a Retry button.

### Set Setter
Add songs from your library and free-text rows ("mic break", "B2B with…"). Drag rows to reorder them. Each row has a cue/time and transition notes. **Plays for** sets how long a song actually plays in the set (e.g. you mix in at 1:04 of a 3:03 song, so 1:59), and the start times and total use it. **Export as text** copies the set to the clipboard or saves a `.txt` file.

---

## With Rekordbox

DJ Crate and Rekordbox stay **separate on purpose**, with one shared music folder as the single source of truth. DJ Crate gets the music and tracks your prep. Rekordbox does analysis (BPM, key, grids, cues), playlists and performance. DJ Crate never touches Rekordbox's database.

1. Import from Spotify and paste links in DJ Crate.
2. Download, then check with ▶ and the ⚠ length flag.
3. In Rekordbox, drag the library folder (or just the new files) into **Collection**. Getting new downloads into Rekordbox is a manual step, so make it part of your routine.
4. In Rekordbox: analyze, fix grids, set cues. Then tick **Cleaned** in DJ Crate.
5. Build playlists in Rekordbox. Once you've learned a song, tick **Verified**.
6. If you edit a song's title, artists or notes in DJ Crate *after* importing, right-click it in Rekordbox and choose **Reload Tag**.

**Don't rename or move MP3s in Finder.** DJ Crate never renames a file after downloading it, so Rekordbox keeps its link. Edit names in DJ Crate; that changes the tags, not the filename. If a file does go missing, DJ Crate marks it with a red **!**, the same as Rekordbox. Use **Relocate…** for one file, or **⋯ → Auto Relocate** to search a folder.

---

## Backups and moving to a new Mac

You need **two separate backups**. The library folder is the one source of truth that both apps share:

| What | Contains | How |
|---|---|---|
| **Library folder** | every MP3 + `.djcrate/` (crates, links, notes, checkmarks, sets) | copy the folder, or keep it on an external SSD |
| **Rekordbox** | cues, grids, BPM fixes, playlists, My Tags | Rekordbox → File → Library → **Backup Library** (no need to include music if you copied the folder) |

Rekordbox's own backup doesn't include DJ Crate's data, or songs you haven't imported into Rekordbox yet. That's why the library folder is the thing you move.

**On the new Mac:**
1. Put the library folder at the **same path** (e.g. `~/Music/DJ Crate/Library`).
2. Restore the Rekordbox backup. With the same path, it finds every file.
3. Install DJ Crate and point it at the folder.

If the path is different, use Rekordbox's relocate and DJ Crate's **Settings → Switch library…**. DJ Crate stores paths relative to the library folder, so it just works.

DJ Crate also saves a copy of its database **every day** (last 14 kept, in `.djcrate/backups/`). **Settings → Export crates** makes a small zip of DJ Crate's data, optionally with the music, and **Import crates** restores one.

> Don't open the same library from two Macs at once (e.g. a shared Dropbox folder). DJ Crate detects it and offers read-only mode, because two writers can corrupt the database.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "YouTube changed something" / lots of failures | **Settings → Update yt-dlp** (or the banner button). YouTube breaks downloaders every few weeks. |
| "Sign in to confirm you're not a bot" / age-restricted | **Settings → Use cookies from browser.** Pick the browser where you're logged in to YouTube. |
| "Video unavailable" | The video was removed or is blocked in your country. Paste a different link. |
| "JavaScript runtime missing" | `brew install deno`, then **Re-check** in Settings. |
| "ffmpeg missing" | `brew install ffmpeg`, then **Re-check**. |
| Spotify shows `INVALID_CLIENT: Invalid redirect URI` | Add exactly `http://127.0.0.1:8765/callback` as a Redirect URI in your Spotify app. |
| "Your Spotify account isn't on this app's user list" | Spotify dashboard → your app → **User Management** → add your Spotify email. |
| Can't import a playlist | You need to own it or collaborate on it (see the Spotify limitation above). |
| "Port 8765 is in use" | Another app is using it. Change `"port"` in `~/Library/Application Support/DJ Crate/config.json`, and add the matching redirect URI in Spotify. |
| Anything else | **Settings → Open logs** (`~/Library/Logs/DJ Crate/`). Attach `djcrate.log` to a GitHub issue. |

---

## Development

```bash
uv sync
uv run python -m djcrate.main            # app window
uv run python -m djcrate.main --browser  # same app in your browser (handy for dev tools)
uv run pytest                            # tests
python3 scripts/make_icon.py             # regenerate the app icon
```

- **Backend:** Python 3.12–3.13, FastAPI + uvicorn on `127.0.0.1:8765`, shown in a pywebview window. Data is in SQLite at `<library>/.djcrate/djcrate.db`. yt-dlp runs as a subprocess, ffmpeg converts, and mutagen writes the tags.
- **Frontend:** plain HTML/CSS/JS modules in `djcrate/web/`, with no build step.
- **Layout:** `djcrate/api.py` (routes), `db.py` (schema and migrations), `downloader.py` (queue and error handling), `spotify.py`, `yt_search.py`, `library.py` (files, relocate, import), `backup.py`, `state.py` (open library, lock, health), `main.py` (window).

Downloading from YouTube may break YouTube's terms of service. DJ Crate only downloads links you choose; use it for music you have the right to use.
