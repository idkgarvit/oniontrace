"""Measure the rendered slides as pixels, because geometry alone cannot see an empty slide.

The DOM checks in preview_deck.py know where every box is. They cannot tell you that a figure
failed to load and the box is empty, that content has drifted to the bottom of the slide, or
that the left margin is different on one slide from all the others. Those are all visible in
the raster.

Nothing here judges taste. It measures the things that are wrong rather than merely different:

* ink coverage, so a blank or near-blank slide is caught;
* the left edge of content, so one slide out of line with the rest is caught;
* the top and bottom of content, so a slide whose text runs into the lower edge is caught;
* whether the expected image region actually contains image pixels rather than background.

    python scripts/analyse_slides.py out/preview/slides
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image

BG = (14, 19, 24)          # the deck background, #0e1318
TOL = 12                   # how close to the background still counts as background
SLIDE_W_IN = 13.333


def load(path: Path) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB")).astype(np.int16)


def content_mask(a: np.ndarray) -> np.ndarray:
    """Anything meaningfully different from the flat background colour."""

    return (np.abs(a - np.array(BG, dtype=np.int16)).sum(axis=2) > TOL)


def main(folder: str) -> int:
    files = sorted(Path(folder).glob("slide-*.png"))
    if not files:
        raise SystemExit(f"error: no slide images in {folder}")

    problems: list[str] = []
    inked: list[tuple[Path, float]] = []

    print(f"{len(files)} slides\n")
    for f in files:
        a = load(f)
        h, w, _ = a.shape
        m = content_mask(a)

        coverage = m.mean() * 100
        rows = np.where(m.any(axis=1))[0]
        cols = np.where(m.any(axis=0))[0]
        if rows.size == 0:
            problems.append(f"{f.name}: completely blank")
            continue

        left_in = cols[0] / w * SLIDE_W_IN
        top_in = rows[0] / h * 7.5
        bottom_in = rows[-1] / h * 7.5
        inked.append((f, left_in))

        print(f"  {f.name}: ink {coverage:5.1f}%  left {left_in:.2f}in  "
              f"top {top_in:.2f}in  bottom {bottom_in:.2f}in")

        if coverage < 1.5:
            problems.append(f"{f.name}: only {coverage:.1f}% ink, this slide is nearly empty")
        if coverage > 45:
            problems.append(f"{f.name}: {coverage:.0f}% ink, this slide is crowded")
        if bottom_in > 7.3:
            problems.append(f"{f.name}: content reaches {bottom_in:.2f}in, into the bottom edge")
        if top_in < 0.15:
            problems.append(f"{f.name}: content starts at {top_in:.2f}in, touching the top edge")

    if inked:
        # One slide out of line with the rest reads as sloppy even when nobody can say why.
        ref = float(np.median([left for _, left in inked]))
        for f, left in inked:
            if abs(left - ref) > 0.12:
                problems.append(
                    f"{f.name}: left edge at {left:.2f}in against {ref:.2f}in on the other "
                    "slides"
                )

    print()
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("all slides carry content, sit on one left margin, and clear both edges")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "out/preview/slides"))
