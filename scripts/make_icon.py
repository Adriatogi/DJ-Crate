"""Make the DJ Crate icon and the website / GitHub image assets.

Run:  uv run python scripts/make_icon.py   (standard library only; resizing uses macOS `sips`)

Source: branding/logo.png if it exists (ideally 1024x1024), otherwise the built-in
vinyl record on a dark rounded square, drawn here.

Writes:
  djcrate/web/icon.png              1024x1024 app icon (make_app.sh turns it into an .icns)
  site/assets/logo.png              512x512
  site/assets/favicon.png           64x64
  site/assets/social-preview.png    1280x640 GitHub social preview: logo centred on the theme --bg
"""

import math
import re
import shutil
import struct
import subprocess
import sys
import zlib
from pathlib import Path

SIZE = 1024
REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "djcrate" / "web" / "icon.png"
BRAND_LOGO = REPO / "branding" / "logo.png"
THEME = REPO / "djcrate" / "web" / "theme.css"
ASSETS = REPO / "site" / "assets"
SOCIAL_W, SOCIAL_H, SOCIAL_LOGO = 1280, 640, 512


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


def write_png(path: Path, rows, width=SIZE, height=SIZE):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(v for px in row for v in px) for row in rows)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.write_bytes(png)


def read_png(path: Path):
    """Decode a non-interlaced 8/16-bit grey/RGB/RGBA PNG into (width, height, rows of RGBA tuples)."""
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path} is not a PNG")
    pos, idat, plte, trns = 8, b"", None, None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif tag == b"PLTE":
            plte = body
        elif tag == b"tRNS":
            trns = body
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None or interlace or (ctype == 3 and depth != 8) or (ctype != 3 and depth not in (8, 16)):
        raise ValueError(f"unsupported PNG format (type {ctype}, depth {depth}, interlace {interlace})")
    bpp = channels * depth // 8
    stride = w * bpp
    raw, prev, rows, i = zlib.decompress(idat), bytearray(stride), [], 0
    for _ in range(h):
        ftype, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if ftype == 1:
                line[x] = (line[x] + a) & 255
            elif ftype == 2:
                line[x] = (line[x] + b) & 255
            elif ftype == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif ftype == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        step = depth // 8  # 16-bit: keep the high byte
        vals = line[::step] if step > 1 else line
        row = []
        for x in range(w):
            v = vals[x * channels:(x + 1) * channels]
            if ctype == 0:
                row.append((v[0], v[0], v[0], 255))
            elif ctype == 4:
                row.append((v[0], v[0], v[0], v[1]))
            elif ctype == 2:
                row.append((v[0], v[1], v[2], 255))
            elif ctype == 6:
                row.append(tuple(v))
            else:
                k = v[0]
                alpha = trns[k] if trns and k < len(trns) else 255
                row.append((plte[3 * k], plte[3 * k + 1], plte[3 * k + 2], alpha))
        rows.append(row)
    return w, h, rows


def png_size(path: Path):
    head = path.read_bytes()[:24]
    return struct.unpack(">II", head[16:24])


def sips_resize(src: Path, dst: Path, w: int, h: int):
    subprocess.run(["sips", "-z", str(h), str(w), "-s", "format", "png", str(src), "--out", str(dst)],
                   check=True, stdout=subprocess.DEVNULL)


def theme_bg():
    m = re.search(r"--bg:\s*#([0-9a-fA-F]{6})\s*;", THEME.read_text()) if THEME.exists() else None
    hexv = m.group(1) if m else "121214"
    return tuple(int(hexv[i:i + 2], 16) for i in (0, 2, 4))


def make_icon():
    if BRAND_LOGO.exists():
        w, h = png_size(BRAND_LOGO)
        if w != h:
            print(f"Warning: {BRAND_LOGO.name} is {w}x{h}; it will be stretched to square. Use a square logo.")
        if (w, h) == (SIZE, SIZE):
            shutil.copyfile(BRAND_LOGO, OUT)
        else:
            sips_resize(BRAND_LOGO, OUT, SIZE, SIZE)
        print(f"Wrote {OUT} (from {BRAND_LOGO})")
    else:
        write_png(OUT, ([pixel(x, y) for x in range(SIZE)] for y in range(SIZE)))
        print(f"Wrote {OUT} (built-in vinyl; put a logo at branding/logo.png to replace it)")


def make_site_assets():
    if not shutil.which("sips"):
        print("Skipping site/assets: needs macOS `sips` to resize.")
        return
    ASSETS.mkdir(parents=True, exist_ok=True)
    logo, favicon = ASSETS / "logo.png", ASSETS / "favicon.png"
    sips_resize(OUT, logo, SOCIAL_LOGO, SOCIAL_LOGO)
    sips_resize(OUT, favicon, 64, 64)
    print(f"Wrote {logo}\nWrote {favicon}")

    # Social preview: the 512px logo alpha-composited onto the theme background.
    try:
        lw, lh, lrows = read_png(logo)
    except ValueError as e:
        print(f"Skipping social preview: {e}")
        return
    bg = theme_bg()
    ox, oy = (SOCIAL_W - lw) // 2, (SOCIAL_H - lh) // 2

    def rows():
        for y in range(SOCIAL_H):
            ly = y - oy
            if not 0 <= ly < lh:
                yield [(*bg, 255)] * SOCIAL_W
                continue
            src = lrows[ly]
            row = [(*bg, 255)] * ox
            for r, g, b, a in src:
                t = a / 255
                row.append((round(r * t + bg[0] * (1 - t)), round(g * t + bg[1] * (1 - t)),
                            round(b * t + bg[2] * (1 - t)), 255))
            row += [(*bg, 255)] * (SOCIAL_W - ox - lw)
            yield row

    social = ASSETS / "social-preview.png"
    write_png(social, rows(), SOCIAL_W, SOCIAL_H)
    print(f"Wrote {social}")


if __name__ == "__main__":
    try:
        make_icon()
        make_site_assets()
    except subprocess.CalledProcessError as e:
        sys.exit(f"sips failed: {e}")
