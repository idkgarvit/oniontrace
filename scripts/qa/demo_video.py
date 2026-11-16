"""Record the analyst console walkthrough to a captioned demo video.

The walkthrough in this directory drives the console over CDP and finishes in about fifteen
seconds, which is right for a QA run and unreadable as a demo: a viewer cannot follow a graph
redraw that happened between two frames. So this records the same run off the X display and slows
it down, with one caption per step, so the video shows the same work the QA harness proves.

It records what is actually on screen. There is no scripted text overlay pretending to be an
interface: if the console is broken, the video is of a broken console.

Prerequisites, all of which this refuses to guess at:

* an X display with Chromium already showing the console (the walkthrough drives CDP on 9222);
* the console answering on 127.0.0.1:8420.

    python3 scripts/qa/demo_video.py                 # record, slow 10x, write out/demo/
    python3 scripts/qa/demo_video.py --slow 14       # a slower read, for a longer cut
    python3 scripts/qa/demo_video.py --out /tmp/x.mp4

The result: a title card, the captioned walkthrough, and an end card carrying the numbers this
project actually verified. Nothing in the end card is typed by hand -- it is read from
out/full-check.json, so the video cannot claim a pass the project did not record.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WALKTHROUGH = ROOT / "scripts" / "qa" / "walkthrough.py"
FULL_CHECK = ROOT / "out" / "full-check.json"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
BG = "0x0e1116"          # the console's own background, so the cards belong to it
FG = "0xdbe3ec"
ACCENT = "0x4ea1ff"
MUTED = "0x8595a8"

TITLE_SECONDS = 7
END_SECONDS = 9


def die(message: str) -> None:
    print(f"demo_video: {message}", file=sys.stderr)
    raise SystemExit(2)


def reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as fh:
            return fh.status == 200
    except (urllib.error.URLError, OSError):
        return False


def assert_recording_the_right_display(display: str, size: str, allow_real: bool) -> None:
    """Refuse to record a desk.

    The first cut of this script defaulted to $DISPLAY and recorded the operator's own desktop,
    complete with an unrelated project open in a browser. It looked exactly like a working
    recording until a frame was read back. Two checks, both cheap:
    """
    if not allow_real:
        probe = subprocess.run(["pgrep", "-f", f"Xvfb {display}"], capture_output=True, text=True)
        if probe.returncode != 0:
            die(
                f"{display} is not an Xvfb. If you really mean to record a live desktop, pass "
                "--allow-real-display; otherwise start one: "
                f"Xvfb {display} -screen 0 {size}x24"
            )
    info = subprocess.run(["xdpyinfo", "-display", display], capture_output=True, text=True)
    match = re.search(r"dimensions:\s+(\d+)x(\d+)", info.stdout)
    if not match:
        die(f"cannot read the geometry of {display}")
    found = f"{match.group(1)}x{match.group(2)}"
    if found != size:
        die(f"{display} is {found} but --size says {size}; the capture would be cropped")


def preflight(display: str) -> None:
    if not shutil.which("ffmpeg"):
        die("ffmpeg is not on PATH; it records the screen")
    if not Path(FONT).exists():
        die(f"no font at {FONT}; captions would be invisible")
    if not reachable("http://127.0.0.1:8420/"):
        die("the console is not answering on 127.0.0.1:8420 — start it with `python3 -m dashboard`")
    if not reachable("http://127.0.0.1:9222/json/list"):
        die(
            f"no CDP on 127.0.0.1:9222 — start Chromium on {display} with "
            "--remote-debugging-port=9222 and the console open"
        )
    probe = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "x11grab",
         "-video_size", "32x32", "-i", display, "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        die(f"cannot capture {display}: {probe.stderr.strip()[:200]}")


def record(display: str, size: str, raw: Path) -> subprocess.Popen:
    """Record until 'q' is written to stdin. x11grab has no fixed length here because the
    walkthrough's duration is not known in advance."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        # 30 fps, not 20: this footage is slowed by an order of magnitude, so each captured frame
        # is held for a third of a second. At 20 fps the holds are long enough to read as stutter.
        "-f", "x11grab", "-video_size", size, "-framerate", "30", "-i", display,
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-y", str(raw),
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)


STEP = re.compile(r"^(\d+)\.\s+(.+)$")


def run_walkthrough() -> list[tuple[int, str, float]]:
    """Run the walkthrough, timing each step against the recording clock.

    The step lines are the walkthrough's own output, so the captions cannot drift from what the
    video shows: the same process that drives the console labels each moment.
    """
    steps: list[tuple[int, str, float]] = []
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, str(WALKTHROUGH)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print("   " + line.rstrip())
        match = STEP.match(line.strip())
        if match:
            steps.append((int(match.group(1)), match.group(2).strip(), time.monotonic() - started))
    proc.wait()
    if proc.returncode != 0:
        die(f"the walkthrough failed (exit {proc.returncode}); refusing to publish a video of it")
    if not steps:
        die("no steps were found in the walkthrough's output, so there is nothing to caption")
    return steps


def drawtext(text: str, *, size: int, colour: str, x: str, y: str, font: str = FONT) -> str:
    escaped = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019")
    return (
        f"drawtext=fontfile={font}:text='{escaped}':fontcolor={colour}:fontsize={size}"
        f":x={x}:y={y}:line_spacing=10"
    )


def card(path: Path, size: str, seconds: int,
         lines: list[tuple[str, int, str, str, str]]) -> None:
    """A still card: colour source plus the given drawtext lines.

    Each line is (text, font size, colour, x, y). Headings get the bold face and body text the
    book face, because a card where everything has the same weight reads as a paragraph.
    """
    filters = ",".join(
        drawtext(text, size=px, colour=colour, x=x, y=y,
                 font=FONT_BOLD if px >= 30 else FONT)
        for text, px, colour, x, y in lines
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={BG}:s={size}:d={seconds}:r=20",
        "-vf", filters, "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-y", str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        die(f"could not build {path.name}: {result.stderr.strip()[:300]}")


def verified_numbers() -> list[str]:
    """Read the results the project recorded rather than restating them from memory."""
    if not FULL_CHECK.exists():
        return ["Run `python3 scripts/full_check.py` to record the verification results."]
    try:
        data = json.loads(FULL_CHECK.read_text())
    except json.JSONDecodeError:
        return ["out/full-check.json could not be parsed, so no results are claimed here."]
    verdict = data.get("verdict") or "verdict not recorded"
    stages = data.get("stages") or []
    # The recorded status is "PASS". Comparing it to "passed" counted zero of ten while the
    # verdict two lines below said all ten had passed -- a card that contradicted itself.
    # `passed` in that file is a boolean, not a count, and bool is a subclass of int -- so an
    # isinstance check would happily print "True of 10". The counts dict is the number.
    counts = data.get("counts") or {}
    passed = counts.get("PASS")
    if not isinstance(passed, int):
        passed = sum(1 for s in stages if str(s.get("status", "")).upper() == "PASS")
    lines = [f"Pipeline:  {verdict}"]
    if stages:
        lines.insert(0, f"{passed} of {len(stages)} full-check stages passed")
    return lines


def build(raw: Path, steps: list[tuple[int, str, float]], out: Path, size: str, slow: float) -> None:
    width, height = size.split("x")
    title = raw.parent / "title.mp4"
    end = raw.parent / "end.mp4"

    card(title, size, TITLE_SECONDS, [
        ("ONIONTRACE", 64, ACCENT, "80", "300"),
        ("analyst console, end to end", 30, FG, "84", "390"),
        ("A walkthrough of the M6-lite console on a synthetic case.", 20, MUTED, "84", "450"),
        ("Every action below is on the case store, not a mock.", 20, MUTED, "84", "482"),
    ])
    card(end, size, END_SECONDS, [
        ("What this run proves", 40, ACCENT, "80", "290"),
    ] + [
        (line, 24, FG, "84", str(380 + i * 46)) for i, line in enumerate(verified_numbers())
    ] + [
        ("Scope: rules, correlation and JSON identity resolution are validated on a synthetic", 18, MUTED, "84", "700"),
        ("farm. Authorship is reported as synthetic persona retrieval, never as human attribution.", 18, MUTED, "84", "728"),
    ])

    caption_filters = []
    for i, (number, label, when) in enumerate(steps):
        start = max(0.0, (when * slow) - 1.2)
        end_at = (steps[i + 1][2] * slow) if i + 1 < len(steps) else 1e9
        caption_filters.append(
            drawtext(f"{number}. {label}", size=30, colour=FG, x="60", y=f"h-110")
            + f":box=1:boxcolor=0x0e1116@0.88:boxborderw=16:enable='between(t,{start:.2f},{end_at:.2f})'"
        )

    # Multiply, not divide: setpts=PTS/10 makes a 14-second recording play in 1.4 seconds, which
    # is the opposite of what a demo needs. The caption windows are already in slowed seconds
    # (`when * slow`), so they line up once the footage itself is stretched.
    chain = f"[1:v]setpts=PTS*{slow},fps=25," + ",".join(caption_filters) + "[walk]"
    filter_complex = (
        f"{chain};[0:v]format=yuv420p[title];[walk]format=yuv420p[walkf];"
        f"[2:v]format=yuv420p[end];[title][walkf][end]concat=n=3:v=1:a=0[out]"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(title), "-i", str(raw), "-i", str(end),
        "-filter_complex", filter_complex, "-map", "[out]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "21",
        "-movflags", "+faststart", "-y", str(out),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        die(f"could not assemble the video: {result.stderr.strip()[:400]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    # Deliberately NOT os.environ["DISPLAY"]. On a workstation that variable points at the
    # operator's real desktop, so a plain `python3 scripts/qa/demo_video.py` would record whatever
    # they have open -- which is both the wrong footage and a privacy problem. Getting a real
    # desktop recorded takes --allow-real-display, said out loud.
    parser.add_argument("--display", default=":31", help="an Xvfb display (default :31)")
    parser.add_argument("--allow-real-display", action="store_true",
                        help="permit recording a display that is not an Xvfb")
    parser.add_argument("--size", default="1440x900")
    parser.add_argument("--slow", type=float, default=10.0,
                        help="how much to slow the recording (default 10)")
    parser.add_argument("--out", default=str(ROOT / "out" / "demo" / "oniontrace-demo.mp4"))
    args = parser.parse_args()

    assert_recording_the_right_display(args.display, args.size, args.allow_real_display)
    preflight(args.display)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    work = out.parent / ".work"
    work.mkdir(exist_ok=True)
    raw = work / "raw.mp4"

    print(f"recording {args.display} while the walkthrough drives the console")
    recorder = record(args.display, args.size, raw)
    try:
        steps = run_walkthrough()
    finally:
        # 'q' is ffmpeg's own graceful stop; SIGTERM can leave the container without its index.
        try:
            recorder.stdin.write("q")          # type: ignore[union-attr]
            recorder.stdin.flush()             # type: ignore[union-attr]
        except (BrokenPipeError, AttributeError):
            pass
        recorder.wait(timeout=60)

    if not raw.exists() or raw.stat().st_size == 0:
        die("the recording is empty; nothing to assemble")

    print(f"assembling: title {TITLE_SECONDS}s + {len(steps)} captioned steps at {args.slow}x "
          f"+ end card {END_SECONDS}s")
    build(raw, steps, out, args.size, args.slow)
    digits = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,size",
         "-of", "default=nw=1", str(out)], capture_output=True, text=True,
    ).stdout.strip().replace("\n", "  ")
    print(f"wrote {out}  ({digits})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())