"""The Genre Rack: your own list of genres and sub-genres.

Songs only ever get genres from the rack, so "house", "House" and "deep house"
can't drift apart. Names found elsewhere (Rekordbox, MP3 tags, MusicBrainz) are
matched to rack genres through names and aliases; nothing is added or assigned
without you clicking it.
"""

import json
import re
import unicodedata

from .errors import AppError

SEP = " / "
MAX_NAME = 60


# ---------------------------------------------------------------- names

def norm(name: str) -> str:
    """Spelling-proof key: 'Hip-Hop', 'hip hop' and 'HIP  HOP' are the same."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[-_]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "")).strip()
    if not name:
        raise AppError("bad_genre", "A genre needs a name.")
    if "/" in name:
        raise AppError("bad_genre", "Genre names can't contain “/” (it separates genres in the MP3). Add them as separate genres.")
    if len(name) > MAX_NAME:
        raise AppError("bad_genre", f"Genre names can be at most {MAX_NAME} characters.")
    return name


def split_names(text: str | None) -> list[str]:
    """'Hip-Hop/Rap; Soul, Funk' -> ['Hip-Hop', 'Rap', 'Soul', 'Funk']"""
    seen, out = set(), []
    for part in re.split(r"\s*[/;,|]\s*", text or ""):
        part = part.strip()
        if part and norm(part) not in seen:
            seen.add(norm(part))
            out.append(part)
    return out


# ---------------------------------------------------------------- lookup

def get(conn, genre_id: int):
    row = conn.execute("SELECT * FROM genres WHERE id = ?", (genre_id,)).fetchone()
    if not row:
        raise AppError("not_found", "That genre doesn't exist anymore.", 404)
    return row


def resolve(conn, name: str) -> int | None:
    key = norm(name)
    if not key:
        return None
    row = conn.execute("SELECT id FROM genres WHERE norm = ?", (key,)).fetchone()
    if row:
        return row["id"]
    row = conn.execute("SELECT genre_id FROM genre_aliases WHERE norm = ?", (key,)).fetchone()
    return row["genre_id"] if row else None


def is_ignored(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM genre_ignored WHERE norm = ?", (norm(name),)).fetchone() is not None


def descendants(conn, genre_id: int) -> list[int]:
    """The genre and everything under it."""
    rows = conn.execute(
        """WITH RECURSIVE sub(id) AS (
               SELECT id FROM genres WHERE id = ?
               UNION ALL SELECT g.id FROM genres g JOIN sub ON g.parent_id = sub.id)
           SELECT id FROM sub""",
        (genre_id,),
    ).fetchall()
    return [r[0] for r in rows]


def ancestors(conn, genre_id: int) -> list[int]:
    """Parents, grandparents… (not the genre itself)."""
    out, cur, seen = [], genre_id, set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        row = conn.execute("SELECT parent_id FROM genres WHERE id = ?", (cur,)).fetchone()
        cur = row["parent_id"] if row else None
        if cur is not None:
            out.append(cur)
    return out


def path(conn, genre_id: int) -> list[str]:
    """Names from the top-level genre down to this one."""
    names, seen, cur = [], set(), genre_id
    while cur is not None and cur not in seen:
        seen.add(cur)
        row = conn.execute("SELECT name, parent_id FROM genres WHERE id = ?", (cur,)).fetchone()
        if not row:
            break
        names.append(row["name"])
        cur = row["parent_id"]
    return names[::-1]


def tree(conn) -> list[dict]:
    """The rack as nested nodes. `count` includes sub-genres, `own` is direct tags."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM genres ORDER BY position, lower(name), id")]
    members: dict[int, set[int]] = {}
    for r in conn.execute("SELECT genre_id, track_id FROM track_genres"):
        members.setdefault(r[0], set()).add(r[1])
    aliases: dict[int, list[str]] = {}
    for r in conn.execute("SELECT genre_id, norm FROM genre_aliases ORDER BY norm"):
        aliases.setdefault(r[0], []).append(r[1])
    info = {r["id"]: (r["artists"], r["cover_url"]) for r in conn.execute("SELECT id, artists, cover_url FROM tracks WHERE id IN (SELECT track_id FROM track_genres)")}
    kids: dict[int | None, list[dict]] = {}
    for g in rows:
        kids.setdefault(g["parent_id"], []).append(g)

    def build(g):
        children = [build(k) for k in kids.get(g["id"], [])]
        tracks = set(members.get(g["id"], set()))
        for c in children:
            tracks |= c.pop("_tracks")
        counts: dict[str, list] = {}
        covers: list[str] = []
        for tid in sorted(tracks):
            artists, cover = info.get(tid, ("", None))
            for a in split_artists(artists):
                counts.setdefault(norm(a), [a, 0])[1] += 1
            if cover and cover not in covers and len(covers) < 4:
                covers.append(cover)
        top = [e[0] for e in sorted(counts.values(), key=lambda e: (-e[1], e[0].lower()))[:3]]
        return {"id": g["id"], "name": g["name"], "parent_id": g["parent_id"], "position": g["position"],
                "own": len(members.get(g["id"], ())), "count": len(tracks), "aliases": aliases.get(g["id"], []),
                "top_artists": top, "covers": covers, "children": children, "_tracks": tracks}

    out = [build(g) for g in kids.get(None, [])]

    def drop(nodes):
        for n in nodes:
            n.pop("_tracks", None)
            drop(n["children"])

    drop(out)
    return out


def unsorted_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM tracks t WHERE NOT EXISTS (SELECT 1 FROM track_genres g WHERE g.track_id = t.id)").fetchone()[0]


# ---------------------------------------------------------------- editing the rack

def _name_taken(conn, name: str, *, except_id: int | None = None) -> str | None:
    """Returns a message if this name already belongs to a genre or an alias."""
    key = norm(name)
    row = conn.execute("SELECT id, name FROM genres WHERE norm = ?", (key,)).fetchone()
    if row and row["id"] != except_id:
        return f"There's already “{row['name']}” in your Genre Rack."
    row = conn.execute("SELECT a.genre_id, g.name FROM genre_aliases a JOIN genres g ON g.id = a.genre_id WHERE a.norm = ?", (key,)).fetchone()
    if row and row["genre_id"] != except_id:
        return f"“{name}” is already an alias of “{row['name']}”."
    return None


def create(conn, name: str, parent_id: int | None = None) -> int:
    name = clean_name(name)
    if parent_id is not None:
        get(conn, parent_id)
    taken = _name_taken(conn, name)
    if taken:
        existing = resolve(conn, name)
        raise AppError("genre_exists", taken, 409, existing_id=existing)
    pos = conn.execute("SELECT coalesce(max(position), -1) + 1 FROM genres WHERE parent_id IS ?", (parent_id,)).fetchone()[0]
    cur = conn.execute("INSERT INTO genres (name, norm, parent_id, position) VALUES (?, ?, ?, ?)", (name, norm(name), parent_id, pos))
    conn.execute("DELETE FROM genre_ignored WHERE norm = ?", (norm(name),))
    return cur.lastrowid


def rename(conn, genre_id: int, name: str) -> list[int]:
    """Returns the songs whose MP3 genre text changes."""
    g = get(conn, genre_id)
    name = clean_name(name)
    taken = _name_taken(conn, name, except_id=genre_id)
    if taken:
        raise AppError("genre_exists", taken, 409, existing_id=resolve(conn, name))
    conn.execute("UPDATE genres SET name = ?, norm = ? WHERE id = ?", (name, norm(name), genre_id))
    # The old spelling keeps matching as an alias, unless it only differed by case.
    if norm(g["name"]) != norm(name):
        conn.execute("INSERT OR IGNORE INTO genre_aliases (norm, genre_id) VALUES (?, ?)", (norm(g["name"]), genre_id))
    return tracks_in(conn, genre_id)


def move(conn, genre_id: int, parent_id: int | None, index: int | None = None) -> list[int]:
    get(conn, genre_id)
    if parent_id is not None:
        get(conn, parent_id)
        if parent_id in descendants(conn, genre_id):
            raise AppError("bad_parent", "A genre can't go inside its own sub-genre.")
    siblings = [r["id"] for r in conn.execute(
        "SELECT id FROM genres WHERE parent_id IS ? AND id <> ? ORDER BY position, lower(name), id", (parent_id, genre_id))]
    i = len(siblings) if index is None else max(0, min(index, len(siblings)))
    siblings.insert(i, genre_id)
    conn.execute("UPDATE genres SET parent_id = ? WHERE id = ?", (parent_id, genre_id))
    for pos, sid in enumerate(siblings):
        conn.execute("UPDATE genres SET position = ? WHERE id = ?", (pos, sid))
    return tracks_in(conn, genre_id)


def delete(conn, genre_id: int) -> list[int]:
    """Sub-genres move up one level. The genre's own song links go; returns those songs."""
    g = get(conn, genre_id)
    affected = tracks_in(conn, genre_id)
    own = [r[0] for r in conn.execute("SELECT track_id FROM track_genres WHERE genre_id = ?", (genre_id,))]
    conn.execute("UPDATE genres SET parent_id = ? WHERE parent_id = ?", (g["parent_id"], genre_id))
    conn.execute("DELETE FROM genres WHERE id = ?", (genre_id,))
    return sorted(set(affected) | set(own))


def merge(conn, src: int, dst: int) -> list[int]:
    if src == dst:
        raise AppError("bad_merge", "Pick a different genre to merge into.")
    s, d = get(conn, src), get(conn, dst)
    if dst in descendants(conn, src):
        raise AppError("bad_merge", f"“{d['name']}” is inside “{s['name']}”. Move it out first.")
    affected = tracks_in(conn, src)
    for row in conn.execute("SELECT track_id, position FROM track_genres WHERE genre_id = ?", (src,)).fetchall():
        conn.execute("INSERT OR IGNORE INTO track_genres (track_id, genre_id, position) VALUES (?, ?, ?)", (row[0], dst, row[1]))
    conn.execute("DELETE FROM track_genres WHERE genre_id = ?", (src,))
    conn.execute("UPDATE genres SET parent_id = ? WHERE parent_id = ?", (dst, src))
    conn.execute("UPDATE genre_aliases SET genre_id = ? WHERE genre_id = ?", (dst, src))
    conn.execute("UPDATE crates SET genre_id = ? WHERE genre_id = ?", (dst, src))
    conn.execute("DELETE FROM genres WHERE id = ?", (src,))
    conn.execute("INSERT OR IGNORE INTO genre_aliases (norm, genre_id) VALUES (?, ?)", (norm(s["name"]), dst))
    return sorted(set(affected) | set(tracks_in(conn, dst)))


def add_alias(conn, genre_id: int, name: str) -> None:
    g = get(conn, genre_id)
    name = clean_name(name)
    if norm(name) == g["norm"]:
        return
    taken = _name_taken(conn, name, except_id=genre_id)
    if taken:
        raise AppError("genre_exists", taken, 409, existing_id=resolve(conn, name))
    conn.execute("INSERT OR IGNORE INTO genre_aliases (norm, genre_id) VALUES (?, ?)", (norm(name), genre_id))
    conn.execute("DELETE FROM genre_ignored WHERE norm = ?", (norm(name),))


def remove_alias(conn, key: str) -> None:
    conn.execute("DELETE FROM genre_aliases WHERE norm = ?", (norm(key),))


def ignore(conn, name: str) -> None:
    if norm(name):
        conn.execute("INSERT OR IGNORE INTO genre_ignored (norm) VALUES (?)", (norm(name),))


def unignore(conn, name: str) -> None:
    conn.execute("DELETE FROM genre_ignored WHERE norm = ?", (norm(name),))


# ---------------------------------------------------------------- songs

def tracks_in(conn, genre_id: int) -> list[int]:
    """Songs tagged with this genre or any sub-genre."""
    ids = descendants(conn, genre_id)
    q = ",".join("?" for _ in ids)
    return [r[0] for r in conn.execute(f"SELECT DISTINCT track_id FROM track_genres WHERE genre_id IN ({q})", ids)]


def set_track_genres(conn, track_id: int, genre_ids: list[int]) -> None:
    ids = list(dict.fromkeys(genre_ids))
    for gid in ids:
        get(conn, gid)
    conn.execute("DELETE FROM track_genres WHERE track_id = ?", (track_id,))
    for pos, gid in enumerate(ids):
        conn.execute("INSERT INTO track_genres (track_id, genre_id, position) VALUES (?, ?, ?)", (track_id, gid, pos))


def add_to_tracks(conn, track_ids: list[int], genre_id: int) -> list[int]:
    """Returns the songs that actually changed."""
    get(conn, genre_id)
    changed = []
    for tid in track_ids:
        if conn.execute("SELECT 1 FROM track_genres WHERE track_id = ? AND genre_id = ?", (tid, genre_id)).fetchone():
            continue
        pos = conn.execute("SELECT coalesce(max(position), -1) + 1 FROM track_genres WHERE track_id = ?", (tid,)).fetchone()[0]
        conn.execute("INSERT INTO track_genres (track_id, genre_id, position) VALUES (?, ?, ?)", (tid, genre_id, pos))
        changed.append(tid)
    return changed


def remove_from_tracks(conn, track_ids: list[int], genre_id: int) -> list[int]:
    changed = []
    for tid in track_ids:
        cur = conn.execute("DELETE FROM track_genres WHERE track_id = ? AND genre_id = ?", (tid, genre_id))
        if cur.rowcount:
            changed.append(tid)
    return changed


def for_tracks(conn, track_ids: list[int]) -> dict[int, list[dict]]:
    """{track_id: [{id, name, path}]} in each song's own order."""
    if not track_ids:
        return {}
    names = {r["id"]: (r["name"], r["parent_id"]) for r in conn.execute("SELECT id, name, parent_id FROM genres")}

    def full_path(gid):
        out, seen = [], set()
        while gid is not None and gid in names and gid not in seen:
            seen.add(gid)
            out.append(names[gid][0])
            gid = names[gid][1]
        return out[::-1]

    result: dict[int, list[dict]] = {}
    q = ",".join("?" for _ in track_ids)
    for r in conn.execute(f"SELECT track_id, genre_id FROM track_genres WHERE track_id IN ({q}) ORDER BY position", list(track_ids)):
        if r[1] in names:
            p = full_path(r[1])
            result.setdefault(r[0], []).append({"id": r[1], "name": names[r[1]][0], "path": p})
    return result


def tag_string(genre_list: list[dict]) -> str:
    """What goes in the MP3's Genre tag: each genre's path, parent first, no repeats.
    Deep House (under House) + Amapiano -> 'House / Deep House / Amapiano'."""
    seen, out = set(), []
    for g in genre_list:
        for name in g["path"]:
            if norm(name) not in seen:
                seen.add(norm(name))
                out.append(name)
    return SEP.join(out)


def genre_tag_for(conn, track_id: int, *, clear: bool = False) -> str | None:
    """The tag text for one song. None means 'leave the file's Genre alone';
    '' (only when `clear`) means 'the DJ removed every genre, empty the tag'."""
    got = for_tracks(conn, [track_id]).get(track_id)
    if got:
        return tag_string(got)
    return "" if clear else None


# ---------------------------------------------------------------- live genre crates

def sync_crates(conn) -> None:
    """Keep every genre crate equal to the songs of its genre (and sub-genres)."""
    for crate in conn.execute("SELECT id, genre_id FROM crates WHERE genre_id IS NOT NULL").fetchall():
        ids = descendants(conn, crate["genre_id"])
        q = ",".join("?" for _ in ids)
        want = [r[0] for r in conn.execute(
            f"SELECT t.id FROM tracks t WHERE t.id IN (SELECT track_id FROM track_genres WHERE genre_id IN ({q})) "
            "ORDER BY t.artists COLLATE NOCASE, t.title COLLATE NOCASE", ids)]
        have = {r[0] for r in conn.execute("SELECT track_id FROM crate_tracks WHERE crate_id = ?", (crate["id"],))}
        wanted = set(want)
        for tid in have - wanted:
            conn.execute("DELETE FROM crate_tracks WHERE crate_id = ? AND track_id = ?", (crate["id"], tid))
        for pos, tid in enumerate(want):
            if tid in have:
                conn.execute("UPDATE crate_tracks SET position = ? WHERE crate_id = ? AND track_id = ?", (pos, crate["id"], tid))
            else:
                conn.execute("INSERT INTO crate_tracks (crate_id, track_id, position) VALUES (?, ?, ?)", (crate["id"], tid, pos))


def make_crate(conn, genre_id: int, parent_id: int | None) -> int:
    """A crate that follows this genre. Returns the existing one if there is one."""
    g = get(conn, genre_id)
    row = conn.execute("SELECT id FROM crates WHERE genre_id = ?", (genre_id,)).fetchone()
    if row:
        return row["id"]
    if parent_id is not None:
        parent = conn.execute("SELECT kind FROM crates WHERE id = ?", (parent_id,)).fetchone()
        if not parent or parent["kind"] != "folder":
            raise AppError("bad_parent", "A crate can only go inside a folder.")
    pos = conn.execute("SELECT coalesce(max(position), -1) + 1 FROM crates WHERE parent_id IS ?", (parent_id,)).fetchone()[0]
    cur = conn.execute("INSERT INTO crates (name, kind, parent_id, position, genre_id) VALUES (?, 'crate', ?, ?, ?)",
                       (g["name"], parent_id, pos, genre_id))
    sync_crates(conn)
    return cur.lastrowid


def guard_not_genre_crate(conn, crate_id: int) -> None:
    row = conn.execute("SELECT c.genre_id, g.name FROM crates c LEFT JOIN genres g ON g.id = c.genre_id WHERE c.id = ?", (crate_id,)).fetchone()
    if row and row["genre_id"] is not None:
        raise AppError("genre_crate", f"This crate follows the genre “{row['name']}” — tag songs with it instead.", 409)


# ---------------------------------------------------------------- attaching to song dicts

def split_artists(text: str | None) -> list[str]:
    """Spotify joins artists with ', ' (see spotify.parse_track), so split the same way."""
    return [a.strip() for a in (text or "").split(",") if a.strip()]


class _Names:
    """Name/alias lookup loaded once per batch of songs."""

    def __init__(self, conn):
        self.by_norm: dict[str, tuple[int, str]] = {}   # any spelling -> (genre id, genre name)
        for r in conn.execute("SELECT id, name, norm FROM genres"):
            self.by_norm[r["norm"]] = (r["id"], r["name"])
        for r in conn.execute("SELECT a.norm an, g.id, g.name FROM genre_aliases a JOIN genres g ON g.id = a.genre_id"):
            self.by_norm.setdefault(r["an"], (r["id"], r["name"]))
        self.ignored = {r[0] for r in conn.execute("SELECT norm FROM genre_ignored")}

    def canon(self, name: str) -> str:
        hit = self.by_norm.get(norm(name))
        return norm(hit[1]) if hit else norm(name)


def _suggestions(t: dict, names: _Names, found: list[dict]) -> list[dict]:
    """Genre names known for a song with no rack genre: Rekordbox's, the MP3's, then MusicBrainz's.
    Each is matched to the rack when possible; unmatched ones are offered with a '?'."""
    out, seen = [], set()
    candidates = [(src, part, "song") for src, text in (("Rekordbox", t.get("rekordbox_genre")), ("MP3", t.get("file_genre")))
                  for part in split_names(text)]
    candidates += [("MusicBrainz", it["name"], it.get("level", "song")) for it in found]
    for source, part, level in candidates:
        key = norm(part)
        if key in names.ignored:
            continue
        hit = names.by_norm.get(key)
        ident = hit[0] if hit else key
        if ident in seen:
            continue
        seen.add(ident)
        out.append({"label": hit[1] if hit else part, "genre_id": hit[0] if hit else None, "source": source, "level": level})
    return out


def attach(conn, tracks: list[dict]) -> None:
    """Adds `genres`, `genre_tag`, `rekordbox_genre_differs` and `genre_suggestions` to song dicts."""
    got = for_tracks(conn, [t["id"] for t in tracks])
    names = None
    mb: dict[int, list[dict]] = {}
    empty = [t["id"] for t in tracks if t["id"] not in got]
    if empty:
        q = ",".join("?" for _ in empty)
        for r in conn.execute(f"SELECT track_id, items_json FROM genre_suggest WHERE status = 'found' AND track_id IN ({q})", empty):
            mb[r[0]] = json.loads(r[1])
    for t in tracks:
        t["genres"] = got.get(t["id"], [])
        t["genre_tag"] = tag_string(t["genres"])
        t["rekordbox_genre_differs"] = False
        t["genre_suggestions"] = []
        rb = (t.get("rekordbox_genre") or "").strip()
        if not (rb or t.get("file_genre") or t["id"] in mb):
            continue
        names = names or _Names(conn)
        if t["genres"]:
            if rb:
                mine = {norm(n) for g in t["genres"] for n in g["path"]}
                theirs = {names.canon(p) for p in split_names(rb)}
                t["rekordbox_genre_differs"] = mine != theirs
        else:
            t["genre_suggestions"] = _suggestions(t, names, mb.get(t["id"], []))


def needed_from_musicbrainz(conn) -> list[dict]:
    """MusicBrainz genre names that songs without a rack genre were matched to, and that
    your rack doesn't know yet — the ones you might need. [{name, songs, level}], most songs first."""
    found: dict[str, dict] = {}
    rows = conn.execute(
        "SELECT gs.track_id, gs.items_json FROM genre_suggest gs WHERE gs.status = 'found' "
        "AND NOT EXISTS (SELECT 1 FROM track_genres g WHERE g.track_id = gs.track_id)").fetchall()
    ignored = {r[0] for r in conn.execute("SELECT norm FROM genre_ignored")}
    names = _Names(conn)
    for r in rows:
        for item in json.loads(r["items_json"]):
            key = norm(item["name"])
            if key in names.by_norm or key in ignored:
                continue
            e = found.setdefault(key, {"name": item["name"], "tracks": set(), "levels": set()})
            e["tracks"].add(r["track_id"])
            e["levels"].add(item.get("level", "song"))
    out = [{"name": e["name"], "songs": len(e["tracks"]), "level": "song" if "song" in e["levels"] else "artist"} for e in found.values()]
    out.sort(key=lambda e: (-e["songs"], e["name"].lower()))
    return out
