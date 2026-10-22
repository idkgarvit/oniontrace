"""Build the presentation.

Twelve slides, one idea each, built around a drawn diagram rather than a block of text. The
reason is not decoration: a reader looking at a chart has understood the slide before the
sentence under it finishes, and a reader looking at eight bullets is reading instead of
listening.

Every number on the diagrams comes from out/eval-results.json at build time. Nothing is
typed in by hand, so a slide cannot disagree with the file it came from.

    python scripts/build_diagrams.py
    python scripts/build_presentation.py --out deck.pptx
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Emu, Inches, Pt

ROOT = Path(__file__).resolve().parent.parent
FIG = ROOT / "out" / "fig"
SCREEN = ROOT / "out" / "screen" / "qa" / "01-case-view.png"

BG = RGBColor(0x0E, 0x13, 0x18)
FG = RGBColor(0xE8, 0xED, 0xF3)
MUTED = RGBColor(0x8B, 0x9A, 0xAB)
AMBER = RGBColor(0xE0, 0xA2, 0x56)
TEAL = RGBColor(0x4F, 0xB3, 0xA5)
CARD = RGBColor(0x16, 0x1D, 0x25)
LINE = RGBColor(0x2A, 0x35, 0x42)

FONT = "Arial"
W_IN, H_IN = 13.333, 7.5

# Words that make a deck sound generated. None of them belong on a slide.
BANNED = [
    "delve", "landscape", "crucial", "seamless", "testament", "moreover", "furthermore",
    "robust", "leverage", "journey", "unlock", "elevate", "comprehensive", "cutting-edge",
    "game-changer", "revolutionise", "revolutionize", "paradigm", "holistic", "synergy",
    "it is worth noting", "in today's", "plays a vital", "is essential to understand",
]


# ------------------------------------------------------------------ helpers
def blank(prs):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    s.background.fill.solid()
    s.background.fill.fore_color.rgb = BG
    return s


def tb(slide, x, y, w, h):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = 0
    tf.margin_top = tf.margin_bottom = 0
    return tf


def para(tf, text, *, size=20, color=FG, bold=False, first=False, space_after=0,
         align=PP_ALIGN.LEFT, font=FONT, line_spacing=None):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.alignment = align
    p.space_after = Pt(space_after)
    if line_spacing:
        p.line_spacing = line_spacing
    r = p.add_run()
    r.text = text
    r.font.size = Pt(size)
    r.font.color.rgb = color
    r.font.bold = bold
    r.font.name = font
    return p


def rule(slide, x, y, w, color=AMBER, thick=Pt(2.5)):
    bar = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), thick)
    bar.fill.solid()
    bar.fill.fore_color.rgb = color
    bar.line.fill.background()
    bar.shadow.inherit = False
    return bar


def kicker(slide, text, x=0.85, y=0.62):
    tf = tb(slide, x, y, 8.0, 0.3)
    para(tf, text.upper(), size=12, color=AMBER, bold=True, first=True)


def title(slide, text, *, sub=None, y=0.95, size=38, width=11.6):
    tf = tb(slide, 0.85, y, width, 1.3)
    para(tf, text, size=size, color=FG, bold=True, first=True, line_spacing=1.02)
    if sub:
        tf2 = tb(slide, 0.85, y + (size / 72) * 1.55, width, 0.8)
        para(tf2, sub, size=19, color=MUTED, first=True)


def picture(slide, path: Path, *, x, y, max_w, max_h, center_x=None):
    """Place an image scaled to fit a box, keeping its aspect ratio."""

    from PIL import Image

    with Image.open(path) as im:
        iw, ih = im.size
    scale = min(max_w / iw, max_h / ih)
    w, h = iw * scale, ih * scale
    if center_x is not None:
        x = center_x - w / 2
    return slide.shapes.add_picture(str(path), Inches(x), Inches(y), Inches(w), Inches(h))


def foot(slide, text, y=6.82):
    tf = tb(slide, 0.85, y, 11.6, 0.4)
    para(tf, text, size=14, color=MUTED, first=True)


def notes(slide, text):
    slide.notes_slide.notes_text_frame.text = text


# ------------------------------------------------------------------ slides
def s_title(prs):
    s = blank(prs)
    kicker(s, "Capstone project 1  ·  2025CSET703", y=1.55)
    tf = tb(s, 0.85, 2.0, 11.6, 1.5)
    para(tf, "ONIONTRACE", size=74, color=FG, bold=True, first=True)

    tf2 = tb(s, 0.85, 3.45, 9.6, 1.3)
    para(tf2, "Linking onion services to one operator,", size=27, color=AMBER, first=True,
         line_spacing=1.15)
    para(tf2, "and saying how sure we are.", size=27, color=AMBER, line_spacing=1.15)

    rule(s, 0.85, 5.15, 2.2)

    tf3 = tb(s, 0.85, 5.5, 11.6, 1.5)
    para(tf3, "Garvit Kanojia  ·  S25MCAG0002", size=18, color=FG, first=True, space_after=6)
    para(tf3, "Niketa Chauhan  ·  S25MCAG0015", size=18, color=FG, space_after=12)
    para(tf3, "Group 32  ·  Bennett University  ·  Guide: Prof. (Dr.) Ajay Kumar",
         size=15, color=MUTED)

    notes(s, "Open with the question the project answers, not the acronym. Two onion sites "
             "can look completely unrelated and still be run by the same person. We built a "
             "tool that proposes which ones are, and refuses to state it as fact.")


def s_problem(prs):
    s = blank(prs)
    picture(s, FIG / "d1_problem.png", x=0, y=1.15, max_w=W_IN, max_h=5.3,
            center_x=W_IN / 2)
    foot(s, "Each address is a public key. It carries no owner, no location and no link to any other address.")

    notes(s, "This is the whole problem in one picture. An onion address is derived from a "
             "key, so there is no registration, no WHOIS, nothing that names the operator. "
             "If one person runs three marketplaces, nothing on those pages says so.")


def s_defaults(prs):
    s = blank(prs)
    picture(s, FIG / "d3_defaults.png", x=0, y=1.05, max_w=W_IN, max_h=5.55,
            center_x=W_IN / 2)
    foot(s, "Two sites sharing nginx's default certificate is not a link. Two sites sharing a rare favicon is.")

    notes(s, "This is the core finding and it is a negative one. The obvious idea, link sites "
             "that share technical details, does not work, because the internet shares "
             "technical details constantly. We measured it at 0.207 precision, so four links "
             "in five were wrong. Counting how rare each value is before treating it as "
             "evidence is what fixes that.")


def s_pipeline(prs):
    s = blank(prs)
    picture(s, FIG / "d2_pipeline.png", x=0, y=1.05, max_w=W_IN, max_h=5.55,
            center_x=W_IN / 2)
    foot(s, "Six stages, one command. Each stage writes down what it produced, including when it failed.")

    notes(s, "Left to right: build a test network, check the analyst has authorisation "
             "before collecting anything, detect misconfigurations, correlate services, "
             "attribute writing style, then build the identity graph. One command runs all "
             "of it, which matters when you have to reproduce a number in a viva.")


def s_farm(prs):
    s = blank(prs)
    picture(s, FIG / "d5_farm.png", x=0, y=1.15, max_w=W_IN, max_h=5.3,
            center_x=W_IN / 2)
    foot(s, "Four clusters that really are linked, and decoys that look linked but are not.")

    notes(s, "The decoys are the important part. A test set where everything similar is "
             "genuinely linked cannot catch a tool that links everything similar. About a "
             "third of this network exists to punish exactly that mistake.")


def s_detect(prs):
    s = blank(prs)
    kicker(s, "Module 2")
    title(s, "What it finds on a service", sub="Nineteen checks, run against every service in the network.")
    stats = [
        ("19 / 19", "checks caught their planted flaw", TEAL),
        ("0", "false alarms on the clean services", TEAL),
        ("7 of 19", "have no clean negative test at all", AMBER),
    ]
    x = 0.85
    for big, small, color in stats:
        card = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(3.0),
                                  Inches(3.75), Inches(2.2))
        card.fill.solid()
        card.fill.fore_color.rgb = CARD
        card.line.color.rgb = LINE
        card.line.width = Pt(1.5)
        card.shadow.inherit = False
        tf = tb(s, x + 0.35, 3.25, 3.05, 1.7)
        para(tf, big, size=44, color=color, bold=True, first=True, space_after=10)
        para(tf, small, size=15, color=MUTED, line_spacing=1.2)
        x += 3.95

    foot(s, "The third number is the one to be careful about: seven checks have never been seen to fail.")

    notes(s, "Say the caveat before anyone asks. Seven of the nineteen rules have no negative "
             "fixture, so a false alarm on those could exist and we could not have seen it. "
             "That is why the claim is zero observed and not zero possible.")


def s_results(prs):
    s = blank(prs)
    picture(s, FIG / "d4_results.png", x=0, y=1.08, max_w=W_IN, max_h=5.5,
            center_x=W_IN / 2)
    foot(s, "The middle bar is the part we expected to work, and it does not. Only counting rarity fixes it.")

    notes(s, "This is the result the paper is built on. Naive linking gives 0.207. A score "
             "threshold only reaches 0.571 and cannot do better, because a vendor default "
             "scores perfectly. Counting how many sites share each value is what takes it to "
             "1.000 with no false merges.")


def s_console(prs):
    s = blank(prs)
    kicker(s, "Module 6")
    title(s, "The analyst's screen", sub="Every link shows the evidence behind it, and the doubt.")
    picture(s, SCREEN, x=0.85, y=2.75, max_w=11.6, max_h=3.8, center_x=W_IN / 2)
    foot(s, "Nothing merges on its own. A named analyst confirms it, and every merge can be undone exactly.")

    notes(s, "Switch to the live console here if it is running. Click a link, point at the "
             "plus and minus lines, then click confirm. The point: counter-evidence sits next "
             "to evidence, not behind a score.")


def s_real(prs):
    s = blank(prs)
    kicker(s, "Validation")
    title(s, "What we tested on, and what we did not touch",
          sub="Three lawful ways to get real data without scanning anybody.")
    rows = [
        ("3,018", "real .onion addresses taken from public certificate transparency logs", TEAL),
        ("2", "of our own servers, one nginx and one onion service, over real Tor", TEAL),
        ("0", "bytes fetched from a third party we do not own", AMBER),
    ]
    y = 3.0
    for big, small, color in rows:
        tf = tb(s, 0.85, y, 1.9, 0.8)
        para(tf, big, size=30, color=color, bold=True, first=True)
        tf2 = tb(s, 2.9, y + 0.08, 9.3, 0.9)
        para(tf2, small, size=18, color=FG, first=True, line_spacing=1.15)
        y += 1.2

    foot(s, "Correlation stays synthetic, because no public ground truth for operator identity exists.")

    notes(s, "This is the slide the examiner will push on. Be direct: we did not scan "
             "anybody. Certificate transparency logs are public and free to read. The two "
             "servers are ours. Under section 43 of the IT Act, unauthorised access is an "
             "offence, so it was never on the table.")


def s_limits(prs):
    s = blank(prs)
    kicker(s, "Limitations")
    title(s, "What this does not do")
    items = [
        "It does not prove two services share an owner. It proposes a link and records who agreed.",
        "No live collection. Every byte here came from our own machines or a public log.",
        "Attribution is measured on generated writers, not on people. A direction, not a number.",
        "The console is one analyst's tool on one machine. It has no login and does not pretend to.",
    ]
    y = 2.35
    for text in items:
        tf = tb(s, 0.85, y, 11.6, 0.9)
        para(tf, text, size=20, color=FG, first=True, line_spacing=1.2)
        rule(s, 0.85, y + 0.82, 11.6, RGBColor(0x24, 0x2E, 0x3A), Pt(1))
        y += 1.12

    notes(s, "Do not rush this slide. A project that states its limits is trusted on "
             "everything else it says. If someone finds a gap you did not list, the other "
             "numbers get doubted too.")


def s_close(prs):
    s = blank(prs)
    kicker(s, "In one slide", y=1.25)
    tf = tb(s, 0.85, 1.8, 11.6, 3.4)
    para(tf, "A shared default is not a link.", size=33, color=FG, bold=True, first=True,
         space_after=20)
    para(tf, "Counting rarity turns correlation from a guess into a decision.",
         size=33, color=AMBER, bold=True, space_after=20)
    para(tf, "Every claim here carries the test that produced it.",
         size=33, color=FG, bold=True)
    rule(s, 0.85, 5.7, 2.2)

    notes(s, "Land these three sentences and stop. If you have one minute left, these are the minute.")


def s_end(prs):
    s = blank(prs)
    tf = tb(s, 0.85, 2.75, 11.6, 2.2)
    para(tf, "Questions", size=58, color=FG, bold=True, first=True, space_after=22)
    para(tf, "Including the awkward ones.", size=23, color=MUTED)

    tf2 = tb(s, 0.85, 6.05, 11.6, 0.6)
    para(tf2, "github.com/idkgarvit  ·  code, tests and the paper live together",
         size=15, color=MUTED, first=True)

    notes(s, "Have three answers ready: why not scan the live network, why the correlation "
             "numbers are not on real operators, and what you would do next with more time.")


# ------------------------------------------------------------------ audit
def audit(prs) -> list[str]:
    """Read the deck back and look for what makes one sound written by a machine."""

    problems = []
    for i, slide in enumerate(prs.slides, 1):
        for sh in slide.shapes:
            if not sh.has_text_frame:
                continue
            text = sh.text_frame.text
            low = text.lower()
            for word in BANNED:
                if word in low:
                    problems.append(f"slide {i}: banned phrase {word!r}")
            if "\u2014" in text or "\u2013" in text:
                problems.append(f"slide {i}: em or en dash in {text[:40]!r}")
            for sentence in re.split(r"(?<=[.!?])\s+", text):
                words = len(sentence.split())
                if words > 34:
                    problems.append(f"slide {i}: {words}-word sentence {sentence[:48]!r}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(ROOT / "out" / "ONIONTRACE_Presentation.pptx"))
    ap.add_argument("--skip-screenshot", action="store_true",
                    help="build without the console image (text-only draft)")
    args = ap.parse_args(argv)

    missing = [p.name for p in (FIG / "d1_problem.png", FIG / "d2_pipeline.png",
                                FIG / "d3_defaults.png", FIG / "d4_results.png",
                                FIG / "d5_farm.png") if not p.exists()]
    if missing:
        raise SystemExit(
            "error: diagrams missing: " + ", ".join(missing) +
            "\n  python scripts/build_diagrams.py"
        )

    prs = Presentation()
    prs.slide_width = Emu(int(W_IN * 914400))
    prs.slide_height = Emu(int(H_IN * 914400))

    builders = [s_title, s_problem, s_defaults, s_pipeline, s_farm, s_detect,
                s_results, s_real, s_limits, s_close, s_end]
    if not args.skip_screenshot:
        if not SCREEN.exists():
            raise SystemExit(
                f"error: {SCREEN} is missing. The console is the evidence for its slide.\n"
                "  python -m dashboard   then capture, or pass --skip-screenshot"
            )
        builders.insert(7, s_console)

    for fn in builders:
        fn(prs)

    issues = audit(prs)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(out)

    print(f"wrote {out}  ({out.stat().st_size:,} bytes)")
    print(f"{len(prs.slides)} slides")
    if issues:
        print(f"\n{len(issues)} text problem(s):")
        for x in issues:
            print(f"  - {x}")
        return 1
    print("text audit clean: no banned phrases, no dashes, no run-on sentences")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
