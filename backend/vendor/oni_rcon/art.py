"""The console's look: the palette, the ONI emblem, biometric glyphs and the small charts, drawn in text."""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

from rich.text import Text

AMBER, CYAN, RED, GREEN, DIM, WHITE, GREY = "#D9A441", "#4FC3D9", "#E5484D", "#5FB98A", "#5C6773", "#D6DCE4", "#8B95A1"
GOLD, INK, FAINT = "#FFD27A", "#06080B", "#1C242E"

# The ONI emblem, traced into pixel grids (largest first, 48 down to 12 px); tones 1 dark face, 2 rays, 3 bright face, 4 the boot scan.
EMBLEMS = [b.split() for b in re.sub(r"(?m)^#.*\n", "", Path(__file__).with_name("emblem.txt")
                                     .read_text(encoding="utf-8")).strip().split("\n\n")]
TONES = {"1": "#2C2415", "2": "#6F5626", "3": AMBER, "4": GOLD}
BARS = "▁▂▃▄▅▆▇█"
EIGHTHS = "▏▎▍▌▋▊▉"
NOISE = "▓▒░▚▞▙▟#%&@$*+=<>/\\"


def noise(x: int, y: int) -> float:
    """A fixed pseudo-random number in [0, 1) for a pixel: the same pixel always gets the same one."""
    h = (x * 374761393 + y * 668265263) & 0xFFFFFFFF
    h = (h ^ (h >> 13)) * 1274126177 & 0xFFFFFFFF
    return (h ^ (h >> 16)) / 2 ** 32


def blend(a: str, b: str, t: float) -> str:
    """The colour t of the way from b to a: blend(a, b, 1) is a."""
    ca, cb = ([int(c[i:i + 2], 16) for i in (1, 3, 5)] for c in (a, b))
    return "#" + "".join(f"{round(y + (x - y) * t):02X}" for x, y in zip(ca, cb))


def halfblocks(grid: list[str], tones: dict[str, str]) -> Text:
    """Pixel rows as text, two pixels per cell in half blocks; "0" is empty and any other digit names a tone."""
    lines = []
    for top, bot in zip(grid[::2], grid[1::2]):
        line = Text()
        for a, b in zip(top, bot):
            if a == b == "0":
                line.append(" ")
            elif a == b:  # a solid cell is a coloured space: no glyph seams between rows
                line.append(" ", f"on {tones[a]}")
            elif b == "0":
                line.append("▀", tones[a])
            elif a == "0":
                line.append("▄", tones[b])
            else:
                line.append("▀", f"{tones[a]} on {tones[b]}")
        lines.append(line)
    return Text("\n").join(lines)


def pixels(grid: list[list[str | None]]) -> Text:
    """Rows of hex colours (None is empty) as text, two pixels per cell in half blocks. Like halfblocks, but each
    pixel is its own colour, so it draws the archive's pictures; runs of one colour share a span."""
    if len(grid) % 2:
        grid = [*grid, [None] * len(grid[0])]
    lines = []
    for top, bot in zip(grid[::2], grid[1::2]):
        line, run, style = Text(), "", None
        for a, b in zip(top, bot):
            cell = (" ", None) if a is None and b is None else (" ", f"on {a}") if a == b else (
                ("▀", a) if b is None else ("▄", b) if a is None else ("▀", f"{a} on {b}"))
            if cell[1] != style and run:
                line.append(run, style)
                run = ""
            run, style = run + cell[0], cell[1]
        line.append(run, style)
        lines.append(line)
    return Text("\n").join(lines)


def fit(grid: list[list[str | None]], rows: int, cols: int) -> list[list[str | None]]:
    """The grid as it was if it fits in rows x cols cells, else shrunk to fit: each new pixel is the average of the
    old ones it covers, weighted by how much of each it covers (empty if under half of it is drawn)."""
    h, w = len(grid), len(grid[0])
    k = min(1.0, cols / w, rows * 2 / h)
    if k >= 1:
        return grid
    nw, nh = max(1, int(w * k)), max(2, int(h * k) // 2 * 2)

    def span(i: int, n: int, m: int) -> list[tuple[int, float]]:  # (old pixel, share of it) under new pixel i of n
        lo, hi = i * m / n, (i + 1) * m / n
        return [(j, min(hi, j + 1) - max(lo, j)) for j in range(int(lo), min(m, math.ceil(hi)))]
    xs, out = [span(x, nw, w) for x in range(nw)], []
    for y in range(nh):
        row: list[str | None] = []
        for sx in xs:
            total = lit = 0.0
            rgb = [0.0, 0.0, 0.0]
            for j, wy in span(y, nh, h):
                for i, wx in sx:
                    total += wy * wx
                    if (c := grid[j][i]) is not None:
                        lit += wy * wx
                        for q in range(3):
                            rgb[q] += wy * wx * int(c[1 + 2 * q:3 + 2 * q], 16)
            row.append(None if lit * 2 < total else "#" + "".join(f"{round(v / lit):02X}" for v in rgb))
        out.append(row)
    return out


def emblem(rows: int, cols: int = 999, reveal: float = 1.0, scan: float | None = None) -> Text:
    """The largest emblem that fits in rows x cols cells, empty if none fits. For the boot sequence, `reveal`
    (0 to 1) materialises it from the centre out and `scan` (0 to 1) is how far a bright sweep has come down."""
    g = next((g for g in EMBLEMS if len(g) // 2 <= rows and len(g[0]) <= cols), [])
    if g and (reveal < 1 or scan is not None):
        h, w = len(g), len(g[0])
        far = math.hypot(w / 2, h / 2)
        line = None if scan is None else scan * (h + 4) - 2

        def px(x: int, y: int, c: str) -> str:
            if c == "0" or 0.55 * noise(x, y) + 0.45 * math.hypot(x - w / 2, y - h / 2) / far >= reveal:
                return "0"
            return "4" if line is not None and abs(y - line) < 1.5 else c
        g = ["".join(px(x, y, c) for x, c in enumerate(row)) for y, row in enumerate(g)]
    return halfblocks(g, TONES)


def biosig(seed: str, color: str, size: int = 12) -> Text:
    """A biometric signature: a mirrored pixel glyph drawn from a hash of the seed (a player ID), in their colour.
    The same ID always draws the same glyph, size x size pixels in size x size/2 cells."""
    bits = int.from_bytes(hashlib.sha512(seed.encode()).digest(), "big")
    tones = {"1": blend(color, INK, .3), "2": blend(color, INK, .6), "3": color}
    rows = []
    for _ in range(size):
        half = ""
        for _ in range((size + 1) // 2):
            half, bits = half + "00001123"[bits & 7], bits >> 3  # half empty, then dark, mid and bright
        rows.append(half + half[::-1][size % 2:])
    return halfblocks(rows, tones)


def gauge(v: float, width: int, color: str) -> Text:
    """A segmented gauge of width cells, v (0 to 1) of it lit."""
    n = round(max(0.0, min(1.0, v)) * width)
    return Text("▰" * n, color) + Text("▱" * (width - n), DIM)


def hbar(v: float, width: int, color: str) -> Text:
    """A solid bar up to width cells long for v (0 to 1), to an eighth of a cell. Whole cells are coloured
    spaces, like the emblem's, so no glyph seams show between them."""
    full, part = divmod(round(max(0.0, min(1.0, v)) * width * 8), 8)
    return Text.assemble((" " * full, f"on {color}"), (EIGHTHS[part - 1] if part else "", color))


def split_bar(parts: list[tuple[float, str]], width: int) -> Text:
    """One bar of width cells shared out between (value, colour) parts in proportion; equal shares if all are 0."""
    total = sum(v for v, _ in parts)
    if not total:
        parts, total = [(1, c) for _, c in parts], len(parts)
    t, used = Text(), 0
    for i, (v, color) in enumerate(parts):
        n = width - used if i == len(parts) - 1 else min(width - used, round(v / total * width))
        t.append("━" * n, color)
        used += n
    return t


def spark(values: list[float], color: str = CYAN) -> Text:
    """A one-line chart of the values, scaled to the largest; nothing at all reads as a faint baseline."""
    top = max(values, default=0) or 1
    t = Text()
    for v in values:
        t.append(BARS[max(0, min(7, math.ceil(v / top * 8) - 1))], color if v > 0 else FAINT)
    return t


def decrypt(text: str, t: float, frame: int = 0) -> str:
    """The text t (0 to 1) of the way through decrypting: characters resolve left to right out of noise."""
    n = max(len(text), 1)
    return "".join(c if c == " " or t >= 1 or i / n + 0.3 * noise(i, 99) < t * 1.3
                   else NOISE[int(noise(i, frame) * len(NOISE))] for i, c in enumerate(text))
