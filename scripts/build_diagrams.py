"""Draw the deck's diagrams.

Each diagram is written as SVG and rendered by headless Chromium at 2x, which gives a
crisp PNG at a size PowerPoint will not have to rescale. Doing it this way rather than with
a charting default keeps the whole deck on one palette and one stroke weight, which is what
makes a set of slides look drawn rather than assembled.

    python scripts/build_diagrams.py            # writes out/fig/*.png
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out" / "fig"
RESULTS = ROOT / "out" / "eval-results.json"


def correlation_numbers() -> dict[str, float]:
    """Read the three precision figures from the frozen evaluation output.

    Drawn numbers that are typed in by hand drift from the file they came from, and a slide
    is exactly where that goes unnoticed. If the artefact is missing the diagram refuses to
    draw rather than inventing a chart.
    """

    import json

    if not RESULTS.exists():
        raise SystemExit(
            f"error: {RESULTS} is missing. Run `python -m eval` first; a results slide "
            "without the results file is a guess."
        )
    mods = json.loads(RESULTS.read_text())["modules"]["M2_correlation"]
    return {
        "naive": mods["naive"]["link_scoring"]["precision"],
        "threshold": mods["at_frozen_threshold"]["link_scoring"]["precision"],
        "filtered": mods["frequency_filtered"]["link_scoring"]["precision"],
        "naive_fm": mods["naive"]["link_scoring"]["false_merge_rate"],
        "threshold_fm": mods["at_frozen_threshold"]["link_scoring"]["false_merge_rate"],
        "filtered_fm": mods["frequency_filtered"]["link_scoring"]["false_merge_rate"],
    }

BG = "#0e1318"
PANEL = "#161d25"
LINE = "#2a3542"
FG = "#e8edf3"
MUTED = "#8b9aab"
AMBER = "#e0a256"
TEAL = "#4fb3a5"
RED = "#d1685f"
SANS = "DejaVu Sans, Liberation Sans, sans-serif"
MONO = "DejaVu Sans Mono, Liberation Mono, monospace"


def render(svg: str, name: str, w: int, h: int, scale: int = 2) -> Path:
    """Rasterise an SVG through the browser so the result is what the browser drew."""

    OUT.mkdir(parents=True, exist_ok=True)
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<style>html,body{margin:0;padding:0;background:%s}"
        "svg{display:block}</style>%s" % (BG, svg)
    )
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, dir=OUT) as fh:
        fh.write(html)
        page = fh.name
    png = OUT / f"{name}.png"
    subprocess.run(
        [
            "chromium", "--headless", "--disable-gpu", "--no-sandbox",
            "--hide-scrollbars", f"--force-device-scale-factor={scale}",
            f"--window-size={w},{h}", f"--screenshot={png}",
            f"--default-background-color={BG.lstrip('#')}",
            f"file://{page}",
        ],
        check=True, capture_output=True,
    )
    (OUT / f"{name}.svg").write_text(svg, encoding="utf-8")
    Path(page).unlink(missing_ok=True)
    if not png.exists() or png.stat().st_size < 5000:
        raise SystemExit(f"render produced nothing usable for {name}")
    print(f"  {png.name:26} {w * scale}x{h * scale}  {png.stat().st_size:,} B")
    return png


def _head(w: int, h: int) -> str:
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{w}' height='{h}' "
        f"viewBox='0 0 {w} {h}'>"
        f"<rect width='{w}' height='{h}' fill='{BG}'/>"
    )


def _iso(x: float, y: float, w: float, h: float, d: float, fill: str, stroke: str,
         opacity: float = 1.0) -> str:
    """An isometric-looking block: front face plus a top and a side that catch light."""

    top = f"{x},{y} {x + d},{y - d} {x + w + d},{y - d} {x + w},{y}"
    side = f"{x + w},{y} {x + w + d},{y - d} {x + w + d},{y + h - d} {x + w},{y + h}"
    face = f"{x},{y} {x + w},{y} {x + w},{y + h} {x},{y + h}"
    return (
        f"<g opacity='{opacity}'>"
        f"<polygon points='{top}' fill='{fill}' stroke='{stroke}' stroke-width='1.5'/>"
        f"<polygon points='{side}' fill='{fill}' stroke='{stroke}' stroke-width='1.5'"
        f" opacity='0.65'/>"
        f"<polygon points='{face}' fill='{fill}' stroke='{stroke}' stroke-width='1.5'/>"
        f"</g>"
    )


def _text(x: float, y: float, s: str, size: float = 20, fill: str = FG,
          anchor: str = "middle", weight: str = "normal", font: str = SANS,
          spacing: float = 0) -> str:
    ls = f" letter-spacing='{spacing}'" if spacing else ""
    return (
        f"<text x='{x}' y='{y}' font-family='{font}' font-size='{size}' fill='{fill}' "
        f"text-anchor='{anchor}' font-weight='{weight}'{ls}>{s}</text>"
    )


def _arrow(x1: float, y1: float, x2: float, y2: float, color: str = MUTED,
           width: float = 2.0, dash: str = "") -> str:
    d = f" stroke-dasharray='{dash}'" if dash else ""
    return (
        f"<line x1='{x1}' y1='{y1}' x2='{x2}' y2='{y2}' stroke='{color}' "
        f"stroke-width='{width}'{d} marker-end='url(#ah)'/>"
    )


DEFS = (
    "<defs><marker id='ah' viewBox='0 0 10 10' refX='9' refY='5' "
    "markerWidth='7' markerHeight='7' orient='auto-start-reverse'>"
    f"<path d='M 0 0 L 10 5 L 0 10 z' fill='{MUTED}'/></marker></defs>"
)


# ---------------------------------------------------------------- diagrams
def d1_problem() -> None:
    """Three sites, no name on any of them."""
    w, h = 1200, 620
    s = [_head(w, h), DEFS]
    s.append(_text(w / 2, 74, "Three services. Nobody says who runs them.", 40, FG, weight="600"))
    s.append(_text(w / 2, 118, "An onion address is a key, not a name.", 22, MUTED))

    names = ["a2x7…k4.onion", "qm3f…p9.onion", "zz81…t2.onion"]
    for i, name in enumerate(names):
        x = 120 + i * 340
        s.append(f"<rect x='{x}' y='190' width='280' height='190' rx='12' "
                 f"fill='{PANEL}' stroke='{LINE}' stroke-width='2'/>")
        s.append(_text(x + 140, 250, "?", 62, LINE, weight="700"))
        s.append(_text(x + 140, 318, name, 22, FG, font=MONO))
        s.append(_text(x + 140, 352, "no owner field", 16, MUTED))

    s.append(_arrow(400, 300, 460, 300, AMBER, 2.5))
    s.append(_arrow(740, 300, 800, 300, AMBER, 2.5))

    s.append(f"<rect x='380' y='450' width='440' height='104' rx='10' "
             f"fill='#1d1610' stroke='{AMBER}' stroke-width='2'/>")
    s.append(_text(600, 492, "Do these belong to one operator?", 22, AMBER, weight="600"))
    s.append(_text(600, 524, "Nothing on the page answers that.", 18, MUTED))
    s.append("</svg>")
    render("".join(s), "d1_problem", w, h)


def d2_pipeline() -> None:
    """The six stages, drawn as blocks on one line."""
    w, h = 1500, 700
    s = [_head(w, h), DEFS]
    s.append(_text(64, 74, "One command runs six stages", 38, FG, anchor="start", weight="600"))
    s.append(_text(64, 112, "python -m oniontrace run", 22, TEAL, anchor="start", font=MONO))

    stages = [
        ("Farm", "14 services\nin 4 clusters"),
        ("Gates", "authorisation\n+ retention"),
        ("Detect", "19 rules over\nevery service"),
        ("Correlate", "which services\nshare an operator"),
        ("Attribute", "which writer\nwrote the post"),
        ("Graph", "proposed links,\nreversible"),
    ]
    bw, gap, d = 168, 46, 16
    x0 = 64
    for i, (name, sub) in enumerate(stages):
        x = x0 + i * (bw + gap)
        fill = "#1b2530" if i not in (3,) else "#1f2a34"
        s.append(_iso(x, 260, bw, 150, d, fill, LINE))
        s.append(_text(x + bw / 2, 340, name, 24, FG, weight="600"))
        for j, line in enumerate(sub.split("\n")):
            s.append(_text(x + bw / 2, 452 + j * 24, line, 16, MUTED))
        if i < len(stages) - 1:
            ax = x + bw + 8
            s.append(_arrow(ax, 335, ax + gap - 12, 335, MUTED, 2.2))

    s.append(f"<line x1='64' y1='560' x2='{w - 64}' y2='560' stroke='{LINE}' stroke-width='1.5'/>")
    s.append(_text(64, 604, "Every stage writes down what it produced, including failure.", 20,
                   MUTED, anchor="start"))
    s.append(_text(64, 640, "A stage that fails is recorded, not skipped silently.", 20,
                   MUTED, anchor="start"))
    s.append("</svg>")
    render("".join(s), "d2_pipeline", w, h)


def d3_defaults() -> None:
    """Why a shared default is not evidence, and a shared rarity is."""
    w, h = 1400, 760
    s = [_head(w, h), DEFS]
    s.append(_text(64, 70, "A value shared by nine sites is not evidence", 36, FG,
                   anchor="start", weight="600"))

    # row 1: a vendor default, shared by many
    s.append(_text(64, 136, "Shared by nine sites, and by every other install of the same software",
                   19, MUTED, anchor="start"))
    for i in range(9):
        x = 64 + i * 96
        s.append(f"<rect x='{x}' y='160' width='80' height='80' rx='8' fill='{PANEL}' "
                 f"stroke='{RED}' stroke-width='2'/>")
        s.append(_text(x + 40, 208, "nginx", 13, RED, font=MONO))
    s.append(_text(1000, 208, "tells you nothing", 22, RED, anchor="start", weight="600"))

    # row 2: a genuinely rare artefact
    s.append(_text(64, 330, "Shared by two sites, and by no other site in the population",
                   19, MUTED, anchor="start"))
    for i in range(2):
        x = 64 + i * 96
        s.append(f"<rect x='{x}' y='354' width='80' height='80' rx='8' fill='#14231f' "
                 f"stroke='{TEAL}' stroke-width='2'/>")
        s.append(_text(x + 40, 402, "f4c9", 15, TEAL, font=MONO))
    for i in range(7):
        x = 256 + i * 96
        s.append(f"<rect x='{x}' y='354' width='80' height='80' rx='8' fill='{PANEL}' "
                 f"stroke='{LINE}' stroke-width='1.5' opacity='0.5'/>")
    s.append(_text(1000, 402, "this is the link", 22, TEAL, anchor="start", weight="600"))

    s.append(f"<rect x='64' y='500' width='1272' height='190' rx='12' fill='{PANEL}' "
             f"stroke='{LINE}' stroke-width='1.5'/>")
    for i, line in enumerate([
        "Thresholding alone does not fix this. A vendor default scores perfectly,",
        "so no cut-off separates it from a real shared artefact.",
        "Counting how rare a value is does fix it. That is the whole result.",
    ]):
        s.append(_text(96, 550 + i * 40, line, 21, FG if i == 2 else MUTED, anchor="start"))
    s.append("</svg>")
    render("".join(s), "d3_defaults", w, h)


def d4_results() -> None:
    """The correlation result as three bars, read from the frozen evaluation output."""
    n = correlation_numbers()
    w, h = 1400, 700
    s = [_head(w, h), DEFS]
    s.append(_text(64, 74, "Link precision, three ways", 38, FG, anchor="start", weight="600"))
    s.append(_text(64, 112, "Same farm, same seed. Only the decision rule changes.", 21, MUTED,
                   anchor="start"))

    bars = [
        ("Every proposed link", n["naive"], f"{n['naive']:.3f}", RED),
        ("Above a score threshold", n["threshold"], f"{n['threshold']:.3f}", AMBER),
        ("Counting how rare each value is", n["filtered"], f"{n['filtered']:.3f}", TEAL),
    ]
    x0, y0, maxw, bh, gap = 560, 200, 700, 96, 60
    for i, (label, val, shown, color) in enumerate(bars):
        y = y0 + i * (bh + gap)
        s.append(_text(x0 - 32, y + bh / 2 + 8, label, 21, FG, anchor="end"))
        s.append(f"<rect x='{x0}' y='{y}' width='{maxw}' height='{bh}' rx='6' "
                 f"fill='{PANEL}' stroke='{LINE}'/>")
        s.append(f"<rect x='{x0}' y='{y}' width='{maxw * val:.0f}' height='{bh}' rx='6' "
                 f"fill='{color}' opacity='0.85'/>")
        s.append(_text(x0 + maxw * val - 18, y + bh / 2 + 10, shown, 30, BG,
                       anchor="end", weight="700", font=MONO))

    s.append(_text(x0, 620,
                   f"False merges, the same three ways: {n['naive_fm']:.3f} -> "
                   f"{n['threshold_fm']:.3f} -> {n['filtered_fm']:.3f}",
                   20, MUTED, anchor="start"))
    s.append("</svg>")
    render("".join(s), "d4_results", w, h)


def d5_farm() -> None:
    """What the test farm contains."""
    w, h = 1300, 620
    s = [_head(w, h), DEFS]
    s.append(_text(64, 74, "A test network where the answer is known", 36, FG,
                   anchor="start", weight="600"))
    s.append(_text(64, 112, "14 services in 4 real clusters, plus decoys that look linked.", 20,
                   MUTED, anchor="start"))

    clusters = [("cluster A", 4, TEAL), ("cluster B", 4, AMBER), ("cluster C", 3, "#7f8ed6"),
                ("cluster D", 3, "#b07fd6")]
    x = 64
    for name, n, color in clusters:
        for i in range(n):
            s.append(f"<rect x='{x}' y='190' width='62' height='62' rx='8' fill='{PANEL}' "
                     f"stroke='{color}' stroke-width='2'/>")
            x += 72
        s.append(_text(x - (72 * n) + 4, 288, name, 16, color, anchor="start"))
        x += 40

    for i, line in enumerate([
        "20 personas with known writing styles     ·     38 hard negatives",
        "25 planted flaws across 19 detection rules",
    ]):
        s.append(_text(64, 400 + i * 44, line, 20, MUTED, anchor="start"))

    s.append(f"<rect x='64' y='470' width='1172' height='96' rx='10' fill='#11181f' "
             f"stroke='{LINE}'/>")
    s.append(_text(96, 512, "Every service here has a known owner, so every wrong answer can be",
                   20, FG, anchor="start"))
    s.append(_text(96, 544, "counted. That is what makes the numbers in this deck mean something.",
                   20, MUTED, anchor="start"))
    s.append("</svg>")
    render("".join(s), "d5_farm", w, h)


def main() -> int:
    print("drawing:")
    d1_problem()
    d2_pipeline()
    d3_defaults()
    d4_results()
    d5_farm()
    print(f"\nwritten to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
