"""Set Setter boards: song / mix-note / text nodes wired together, branches
allowed. The *main path* (edges marked main) is the set as you'd play it, and
is what the list view, running time and text export show.
"""

from . import cutting, db
from .errors import AppError


def fmt_ms(ms: int | None) -> str:
    if not ms:
        return "?:??"
    s = int(ms // 1000)
    h, m, s = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _nodes(conn, set_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT n.*, t.title, t.artists, t.cover_url, t.download_status, t.ready, t.broken,
                  coalesce(t.file_duration_ms, t.duration_ms) duration_ms, m.note mix_note
           FROM set_nodes n LEFT JOIN tracks t ON t.id = n.track_id LEFT JOIN mixes m ON m.id = n.mix_id
           WHERE n.set_id = ? ORDER BY n.id""", (set_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ready"], d["broken"] = bool(d["ready"]), bool(d["broken"])
        if d["kind"] == "mix" and d["mix_id"] and d["mix_note"] is not None:
            d["text"] = d["mix_note"]  # linked to a saved mix: the saved note is the truth
        d["saved"] = bool(d["mix_id"] and d["mix_note"] is not None)
        d.pop("mix_note")
        out.append(d)
    return out


def _edges(conn, set_id: int) -> list[dict]:
    return [{**dict(r), "main": bool(r["main"])} for r in conn.execute(
        "SELECT id, from_node, to_node, main FROM set_edges WHERE set_id = ? ORDER BY id", (set_id,))]


def main_path(nodes: list[dict], edges: list[dict]) -> list[dict]:
    """Walk main edges from the start; mix/text-between notes become each row's transition."""
    by_id = {n["id"]: n for n in nodes}
    main = [e for e in edges if e["main"] and e["from_node"] in by_id and e["to_node"] in by_id]
    if not main:
        songs = sorted((n for n in nodes if n["kind"] == "song"), key=lambda n: (n["x"], n["id"]))
        chain = songs[:1]
    else:
        has_in = {e["to_node"] for e in main}
        starts = sorted((by_id[e["from_node"]] for e in main if e["from_node"] not in has_in), key=lambda n: (n["x"], n["id"]))
        cur = (starts or [by_id[main[0]["from_node"]]])[0]
        chain, seen = [cur], {cur["id"]}
        nxt = {}
        for e in main:
            nxt.setdefault(e["from_node"], e["to_node"])
        while cur["id"] in nxt and nxt[cur["id"]] not in seen:
            cur = by_id[nxt[cur["id"]]]
            chain.append(cur)
            seen.add(cur["id"])
    rows, elapsed = [], 0
    for node in chain:
        if node["kind"] == "mix":
            if rows:
                rows[-1]["transition"] = {"node_id": node["id"], "text": node["text"], "saved": node["saved"]}
            continue
        played = node["length_ms"] or node.get("duration_ms") or 0
        rows.append({
            "node_id": node["id"], "kind": node["kind"], "track_id": node["track_id"],
            "title": node.get("title"), "artists": node.get("artists"), "cover_url": node.get("cover_url"),
            "label": node["label"], "text": node["text"], "download_status": node.get("download_status"),
            "ready": node.get("ready"), "broken": node.get("broken"),
            "duration_ms": node.get("duration_ms"), "length_ms": node["length_ms"], "cue_time": node["cue_time"],
            "start_ms": elapsed if node["kind"] == "song" else None, "played_ms": played if node["kind"] == "song" else 0,
            "transition": None,
        })
        if node["kind"] == "song":
            elapsed += played
    return rows


def get_board(conn, set_id: int) -> dict:
    s = conn.execute("SELECT * FROM sets WHERE id = ?", (set_id,)).fetchone()
    if not s:
        raise AppError("not_found", "That set doesn't exist anymore.", 404)
    nodes, edges = _nodes(conn, set_id), _edges(conn, set_id)
    path = main_path(nodes, edges)
    # ✓ "saved mix" between consecutive main-path songs (direct song→song wire counts too)
    for a, b in zip(path, path[1:]):
        if a["kind"] == "song" and b["kind"] == "song" and a["track_id"] and b["track_id"] and not (a["transition"] or {}).get("saved"):
            a["saved_mix_id"] = known_mix(conn, a["track_id"], b["track_id"])
    return {"set": dict(s), "nodes": nodes, "edges": edges, "main_path": path,
            "total_ms": sum(r["played_ms"] for r in path)}


def known_mix(conn, a: int, b: int) -> int | None:
    row = conn.execute(
        """SELECT i.mix_id FROM mix_songs i JOIN mix_songs o ON o.mix_id = i.mix_id
           WHERE i.side = 'in' AND i.track_id = ? AND o.side = 'out' AND o.track_id = ? LIMIT 1""", (a, b)).fetchone()
    return row[0] if row else None


def replace_board(conn, set_id: int, nodes: list[dict], edges: list[dict]) -> dict:
    """Save the whole board. Existing node ids are kept; new nodes (id missing
    or negative) get ids, and edges are remapped. Editing a node linked to a
    saved mix updates that mix everywhere."""
    if not conn.execute("SELECT 1 FROM sets WHERE id = ?", (set_id,)).fetchone():
        raise AppError("not_found", "That set doesn't exist anymore.", 404)
    with db.tx(conn):
        old = {r["id"] for r in conn.execute("SELECT id FROM set_nodes WHERE set_id = ?", (set_id,))}
        conn.execute("DELETE FROM set_edges WHERE set_id = ?", (set_id,))
        conn.execute("DELETE FROM set_nodes WHERE set_id = ?", (set_id,))
        idmap = {}
        for n in nodes:
            kind = n.get("kind")
            if kind not in ("song", "mix", "text"):
                raise AppError("bad_node", "Unknown node type.")
            track_id = n.get("track_id") if kind == "song" else None
            if track_id and not conn.execute("SELECT 1 FROM tracks WHERE id = ?", (track_id,)).fetchone():
                track_id = None
            mix_id = n.get("mix_id") if kind == "mix" else None
            if mix_id and not conn.execute("SELECT 1 FROM mixes WHERE id = ?", (mix_id,)).fetchone():
                mix_id = None
            text = (n.get("text") or "").strip() if kind != "song" else ""
            if mix_id:
                conn.execute("UPDATE mixes SET note = ?, updated_at = datetime('now') WHERE id = ? AND note <> ?", (text, mix_id, text))
            label = n.get("label") or (cutting.label_of(conn, track_id) if track_id else "")
            length = n.get("length_ms") if n.get("length_ms") and n["length_ms"] > 0 else None
            keep_id = n["id"] if isinstance(n.get("id"), int) and n["id"] > 0 and n["id"] in old else None
            cur = conn.execute(
                "INSERT INTO set_nodes (id, set_id, kind, track_id, mix_id, text, label, cue_time, length_ms, x, y) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (keep_id, set_id, kind, track_id, mix_id, text, label, n.get("cue_time") or "", length,
                 float(n.get("x", 0)), float(n.get("y", 0))),
            )
            idmap[n.get("id")] = cur.lastrowid
        for e in edges:
            a, b = idmap.get(e.get("from_node")), idmap.get(e.get("to_node"))
            if a and b and a != b:
                conn.execute("INSERT INTO set_edges (set_id, from_node, to_node, main) VALUES (?, ?, ?, ?)",
                             (set_id, a, b, int(bool(e.get("main")))))
        conn.execute("UPDATE sets SET updated_at = datetime('now') WHERE id = ?", (set_id,))
    board = get_board(conn, set_id)
    board["idmap"] = {str(k): v for k, v in idmap.items()}
    return board


def patch_node(conn, set_id: int, node_id: int, changes: dict) -> dict:
    node = conn.execute("SELECT * FROM set_nodes WHERE id = ? AND set_id = ?", (node_id, set_id)).fetchone()
    if not node:
        raise AppError("not_found", "That part of the set doesn't exist anymore.", 404)
    allowed = {k: v for k, v in changes.items() if k in ("cue_time", "length_ms", "text")}
    if "length_ms" in allowed and (not allowed["length_ms"] or allowed["length_ms"] <= 0):
        allowed["length_ms"] = None
    with db.tx(conn):
        if allowed:
            sets = ", ".join(f"{k} = ?" for k in allowed)
            conn.execute(f"UPDATE set_nodes SET {sets} WHERE id = ?", (*allowed.values(), node_id))
        if "text" in allowed and node["mix_id"]:
            conn.execute("UPDATE mixes SET note = ?, updated_at = datetime('now') WHERE id = ?", (allowed["text"].strip(), node["mix_id"]))
        conn.execute("UPDATE sets SET updated_at = datetime('now') WHERE id = ?", (set_id,))
    return get_board(conn, set_id)


def add_transition(conn, set_id: int, after_node: int, text: str) -> dict:
    """List view: write a transition after a row that has none yet — splices a mix
    node into the main path between that row and the next."""
    nodes = {n["id"]: n for n in _nodes(conn, set_id)}
    if after_node not in nodes:
        raise AppError("not_found", "That part of the set doesn't exist anymore.", 404)
    a = nodes[after_node]
    with db.tx(conn):
        mid = conn.execute("INSERT INTO set_nodes (set_id, kind, text, x, y) VALUES (?, 'mix', ?, ?, ?)",
                           (set_id, text.strip(), a["x"] + 150, a["y"] + 90)).lastrowid
        edge = conn.execute("SELECT id, to_node FROM set_edges WHERE set_id = ? AND from_node = ? AND main = 1 LIMIT 1",
                            (set_id, after_node)).fetchone()
        if edge:
            conn.execute("UPDATE set_edges SET to_node = ? WHERE id = ?", (mid, edge["id"]))
            conn.execute("INSERT INTO set_edges (set_id, from_node, to_node, main) VALUES (?, ?, ?, 1)", (set_id, mid, edge["to_node"]))
        else:
            conn.execute("INSERT INTO set_edges (set_id, from_node, to_node, main) VALUES (?, ?, ?, 1)", (set_id, after_node, mid))
    return get_board(conn, set_id)


def save_mix(conn, set_id: int, node_id: int) -> dict:
    """Turn a set-only mix note into a saved mix (shared with Cutting)."""
    node = conn.execute("SELECT * FROM set_nodes WHERE id = ? AND set_id = ? AND kind = 'mix'", (node_id, set_id)).fetchone()
    if not node:
        raise AppError("not_found", "That mix note doesn't exist anymore.", 404)
    ins = [r[0] for r in conn.execute(
        """SELECT n.track_id FROM set_edges e JOIN set_nodes n ON n.id = e.from_node
           WHERE e.to_node = ? AND n.kind = 'song' AND n.track_id IS NOT NULL""", (node_id,))]
    outs = [r[0] for r in conn.execute(
        """SELECT n.track_id FROM set_edges e JOIN set_nodes n ON n.id = e.to_node
           WHERE e.from_node = ? AND n.kind = 'song' AND n.track_id IS NOT NULL""", (node_id,))]
    if not ins or not outs:
        raise AppError("bad_mix", "Wire a song into this note and a song out of it first.")
    mix = cutting.create_mix(conn, ins, outs, node["text"])
    with db.tx(conn):
        conn.execute("UPDATE set_nodes SET mix_id = ? WHERE id = ?", (mix["id"], node_id))
    return get_board(conn, set_id)


def save_pair_as_mix(conn, set_id: int, a_node: int, b_node: int, text: str = "") -> dict:
    """List view "Save as mix" between two consecutive songs."""
    a = conn.execute("SELECT track_id FROM set_nodes WHERE id = ? AND set_id = ?", (a_node, set_id)).fetchone()
    b = conn.execute("SELECT track_id FROM set_nodes WHERE id = ? AND set_id = ?", (b_node, set_id)).fetchone()
    if not a or not b or not a["track_id"] or not b["track_id"]:
        raise AppError("bad_mix", "Both sides need to be songs from your library.")
    cutting.create_mix(conn, [a["track_id"]], [b["track_id"]], text)
    return get_board(conn, set_id)


def snapshot(conn, set_id: int) -> dict:
    data = get_board(conn, set_id)
    return {"set": data["set"], "nodes": data["nodes"], "edges": data["edges"]}


def restore(conn, snap: dict) -> int:
    s = snap["set"]
    with db.tx(conn):
        sid = conn.execute("INSERT INTO sets (name, notes, created_at) VALUES (?, ?, coalesce(?, datetime('now')))",
                           (s["name"], s.get("notes", ""), s.get("created_at"))).lastrowid
    replace_board(conn, sid, [{**n, "id": -n["id"]} for n in snap["nodes"]],
                  [{**e, "from_node": -e["from_node"], "to_node": -e["to_node"]} for e in snap["edges"]])
    return sid


def set_text(data: dict) -> str:
    s, path = data["set"], data["main_path"]
    songs = [r for r in path if r["kind"] == "song"]
    lines = [f"SET: {s['name']}", f"{len(songs)} songs · {fmt_ms(data['total_ms'])} total", ""]
    if (s["notes"] or "").strip():
        lines += ["NOTES", s["notes"].strip(), ""]
    lines.append("RUNNING ORDER")
    n = 0
    for r in path:
        if r["kind"] == "text":
            lines.append(f"   — {r['text'] or '(note)'} —")
        else:
            n += 1
            name = f"{r['title']} — {r['artists']}" if r["title"] else (r["label"] or "(deleted song)")
            length = fmt_ms(r["played_ms"])
            if r["length_ms"] and r["duration_ms"] and r["length_ms"] != r["duration_ms"]:
                length = f"plays {length} of {fmt_ms(r['duration_ms'])}"
            lines.append(f"{n:>2}. [{fmt_ms(r['start_ms']) if r['start_ms'] else '0:00'}] {name} ({length})")
        if (r["cue_time"] or "").strip():
            lines.append(f"      Cue: {r['cue_time'].strip()}")
        if r["transition"] and r["transition"]["text"].strip():
            lines.append(f"      Transition: {r['transition']['text'].strip()}")
    # Branches: wires out of main-path nodes that aren't the main one.
    on_path = {r["node_id"] for r in path} | {r["transition"]["node_id"] for r in path if r["transition"]}
    by_id = {nd["id"]: nd for nd in data["nodes"]}
    alts: dict[int, list[str]] = {}
    for e in data["edges"]:
        if e["main"] or e["from_node"] not in on_path or e["to_node"] in on_path:
            continue
        target = by_id.get(e["to_node"])
        note = ""
        if target and target["kind"] == "mix":
            note = target["text"]
            nxt = [by_id.get(x["to_node"]) for x in data["edges"] if x["from_node"] == target["id"]]
            target = next((t for t in nxt if t and t["kind"] == "song"), None)
        if target and target["kind"] == "song":
            src = by_id[e["from_node"]]
            if src["kind"] == "mix":  # branch leaves from a note: credit the song before it
                prev = [by_id.get(x["from_node"]) for x in data["edges"] if x["to_node"] == src["id"]]
                src = next((p for p in prev if p and p["kind"] == "song"), src)
            alts.setdefault(src["id"], []).append(
                f"   → {target.get('title') or target['label']} — {target.get('artists') or ''}".rstrip(" —") + (f"  ({note})" if note else ""))
    if alts:
        lines += ["", "ALTERNATIVES"]
        for src_id, options in alts.items():
            src = by_id[src_id]
            lines.append(f"From {src.get('title') or src['label'] or src['text']}:")
            lines += options
    return "\n".join(lines) + "\n"
