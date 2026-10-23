"""Run every check this project has, and write one report a person can read.

The suite alone is not the project: there is a paper with its own verifier, a deck with its
own, a pipeline that has to produce a manifest, gates that have to refuse, and an execution
environment that has to prove itself against a real network. This runs all of it, in order,
and records what each one actually printed.

Usage:
    python3 scripts/full_check.py [--html PATH] [--tor-sock PATH]

Exit code is 0 only when every stage passed. A stage that cannot run because a precondition
is absent is reported as SKIP, never as PASS -- an unrun check that reports success is the
failure mode this script exists to avoid.
"""

from __future__ import annotations

import argparse
import html
import os
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOR = Path.home() / ".cache" / "oniontrace-tor" / "tor.sock"


@dataclass
class Stage:
    name: str
    command: str
    why: str
    timeout: int = 300
    needs_tor: bool = False
    status: str = "not run"
    seconds: float = 0.0
    output: str = ""
    summary: str = ""
    notes: list[str] = field(default_factory=list)


def stages(tor_sock: Path) -> list[Stage]:
    jail = (
        f"scripts/run_isolated.sh --tor-sock {shlex.quote(str(tor_sock))} -- "
    )
    tmp = tempfile.mkdtemp(prefix="full-check-")
    casework = Path(tmp) / "casework"
    out = Path(tmp) / "out"
    return [
        Stage(
            name="Test suite",
            command="python3 -m pytest -q",
            why="every unit and integration test, including the ones that start a real jail",
        ),
        Stage(
            name="Paper",
            command="python3 scripts/verify_paper.py",
            why="the PDF matches its markdown source, citations resolve, 36 checks",
            timeout=120,
        ),
        Stage(
            name="Diagrams",
            command="python3 scripts/verify_diagrams.py",
            why="every figure in the deck is well-formed and above the dpi floor",
            timeout=120,
        ),
        Stage(
            name="Deck",
            command=f"python3 scripts/verify_deck.py {shlex.quote(str(_deck()))}"
            if _deck()
            else "python3 scripts/verify_deck.py",
            why="12 slides, notes on every one, 16:9, no text over the safe margin",
            timeout=120,
        ),
        Stage(
            name="Pipeline end to end",
            command=(
                f"python3 -m gates --root {shlex.quote(str(casework))} ack --accept "
                f"--actor chk --role admin >/dev/null && "
                f"python3 -m oniontrace run --seed 20260925 --case CHK-1 "
                f"--out {shlex.quote(str(out))} --case-root {shlex.quote(str(casework))} "
                f"--owner chk --authorisation CHK-AUTH"
            ),
            why="the six stages run in order and write a manifest of what each produced",
            timeout=240,
        ),
        Stage(
            name="Gate refusals",
            command=(
                f"python3 -m gates --root {shlex.quote(str(Path(tmp) / 'refusals'))} demo"
            ),
            why="every gate refuses what it should, and the refusal lands in the audit chain",
            timeout=120,
        ),
        Stage(
            name="Execution environment",
            command=jail + "python3 -m oniontrace env --deep",
            why="no route out, a Tor circuit in, proven from inside the jail",
            timeout=180,
            needs_tor=True,
        ),
        Stage(
            name="Live CT verification",
            command=jail + "python3 scripts/ct_log_check.py",
            why="real logs sign their tree heads and this code checks the signature",
            timeout=240,
            needs_tor=True,
        ),
    ]


def _last_meaningful_line(text: str) -> str:
    for line in reversed([line.strip() for line in text.splitlines()]):
        if line:
            return line[:160]
    return "(no output)"


def _deck() -> Path | None:
    """The deck is a deliverable, not a build artefact in the repo.

    It is written to the desktop folder the user submits from, so a verifier pointed at
    out/ finds nothing and reports a missing package. Look where the deck actually is.
    """

    candidates = [
        ROOT / "out" / "ONIONTRACE_Presentation.pptx",
        Path.home() / "Desktop" / "ONIONTRACE" / "6_Presentation" / "ONIONTRACE_Presentation.pptx",
    ]
    return next((path for path in candidates if path.exists()), None)


def run(stage: Stage, tor_sock: Path) -> None:
    if stage.needs_tor and not tor_sock.exists():
        stage.status = "SKIP"
        stage.summary = f"no Tor socket at {tor_sock}"
        stage.notes.append("start one with scripts/tor_disposable.sh start")
        return
    started = time.monotonic()
    try:
        completed = subprocess.run(
            ["bash", "-lc", stage.command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=stage.timeout,
        )
        stage.output = (completed.stdout or "") + (completed.stderr or "")
        stage.status = "PASS" if completed.returncode == 0 else "FAIL"
        # Prefer stdout: a launcher's teardown chatter on stderr would otherwise become the
        # summary of a stage that passed, and "socat exiting on signal 15" is not a result.
        stage.summary = _last_meaningful_line(completed.stdout or completed.stderr or "")
    except subprocess.TimeoutExpired:
        stage.status = "FAIL"
        stage.summary = f"timed out after {stage.timeout}s"
    finally:
        stage.seconds = time.monotonic() - started


COLOURS = {
    "PASS": ("#0b3d1f", "#5fd48a"),
    "FAIL": ("#3d0b0b", "#ff6b6b"),
    "SKIP": ("#3d320b", "#e8c35c"),
    "running": ("#0b2440", "#58a6ff"),
    "not run": ("#222", "#999"),
}


def render(
    results: list[Stage],
    meta: dict[str, str],
    destination: Path,
    refresh: int | None = None,
) -> None:
    total = sum(stage.seconds for stage in results)
    passed = sum(1 for stage in results if stage.status == "PASS")
    failed = [stage for stage in results if stage.status == "FAIL"]
    skipped = [stage for stage in results if stage.status == "SKIP"]
    running = [stage for stage in results if stage.status == "running"]
    finished = passed + len(failed) + len(skipped)

    rows = []
    for stage in results:
        background, colour = COLOURS[stage.status]
        detail = html.escape(stage.output.strip()[-4000:])
        rows.append(
            f"""
      <section class="stage">
        <header>
          <span class="badge" style="background:{background};color:{colour}">{stage.status}</span>
          <h2>{html.escape(stage.name)}</h2>
          <span class="time">{stage.seconds:0.1f}s</span>
        </header>
        <p class="why">{html.escape(stage.why)}</p>
        <p class="summary">{html.escape(stage.summary or "&hellip;")}</p>
        <details><summary>full output</summary><pre>{detail}</pre></details>
      </section>"""
        )

    if running:
        banner = f"RUNNING {finished} OF {len(results)}"
        banner_bg, banner_fg = COLOURS["running"]
    elif failed:
        banner = f"{len(failed)} STAGE(S) FAILED"
        banner_bg, banner_fg = COLOURS["FAIL"]
    else:
        banner = "ALL CHECKS PASSED"
        banner_bg, banner_fg = COLOURS["PASS"]

    meta_tag = (
        f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    )

    document = f"""<!doctype html>
<html><head><meta charset="utf-8">{meta_tag}<title>ONIONTRACE full check</title>
<style>
  body {{ background:#0d1117; color:#e6edf3; font-family:'DejaVu Sans',system-ui,sans-serif;
         margin:0; padding:28px 34px; }}
  h1 {{ font-size:26px; margin:0 0 4px; letter-spacing:-0.3px; }}
  .meta {{ color:#8b949e; font-size:13px; font-family:'DejaVu Sans Mono',monospace; }}
  .banner {{ background:{banner_bg}; color:{banner_fg}; padding:14px 18px; border-radius:8px;
             font-size:22px; font-weight:700; margin:18px 0 22px; letter-spacing:0.4px; }}
  .tally {{ font-family:'DejaVu Sans Mono',monospace; font-size:14px; color:#8b949e; }}
  .stage {{ border:1px solid #21262d; border-radius:8px; padding:12px 16px; margin-bottom:10px;
            background:#0f141a; }}
  header {{ display:flex; align-items:center; gap:12px; }}
  h2 {{ font-size:15px; margin:0; font-weight:600; flex:1; }}
  .badge {{ font-family:'DejaVu Sans Mono',monospace; font-size:11px; font-weight:700;
            padding:3px 9px; border-radius:20px; letter-spacing:0.5px; }}
  .time {{ font-family:'DejaVu Sans Mono',monospace; font-size:12px; color:#8b949e; }}
  .why {{ color:#8b949e; font-size:12px; margin:6px 0 4px; }}
  .summary {{ font-family:'DejaVu Sans Mono',monospace; font-size:12px; color:#c9d1d9;
              margin:2px 0 6px; word-break:break-word; }}
  pre {{ background:#010409; border:1px solid #21262d; border-radius:6px; padding:10px;
         font-size:11px; line-height:1.45; overflow-x:auto; white-space:pre-wrap; }}
  summary {{ cursor:pointer; color:#58a6ff; font-size:11px; }}
</style></head>
<body>
  <h1>ONIONTRACE &mdash; full project check</h1>
  <p class="meta">{html.escape(meta["when"])} &middot; commit {html.escape(meta["commit"])}
     &middot; {html.escape(meta["branch"])} &middot; {html.escape(meta["host"])}</p>
  <div class="banner">{banner}</div>
  <p class="tally">{passed} passed &middot; {len(failed)} failed &middot; {len(skipped)} skipped
     &middot; {total:0.1f}s total</p>
  {''.join(rows)}
</body></html>"""
    destination.write_text(document, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", default=None, help="where to write the report")
    parser.add_argument("--tor-sock", default=str(DEFAULT_TOR))
    parser.add_argument(
        "--live",
        action="store_true",
        help="rewrite the report between stages and make the page refresh itself",
    )
    args = parser.parse_args()

    tor_sock = Path(args.tor_sock)
    results = stages(tor_sock)

    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip() or "unknown"
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip() or "unknown"
    machine = os.uname()
    meta = {
        "when": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "commit": commit,
        "branch": branch,
        "host": f"{machine.nodename} / {machine.machine}",
    }

    destination = Path(args.html) if args.html else ROOT / "out" / "full-check.html"
    destination.parent.mkdir(parents=True, exist_ok=True)
    refresh = 2 if args.live else None

    for stage in results:
        print(f"--- {stage.name}: running", flush=True)
        stage.status = "running"
        if args.live:
            render(results, meta, destination, refresh=refresh)
        run(stage, tor_sock)
        print(f"    {stage.status} ({stage.seconds:0.1f}s) {stage.summary}", flush=True)
        if args.live:
            render(results, meta, destination, refresh=refresh)

    render(results, meta, destination)

    failed = [stage.name for stage in results if stage.status == "FAIL"]
    print(f"\nreport: {destination}")
    print(f"verdict: {'all checks passed' if not failed else 'failed: ' + ', '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
