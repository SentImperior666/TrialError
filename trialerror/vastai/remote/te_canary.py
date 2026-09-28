#!/usr/bin/env python3
"""Runs ON the rented vast.ai instance, after the install and before any
document is uploaded: renders known text into a one-page IMAGE PDF (the
vast.ai OCR design, section 5.2 check 5).

    python3 te_canary.py <canary_text.txt> <output.pdf>

The page is a raster, like a scan: marker has to OCR it, which is what proves
the host really runs a working OCR stack (and makes marker fetch its models
into the cache DEV then hashes). Pillow is one of marker's own dependencies,
so nothing is installed for this. Deterministic for a given Pillow and font.
DEV compares the markdown that comes back with the same text file.
"""

from __future__ import annotations

import sys

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT, DPI = 1700, 2200, 200  # US letter at 200 dpi
MARGIN, FONT_PX, LINE_PX = 150, 44, 80


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1: an embedded TrueType face
    except Exception:
        pass
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def main(text_path: str, pdf_path: str) -> int:
    with open(text_path, encoding="utf-8") as handle:
        lines = [line.rstrip() for line in handle.read().splitlines()]
    page = Image.new("L", (WIDTH, HEIGHT), 255)
    draw = ImageDraw.Draw(page)
    font = _font(FONT_PX)
    y = MARGIN
    for line in lines:
        draw.text((MARGIN, y), line, fill=0, font=font)
        y += LINE_PX
    page.save(pdf_path, "PDF", resolution=float(DPI))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python3 te_canary.py <canary_text.txt> <output.pdf>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
