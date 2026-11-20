#!/usr/bin/env python3
"""Draw the app icon (two stacked cards on green) as PNGs, with the standard library only.
Run: python3 scripts/make_icons.py"""
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "app")

BG = (15, 122, 92)
CARD = (243, 247, 244)
CARD_BACK = (140, 205, 182)
LINE = (15, 122, 92)


def inside_round_rect(x, y, x0, y0, x1, y1, r):
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def pixel(u, v):
    """u, v in 0..1. Full-bleed background so Android's maskable crop stays green."""
    color = BG
    # back card, offset up and right
    if inside_round_rect(u, v, 0.30, 0.22, 0.78, 0.62, 0.06):
        color = CARD_BACK
    # front card
    if inside_round_rect(u, v, 0.22, 0.34, 0.70, 0.78, 0.06):
        color = CARD
        # three text lines on the front card
        for y0, x1 in ((0.45, 0.58), (0.54, 0.62), (0.63, 0.50)):
            if inside_round_rect(u, v, 0.29, y0, x1, y0 + 0.035, 0.017):
                color = LINE
    return color


def png(size, path):
    rows = []
    ss = 3  # supersample for smooth edges
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            acc = [0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    c = pixel((x + (sx + 0.5) / ss) / size, (y + (sy + 0.5) / ss) / size)
                    acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]
            row += bytes(v // (ss * ss) for v in acc)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(kind, data):
        c = struct.pack(">I", len(data)) + kind + data
        return c + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", raw))
        f.write(chunk(b"IEND", b""))


if __name__ == "__main__":
    for s in (192, 512):
        p = os.path.join(OUT, f"icon-{s}.png")
        png(s, p)
        print("wrote", p)
