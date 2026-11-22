# DJ Crate

**Spotify playlists → crates → the right audio, every time.**

Tools like spotdl guess the YouTube video for each song and often get the wrong one (radio edit, live version, music-video intro). In DJ Crate, **you** pick the link.

1. **Import a Spotify playlist.** It becomes a crate with every song's title, artists, album, cover and length.
2. **Pick a link** for each song: paste one, or use **Find ▾**, which highlights the result whose length matches Spotify's and warns about radio edits, live versions and the like.
3. **Download All.** Each song becomes a tagged `Title - Artist.mp3` (constant 192 kbps) in one library folder that Rekordbox imports.
4. **Prep:** notes, **Cutting** (how songs mix into each other) and **Set Setter** (plan sets as a board).

It also flags clean versions, shows BPM, key and My Tags from Rekordbox, and sends your crates and sets to Rekordbox as playlists. Everything stays on your Mac. **Open the Docs tab in the app for how everything works.**

**Philosophy:** the DJ decides. No recommendation algorithm, no auto-mixing.

---

## Install (macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/Adriatogi/DJ-Crate/main/scripts/install.sh | bash
```

Needs [Homebrew](https://brew.sh). The installer adds `ffmpeg`, `uv` and `deno` if missing, clones the repo to `~/DJ-Crate`, and builds **DJ Crate.app** there. Your libraries live in `~/DJ-Crate/Libraries/`, so **back up before deleting that folder**.

<details>
<summary>By hand</summary>

```bash
brew install ffmpeg uv deno       # deno: skip if you already have Node
git clone https://github.com/Adriatogi/DJ-Crate.git ~/DJ-Crate
cd ~/DJ-Crate && uv sync
scripts/make_app.sh               # or run directly: uv run python -m djcrate.main
```
</details>

**Update:** `~/DJ-Crate/scripts/update.sh`. It rolls back if anything fails, and never touches your music or crates.

## First launch

1. **Create a library** (or **Open a folder…**, e.g. on an external SSD). This one folder holds every MP3 and DJ Crate's data. It's what Rekordbox imports and what you copy to a new Mac.
2. **Connect Spotify** (once, ~2 minutes, needs **Spotify Premium**):
   1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) → **Create app**, any name, tick **Web API**.
   2. Redirect URI: `http://127.0.0.1:8765/callback` (exactly; Settings has a copy button).
   3. Paste the app's **Client ID** into **Settings → Spotify**, then **Log in to Spotify**.

> Spotify only lets apps read playlists **you own or collaborate on**, plus Liked Songs. For anyone else's, copy its songs into a new playlist of your own and import that.

**Several people on one Mac:** each library is separate (music, crates, sets and Spotify login). Switch by clicking the library name in the top bar. Someone sharing your Spotify developer app must be added under **User Management** in your Spotify dashboard.

## With Rekordbox

DJ Crate gets the music and tracks your prep; Rekordbox analyses, sets cues and plays. They share one music folder and talk only through XML files. DJ Crate **never touches Rekordbox's database**.

- **New downloads:** drag the library folder (or new files) into Rekordbox's Collection. After editing a song's title or artists in DJ Crate, right-click it in Rekordbox → **Reload Tag**.
- **BPM, key and My Tags in:** in Rekordbox, File → **Export Collection in xml format**, then pick that file in **Settings → Rekordbox**. Export again over the same file after changes. My Tags are read-only and need Rekordbox's setting that adds My Tags to Comments.
- **Crates and sets out:** turn on **Settings → Rekordbox → Send crates & sets**. Only playlists are sent, never BPM, key, grids, cues or tags.
- **Don't rename or move MP3s in Finder.** If a file goes missing, use **Relocate…** or **⋯ → Auto Relocate**.

The Docs tab has an FAQ with the step-by-step Rekordbox setup.

## Backups and a new Mac

Keep **two backups**: the **library folder** (MP3s + `.djcrate/`) and **Rekordbox's own backup** (File → Library → Backup Library: cues, grids, playlists, My Tags). DJ Crate also keeps daily database copies (last 14) in `.djcrate/backups/`.

On a new Mac: put the library folder at the **same path**, restore the Rekordbox backup, install DJ Crate and open the folder. Don't open one library from two Macs at once (e.g. a shared Dropbox folder).

## Troubleshooting

| Problem | Fix |
|---|---|
| Lots of download failures | **Settings → Update yt-dlp.** YouTube breaks downloaders every few weeks. |
| "Sign in to confirm you're not a bot" | **Settings → Use cookies from browser.** |
| "Video unavailable" | Removed or blocked in your country. Use a different link. |
| "JavaScript runtime" or "ffmpeg missing" | `brew install deno` / `brew install ffmpeg`, then **Re-check** in Settings. |
| Spotify `INVALID_CLIENT: Invalid redirect URI` | Add exactly `http://127.0.0.1:8765/callback` in your Spotify app. |
| "Not on this app's user list" | Spotify dashboard → your app → **User Management** → add the email. |
| "Port 8765 is in use" | Change `"port"` in `~/Library/Application Support/DJ Crate/config.json` and the redirect URI to match. |
| Anything else | **Settings → Open logs** and attach `djcrate.log` to a GitHub issue. |

## Development

```bash
uv sync
uv run python -m djcrate.main            # app window
uv run python -m djcrate.main --browser  # in your browser (dev tools)
uv run pytest
```

Python 3.12–3.13, FastAPI on `127.0.0.1:8765` in a pywebview window, SQLite at `<library>/.djcrate/djcrate.db`, yt-dlp + ffmpeg + mutagen for downloads and tags. The frontend is plain JS modules in `djcrate/web/` with no build step.

Downloading from YouTube may break YouTube's terms of service. DJ Crate only downloads links you choose; use it for music you have the right to use.
