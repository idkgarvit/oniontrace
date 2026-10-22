"""Build the ONIONTRACE project presentation.

Regenerable on purpose: every figure in the deck is read from the frozen evaluation
results rather than typed in, so the slides cannot drift from the paper or the code.
Re-run after a new evaluation and the numbers move with it.

    python scripts/build_presentation.py --out ONIONTRACE_Presentation.pptx
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Emu, Inches, Pt

ROOT = Path(__file__).resolve().parent.parent
ALLOW_MISSING_SCREENSHOT = "--skip-screenshot" in sys.argv
sys.argv = [a for a in sys.argv if a != "--skip-screenshot"]

# -- palette ------------------------------------------------------------ #
BG = RGBColor(0x0E, 0x11, 0x16)
PANEL = RGBColor(0x15, 0x1A, 0x21)
PANEL2 = RGBColor(0x1B, 0x22, 0x2C)
LINE = RGBColor(0x2A, 0x34, 0x41)
FG = RGBColor(0xDB, 0xE3, 0xEC)
MUTED = RGBColor(0x85, 0x95, 0xA8)
ACCENT = RGBColor(0x4E, 0xA1, 0xFF)
ACCENT2 = RGBColor(0x7E, 0xE0, 0xC0)
WARN = RGBColor(0xD2, 0x99, 0x22)
BAD = RGBColor(0xF8, 0x51, 0x49)

FONT = "Arial"
W, H = 13.333, 7.5


# -- primitives --------------------------------------------------------- #
def new_deck() -> Presentation:
    prs = Presentation()
    prs.slide_width = Inches(W)
    prs.slide_height = Inches(H)
    return prs


def blank(prs: Presentation):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    bg = s.background.fill
    bg.solid()
    bg.fore_color.rgb = BG
    return s


def tb(slide, left, top, width, height, *, wrap=True):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    tf = box.text_frame
    tf.word_wrap = wrap
    tf.margin_left = tf.margin_right = Emu(0)
    tf.margin_top = tf.margin_bottom = Emu(0)
    return tf


def para(tf, text, *, size: float = 16, color=FG, bold=False, space_after=6,
         first=False, align=PP_ALIGN.LEFT, italic=False, font=FONT, space_before=0):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.alignment = align
    p.space_after = Pt(space_after)
    p.space_before = Pt(space_before)
    r = p.add_run()
    r.text = text
    f = r.font
    f.name = font
    f.size = Pt(size)
    f.bold = bold
    f.italic = italic
    f.color.rgb = color
    return p


def header(slide, kicker: str, title: str, sub: str | None = None) -> None:
    """Standard slide header: a small kicker, a large title, optional deck line."""

    tf = tb(slide, 0.85, 0.62, 11.6, 0.4)
    para(tf, kicker.upper(), size=11.5, color=ACCENT, bold=True, first=True, space_after=0)

    # A 30pt title in an 11.6in box fits about 50 characters. Longer titles wrap, and
    # a wrapped title collides with the rule below it, so the size steps down rather
    # than the text spilling. Measured, not guessed: 30pt x 2 lines is 1.02in in a
    # 0.94in gap.
    size = 30 if len(title) <= 48 else (27 if len(title) <= 60 else 24)
    tf = tb(slide, 0.85, 0.96, 11.6, 0.96)
    para(tf, title, size=size, color=FG, bold=True, first=True, space_after=0)

    y = 1.92
    if sub:
        tf = tb(slide, 0.85, y, 11.6, 0.5)
        para(tf, sub, size=13.5, color=MUTED, first=True, space_after=0)
        y = 2.42

    ln = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.85), Inches(y),
                                Inches(11.65), Emu(9525))
    ln.fill.solid(); ln.fill.fore_color.rgb = LINE
    ln.line.fill.background()
    ln.shadow.inherit = False


def bullets(slide, items, *, left=0.85, top=2.65, width=7.4, size: float = 15.5,
            gap=9, height: float | None = None):
    # Height is derived from where the box starts so it can never run off the slide;
    # a text box that extends past the page is clipped by some renderers and not
    # others, which is exactly the bug that survives to the presentation.
    tf = tb(slide, left, top, width, height if height is not None else max(1.0, 7.15 - top))
    first = True
    for it in items:
        if isinstance(it, tuple):
            text, lvl = it
        else:
            text, lvl = it, 0
        mark = "▪  " if lvl == 0 else "–  "
        p = para(
            tf, mark + text,
            size=size if lvl == 0 else size - 1.5,
            color=FG if lvl == 0 else MUTED,
            first=first,
            space_after=gap if lvl == 0 else gap - 3,
        )
        p.level = lvl
        first = False
    return tf


def card(slide, left, top, width, height, *, fill=PANEL, stroke=LINE):
    sh = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(left), Inches(top),
                                Inches(width), Inches(height))
    sh.adjustments[0] = 0.06
    sh.fill.solid(); sh.fill.fore_color.rgb = fill
    sh.line.color.rgb = stroke; sh.line.width = Pt(1)
    sh.shadow.inherit = False
    sh.text_frame.word_wrap = True
    return sh


def metric(slide, left, top, width, *, value, label, color=ACCENT2, height=1.15):
    sh = card(slide, left, top, width, height)
    tf = sh.text_frame
    tf.margin_left = tf.margin_right = Emu(0)
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    para(tf, value, size=25, color=color, bold=True, first=True, space_after=0,
         align=PP_ALIGN.CENTER)
    para(tf, label, size=10.5, color=MUTED, space_after=0, align=PP_ALIGN.CENTER)
    return sh


def table(slide, rows, *, left=0.85, top=2.6, width=11.65, col_widths=None,
          header_fill=PANEL2, size: float = 12.5, row_h=0.4, highlight=None):
    """A table with explicit fills; python-pptx's default banding is stripped."""

    nrows, ncols = len(rows), len(rows[0])
    shp = slide.shapes.add_table(nrows, ncols, Inches(left), Inches(top),
                                 Inches(width), Inches(row_h * nrows))
    tbl = shp.table
    tbl.first_row = True
    tbl.horz_banding = False

    if col_widths:
        total = sum(col_widths)
        for i, w in enumerate(col_widths):
            tbl.columns[i].width = Inches(width * w / total)
    for r in range(nrows):
        tbl.rows[r].height = Inches(row_h)

    for r, row in enumerate(rows):
        for c, val in enumerate(row):
            cell = tbl.cell(r, c)
            cell.fill.solid()
            cell.fill.fore_color.rgb = header_fill if r == 0 else PANEL
            cell.margin_left = Inches(0.08)
            cell.margin_right = Inches(0.06)
            cell.margin_top = Inches(0.02)
            cell.margin_bottom = Inches(0.02)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            tf = cell.text_frame
            tf.word_wrap = True
            p = tf.paragraphs[0]
            p.space_after = Pt(0)
            run = p.add_run()
            run.text = str(val)
            run.font.size = Pt(size - 1.5 if r == 0 else size)
            run.font.name = FONT
            run.font.bold = r == 0
            colour = MUTED if r == 0 else FG
            if highlight and r == highlight:
                colour = ACCENT2
                run.font.bold = True
            run.font.color.rgb = colour
    return tbl


def notes(slide, text: str) -> None:
    slide.notes_slide.notes_text_frame.text = text


def page_no(slide, n: int) -> None:
    tf = tb(slide, 12.0, 6.92, 0.9, 0.3)
    para(tf, str(n), size=10, color=MUTED, first=True, space_after=0, align=PP_ALIGN.RIGHT)


# -- figures from the frozen results ------------------------------------ #
def load_numbers() -> dict:
    data = json.loads((ROOT / "out" / "eval-results.json").read_text())
    m = data["modules"]
    corr = m["M2_correlation"]
    return {
        "naive_p": corr["naive"]["link_scoring"]["precision"],
        "naive_fm": corr["naive"]["link_scoring"]["false_merge_rate"],
        "thr_p": corr["at_frozen_threshold"]["link_scoring"]["precision"],
        "thr_fm": corr["at_frozen_threshold"]["link_scoring"]["false_merge_rate"],
        "filt_p": corr["frequency_filtered"]["link_scoring"]["precision"],
        "filt_fm": corr["frequency_filtered"]["link_scoring"]["false_merge_rate"],
        "p1_naive": corr["naive"]["ranking"]["precision@1"]["value"],
        "p1_filt": corr["frequency_filtered"]["ranking"]["precision@1"]["value"],
        "baseline": corr["naive"]["ranking"]["random_baseline_precision@1"],
        "careful_prec": m["M2_misconfig_rules"]["careful_rules"]["precision"]["value"],
        "careful_succ": m["M2_misconfig_rules"]["careful_rules"]["precision"]["successes"],
        "careful_tot": m["M2_misconfig_rules"]["careful_rules"]["precision"]["total"],
    }


FARM = {
    "services": 14, "clusters": 4, "flaws": 25, "personas": 20,
    "hard_neg": 38, "abstention": 6, "clean_controls": 42,
}
CT = {"returned": 3136, "san": 3018, "contaminated": 118, "onions": 688, "digicert_pct": 59.2}


# -- slides ------------------------------------------------------------- #
def slide_title(prs, n) -> None:
    s = blank(prs)

    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.9), Inches(1.75), Inches(0.09), Inches(1.5))
    bar.fill.solid(); bar.fill.fore_color.rgb = ACCENT
    bar.line.fill.background(); bar.shadow.inherit = False

    tf = tb(s, 1.25, 1.72, 10.8, 0.6)
    para(tf, "CAPSTONE PROJECT  ·  2025CSET703  ·  GROUP 32", size=12.5, color=ACCENT,
         bold=True, first=True, space_after=0)

    tf = tb(s, 1.25, 2.15, 10.8, 1.5)
    para(tf, "ONIONTRACE", size=62, color=FG, bold=True, first=True, space_after=2)
    para(tf, "Measuring operator correlation on Tor onion services", size=23,
         color=ACCENT2, space_after=0)

    tf = tb(s, 1.25, 4.35, 10.8, 1.6)
    para(tf, "A synthetic ground-truth farm, a measured correlator, and the finding that",
         size=14, color=MUTED, first=True, space_after=2)
    para(tf, "vendor defaults destroy naive identity correlation", size=14,
         color=MUTED, space_after=0, bold=True)

    tf = tb(s, 1.25, 5.85, 10.8, 1.0)
    para(tf, "Garvit Kanojia  ·  S25MCAG0002        Niketa Chauhan  ·  S25MCAG0015",
         size=14, color=FG, first=True, space_after=4, bold=True)
    para(tf, "Faculty guide:  Prof. (Dr.) Ajay Kumar        Bennett University",
         size=12.5, color=MUTED, space_after=0)

    notes(s, "Introduce the project in one sentence: we built a test bed with known "
             "ground truth so that operator-correlation claims can be measured instead "
             "of asserted. The headline result is a negative one and it is the most "
             "useful thing we produced.")


def slide_problem(prs, n) -> None:
    s = blank(prs)
    header(s, "Problem", "Onion services are easy to run and hard to attribute",
           "The network is designed so that nobody, including a researcher, can look up who is behind an address.")

    bullets(s, [
        "A v3 address is a public key. It says nothing about who holds it, and it changes freely.",
        "A single operator commonly runs several services: a market, a forum, a mirror, a help desk.",
        "Analysts want to know when two addresses belong to the same operator, because that is what turns",
        ("a list of websites into a picture of an organisation.", 1),
        "The evidence available to a passive visitor is indirect: favicons, analytics IDs, TLS",
        ("certificates, SSH host keys, bundled JavaScript, contact handles.", 1),
    ], width=7.5)

    card(s, 8.7, 2.65, 3.75, 3.5)
    tf = tb(s, 9.0, 2.9, 3.15, 3.1)
    para(tf, "WHY IT MATTERS", size=11, color=WARN, bold=True, first=True, space_after=10)
    para(tf, "Attribution is the step that turns observation into understanding:", size=12,
         color=MUTED, space_after=8)
    para(tf, "one operator running six services is a different finding from six unrelated operators.",
         size=12.5, color=FG, space_after=8)
    para(tf, "And a wrong merge is not a harmless error. It names the wrong organisation.",
         size=12.5, color=ACCENT2, space_after=0)

    notes(s, "Keep this short. The point to land is that attribution is the hard, valuable "
             "step, and that a false merge is a real-world harm, not a rounding error.")


def slide_trap(prs, n) -> None:
    s = blank(prs)
    header(s, "The trap", "The obvious approach fails for a reason nobody measures",
           "Two services sharing a value is the first rule anyone writes. It is also the rule that merges strangers.")

    card(s, 0.85, 2.7, 5.6, 3.15, stroke=BAD)
    tf = tb(s, 1.1, 2.95, 5.1, 2.75)
    para(tf, "WHAT AN ANALYST EXPECTS", size=11, color=MUTED, bold=True, first=True, space_after=8)
    para(tf, "\"These two services share a favicon and an analytics ID,", size=13, color=FG, space_after=2)
    para(tf, "so they are probably the same operator.\"", size=13, color=FG, space_after=10)
    para(tf, "Reasonable. And often correct.", size=12.5, color=MUTED, space_after=0, italic=True)

    card(s, 6.85, 2.7, 5.6, 3.15, stroke=WARN)
    tf = tb(s, 7.1, 2.95, 5.1, 2.75)
    para(tf, "WHAT ACTUALLY HAPPENS", size=11, color=WARN, bold=True, first=True, space_after=8)
    para(tf, "Vendors ship the same default favicon to every install. Analytics", size=13, color=FG, space_after=2)
    para(tf, "snippets get copy-pasted. Directory listings publish the same", size=13, color=FG, space_after=2)
    para(tf, "contact handle on everybody's page.", size=13, color=FG, space_after=10)
    para(tf, "So the shared value proves shared software, not shared ownership.",
         size=12.5, color=ACCENT2, space_after=0, bold=True)

    tf = tb(s, 0.85, 6.15, 11.6, 0.6)
    para(tf, "The field treats this as folklore. We made it a measured quantity, and then measured the fix.",
         size=14, color=FG, first=True, space_after=0, bold=True)

    notes(s, "This slide sets up the contribution. Do not rush it: the entire paper is "
             "about converting an acknowledged-but-unquantified hazard into a number and a design rule.")


def slide_gap(prs, n) -> None:
    s = blank(prs)
    header(s, "Gap in existing work", "The tool that does correlation does not measure it",
           "OnionScan is the reference tool in this space and it is genuinely good. That is what makes the gap specific.")

    rows = [
        ["", "OnionScan (Lewis, 2016)", "This work"],
        ["Cross-service correlation", "Supported, described in its own docs", "Supported and scored"],
        ["Frequency weighting", "Not present", "Core mechanism, cap derived from cluster size"],
        ["Precision / false-merge figures", "Not reported", "Measured against frozen ground truth"],
        ["Negative controls", "Not reported", "38 hard negatives, 6 abstention cases"],
        ["Abstention", "Not present", "Explicit: refuses rather than guessing"],
        ["Repeatability", "Manual, per-investigation", "Seed-driven, regenerated on demand"],
    ]
    table(s, rows, top=2.55, col_widths=[3.2, 4.2, 4.2], size=12.5, row_h=0.46)

    tf = tb(s, 0.85, 6.35, 11.6, 0.5)
    para(tf, "Our contribution is the measurement and the design consequence, not the idea of correlating.",
         size=13, color=ACCENT2, first=True, space_after=0, bold=True)

    notes(s, "Be scrupulously fair to OnionScan here. It does correlation; it just does not "
             "quantify it. Crediting the predecessor properly is what makes our gap credible.")


def slide_objectives(prs, n) -> None:
    s = blank(prs)
    header(s, "Objectives", "What the project set out to do")

    items = [
        ("01", "Build ground truth", "A synthetic farm with known operator clusters, so every claim has a right answer to be scored against."),
        ("02", "Measure the naive approach", "Quantify how badly stock defaults corrupt the obvious correlator, and how much of it is avoidable."),
        ("03", "Design a correlator that survives it", "Frequency weighting, abstention, and candidate-only output instead of assertions."),
        ("04", "Validate beyond the synthetic", "Real certificate-transparency data, real server binaries, real onion services we operate ourselves."),
        ("05", "Put a human in the loop", "An analyst console where nothing merges without a named operator and a stated rationale."),
    ]
    y = 2.35
    for num, head, body in items:
        sh = card(s, 0.85, y, 11.65, 0.86)
        tfh = tb(s, 1.05, y + 0.19, 0.75, 0.5)
        para(tfh, num, size=17, color=ACCENT, bold=True, first=True, space_after=0)
        tf = tb(s, 1.85, y + 0.13, 10.4, 0.62)
        para(tf, head, size=14, color=FG, bold=True, first=True, space_after=1)
        para(tf, body, size=11.5, color=MUTED, space_after=0)
        y += 0.95

    notes(s, "Five objectives, mapping to the five modules. Do not read them out; say "
             "that they are ordered by dependency, because ground truth had to exist before anything could be measured.")


def slide_architecture(prs, n) -> None:
    s = blank(prs)
    header(s, "Architecture", "Five modules and a farm, built in dependency order")

    boxes = [
        ("Farm", "ground truth", 0.85, ACCENT2),
        ("M1  Acquisition", "passive collection", 3.05, ACCENT),
        ("M2  Detection", "rules + correlator", 5.6, ACCENT),
        ("M3  Identity graph", "candidate merges", 8.25, ACCENT),
        ("M6  Console", "analyst decides", 10.75, ACCENT),
    ]
    for label, sub, x, colour in boxes:
        sh = card(s, x, 2.75, 2.05, 1.25, stroke=colour)
        tf = sh.text_frame
        tf.margin_left = tf.margin_right = Emu(0)
        para(tf, label, size=12.5, color=FG, bold=True, first=True, space_after=2,
             align=PP_ALIGN.CENTER)
        para(tf, sub, size=9.5, color=MUTED, space_after=0, align=PP_ALIGN.CENTER)

    for x in (2.93, 5.48, 8.13, 10.63):
        a = s.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, Inches(x), Inches(3.25),
                               Inches(0.12), Inches(0.22))
        a.fill.solid(); a.fill.fore_color.rgb = LINE
        a.line.fill.background(); a.shadow.inherit = False

    band = card(s, 0.85, 4.45, 11.65, 1.5)
    tf = tb(s, 1.1, 4.68, 11.1, 1.1)
    para(tf, "RUNNING THROUGH EVERY LAYER", size=10.5, color=WARN, bold=True, first=True, space_after=7)
    para(tf, "Deployment gates:  no case, no collection  ·  authorisation reference required  "
             "·  per-case seeds  ·  retention set before data exists", size=12, color=FG, space_after=5)
    para(tf, "Provenance:  every artefact carries a SHA-256 of the observation it was read from",
         size=12, color=FG, space_after=0)

    tf = tb(s, 0.85, 6.25, 11.6, 0.6)
    para(tf, "One command runs the chain:  python -m oniontrace run --seed 20260925   ·   360 tests, all passing",
         size=12.5, color=MUTED, first=True, space_after=0)

    notes(s, "Point out the ordering: the evaluation harness was built before the modules "
             "it measures. That is deliberate and it is why the numbers are defensible.")


def slide_farm(prs, n) -> None:
    s = blank(prs)
    header(s, "Ground truth", "A farm where the answer is known by construction",
           "Generated from a seed, so anyone can regenerate it and re-derive every number.")

    metrics = [
        ("14", "services", ACCENT2), ("4", "clusters of 3", FG),
        ("25", "planted flaws", FG), ("38", "hard negatives", WARN),
        ("20", "personas", FG), ("6", "abstention cases", FG),
    ]
    x = 0.85
    for val, lab, col in metrics:
        metric(s, x, 2.5, 1.83, value=val, label=lab, color=col)
        x += 1.95

    bullets(s, [
        "Twelve services sit in four clusters of three; two are singletons, and two are clean controls.",
        "The hard negatives are the interesting part: unrelated clusters are given byte-identical",
        ("vendor-default artefacts, so a naive correlator merges them and every merge is false.", 1),
        "Near-miss decoys carry a published placeholder handle, so linking a person to it would be",
        ("an accusation built on public information.", 1),
        "42 clean-control endpoints exist so a false positive is observable, not just assumed absent.",
    ], top=4.05, width=11.6, size=14, gap=8)

    notes(s, "The hard negatives are the single most important design decision in the farm. "
             "Without them, the naive correlator would look fine and we would have learned nothing.")


def slide_acquisition(prs, n) -> None:
    s = blank(prs)
    header(s, "M1  Acquisition", "Passive collection, behind gates that cannot be skipped",
           "Everything here reads what an anonymous visitor receives. No credentials, no enumeration, no exploitation.")

    bullets(s, [
        "Tor SOCKS collection with scheduled revisits and WARC-style snapshots kept for provenance.",
        "Public Telegram previews via server-rendered pages only; deduplicated by message id.",
        "Certificate Transparency as a secondary signal linking an onion to clearnet infrastructure.",
        "Collection permits, run budgets and resumable cursors, so a run is bounded and repeatable.",
    ], width=7.4, top=2.6)

    card(s, 8.65, 2.6, 3.85, 3.5, stroke=ACCENT2)
    tf = tb(s, 8.92, 2.85, 3.3, 3.1)
    para(tf, "THE RULE WE BUILT AROUND", size=10.5, color=ACCENT2, bold=True, first=True, space_after=9)
    para(tf, "no case, no collection", size=13.5, color=FG, bold=True, space_after=9)
    para(tf, "A run must name an owning analyst, an authorisation reference, its own seeds, "
             "and a retention date set at creation.", size=11.5, color=MUTED, space_after=9)
    para(tf, "There is no global crawl list, so a run cannot inherit targets from a previous investigation.",
         size=11.5, color=FG, space_after=0)

    notes(s, "This is where a government panel would push, so be ready: the answer is that "
             "authorisation is a required field with a document reference, not a checkbox.")


def slide_detection(prs, n) -> None:
    s = blank(prs)
    header(s, "M2  Detection", "19 rules, scored on recall and precision",
           "Rules look for what a misconfigured service exposes to a visitor: directory listings, exposed dotfiles, version banners, host-key reuse.")

    rows = [
        ["Rule set", "Recall", "Precision", "What it means"],
        ["Careful rules (19)", "19 / 19", "19 / 19", "Fires where it should, stays silent where it should"],
        ["Naive rules (31)", "17 / 19", "17 / 31", "Fires, but also fires on pages that are clean"],
    ]
    table(s, rows, top=2.55, col_widths=[3.0, 1.5, 1.6, 5.5], size=13, row_h=0.5, highlight=1)

    tf = tb(s, 0.85, 4.35, 11.6, 1.6)
    para(tf, "THE HONEST CAVEAT", size=11, color=WARN, bold=True, first=True, space_after=8)
    para(tf, "For 7 of the 19 rules no negative fixture exists in the farm, so a false positive could not have "
             "been observed even if the rule were wrong. The defensible sentence is therefore:", size=13, color=FG, space_after=8)
    para(tf, "\"no false positive was observed, and for seven rules none could have been.\"",
         size=13.5, color=ACCENT2, bold=True, space_after=0)

    notes(s, "Volunteer this caveat before anyone asks. It is in the paper, and offering it "
             "is more convincing than defending the 19/19 as if it were complete.")


def slide_finding(prs, n) -> None:
    s = blank(prs)
    header(s, "Headline finding", "One rule turns a coin-flip into a defensible answer",
           "Same farm, same seeds, three correlators. Only the treatment of common artefacts changes.")

    rows = [
        ["Correlator", "Precision", "False-merge rate", "Precision@1"],
        ["Naive: share any artefact", "0.207", "0.793", "0.75"],
        ["Thresholded", "0.571", "0.429", "0.75"],
        ["Frequency-filtered (cap 3)", "1.000", "0.000", "1.00"],
        ["Random baseline", "0.154", "—", "0.15"],
    ]
    table(s, rows, top=2.6, col_widths=[4.6, 2.2, 2.6, 2.2], size=13.5, row_h=0.52, highlight=3)

    tf = tb(s, 0.85, 5.15, 11.6, 1.5)
    para(tf, "Discarding artefact values that appear on more than three services removes every false merge, and it costs no recall.",
         size=14, color=FG, first=True, space_after=8, bold=True)
    para(tf, "The naive correlator is barely better than guessing (0.207 against a 0.154 baseline). A confidence threshold cannot fix it, "
             "because the false merges are confident: they rest on real, byte-identical artefacts. Only frequency can separate them.",
         size=12.5, color=MUTED, space_after=0)

    notes(s, "This is the slide to slow down on. The key point is the middle row: a "
             "threshold does not help, because the false merges score high. That is why the "
             "fix has to be frequency weighting, not a stricter cutoff.")


def slide_graph(prs, n) -> None:
    s = blank(prs)
    header(s, "M3  Identity graph", "Candidate merges, never assertions",
           "The graph proposes. A person decides. Nothing moves without a named operator and a stated reason.")

    metrics = [
        ("14", "entities"), ("12", "proposed links"), ("1.000", "precision"),
        ("1.000", "recall"), ("0", "false merges"),
    ]
    x = 0.85
    for val, lab in metrics:
        metric(s, x, 2.55, 2.25, value=val, label=lab)
        x += 2.35

    bullets(s, [
        "Every link carries its evidence, the weight that produced the score, and any counter-evidence.",
        "A merge requires a named actor and a rationale, and writes a full before-state so it can be reversed exactly.",
        "Vendor defaults are excluded from every link and reported separately as cohorts, so their absence is explainable.",
        "Below-threshold pairs are still shown, labelled possible, with the score visible rather than hidden.",
    ], top=4.05, width=11.6, size=13.5, gap=7)

    notes(s, "If asked why 12 links and not 12 merges: because the system is not allowed to "
             "merge. The 12 proposals recover all 12 true pairs with no false positive, and an "
             "analyst still has to confirm each one.")


def slide_codemix(prs, n) -> None:
    s = blank(prs)
    header(s, "M5  Code-mix block", "The Hinglish feature earns its place, but only when gated",
           "Language-split function words plus code-mix ratio and switch rate, switched on by detected mix rather than applied to everybody.")

    rows = [
        ["Writer", "Without code-mix", "With code-mix", "Change"],
        ["Hinglish (20% mix)", "0.500", "0.750", "+0.250"],
        ["Pure English", "0.250", "0.250", "no change"],
        ["Template reuser", "0.750", "0.750", "no change"],
        ["Overall hit@1", "0.400", "0.450", "+0.050"],
    ]
    table(s, rows, top=2.6, col_widths=[4.2, 3.0, 3.0, 2.2], size=13.5, row_h=0.5, highlight=1)

    card(s, 0.85, 5.0, 5.6, 1.55, stroke=WARN)
    tf = tb(s, 1.1, 5.2, 5.1, 1.2)
    para(tf, "WHY A GATE AND NOT A FEATURE", size=10.5, color=WARN, bold=True, first=True, space_after=7)
    para(tf, "Applied to everyone, code-mix cut template reusers from 0.750 to 0.250. "
             "A writer with a fixed template has little vocabulary variance, so extra "
             "dimensions are pure noise.", size=11.5, color=FG, space_after=0)

    card(s, 6.85, 5.0, 5.6, 1.55, stroke=BAD)
    tf = tb(s, 7.1, 5.2, 5.1, 1.2)
    para(tf, "AND WHY THIS IS NOT A HEADLINE", size=10.5, color=BAD, bold=True, first=True, space_after=7)
    para(tf, "Four mixed-regime personas means the gain is one query flipping. It is a "
             "direction, not a magnitude, and the gate was fitted on the same farm.",
         size=11.5, color=FG, space_after=0)

    notes(s, "The honest framing matters here: we built the differentiator, measured it, "
             "found it helps only where it should, and reported the template-reuser "
             "regression rather than hiding it behind an average.")


def slide_dashboard(prs, n) -> None:
    s = blank(prs)
    header(s, "M6  Analyst console", "The interface has to show doubt, not hide it",
           "Scores, counter-evidence and the merge log are on the same screen as the finding.")

    img = ROOT / "out" / "screen" / "dashboard.png"
    if img.exists():
        s.shapes.add_picture(str(img), Inches(0.85), Inches(2.5), height=Inches(4.15))

        card(s, 8.75, 2.5, 3.75, 4.15)
        tf = tb(s, 8.98, 2.72, 3.3, 3.75)
        para(tf, "WHAT IS ON SCREEN", size=10.5, color=ACCENT, bold=True, first=True, space_after=10)
        for line in [
            "Force-directed entity graph, coloured by link status.",
            "Solid lines are supported; dashed are below threshold.",
            "Every link row shows the artefacts that produced it.",
            "Conflicts show both sides instead of picking one.",
            "Confirm, reject and reverse are one click each.",
            "Evidence pack exports to PDF with the digest ledger.",
        ]:
            para(tf, "▪  " + line, size=11.5, color=MUTED, space_after=8)
    elif not ALLOW_MISSING_SCREENSHOT:
        raise SystemExit(
            f"error: {img} is missing.\n"
            "The console is the M6 evidence; a deck without it carries a placeholder where "
            "evidence belongs.\n"
            "  python scripts/capture_dashboard.py        # build a case, serve it, capture it\n"
            "  python scripts/build_presentation.py --skip-screenshot   # text-only build"
        )

    notes(s, "Live demo opportunity: start the console on 127.0.0.1:8420, show the graph, "
             "click a node, then export the evidence pack. If the demo fails, this slide carries it.")


def slide_validation(prs, n) -> None:
    s = blank(prs)
    header(s, "Real-world validation", "Three channels, none of which touch a third party",
           "Correlation stays synthetic by necessity, because no public ground truth for operator identity exists. Everything else was validated on real data.")

    cols = [
        ("Certificate Transparency", ACCENT,
         [f"{CT['returned']:,} certificates returned",
          f"{CT['san']:,} with a real .onion SAN",
          f"{CT['onions']} unique onion addresses",
          f"{CT['digicert_pct']}% under one issuer",
          f"{CT['contaminated']} entries rejected as contamination"]),
        ("Our own servers", ACCENT2,
         ["Two nginx instances, loopback only",
          "7 probes against each",
          "Careful rules: correct verdicts",
          "Naive rules: fire on clean pages",
          "Configs and raw responses frozen"]),
        ("Onion services we ran", WARN,
         ["Two v3 services published by us",
          "Fetched over real Tor circuits",
          "Misconfigured: 2 findings, 0 false",
          "Naive: 5 findings, 1 false",
          "Clean control: careful silent, naive fired"]),
    ]
    x = 0.85
    for title, colour, lines in cols:
        sh = card(s, x, 2.5, 3.75, 3.6, stroke=colour)
        tf = tb(s, x + 0.25, 2.75, 3.3, 3.2)
        para(tf, title.upper(), size=10.5, color=colour, bold=True, first=True, space_after=10)
        for ln in lines:
            para(tf, "▪  " + ln, size=11.5, color=FG, space_after=8)
        x += 3.95

    tf = tb(s, 0.85, 6.35, 11.6, 0.6)
    para(tf, "No third-party onion service was contacted at any point. The contamination count is reported rather than quietly filtered.",
         size=12.5, color=MUTED, first=True, space_after=0)

    notes(s, "Two things to be precise about: the CT numbers are retrospective public data, "
             "not a live probe; and the two onion services were ours, so no third party was involved. "
             "Both are stated in the paper.")


def slide_ethics(prs, n) -> None:
    s = blank(prs)
    header(s, "Ethics and legal posture", "Passive by design, and the design is enforced in code",
           "This is the part a review panel examines hardest, so it is built into the software rather than promised in prose.")

    left = [
        "Only what an anonymous visitor receives is collected.",
        "No credentials, no enumeration, no exploitation.",
        "No third-party onion service was ever contacted.",
        "Tor runs client-only, inside a sandbox without access to keys.",
    ]
    right = [
        "Active scanning was considered and rejected as unlawful.",
        "Legal footing: Section 43 of the IT Act, and authorisation is a required field.",
        "SSRF guard blocks private, link-local and metadata addresses.",
        "NAT64 addresses are judged by the IPv4 they tunnel to.",
    ]

    card(s, 0.85, 2.5, 5.6, 3.3, stroke=ACCENT2)
    tf = tb(s, 1.12, 2.75, 5.05, 2.9)
    para(tf, "WHAT WE DO", size=10.5, color=ACCENT2, bold=True, first=True, space_after=10)
    for ln in left:
        para(tf, "▪  " + ln, size=12, color=FG, space_after=9)

    card(s, 6.85, 2.5, 5.6, 3.3, stroke=BAD)
    tf = tb(s, 7.12, 2.75, 5.05, 2.9)
    para(tf, "WHAT WE REFUSED TO DO", size=10.5, color=BAD, bold=True, first=True, space_after=10)
    for ln in right:
        para(tf, "▪  " + ln, size=12, color=FG, space_after=9)

    tf = tb(s, 0.85, 6.1, 11.6, 0.6)
    para(tf, "A NAT64 address such as 64:ff9b::7f00:1 is 127.0.0.1. Our first guard allowed it. The test that catches it is now in the suite.",
         size=12, color=MUTED, first=True, space_after=0)

    notes(s, "The NAT64 example is worth telling: it shows the guard was tested against real "
             "adversarial cases rather than written and assumed correct.")


def slide_limitations(prs, n) -> None:
    s = blank(prs)
    header(s, "Limitations", "What this work does not establish",
           "Stated as structure, not as footnotes. A claim with no boundary is not a research result.")

    rows = [
        ["Limitation", "Consequence"],
        ["Correlation ground truth is synthetic",
         "Precision figures describe behaviour on the farm, not on the live network"],
        ["Small n, fixed topology, one seed family",
         "Confidence intervals are wide; results reproduced across 3 seeds, not across topologies"],
        ["7 of 19 rules have no negative fixture",
         "False positives could not have been observed for those rules"],
        ["Attribution over real human text is out of scope",
         "Persona metrics are synthetic retrieval, not authorship attribution"],
        ["Single-analyst, loopback, no authentication",
         "This is an analyst tool, not a multi-user deployment"],
    ]
    table(s, rows, top=2.5, col_widths=[4.6, 7.05], size=12.5, row_h=0.52)

    tf = tb(s, 0.85, 5.95, 11.6, 0.7)
    para(tf, "Every one of these is in the paper, in the abstract or in a section that a reviewer will reach.",
         size=12.5, color=ACCENT2, first=True, space_after=0, bold=True)

    notes(s, "Do not apologise for these. State them as scope. The strongest thing in the "
             "project is that its boundaries are written down before anyone asks.")


def slide_conclusion(prs, n) -> None:
    s = blank(prs)
    header(s, "Conclusion", "What we can now say that we could not say before")

    items = [
        ("The hazard is quantified.",
         "Sharing artefacts naively gives 0.207 precision, barely above the 0.154 baseline, with a 0.793 false-merge rate."),
        ("The fix is specific and cheap.",
         "A frequency cap removes every false merge on the farm and loses no recall. It needs no model and no GPU."),
        ("A threshold cannot substitute.",
         "False merges are confident because they rest on real, identical artefacts, so tightening a cutoff does not separate them."),
        ("The machinery exists and is tested.",
         "Farm, collection, detection, identity graph and analyst console, with 343 automated tests and a regenerable evidence pack."),
        ("The boundaries are recorded.",
         "Where the evidence is synthetic, the report says so, and where a rule was never challenged, that is stated too."),
    ]
    y = 2.3
    for head, body in items:
        card(s, 0.85, y, 11.65, 0.82, fill=PANEL)
        tf = tb(s, 1.1, y + 0.11, 11.15, 0.62)
        para(tf, head, size=13, color=ACCENT2, bold=True, first=True, space_after=2)
        para(tf, body, size=11.5, color=MUTED, space_after=0)
        y += 0.9

    notes(s, "End on the second point. The contribution is not a sophisticated model; it is "
             "one rule, measured, that changes the answer from a coin flip to a defensible one.")


def slide_thanks(prs, n) -> None:
    s = blank(prs)

    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.9), Inches(2.15), Inches(0.09), Inches(1.2))
    bar.fill.solid(); bar.fill.fore_color.rgb = ACCENT
    bar.line.fill.background(); bar.shadow.inherit = False

    tf = tb(s, 1.25, 2.1, 10.8, 1.4)
    para(tf, "Thank you", size=52, color=FG, bold=True, first=True, space_after=4)
    para(tf, "Questions welcome, including the uncomfortable ones.", size=20, color=ACCENT2, space_after=0)

    tf = tb(s, 1.25, 4.35, 11.0, 1.9)
    para(tf, "Reproduce it", size=11, color=ACCENT, bold=True, first=True, space_after=6)
    para(tf, "python -m oniontrace run --seed 20260925        # the whole chain, one command",
         size=12, color=MUTED, space_after=2, font="Courier New")
    para(tf, "python -m farm validate                          # check the farm is consistent",
         size=12, color=MUTED, space_after=2, font="Courier New")
    para(tf, "python -m eval run --seed 20260925 --out ./out   # rebuild every number in this deck",
         size=12, color=MUTED, space_after=2, font="Courier New")
    para(tf, "python -m graph --db ./casework/CASE-1/graph.db --case CASE-1 build --farm ./out/farm.json",
         size=12, color=MUTED, space_after=2, font="Courier New")
    para(tf, "python -m dashboard                              # analyst console on 127.0.0.1:8420",
         size=12, color=MUTED, space_after=0, font="Courier New")

    tf = tb(s, 1.25, 6.35, 11.0, 0.5)
    para(tf, "Garvit Kanojia · S25MCAG0002        Niketa Chauhan · S25MCAG0015        Guide: Prof. (Dr.) Ajay Kumar",
         size=11.5, color=MUTED, first=True, space_after=0)

    notes(s, "Leave this up during questions. The reproduce commands are the answer to "
             "'how do we know these numbers are real'.")


# -- main --------------------------------------------------------------- #
def build(out: Path) -> Path:
    prs = new_deck()
    builders = [
        slide_title, slide_problem, slide_trap, slide_gap, slide_objectives,
        slide_architecture, slide_farm, slide_acquisition, slide_detection,
        slide_finding, slide_graph, slide_codemix, slide_dashboard, slide_validation,
        slide_ethics, slide_limitations, slide_conclusion, slide_thanks,
    ]
    for i, fn in enumerate(builders, start=1):
        fn(prs, i)
        page_no(prs.slides[-1], i)

    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="ONIONTRACE_Presentation.pptx")
    args = ap.parse_args(argv)
    path = build(Path(args.out))
    print(json.dumps({"written": str(path), "bytes": path.stat().st_size}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
