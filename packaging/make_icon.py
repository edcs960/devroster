"""Draw the DevRoster app icon (packaging/DevRoster.png, 1024x1024, transparent).

Same shapes as agent_dev/ui/assets/brand-mark.svg, drawn with 3x3 supersampling and no
third-party packages, so the icon can be rebuilt on any OS:

    python packaging/make_icon.py
"""

import struct
import zlib
from pathlib import Path

SIZE = 1024
MARGIN = 100  # macOS icon grid: the artwork sits inside ~824 px.
SCALE = (SIZE - 2 * MARGIN) / 32
SAMPLES = 3
PURPLE = (0x4F, 0x46, 0xE5)
WHITE = (0xFF, 0xFF, 0xFF)


def inside_rounded(u, v, radius=7.5, size=32.0):
    if not (0 <= u <= size and 0 <= v <= size):
        return False
    cx = min(max(u, radius), size - radius)
    cy = min(max(v, radius), size - radius)
    return (u - cx) ** 2 + (v - cy) ** 2 <= radius * radius


def white_at(u, v):
    ring = (u - 13) ** 2 + (v - 19) ** 2
    if (4.6 - 1.7) ** 2 <= ring <= (4.6 + 1.7) ** 2:
        return True
    if 17.6 - 1.7 <= u <= 17.6 + 1.7 and 7.5 <= v <= 25.3:
        return True
    return (u - 23.6) ** 2 + (v - 23.4) ** 2 <= 1.9 * 1.9


def pixel(x, y):
    shape = white = 0
    for sy in range(SAMPLES):
        for sx in range(SAMPLES):
            u = (x + (sx + 0.5) / SAMPLES - MARGIN) / SCALE
            v = (y + (sy + 0.5) / SAMPLES - MARGIN) / SCALE
            if inside_rounded(u, v):
                shape += 1
                white += white_at(u, v)
    total = SAMPLES * SAMPLES
    if not shape:
        return (0, 0, 0, 0)
    mix = white / shape
    rgb = tuple(round(p * (1 - mix) + w * mix) for p, w in zip(PURPLE, WHITE, strict=True))
    return (*rgb, round(255 * shape / total))


def png(width, height, rows):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(value for px in row for value in px) for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main():
    rows = [[pixel(x, y) for x in range(SIZE)] for y in range(SIZE)]
    target = Path(__file__).with_name("DevRoster.png")
    target.write_bytes(png(SIZE, SIZE, rows))
    print(f"wrote {target}")


if __name__ == "__main__":
    main()
