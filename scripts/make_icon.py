"""Draw the DJ Crate icon (a vinyl record on a dark rounded square) as a PNG.

Pure standard library so it runs anywhere:  python3 scripts/make_icon.py
Writes djcrate/web/icon.png (1024x1024). make_app.sh turns it into an .icns.
"""

import math
import struct
import zlib
from pathlib import Path

SIZE = 1024
OUT = Path(__file__).resolve().parent.parent / "djcrate" / "web" / "icon.png"


def mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(len(a)))


def smooth(edge, x, width=1.2):
    """0 → 1 across `width` px around `edge` (anti-aliasing)."""
    return min(1.0, max(0.0, (x - edge) / width + 0.5))


def rounded_square_alpha(x, y, margin=100, radius=185):
    lo, hi = margin, SIZE - margin
    cx = min(max(x, lo + radius), hi - radius)
    cy = min(max(y, lo + radius), hi - radius)
    d = math.hypot(x - cx, y - cy)
    return 1.0 - smooth(radius, d)


def pixel(x, y):
    px, py = x + 0.5, y + 0.5
    a = rounded_square_alpha(px, py)
    if a <= 0:
        return (0, 0, 0, 0)
    # background: subtle diagonal gradient
    t = (px + py) / (2 * SIZE)
    col = mix((40, 30, 78), (18, 18, 22), t)

    c = SIZE / 2
    r = math.hypot(px - c, py - c)
    disc, label, hole = 330, 118, 16
    if r < disc + 1:
        vinyl = (14, 14, 18)
        # grooves: faint rings
        groove = 0.5 + 0.5 * math.cos(r * 0.9)
        vinyl = mix(vinyl, (34, 34, 42), 0.35 * groove if label + 18 < r < disc - 14 else 0)
        # sheen across the record
        ang = math.atan2(py - c, px - c)
        sheen = max(0.0, math.cos(2 * (ang + 0.8))) ** 6
        vinyl = mix(vinyl, (90, 80, 140), 0.18 * sheen if r > label + 10 else 0)
        col = mix(col, vinyl, 1 - smooth(disc, r))
    if r < label + 1:
        lab = mix((157, 133, 255), (124, 92, 255), (py - (c - label)) / (2 * label))
        col = mix(col, lab, 1 - smooth(label, r))
    if r < hole + 1:
        col = mix(col, (18, 18, 22), 1 - smooth(hole, r))
    return (*[round(v) for v in col], round(255 * a))


def write_png(path: Path, rows):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in rows)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


if __name__ == "__main__":
    write_png(OUT, ([pixel(x, y) for x in range(SIZE)] for y in range(SIZE)))
    print(f"Wrote {OUT}")
