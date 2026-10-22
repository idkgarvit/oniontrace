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
            raise SystemExit(f"error: the console would not serve {url}: {exc}")

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
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

    if not out.exists() or out.stat().st_size < 20_000:
        raise SystemExit(f"error: screenshot at {out} is missing or suspiciously small")

    from PIL import Image

    w, h = Image.open(out).size
    print(f"wrote {out}  ({out.stat().st_size:,} bytes, {w}x{h})")
    if w < 2400:
        raise SystemExit(
            f"error: capture is only {w}px wide. At full slide width that is under 200 dpi, "
            "which prints soft on a projector. Raise --scale."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
