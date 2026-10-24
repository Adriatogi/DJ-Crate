"""Cutting: how songs mix into each other, and what they sample.

A *mix* is a written note with songs wired into it (`in`) and out of it
(`out`): Shangri-La (+ an acapella) → [loop ×8, reverb…] → Elevators. Mixes are
one-way and shared by every board. A song's *board* is that song in the middle
with everything it connects to (1–3 steps out) around it.
"""

from collections import deque

from . import db
from .errors import AppError

BRIEF = "id, title, artists, album, cover_url, duration_ms, file_duration_ms, download_status, ready, broken, source_url"


def label_of(conn, track_id: int) -> str:
    t = conn.execute("SELECT title, artists FROM tracks WHERE id = ?", (track_id,)).fetchone()
    return f"{t['title']} — {t['artists']}" if t else ""


def briefs(conn, ids) -> dict[int, dict]:
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    rows = conn.execute(f"SELECT {BRIEF} FROM tracks WHERE id IN ({','.join('?' * len(ids))})", ids)
    out = {}
    for r in rows:
        d = dict(r)
        d["ready"], d["broken"] = bool(d["ready"]), bool(d["broken"])
        out[d["id"]] = d
    return out


def _require_tracks(conn, ids):
    if not ids:
        return
    found = {r[0] for r in conn.execute(f"SELECT id FROM tracks WHERE id IN ({','.join('?' * len(ids))})", list(ids))}
    missing = set(ids) - found
    if missing:
        raise AppError("not_found", "One of those songs isn't in your library anymore.", 404)


# ---------------------------------------------------------------- mixes

def get_mix(conn, mix_id: int) -> dict | None:
    m = conn.execute("SELECT * FROM mixes WHERE id = ?", (mix_id,)).fetchone()
    if not m:
        return None
    songs = conn.execute("SELECT * FROM mix_songs WHERE mix_id = ? ORDER BY side, position", (mix_id,)).fetchall()
    info = briefs(conn, [s["track_id"] for s in songs])

    def side(name):
        return [{"track_id": s["track_id"], "label": s["label"], "song": info.get(s["track_id"])}
                for s in songs if s["side"] == name]

    return {"id": m["id"], "note": m["note"], "ins": side("in"), "outs": side("out"),
            "created_at": m["created_at"], "updated_at": m["updated_at"]}


def _set_mix_songs(conn, mix_id: int, in_ids: list[int], out_ids: list[int]) -> None:
    in_ids, out_ids = list(dict.fromkeys(in_ids)), list(dict.fromkeys(out_ids))
    if not in_ids or not out_ids:
        raise AppError("bad_mix", "A mix needs at least one song going in and one coming out.")
    if set(in_ids) & set(out_ids):
        raise AppError("bad_mix", "A song can't mix into itself in the same mix.")
    _require_tracks(conn, in_ids + out_ids)
    conn.execute("DELETE FROM mix_songs WHERE mix_id = ?", (mix_id,))
    for side, ids in (("in", in_ids), ("out", out_ids)):
        for pos, tid in enumerate(ids):
            conn.execute("INSERT INTO mix_songs (mix_id, track_id, label, side, position) VALUES (?, ?, ?, ?, ?)",
                         (mix_id, tid, label_of(conn, tid), side, pos))


def create_mix(conn, in_ids: list[int], out_ids: list[int], note: str) -> dict:
    with db.tx(conn):
        mix_id = conn.execute("INSERT INTO mixes (note) VALUES (?)", ((note or "").strip(),)).lastrowid
        _set_mix_songs(conn, mix_id, in_ids, out_ids)
    return get_mix(conn, mix_id)


def update_mix(conn, mix_id: int, note: str | None = None, in_ids=None, out_ids=None) -> dict:
    mix = get_mix(conn, mix_id)
    if not mix:
        raise AppError("not_found", "That mix doesn't exist anymore.", 404)
    with db.tx(conn):
        if note is not None:
            conn.execute("UPDATE mixes SET note = ?, updated_at = datetime('now') WHERE id = ?", (note.strip(), mix_id))
        if in_ids is not None or out_ids is not None:
            keep_in = [s["track_id"] for s in mix["ins"] if s["track_id"]]
            keep_out = [s["track_id"] for s in mix["outs"] if s["track_id"]]
            _set_mix_songs(conn, mix_id, in_ids if in_ids is not None else keep_in, out_ids if out_ids is not None else keep_out)
            conn.execute("UPDATE mixes SET updated_at = datetime('now') WHERE id = ?", (mix_id,))
    return get_mix(conn, mix_id)


def delete_mix(conn, mix_id: int) -> dict:
    """Returns a snapshot for Undo."""
    mix = get_mix(conn, mix_id)
    if not mix:
        raise AppError("not_found", "That mix doesn't exist anymore.", 404)
    with db.tx(conn):
        conn.execute("DELETE FROM mixes WHERE id = ?", (mix_id,))
    return mix


def restore_mix(conn, snap: dict) -> dict:
    with db.tx(conn):
        mix_id = conn.execute("INSERT INTO mixes (note, created_at) VALUES (?, coalesce(?, datetime('now')))",
                              (snap.get("note", ""), snap.get("created_at"))).lastrowid
        for side, key in (("in", "ins"), ("out", "outs")):
            for pos, s in enumerate(snap.get(key, [])):
                tid = s.get("track_id")
                if tid and not conn.execute("SELECT 1 FROM tracks WHERE id = ?", (tid,)).fetchone():
                    tid = None
                conn.execute("INSERT INTO mix_songs (mix_id, track_id, label, side, position) VALUES (?, ?, ?, ?, ?)",
                             (mix_id, tid, s.get("label", ""), side, pos))
    return get_mix(conn, mix_id)


def mixes_from(conn, track_id: int) -> list[dict]:
    """Saved mixes that start from this song (it's wired *into* the note)."""
    ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT mix_id FROM mix_songs WHERE track_id = ? AND side = 'in' ORDER BY mix_id", (track_id,))]
    return [get_mix(conn, i) for i in ids]


# ---------------------------------------------------------------- samples

def samples_of(conn, track_id: int) -> dict:
    rows = conn.execute("SELECT * FROM samples WHERE track_id = ? ORDER BY position, id", (track_id,)).fetchall()
    by = conn.execute("SELECT * FROM samples WHERE sample_track_id = ? ORDER BY id", (track_id,)).fetchall()
    info = briefs(conn, [r["sample_track_id"] for r in rows] + [r["track_id"] for r in by])
    return {
        "samples": [{**dict(r), "song": info.get(r["sample_track_id"])} for r in rows],
        "sampled_by": [{**dict(r), "song": info.get(r["track_id"])} for r in by],
    }


def add_sample(conn, track_id: int, sample_track_id: int | None, text: str) -> dict:
    text = (text or "").strip()
    if not sample_track_id and not text:
        raise AppError("bad_sample", "Pick a song or type what it samples.")
    if sample_track_id == track_id:
        raise AppError("bad_sample", "A song can't sample itself.")
    _require_tracks(conn, [track_id] + ([sample_track_id] if sample_track_id else []))
    with db.tx(conn):
        pos = conn.execute("SELECT coalesce(max(position), -1) + 1 FROM samples WHERE track_id = ?", (track_id,)).fetchone()[0]
        conn.execute("INSERT INTO samples (track_id, sample_track_id, sample_label, text, position) VALUES (?, ?, ?, ?, ?)",
                     (track_id, sample_track_id, label_of(conn, sample_track_id) if sample_track_id else "", text, pos))
    return samples_of(conn, track_id)


def update_sample(conn, sample_id: int, text: str) -> None:
    with db.tx(conn):
        conn.execute("UPDATE samples SET text = ? WHERE id = ?", ((text or "").strip(), sample_id))


def delete_sample(conn, sample_id: int) -> None:
    with db.tx(conn):
        conn.execute("DELETE FROM samples WHERE id = ?", (sample_id,))


# ---------------------------------------------------------------- song list + boards

def cutting_songs(conn, q: str = "", only_connected: bool = False) -> list[dict]:
    where, params = "1", []
    if q.strip():
        where = "(t.title LIKE ? OR t.artists LIKE ? OR t.album LIKE ?)"
        params = [f"%{q.strip()}%"] * 3
    rows = conn.execute(f"""
        SELECT t.id, t.title, t.artists, t.cover_url, t.ready, t.broken,
               (SELECT count(DISTINCT mix_id) FROM mix_songs ms WHERE ms.track_id = t.id) mixes,
               (SELECT count(*) FROM samples s WHERE s.track_id = t.id OR s.sample_track_id = t.id) samples
        FROM tracks t WHERE {where} ORDER BY t.title COLLATE NOCASE, t.artists COLLATE NOCASE""", params).fetchall()
    out = [dict(r) for r in rows]
    if only_connected:
        out = [r for r in out if r["mixes"] or r["samples"]]
    return out


def board(conn, track_id: int, depth: int = 1) -> dict:
    """Breadth-first from the song through mixes (both directions) and samples.

    `col` drives the layout: the center is column 0, songs a mix leads *to* go
    one column right, songs that lead *into* it one column left, and songs that
    share a side of a mix (e.g. an acapella layered in) stay in the same column.
    """
    if not conn.execute("SELECT 1 FROM tracks WHERE id = ?", (track_id,)).fetchone():
        raise AppError("not_found", "That song isn't in your library anymore.", 404)
    depth = max(1, min(int(depth or 1), 3))
    songs: dict[int, dict] = {track_id: {"hop": 0, "col": 0}}
    queue = deque([track_id])
    mix_ids: set[int] = set()
    sample_rows: dict[int, dict] = {}
    while queue:
        cur = queue.popleft()
        here = songs[cur]
        if here["hop"] >= depth:
            continue
        for ms in conn.execute("SELECT mix_id, side FROM mix_songs WHERE track_id = ?", (cur,)).fetchall():
            for other in conn.execute("SELECT track_id, side FROM mix_songs WHERE mix_id = ? AND track_id IS NOT NULL",
                                      (ms["mix_id"],)).fetchall():
                tid = other["track_id"]
                if tid in songs:
                    continue
                shift = 0 if other["side"] == ms["side"] else (1 if ms["side"] == "in" else -1)
                songs[tid] = {"hop": here["hop"] + 1, "col": here["col"] + shift}
                queue.append(tid)
        for s in conn.execute("SELECT * FROM samples WHERE (track_id = ? OR sample_track_id = ?) AND sample_track_id IS NOT NULL",
                              (cur, cur)).fetchall():
            sample_rows[s["id"]] = dict(s)
            other = s["sample_track_id"] if s["track_id"] == cur else s["track_id"]
            if other not in songs:
                songs[other] = {"hop": here["hop"] + 1, "col": here["col"]}
                queue.append(other)

    # Only show mixes whose songs are all on this board (the rest sit beyond the depth).
    candidate = {r[0] for r in conn.execute(
        f"SELECT DISTINCT mix_id FROM mix_songs WHERE track_id IN ({','.join('?' * len(songs))})", list(songs))}
    mixes = []
    for mid in sorted(candidate):
        m = get_mix(conn, mid)
        members = [s["track_id"] for s in m["ins"] + m["outs"] if s["track_id"]]
        if all(t in songs for t in members):
            ins = [songs[s["track_id"]]["col"] for s in m["ins"] if s["track_id"]] or [0]
            outs = [songs[s["track_id"]]["col"] for s in m["outs"] if s["track_id"]] or [0]
            m["col"] = (sum(ins) / len(ins) + sum(outs) / len(outs)) / 2
            mixes.append(m)
            mix_ids.add(mid)
    info = briefs(conn, list(songs))
    layout = {f"{r['node_kind']}:{r['node_id']}": {"x": r["x"], "y": r["y"]} for r in conn.execute(
        "SELECT * FROM board_layout WHERE board_track_id = ?", (track_id,))}
    return {
        "center": {**db.get_track(conn, track_id), **samples_of(conn, track_id)},
        "depth": depth,
        "songs": [{**info[t], **pos} for t, pos in songs.items() if t in info],
        "mixes": mixes,
        "samples": [s for s in sample_rows.values() if s["track_id"] in songs and s["sample_track_id"] in songs],
        "layout": layout,
    }


def save_layout(conn, track_id: int, positions: list[dict]) -> None:
    with db.tx(conn):
        for p in positions:
            if p.get("kind") not in ("song", "mix"):
                continue
            conn.execute(
                "INSERT INTO board_layout (board_track_id, node_kind, node_id, x, y) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(board_track_id, node_kind, node_id) DO UPDATE SET x = excluded.x, y = excluded.y",
                (track_id, p["kind"], int(p["id"]), float(p["x"]), float(p["y"])),
            )


def reset_layout(conn, track_id: int) -> None:
    with db.tx(conn):
        conn.execute("DELETE FROM board_layout WHERE board_track_id = ?", (track_id,))

