"""Capture the analyst console to a PNG for the deck.

The deck embeds a screenshot of the console. If that image is produced by hand, a clean
checkout either ships a deck with a hole in it or ships a screenshot of a UI that has since
changed. Neither is acceptable in something a reader is meant to trust, so the capture is a
script: it builds a case, serves it, photographs it, and stops.

    python scripts/capture_dashboard.py                 # build + serve + capture
    python scripts/capture_dashboard.py --case CASE-1   # reuse a case already built

Requires chromium (or google-chrome) on PATH. The server binds loopback only, matching the
console's own non-configurable host, so nothing here is reachable off the machine.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "out" / "screen" / "console.png"
HOST = "127.0.0.1"
PORT = 8420


def _browser() -> str:
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit(
        "error: no chromium or chrome on PATH. The console screenshot cannot be captured, "
        "and shipping the deck without it would leave a placeholder where evidence belongs."
    )


def _capture_element(url: str, selector: str, out: Path, width: int, height: int,
                     scale: int, max_css_width: float = 1000.0) -> None:
    """Screenshot one element rather than the page, at a device scale factor.

    This exists because of a measured problem, not tidiness. Captured as a full page at 1920
    wide, the console's 10 to 13 pixel labels land about 3.5pt tall once the image is placed
    on a 13.33in slide, which is illegible from anywhere but the front row. Cropping to the
    graph at a narrower viewport makes the same labels roughly three times that size relative
    to the slide, because what matters is how many CSS pixels share the slide's width, not how
    many pixels the file has.
    """

    import asyncio
    import base64
    import json
    import urllib.request

    import websockets

    port = 9333
    proc = subprocess.Popen(
        [_browser(), "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         f"--window-size={width},{height}", f"--remote-debugging-port={port}",
         "--user-data-dir=/tmp/capture-console", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    async def run() -> None:
        await asyncio.sleep(3.0)
        for _ in range(40):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                page = [t for t in targets if t.get("type") == "page"]
                if page:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.25)
        else:
            raise SystemExit("error: could not attach to the browser to capture the element")

        async with websockets.connect(page[0]["webSocketDebuggerUrl"], max_size=None) as ws:
            async def send(method: str, **params):
                await ws.send(json.dumps({"id": 7, "method": method, "params": params}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == 7:
                        if "error" in msg:
                            raise SystemExit(f"error: {method} failed: {msg['error']}")
                        return msg["result"]

            async def js(expr: str):
                r = await send("Runtime.evaluate", expression=expr, returnByValue=True,
                               awaitPromise=True)
                return r["result"].get("value")

            # Do not trust the URL given on the command line: headless chromium will
            # sometimes sit on about:blank with the target already reporting the requested
            # URL, and a capture of about:blank passes every size check while showing nothing.
            await send("Page.navigate", url=url)
            for _ in range(60):
                if await js("document.readyState") == "complete" and await js(
                        "document.querySelector('body') && document.body.children.length"):
                    break
                await asyncio.sleep(0.25)
            else:
                raise SystemExit(f"error: {url} never finished loading")

            rect = None
            for _ in range(40):
                rect = await js(f"""(() => {{
                    const e = document.querySelector({selector!r});
                    if (!e) return null;
                    const r = e.getBoundingClientRect();
                    if (r.width < 50 || r.height < 50) return null;
                    return {{x: r.left + window.scrollX, y: r.top + window.scrollY,
                             w: r.width, h: r.height}};
                }})()""")
                if rect:
                    break
                await asyncio.sleep(0.25)
            if not rect:
                raise SystemExit(
                    f"error: {selector!r} never appeared, or stayed too small to be worth "
                    "photographing"
                )

            if rect["w"] > max_css_width:
                raise SystemExit(
                    f"error: {selector} is {rect['w']:.0f} css px wide. Placed across a "
                    f"13.33in slide that makes its labels about "
                    f"{10 / rect['w'] * 11.6 * 72:.1f}pt, too small to read from the back of "
                    f"a room. Narrow the viewport so the element is under "
                    f"{max_css_width:.0f}px."
                )

            shot = await send("Page.captureScreenshot", format="png", captureBeyondViewport=True,
                              clip={"x": rect["x"], "y": rect["y"], "width": rect["w"],
                                    "height": rect["h"], "scale": scale})
            out.write_bytes(base64.b64decode(shot["data"]))
            print(f"   element {selector} -> {rect['w']:.0f}x{rect['h']:.0f} css px "
                  f"at {scale}x")

    try:
        asyncio.run(run())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, port)) == 0


def _wait_for_port(port: int, timeout: float = 30.0) -> bool:
    """Wait until something is listening, rather than sleeping a guessed interval."""

    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex((HOST, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def _build_case(case: str, seed: int) -> Path:
    """Run the pipeline so the captured console shows a real case, not a fixture stub."""

    subprocess.run(
        [sys.executable, "-m", "oniontrace", "run", "--seed", str(seed), "--case", case],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    return ROOT / "casework" / case


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", default="CASE-DEMO")
    ap.add_argument("--seed", type=int, default=20260925)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--width", type=int, default=1500)
    ap.add_argument("--height", type=int, default=1800)
    ap.add_argument("--scale", type=int, default=2,
                    help="device pixel ratio; 2 keeps the console crisp when it fills a "
                         "projected slide")
    ap.add_argument("--skip-build", action="store_true",
                    help="use the case as it stands on disk")
    ap.add_argument("--selector", help="capture one CSS element instead of the whole page; "
                                      "keeps its labels readable at slide width")
    ap.add_argument("--max-css-width", type=float, default=1000.0,
                    help="refuse an element wider than this; its text would be too small")
    args = ap.parse_args(argv)

    if not args.skip_build:
        print(f"building case {args.case} ...", flush=True)
        _build_case(args.case, args.seed)

    case_db = ROOT / "casework" / args.case / "graph.db"
    if not case_db.exists():
        raise SystemExit(f"error: no case at {case_db}; run without --skip-build")

    # Refuse to photograph a server someone else started: a stale console on the same
    # port answers the health check and serves a different case root, so the screenshot
    # would show the wrong data and nothing would look wrong.
    if _port_busy(PORT):
        raise SystemExit(
            f"error: something is already listening on {HOST}:{PORT}. Stop it first, or the "
            "capture will photograph that server instead of the one this script builds."
        )

    env = dict(os.environ, ONIONTRACE_CASES=str(ROOT / "casework"))
    server = subprocess.Popen(
        [sys.executable, "-m", "dashboard"], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not _wait_for_port(PORT):
            raise SystemExit(f"error: console did not start on {HOST}:{PORT}")
        url = f"http://{HOST}:{PORT}/case/{args.case}"
        try:
            urlopen(url, timeout=10).read()
        except URLError as exc:
            raise SystemExit(f"error: the console would not serve {url}: {exc}") from None

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if args.selector:
            _capture_element(url, args.selector, out, args.width, args.height, args.scale,
                             args.max_css_width)
        else:
            subprocess.run(
                [
                    _browser(), "--headless", "--disable-gpu", "--no-sandbox",
                    "--hide-scrollbars", f"--window-size={args.width},{args.height}",
                    f"--force-device-scale-factor={args.scale}",
                    "--virtual-time-budget=8000", f"--screenshot={out}", url,
                ],
                cwd=ROOT, check=True, capture_output=True,
            )
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()

    return _finish(out, args)


def _finish(out: Path, args) -> int:
    if not out.exists() or out.stat().st_size < 20_000:
        raise SystemExit(f"error: screenshot at {out} is missing or suspiciously small")

    from PIL import Image

    w, h = Image.open(out).size
    print(f"wrote {out}  ({out.stat().st_size:,} bytes, {w}x{h})")
    if not args.selector and w < 2400:
        raise SystemExit(
            f"error: capture is only {w}px wide. At full slide width that is under 200 dpi, "
            "which prints soft on a projector. Raise --scale."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
