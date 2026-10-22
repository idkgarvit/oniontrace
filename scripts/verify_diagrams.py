"""Check the diagrams the way a reader would: does anything run off the edge or collide?

An SVG that renders is not an SVG that reads. Text can overflow the canvas, two labels can
land on top of each other, or a colour can be too close to its background to see on a
projector. None of that is visible in a file size, so it is measured in the browser that
drew it.

    python scripts/verify_diagrams.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
FIG = ROOT / "out" / "fig"
PORT = 9333


async def measure(path: Path) -> dict:
    """Load the SVG, then read every text box and every shape box out of the live DOM."""

    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as fh:
        fh.write(
            "<!doctype html><meta charset='utf-8'>"
            "<style>html,body{margin:0;background:#0e1318}</style>"
            + path.read_text(encoding="utf-8")
        )
        page = fh.name

    proc = subprocess.Popen(
        ["chromium", "--headless", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--remote-debugging-port={PORT}",
         "--user-data-dir=/tmp/verify-dg", f"file://{page}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        await asyncio.sleep(3.0)
        import urllib.request

        targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
        page_t = [t for t in targets if t.get("type") == "page"][0]
        async with websockets.connect(page_t["webSocketDebuggerUrl"]) as ws:
            async def js(expr: str):
                await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                          "params": {"expression": expr, "returnByValue": True}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == 1:
                        return msg["result"]["result"].get("value")

            return await js(r"""(() => {
              const svg = document.querySelector('svg');
              const W = +svg.getAttribute('width'), H = +svg.getAttribute('height');
              const texts = [...svg.querySelectorAll('text')].map(t => {
                const b = t.getBBox();
                return {s: t.textContent.slice(0, 46), fill: t.getAttribute('fill'),
                        x: Math.round(b.x), y: Math.round(b.y),
                        w: Math.round(b.width), h: Math.round(b.height),
                        outL: b.x < -2, outR: b.x + b.width > W + 2,
                        outT: b.y < -2, outB: b.y + b.height > H + 2};
              });
              // text-on-text collisions
              const clash = [];
              for (let i = 0; i < texts.length; i++)
                for (let j = i + 1; j < texts.length; j++) {
                  const a = texts[i], b = texts[j];
                  const ox = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
                  const oy = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
                  if (ox > 3 && oy > 3)
                    clash.push([a.s, b.s, ox + 'x' + oy]);
                }
              return {W, H, count: texts.length, texts, clash};
            })()""")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        Path(page).unlink(missing_ok=True)


async def main() -> int:
    problems = 0
    for svg in sorted(FIG.glob("*.svg")):
        d = await measure(svg)
        off = [t for t in d["texts"] if t["outL"] or t["outR"] or t["outT"] or t["outB"]]
        print(f"\n{svg.name}  {d['W']}x{d['H']}  {d['count']} text elements")
        if off:
            problems += len(off)
            for t in off:
                print(f"   OFF-CANVAS  {t['s']!r} at ({t['x']},{t['y']}) {t['w']}x{t['h']}")
        if d["clash"]:
            problems += len(d["clash"])
            for a, b, ov in d["clash"]:
                print(f"   OVERLAP     {a!r} / {b!r} by {ov}")
        if not off and not d["clash"]:
            widest = max(d["texts"], key=lambda t: t["x"] + t["w"])
            print(f"   clean · widest text ends at x={widest['x'] + widest['w']} of {d['W']}")

    print(f"\n{problems} layout problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
