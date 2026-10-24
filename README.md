# DJ Crate

**Spotify playlists → crates → the right audio, every time.**

Tools like spotdl download your Spotify playlists by guessing the YouTube video. They often get the wrong one: the radio edit, a live version, a music-video intro. DJ Crate does the guessing differently: **you** pick the link.

1. Import a Spotify playlist. It becomes a **crate** with every song's title, artists, album, cover and length.
2. Paste a YouTube link for each song, or use **Find ▾**. It shows the top results, highlights the one whose length matches Spotify's, and warns about "Radio Edit", "Live", "Sped up" and other versions.
3. Hit **Download All**. Each song becomes a tagged `Title - Artist.mp3` in one library folder, which Rekordbox imports.
4. Track your prep with three checkboxes: **Edited** (you cut the audio on purpose), **Ready** (prepped and gig-ready), **Broken** (needs fixing; say what in the notes). Each song also has its own notes.
5. Plan sets in **Set Setter**: an ordered running list with cue times, transition notes and running time, exportable as text.

Everything stays on your Mac. DJ Crate runs as a normal app window; no browser tab or terminal is needed.

---

## Install (macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/Adriatogi/DJ-Crate/main/scripts/install.sh | bash
```

This needs [Homebrew](https://brew.sh). The installer adds whatever is missing (`ffmpeg`, `uv`, and `deno` if you have neither Deno nor Node). It clones the repo to `~/DJ-Crate`, installs the dependencies, and builds **DJ Crate.app** inside that folder. Open it from there or with Spotlight, and drag it to your Dock to keep it handy.

Everything lives in that one folder:

```
~/DJ-Crate/
├── DJ Crate.app      ← the app (built by the installer)
├── Libraries/
│   ├── My Library/   ← your music + DJ Crate's data
│   └── Sam/          ← another person's library (optional)
└── djcrate/ …        ← the code
```

> Deleting the `DJ-Crate` folder deletes the libraries inside it too. Back it up (see below) before you remove or reinstall it.

<details>
<summary>Prefer to do it by hand?</summary>

```bash
brew install ffmpeg uv deno       # deno: skip if you already have Node
git clone https://github.com/Adriatogi/DJ-Crate.git ~/DJ-Crate
cd ~/DJ-Crate
uv sync
scripts/make_app.sh              # builds ~/DJ-Crate/DJ Crate.app
# or run it directly:  uv run python -m djcrate.main
```
</details>

**Update:** `~/DJ-Crate/scripts/update.sh`. It pulls the latest version, reinstalls and rebuilds the app. If anything fails, it rolls back to the version you had. Your music and crates are never touched.

---

## First launch

1. **Create your library.** Give it a name, and it's created in `Libraries/<name>` inside the DJ-Crate folder. You can also use **Open a folder…** to pick any other folder, like an external SSD. This one folder holds every MP3 *and* DJ Crate's data (`.djcrate/`). It's what Rekordbox imports, and what you copy to move to a new Mac.
2. **Connect Spotify** (once, about 2 minutes). Everyone uses their own free Spotify developer app, so there's no shared app and no user limit:
   1. Go to [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) and log in. **Your account needs Spotify Premium.**
   2. **Create app.** Use any name (e.g. "DJ Crate") and description, and tick **Web API**.
   3. **Redirect URI:** `http://127.0.0.1:8765/callback`. Enter it exactly like that; Settings has a copy button.
   4. Open the app's settings, copy the **Client ID**, and paste it into **DJ Crate → Settings → Spotify**. Then click **Save** and **Log in to Spotify**.

   Your login is stored in the macOS Keychain, never in the library folder.

> **Spotify limitation:** since 2026, Spotify only lets apps read the songs in playlists **you own or collaborate on**, and Liked Songs. For anyone else's playlist (including Spotify's own), select all its songs in Spotify, choose **Add to playlist → New playlist**, and import that.

### More than one person on the same Mac
Every library is fully separate: its own music, crates, notes, sets **and Spotify login**. When DJ Crate opens, it asks **which library** to use. Tick **"Don't ask again"** to always open the last one. Switch any time by clicking the library name in the top bar, or with **Settings → Switch library…**. Removing a library from the list never deletes its folder.

Each person logs in to their own Spotify account. They can create their own developer app (steps above) or share yours. If they share yours, add their Spotify email under **User Management** in your Spotify dashboard, because unapproved apps only allow listed users.

---

## Using it

### Crates
- **All Tracks** holds every song you have. **Smart crates** fill themselves: Needs Link, Ready to Download, Downloaded, Ready, Broken, ⚠ Check Version, Missing Files.
- **Your crates and folders:** create them with **+♪** and **+📁**. Drag crates into folders, and drag songs onto crates. Every crate shows its progress (`12/30 ↓ · 8 ready · 1 broken`).
- **One song, one record.** If a song is in five crates, it's still one song. Its link, file, checkmarks and notes are shared everywhere.
- **Re-sync** a Spotify crate to pick up new songs. Songs removed on Spotify are **kept**, dimmed with a "Removed on Spotify" badge, so you never lose a file or your notes.
- **Select songs** with the checkboxes, or the header box to select all. Then **Mark** (Edited / Ready / Broken), **Add to crate**, **Download**, **Remove from crate** (only takes it out of that crate), or **Delete from library** (asks first; the MP3 goes to the Trash).
- **Sort** by clicking a column header (first click ascending, second descending, third back to default), or use **Sort ▾**, which also has Date added.
- **Add track** handles songs that aren't on Spotify, such as bootlegs and edits; SoundCloud and Bandcamp links work too. **Import folder** adds MP3s you already have.

### Links and downloads
- Paste a link into the row, or use **Find ▾**. Green means the result's length matches Spotify's, and ⚠ marks a different version.
- **Find remembers results** for a week ("Saved results · Search again"). When you open Find, DJ Crate also quietly searches the **next 3 songs** that need a link, so theirs open instantly. Change this under **Settings → Downloads → Search ahead**, from 0 (off) to 10 max. If YouTube rate-limits the searches, it tells you and pauses for 3 minutes.
- Files are saved as **`Title - Artist.mp3`**, with all artists, features included. They're tagged with title, artists, album, year, ISRC and cover art, and your **notes go into the comment tag**.
- **Format: MP3, constant 192 kbps.** YouTube's audio is about 130 kbps, so higher bitrates don't add quality. Constant bitrate on purpose: variable-bitrate MP3s can make cue points drift between Rekordbox and CDJs.
- **⚠ Check Version:** if a download is more than 5 seconds off Spotify's length, you probably got a different version. If you cut the song on purpose, tick **Edited** and the warning goes away.
- **▶** previews a downloaded song in a player under its row. Drag the slider to skip around; **Space** plays and pauses, **Esc** closes.
- **The bell (Activity)** shows downloads in progress and failures, with the reason and a Retry button.

### Cutting: how your songs mix into each other
Every song has its own **board**, a node editor with the song in the middle:
- **A mix is a note between songs:** *Shangri-La → [loop the last 8 bars, reverb, slow it down, bring in the Elevators instrumental first] → Elevators*.
  - Mixes are one-way. Elevators → Shangri-La would be its own mix.
  - Several songs can wire into one note (e.g. layering an acapella), and a note can lead to several songs.
- **To make one,** drag from a song's **●** onto another song, or onto empty space to pick one, then write the note. You can also use **+ Connect a song…**.
- **Steps 1 · 2 · 3** shows the songs your song connects to as faded "ghost" nodes, then their connections, and so on. Double-click a ghost to open its board.
- **Samples:** on the right, add what a song samples. It can be text, or a link to another song in your library, which shows as a dotted line, plus a "Sampled by" list.
- Drag nodes around and DJ Crate remembers the layout. **Tidy up** resets it.
- To remove something, select a note or wire and press **Delete**.

### Set Setter
Build a set as a **board**:
- **Add nodes:** songs with **+ Song** and free-text cards with **+ Text** ("mic break", "B2B with…").
- **Wire songs together through mix notes.** Branch out as much as you like, e.g. a backup song if the crowd isn't feeling it.
- **The bright wires are your main path:** the set as you'd play it. Select any wire to **Make main path** or **Add mix note here**.
- **Select a song** to see the **mixes you've saved from it in Cutting**. Click one to drop it in.
- **Save as mix** on a note sends that transition to Cutting, so you can reuse it in other sets.

**List** shows the main path as a running order with start times and the total length. **Plays for** sets how long a song actually plays in the set (e.g. you mix in at 1:04 of a 3:03 song, so 1:59). Cue and transition notes can be edited there too. **Export as text** copies the main path to the clipboard or saves a `.txt` file, plus an "Alternatives" section for your branches.

---

## With Rekordbox

DJ Crate and Rekordbox stay **separate on purpose**, with one shared music folder as the single source of truth. DJ Crate gets the music and tracks your prep. Rekordbox does analysis (BPM, key, grids, cues), playlists and performance. DJ Crate never touches Rekordbox's database.

1. Import from Spotify and paste links in DJ Crate.
2. Download, then check with ▶ and the ⚠ length flag.
3. In Rekordbox, drag the library folder (or just the new files) into **Collection**. Getting new downloads into Rekordbox is a manual step, so make it part of your routine.
4. In Rekordbox: analyze, fix grids, set cues, and build playlists. Once a song is prepped and you know it, tick **Ready** in DJ Crate. If something's off (bad rip, wrong version, clipping), tick **Broken** and put what's wrong in its notes.
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
1. Put the library folder at the **same path** (e.g. `~/DJ-Crate/Libraries/My Library`).
2. Restore the Rekordbox backup. With the same path, it finds every file.
3. Install DJ Crate and point it at the folder.

If the path is different, use Rekordbox's relocate, and in DJ Crate use **Switch library → Open a folder…**. DJ Crate stores paths relative to the library folder, so it just works.

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
