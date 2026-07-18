"""Stdlib-only SVG share code rendering.

This is intentionally not advertised as a standards-compliant QR encoder. The
SVG embeds the short URL in metadata and renders a stable code-like matrix for
visual sharing until a real QR dependency is introduced.
"""

from __future__ import annotations

import hashlib
import html


def share_svg(data: str) -> str:
    digest = hashlib.sha256(data.encode("utf-8")).digest()
    size = 29
    cell = 8
    quiet = 4
    modules = [[False for _ in range(size)] for _ in range(size)]

    def finder(x: int, y: int) -> None:
        for dy in range(7):
            for dx in range(7):
                border = dx in {0, 6} or dy in {0, 6}
                center = 2 <= dx <= 4 and 2 <= dy <= 4
                modules[y + dy][x + dx] = border or center

    finder(0, 0)
    finder(size - 7, 0)
    finder(0, size - 7)
    bit_index = 0
    for y in range(size):
        for x in range(size):
            in_finder = (x < 7 and y < 7) or (x >= size - 7 and y < 7) or (x < 7 and y >= size - 7)
            if in_finder:
                continue
            byte = digest[(bit_index // 8) % len(digest)]
            modules[y][x] = bool(byte & (1 << (bit_index % 8)))
            bit_index += 1
    width = (size + quiet * 2) * cell
    rects = []
    for y, row in enumerate(modules):
        for x, filled in enumerate(row):
            if filled:
                rects.append(f'<rect x="{(x + quiet) * cell}" y="{(y + quiet) * cell}" width="{cell}" height="{cell}"/>')
    safe_data = html.escape(data, quote=True)
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{width}" viewBox="0 0 {width} {width}" role="img" aria-label="Share code for {safe_data}"><metadata>{safe_data}</metadata><rect width="100%" height="100%" fill="white"/><g fill="black">{"".join(rects)}</g></svg>'
