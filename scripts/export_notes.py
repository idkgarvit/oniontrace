"""Export the speaker notes beside the deck, titled by each slide's real heading.

The heading is the text at the largest font size on the slide, not the first text box: the
first box on several slides is the small amber kicker above the title, and a notes sheet whose
sections read MODULE 2 and VALIDATION is no use to somebody rehearsing.

    python scripts/export_notes.py deck.pptx notes.md
"""

from __future__ import annotations

import sys
from pathlib import Path

from pptx import Presentation


def biggest_text(slide) -> str:
    """The slide's heading, taken to be the largest text that sits highest on the slide.

    Largest alone is not enough: on the results slide a 44pt statistic inside a card is bigger
    than the 38pt heading above it, and the notes would be titled "19 / 19". Anything close to
    the biggest size is a candidate, and the topmost candidate wins.
    """

    candidates: list[tuple[float, int, str]] = []
    for sh in slide.shapes:
        if not sh.has_text_frame or not sh.text_frame.text.strip():
            continue
        top = sh.top if sh.top is not None else 0
        for p in sh.text_frame.paragraphs:
            text = "".join(r.text for r in p.runs).strip()
            if not text:
                continue
            pt = max((r.font.size.pt for r in p.runs if r.font.size), default=0)
            if pt:
                candidates.append((pt, top, text))

    if not candidates:
        return ""
    largest = max(c[0] for c in candidates)
    near = [c for c in candidates if c[0] >= largest * 0.8]
    return min(near, key=lambda c: c[1])[2]


def main(deck: str, out: str) -> int:
    prs = Presentation(deck)
    lines = [
        "# ONIONTRACE: speaker notes",
        "",
        "One section per slide. The slide holds the figure; these are the words to say over it.",
        "",
    ]
    for i, slide in enumerate(prs.slides, 1):
        note = slide.notes_slide.notes_text_frame.text.strip()
        lines += [f"## {i}. {biggest_text(slide) or '(figure only)'}", "", note or "(no note)", ""]

    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")

    missing = sum(1 for s in prs.slides if not s.notes_slide.notes_text_frame.text.strip())
    print(f"wrote {path}  ({path.stat().st_size:,} bytes, {len(prs.slides)} slides, "
          f"{missing} without notes)")
    return 0 if not missing else 1


if __name__ == "__main__":
    deck = sys.argv[1] if len(sys.argv) > 1 else "out/ONIONTRACE_Presentation.pptx"
    out = sys.argv[2] if len(sys.argv) > 2 else "out/ONIONTRACE_Speaker_Notes.md"
    raise SystemExit(main(deck, out))
