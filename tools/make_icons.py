#!/usr/bin/env python3
"""make_icons.py — derive the maskable PWA icon set (audit 9.10).

    tools/make_icons.py

The shipped icons are the bare mark on transparency. That is right for a favicon and
wrong for a maskable icon: Android crops a maskable icon to whatever shape the launcher
uses, so a transparent ground shows through and anything outside the inner ~80% can be
cut off. A maskable icon needs an opaque ground and the mark inside the safe zone.

Derived from icon-512.png rather than re-rasterising the SVG, because rasterising would
mean adding cairosvg or rsvg to the runtime venv for a build-time job. icon-512.png was
itself generated from static/brand/loupe-mark.svg, so the SVG remains the one source;
this is a second step on the same chain, not a second source.

Ground is the table slate the app's --n-0 stop resolves to -- 9.10 asks for the o-mark on
the table-slate ground, and 8.0's rule is that the chrome is the table.
"""
import os
import sys

from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONS = os.path.join(REPO, "static", "icons")
SRC = os.path.join(ICONS, "icon-512.png")

GROUND = (21, 17, 13, 255)     # #15110d -- the deepest table stop
SAFE = 0.62                    # mark occupies 62% of the edge: inside the 80% safe circle


def build(size):
    src = Image.open(SRC).convert("RGBA")
    # trim to the mark's own bounding box first, so the safe-zone maths is about the
    # artwork rather than about however much padding the source happened to carry
    bbox = src.getbbox()
    if bbox:
        src = src.crop(bbox)
    target = int(size * SAFE)
    w, h = src.size
    scale = min(target / w, target / h)
    mark = src.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), GROUND)
    canvas.alpha_composite(mark, ((size - mark.width) // 2, (size - mark.height) // 2))
    return canvas


def main():
    if not os.path.exists(SRC):
        print("missing %s" % SRC, file=sys.stderr)
        return 2
    for size in (192, 512):
        out = os.path.join(ICONS, "icon-%d-maskable.png" % size)
        img = build(size)
        img.save(out, "PNG", optimize=True)
        corner = img.getpixel((1, 1))
        print("wrote %s  %dx%d  corner=%s (opaque: %s)"
              % (os.path.relpath(out, REPO), img.width, img.height, corner,
                 corner[3] == 255))
    return 0


if __name__ == "__main__":
    sys.exit(main())
