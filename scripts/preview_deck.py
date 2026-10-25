"""Render the deck's own geometry in a browser and measure it.

PowerPoint lays text out itself and python-pptx cannot ask it what it did, so `verify_deck.py`
has to estimate. This closes most of that gap without a PowerPoint install: it reads every
shape out of the pptx, rebuilds it as absolutely positioned HTML at the same inch
coordinates, and lets the browser lay the text out for real. Then it measures what the
browser produced.

What this catches: text that wraps to more lines than its box allows, and text that collides
with an image or another block. What it cannot catch: font substitution differences between
Linux and the machine that will present the deck, which is why the deck only uses Arial.

    python scripts/preview_deck.py deck.pptx out/preview
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
from pathlib import Path

import websockets
from pptx import Presentation

EMU_IN = 914400
PX_PER_IN = 96  # CSS reference pixel; only affects the preview, never the deck
PORT = 9344


def slide_html(prs, slide, index: int, imgdir: Path) -> str:
    W = prs.slide_width / EMU_IN
    H = prs.slide_height / EMU_IN
    parts = [
        f"<section class='slide' id='s{index}' "
        f"style='width:{W}in;height:{H}in;position:relative;overflow:hidden;"
        f"background:#0e1318;margin:0 0 24px 0'>"
    ]
    for sh in slide.shapes:
        if sh.left is None:
            continue
        x, y = sh.left / EMU_IN, sh.top / EMU_IN
        w, h = sh.width / EMU_IN, sh.height / EMU_IN
        if sh.shape_type == 13:  # picture
            imgdir.mkdir(parents=True, exist_ok=True)
            ext = sh.image.ext or "png"
            f = imgdir / f"s{index}_{sh.shape_id}.{ext}"
            if not f.exists():
                f.write_bytes(sh.image.blob)
            parts.append(
                f"<img src='{f.resolve().as_uri()}' "
                f"style='position:absolute;left:{x}in;top:{y}in;width:{w}in;height:{h}in' "
                f"data-kind='img' alt=''>"
            )
            continue
        if not sh.has_text_frame or not sh.text_frame.text.strip():
            # Shapes with no text are the accent rules and the stat cards. Paint them with
            # their real fill and border: a preview that draws every one of them as a solid
            # border-coloured block makes the cards look like grey slabs on the contact sheet
            # while the actual deck has dark cards with a thin outline.
            fill, border, radius = "#2a3542", "none", "0"
            try:
                if sh.fill.type is not None and sh.fill.fore_color.type is not None:
                    fill = f"#{sh.fill.fore_color.rgb}"
            except (AttributeError, TypeError):
                pass
            try:
                if sh.line.color.type is not None:
                    border = f"1px solid #{sh.line.color.rgb}"
            except (AttributeError, TypeError):
                pass
            if sh.shape_type == 5:  # rounded rectangle
                radius = "6px"
            parts.append(
                f"<div style='position:absolute;left:{x}in;top:{y}in;width:{w}in;height:{h}in;"
                f"background:{fill};border:{border};border-radius:{radius}' "
                f"data-kind='bar'></div>"
            )
            continue

        inner = []
        for p in sh.text_frame.paragraphs:
            text = "".join(r.text for r in p.runs)
            sizes = [r.font.size.pt for r in p.runs if r.font.size]
            pt = max(sizes) if sizes else 18.0
            colors = [r.font.color.rgb for r in p.runs
                      if r.font.color and r.font.color.type is not None]
            colour = f"#{colors[0]}" if colors else "#e8edf3"
            bold = any(r.font.bold for r in p.runs)
            sa = p.space_after.pt if p.space_after is not None else 0
            inner.append(
                f"<p style='margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;"
                f"font-size:{pt}pt;color:{colour};font-weight:{700 if bold else 400};"
                f"margin-bottom:{sa}pt;line-height:1.22'>{text}</p>"
            )
        parts.append(
            f"<div style='position:absolute;left:{x}in;top:{y}in;width:{w}in;height:{h}in;"
            f"overflow:visible' data-kind='text' data-box-h='{h}'>" + "".join(inner) + "</div>"
        )
    parts.append("</section>")
    return "".join(parts)


def build_html(path: Path, out: Path) -> tuple[Path, list[dict]]:
    """Write the preview page and return per-shape geometry for cross-checking."""

    prs = Presentation(path)
    imgdir = out / "img"
    body = "".join(slide_html(prs, s, i, imgdir) for i, s in enumerate(prs.slides, 1))
    html = (
        "<!doctype html><meta charset='utf-8'>"
        "<style>body{margin:0;padding:20px;background:#05080b}"
        ".slide{box-shadow:0 0 0 1px #232c36}img{object-fit:fill}"
        "@page{size:13.333in 7.5in;margin:0}"
        "@media print{body{padding:0;background:#0e1318}"
        ".slide{box-shadow:none;margin:0!important;break-after:page;break-inside:avoid}}"
        "</style>"
        + body
    )
    page = out / "preview.html"
    page.write_text(html, encoding="utf-8")
    return page, []


async def measure(html_path: Path, shots_dir: Path | None = None,
                  pdf_path: Path | None = None) -> list[dict]:
    proc = subprocess.Popen(
        ["chromium", "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         "--allow-file-access-from-files", f"--remote-debugging-port={PORT}",
         "--user-data-dir=/tmp/preview-deck", "--window-size=1400,1000",
         html_path.resolve().as_uri()],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        await asyncio.sleep(3.5)
        import urllib.request

        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
        page = [t for t in targets if t.get("type") == "page"][0]
        async with websockets.connect(page["webSocketDebuggerUrl"]) as ws:
            _last_frame = [b""]

            async def cdp(ws, method: str, **params):
                await ws.send(json.dumps({"id": 2, "method": method, "params": params}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == 2:
                        if "error" in msg:
                            raise RuntimeError(f"{method}: {msg['error']}")
                        res = msg["result"]
                        if "data" in res:
                            _last_frame[0] = base64.b64decode(res["data"])
                        return res

            async def js(expr: str):
                await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                          "params": {"expression": expr, "returnByValue": True}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == 1:
                        return msg["result"]["result"].get("value")

            # Wait for the document to actually have the slides. Sleeping a guessed
            # interval and then measuring an empty page reports zero problems, which is
            # indistinguishable from a clean deck.
            for _ in range(40):
                ready = await js("document.querySelectorAll('section.slide').length")
                if ready:
                    break
                await asyncio.sleep(0.25)
            if not ready:
                raise SystemExit("error: preview page never rendered its slides")

            slides = await js(r"""(() => {
              const out = [];
              document.querySelectorAll('section.slide').forEach((sec, i) => {
                const sr = sec.getBoundingClientRect();
                const texts = [...sec.querySelectorAll('[data-kind=text]')];
                const imgs = [...sec.querySelectorAll('[data-kind=img]')];
                const rec = {slide: i + 1, clipped: [], overflow: [], overlap: [],
                             textCount: texts.length, imgCount: imgs.length,
                             rect: {x: sr.left + window.scrollX, y: sr.top + window.scrollY,
                                    w: sr.width, h: sr.height}};
                texts.forEach(t => {
                  const top = t.getBoundingClientRect().top;
                  const last = t.lastElementChild;
                  if (!last) return;
                  const lr = last.getBoundingClientRect();
                  const need = (lr.bottom - top) / 96;
                  const box = t.getBoundingClientRect().height / 96;
                  if (need > box + 0.02)
                    rec.overflow.push({text: t.textContent.slice(0, 40),
                                       need: +need.toFixed(2), box: +box.toFixed(2)});
                  if (lr.bottom > sr.bottom + 1)
                    rec.clipped.push({text: t.textContent.slice(0, 40)});
                });
                imgs.forEach(img => {
                  const ir = img.getBoundingClientRect();
                  texts.forEach(t => {
                    const tr = t.getBoundingClientRect();
                    const ox = Math.min(ir.right, tr.right) - Math.max(ir.left, tr.left);
                    const oy = Math.min(ir.bottom, tr.bottom) - Math.max(ir.top, tr.top);
                    if (ox > 3 && oy > 3)
                      rec.overlap.push({text: t.textContent.slice(0, 34),
                                        by: Math.round(ox) + 'x' + Math.round(oy)});
                  });
                });
                out.push(rec);
              });
              return out;
            })()""")

            if shots_dir is not None:
                shots_dir.mkdir(parents=True, exist_ok=True)
                for s in slides:
                    r = s["rect"]
                    await cdp(ws, "Page.captureScreenshot", format="png",
                              captureBeyondViewport=True,
                              clip={"x": r["x"], "y": r["y"], "width": r["w"],
                                    "height": r["h"], "scale": 1})
                    shot = shots_dir / f"slide-{s['slide']:02d}.png"
                    shot.write_bytes(_last_frame[0])
                    s["shot"] = str(shot)

            if pdf_path is not None:
                pdf_path.parent.mkdir(parents=True, exist_ok=True)
                await cdp(ws, "Page.printToPDF", printBackground=True,
                                 paperWidth=13.333, paperHeight=7.5,
                                 marginTop=0, marginBottom=0, marginLeft=0, marginRight=0,
                                 preferCSSPageSize=True)
                pdf_path.write_bytes(_last_frame[0])
            return slides
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def amain(src: str, outdir: str, *, shots: bool = False,
                pdf: str | None = None) -> int:
    src_p = Path(src)
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    html_path, _ = build_html(src_p, out)
    slides = await measure(html_path,
                           shots_dir=(out / "slides") if shots else None,
                           pdf_path=Path(pdf) if pdf else None)

    problems = 0
    for s in slides:
        notes = []
        if s["overflow"]:
            problems += len(s["overflow"])
            for o in s["overflow"]:
                notes.append(f"OVERFLOW needs {o['need']}in in a {o['box']}in box: {o['text']!r}")
        if s["clipped"]:
            problems += len(s["clipped"])
            for c in s["clipped"]:
                notes.append(f"PAST SLIDE EDGE: {c['text']!r}")
        if s["overlap"]:
            problems += len(s["overlap"])
            for o in s["overlap"]:
                notes.append(f"IMAGE OVER TEXT by {o['by']}: {o['text']!r}")
        status = "clean" if not notes else f"{len(notes)} problem(s)"
        print(f"  slide {s['slide']:2}  {s['imgCount']} img, {s['textCount']} text  {status}")
        for n in notes:
            print(f"     - {n}")

    print(f"\n{len(slides)} slides rendered, {problems} problem(s)")
    print(f"preview html: {html_path}")
    return 1 if problems else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("deck", nargs="?", default="out/ONIONTRACE_Presentation.pptx")
    ap.add_argument("outdir", nargs="?", default="out/preview")
    ap.add_argument("--shots", action="store_true", help="write one PNG per slide")
    ap.add_argument("--pdf", help="also write a PDF of the slides")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(amain(a.deck, a.outdir, shots=a.shots, pdf=a.pdf)))
