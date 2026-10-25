"""Check the finished deck for the things a reader notices and a build does not.

Three failure modes matter here and none of them raises an error when you save a pptx:

* a text box whose content needs more height than the box has, so the last line is clipped
  or spills over whatever sits below it;
* an image that overlaps a caption or runs off the slide;
* a slide with no speaker notes, discovered five minutes before the presentation.

Text height is estimated from the font metrics rather than measured, because PowerPoint
lays text out itself and python-pptx cannot ask it. The estimate is deliberately generous.

    python scripts/verify_deck.py deck.pptx
"""

from __future__ import annotations

import sys
from pathlib import Path

from pptx import Presentation

EMU_IN = 914400
# Arial averages close to 0.5em per character across mixed prose. Use 0.52 to stay
# pessimistic: a false overflow report costs a minute, a missed one costs a slide.
CHAR_W = 0.52
LINE_H = 1.22


def est_height(tf, box_w_in: float) -> float:
    """Rough height in inches that this text frame needs at its own font sizes."""

    total = 0.0
    for p in tf.paragraphs:
        text = "".join(r.text for r in p.runs)
        sizes = [r.font.size.pt for r in p.runs if r.font.size]
        pt = max(sizes) if sizes else 18.0
        if not text.strip():
            total += pt * LINE_H / 72 * 0.6
            continue
        cpl = max(1, int((box_w_in * 72) / (pt * CHAR_W)))
        lines = max(1, -(-len(text) // cpl))
        total += lines * pt * LINE_H / 72
        if p.space_after is not None:
            total += p.space_after.pt / 72
    return total


def boxes_overlap(a, b, pad=0.02) -> bool:
    ax, ay = a.left / EMU_IN, a.top / EMU_IN
    aw, ah = a.width / EMU_IN, a.height / EMU_IN
    bx, by = b.left / EMU_IN, b.top / EMU_IN
    bw, bh = b.width / EMU_IN, b.height / EMU_IN
    return (
        ax + aw - pad > bx and bx + bw - pad > ax
        and ay + ah - pad > by and by + bh - pad > ay
    )


def main(path: str) -> int:
    prs = Presentation(path)
    SW, SH = prs.slide_width / EMU_IN, prs.slide_height / EMU_IN
    problems: list[str] = []
    print(f"{Path(path).name}  {SW:.2f} x {SH:.2f} in  {len(prs.slides)} slides\n")

    for i, slide in enumerate(prs.slides, 1):
        imgs, texts, has_notes = 0, 0, bool(slide.notes_slide.notes_text_frame.text.strip())
        pictures = []
        textboxes = []

        for sh in slide.shapes:
            if sh.left is None:
                continue
            x, y = sh.left / EMU_IN, sh.top / EMU_IN
            w, h = sh.width / EMU_IN, sh.height / EMU_IN

            if x < -0.01 or y < -0.01 or x + w > SW + 0.01 or y + h > SH + 0.01:
                problems.append(
                    f"slide {i}: {sh.name} out of bounds "
                    f"({x:.2f},{y:.2f} {w:.2f}x{h:.2f})"
                )

            if sh.shape_type == 13:  # picture
                imgs += 1
                pictures.append(sh)
                continue

            if not sh.has_text_frame or not sh.text_frame.text.strip():
                continue
            texts += 1
            need = est_height(sh.text_frame, w)
            if need > h * 1.02:
                problems.append(
                    f"slide {i}: {sh.name} needs {need:.2f} in of text in a {h:.2f} in box "
                    f"-> {sh.text_frame.text[:44]!r}"
                )
            if need > h * 1.6:
                # a big miss usually means the box is the wrong shape for its content
                problems.append(
                    f"slide {i}: {sh.name} overflowing by more than half its height"
                )
            textboxes.append(sh)

        for pic in pictures:
            for other in textboxes:
                if boxes_overlap(pic, other):
                    problems.append(
                        f"slide {i}: image overlaps text {other.text_frame.text[:32]!r}"
                    )

        flag = "" if has_notes else "   NO NOTES"
        print(f"  {i:2}. {imgs} image(s), {texts} text box(es){flag}")
        if not has_notes:
            problems.append(f"slide {i}: no speaker notes")

    print()
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("no layout problems: everything inside the slide, no clipped text, no overlaps")
    with_notes = sum(
        1 for slide in prs.slides if slide.notes_slide.notes_text_frame.text.strip()
    )
    print(
        f"VERDICT: {len(prs.slides)} slides at {SW:.2f}x{SH:.2f} in, "
        f"{with_notes} with speaker notes, nothing clipped or overlapping"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "out/ONIONTRACE_Presentation.pptx"))
