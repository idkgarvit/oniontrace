"""Run every check this project has, and write one report a person can read.

The suite alone is not the project: there is a paper with its own verifier, a deck with its
own, a pipeline that has to produce a manifest, gates that have to refuse, and an execution
environment that has to prove itself against a real network. This runs all of it, in order,
and records what each one actually proved.

The contract
------------
Every stage states its own verdict, as the last line of its standard output:

    VERDICT: 36 checks passed

A stage that exits 0 without one is UNPROVEN, never PASS. An exit code says a program
finished; it does not say the program measured anything. Two stages of this project have
already exited 0 while reporting a refusal as an empty result, and that is the failure this
contract exists to make impossible: success is a claim the stage has to make, not one the
harness infers from a return code.

Two stages keep their own format instead of a VERDICT line. The test suite prints pytest's
summary ("460 passed in 46s"), read strictly: a failure count anywhere in the output, or fewer
tests than the floor, means no verdict. The pipeline prints a manifest, and the harness reads
the file rather than anything the runner says about it.

Usage:
    python3 scripts/full_check.py [--html PATH] [--json PATH] [--live] [--tor-sock PATH]

Exit code is 0 only when every stage passed. A stage whose precondition is absent is SKIP,
never PASS.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOR = Path.home() / ".cache" / "oniontrace-tor" / "tor.sock"

# A suite that suddenly runs half as many tests as it did yesterday still exits 0. A floor
# turns "the tests that ran passed" into "the tests that should exist, ran, and passed".
MIN_TESTS = 440

VERDICT_LINE = re.compile(r"^VERDICT:\s*(\S.*)$", re.M)
PYTEST_SUMMARY = re.compile(
    r"^(?:=+\s*)?(\d+) passed(?:, (\d+) skipped)?(?:, \d+ warnings?)? in ([\d.]+)s", re.M
)
PYTEST_FAILURE = re.compile(r"\b(\d+) (?:failed|error)")

Claim = tuple[bool, str]


@dataclass
class Stage:
    name: str
    command: str
    why: str
    claim: Callable[[str, str], Claim]
    timeout: int = 300
    needs_tor: bool = False
    status: str = "not run"
    seconds: float = 0.0
    output: str = ""
    summary: str = ""
    notes: list[str] = field(default_factory=list)


def verdict_line(stdout: str, _stderr: str) -> Claim:
    """The stage's own words, or a refusal to invent some."""

    found = VERDICT_LINE.findall(stdout)
    if not found:
        return False, "exited 0 but printed no VERDICT line, so it proved nothing"
    sentence = found[-1].strip()
    if len(sentence) < 8:
        return False, f"VERDICT line too short to mean anything: {sentence!r}"
    return True, sentence


def pytest_claim(stdout: str, _stderr: str) -> Claim:
    """pytest keeps its own summary line; read it strictly."""

    failure = PYTEST_FAILURE.search(stdout)
    if failure:
        return False, f"{failure.group(0)} in the suite"
    match = PYTEST_SUMMARY.search(stdout)
    if not match:
        return False, "no pytest summary line, so the suite may not have run"
    passed = int(match.group(1))
    skipped = f", {match.group(2)} skipped" if match.group(2) else ""
    if passed < MIN_TESTS:
        return (
            False,
            f"only {passed} tests collected, floor is {MIN_TESTS}: a suite that shrank "
            "still exits 0",
        )
    return True, f"{passed} tests passed{skipped} in {match.group(3)}s"


def pipeline_claim(manifest: Path) -> Callable[[str, str], Claim]:
    """Read the artefact, not the console output.

    The runner prints its manifest as JSON and the last line of that is a closing brace, which
    is how a summary becomes punctuation. The file is the evidence, so this reads the file.
    """

    def claim(_stdout: str, _stderr: str) -> Claim:
        if not manifest.exists():
            return False, f"no manifest at {manifest}"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as broken:
            return False, f"manifest is not valid JSON: {broken}"
        stages = data.get("stages", [])
        if not stages:
            return False, "manifest lists no stages"
        bad = [stage.get("stage", "?") for stage in stages if not stage.get("ok")]
        if bad:
            return False, f"stages that did not pass: {', '.join(bad)}"
        return True, f"{len(stages)} of {len(stages)} stages ok, run {data.get('run_id', '?')}"

    return claim


def _deck() -> Path | None:
    """The deck is a deliverable, not an artefact in the repo.

    It is written to the folder the user submits from, so a verifier pointed at out/ finds
    nothing and reports a missing package.
    """

    candidates = [
        ROOT / "out" / "ONIONTRACE_Presentation.pptx",
        Path.home() / "Desktop" / "ONIONTRACE" / "6_Presentation" / "ONIONTRACE_Presentation.pptx",
    ]
    return next((path for path in candidates if path.exists()), None)


def stages(tor_sock: Path) -> list[Stage]:
    jail = f"scripts/run_isolated.sh --tor-sock {shlex.quote(str(tor_sock))} -- "
    tmp = tempfile.mkdtemp(prefix="full-check-")
    casework = Path(tmp) / "casework"
    out = Path(tmp) / "out"
    deck = _deck()
    return [
        Stage(
            name="Test suite",
            command="python3 -m pytest -q",
            why="every unit and integration test, including the ones that start a real jail",
            claim=pytest_claim,
        ),
        Stage(
            name="Paper",
            command="python3 scripts/verify_paper.py",
            why="the PDF matches its markdown source and the claims match the code",
            claim=verdict_line,
            timeout=120,
        ),
        Stage(
            name="Diagrams",
            command="python3 scripts/verify_diagrams.py",
            why="every figure measured in a browser: nothing off-canvas, nothing colliding",
            claim=verdict_line,
            timeout=180,
        ),
        Stage(
            name="Deck",
            command=f"python3 scripts/verify_deck.py {shlex.quote(str(deck))}"
            if deck
            else "python3 scripts/verify_deck.py",
            why="every slide, its notes, the aspect ratio, and the safe margin",
            claim=verdict_line,
            timeout=120,
        ),
        Stage(
            name="Pipeline end to end",
            command=(
                f"python3 -m gates --root {shlex.quote(str(casework))} ack --accept "
                f"--actor chk --role admin >/dev/null && "
                f"python3 -m oniontrace run --seed 20260925 --case CHK-1 "
                f"--out {shlex.quote(str(out))} --case-root {shlex.quote(str(casework))} "
                f"--owner chk --authorisation CHK-AUTH >/dev/null"
            ),
            why="the stages run in order and write a manifest saying what each produced",
            claim=pipeline_claim(out / "manifest.json"),
            timeout=240,
        ),
        Stage(
            name="Base-rate sensitivity",
            command="python3 scripts/base_rate_sensitivity.py",
            why="the correlation result survives prevalence being dropped to the measured rate",
            claim=verdict_line,
            timeout=180,
        ),
        Stage(
            name="Gate refusals",
            command=f"python3 -m gates --root {shlex.quote(str(Path(tmp) / 'refusals'))} demo",
            why="every gate refuses what it should, and the refusal lands in the audit chain",
            claim=verdict_line,
            timeout=120,
        ),
        Stage(
            name="Execution environment",
            command=jail + "python3 -m oniontrace env --deep",
            why="no route out, a Tor circuit in, proven from inside the jail",
            claim=verdict_line,
            timeout=180,
            needs_tor=True,
        ),
        Stage(
            name="Live CT verification",
            command=jail + "python3 scripts/ct_log_check.py",
            why="real logs sign their tree heads and this code checks the signature",
            claim=verdict_line,
            timeout=240,
            needs_tor=True,
        ),
    ]


def _last_line(text: str) -> str:
    for line in reversed([line.strip() for line in text.splitlines()]):
        if line:
            return line[:200]
    return ""


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
        if completed.returncode != 0:
            stage.status = "FAIL"
            stage.summary = _last_line(completed.stderr or "") or f"exit {completed.returncode}"
            return
        proved, sentence = stage.claim(completed.stdout or "", completed.stderr or "")
        stage.status = "PASS" if proved else "UNPROVEN"
        stage.summary = sentence
    except subprocess.TimeoutExpired:
        stage.status = "FAIL"
        stage.summary = f"timed out after {stage.timeout}s"
    finally:
        stage.seconds = time.monotonic() - started


COLOURS = {
    "PASS": ("#0b3d1f", "#5fd48a"),
    "FAIL": ("#3d0b0b", "#ff6b6b"),
    "UNPROVEN": ("#3d2200", "#ffa657"),
    "SKIP": ("#3d320b", "#e8c35c"),
    "running": ("#0b2440", "#58a6ff"),
    "not run": ("#222", "#999"),
}


def _counts(results: list[Stage]) -> dict[str, int]:
    counts = {status: 0 for status in ("PASS", "FAIL", "UNPROVEN", "SKIP")}
    for stage in results:
        counts[stage.status] = counts.get(stage.status, 0) + 1
    return counts


def render(
    results: list[Stage],
    meta: dict[str, str],
    destination: Path,
    refresh: int | None = None,
) -> None:
    total = sum(stage.seconds for stage in results)
    counts = _counts(results)
    running = [stage for stage in results if stage.status == "running"]
    finished = len(results) - len(running)

    rows = []
    for stage in results:
        background, colour = COLOURS.get(stage.status, COLOURS["not run"])
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
        <p class="summary">{html.escape(stage.summary) if stage.summary else "&hellip;"}</p>
        <details><summary>full output</summary><pre>{detail}</pre></details>
      </section>"""
        )

    if running:
        banner = f"RUNNING {finished} OF {len(results)}"
        banner_bg, banner_fg = COLOURS["running"]
    elif counts["FAIL"]:
        banner = f"{counts['FAIL']} FAILED, {counts['UNPROVEN']} UNPROVEN"
        banner_bg, banner_fg = COLOURS["FAIL"]
    elif counts["UNPROVEN"]:
        banner = f"{counts['UNPROVEN']} STAGE(S) UNPROVEN"
        banner_bg, banner_fg = COLOURS["UNPROVEN"]
    else:
        banner = "ALL CHECKS PASSED"
        banner_bg, banner_fg = COLOURS["PASS"]

    meta_tag = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    tally = " &middot; ".join(
        f"{counts[key]} {key.lower()}" for key in ("PASS", "FAIL", "UNPROVEN", "SKIP")
    )

    document = f"""<!doctype html>
<html><head><meta charset="utf-8">{meta_tag}<title>ONIONTRACE full check</title>
<style>
  body {{ background:#0d1117; color:#e6edf3; font-family:'DejaVu Sans',system-ui,sans-serif;
         margin:0; padding:28px 34px; }}
  h1 {{ font-size:26px; margin:0 0 4px; letter-spacing:-0.3px; }}
  .meta {{ color:#8b949e; font-size:13px; font-family:'DejaVu Sans Mono',monospace; }}
  .banner {{ background:{banner_bg}; color:{banner_fg}; padding:14px 18px; border-radius:8px;
             font-size:22px; font-weight:700; margin:18px 0 14px; letter-spacing:0.4px; }}
  .tally {{ font-family:'DejaVu Sans Mono',monospace; font-size:14px; color:#8b949e;
            margin:0 0 18px; }}
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
     &middot; {html.escape(meta["branch"])} &middot; tree {html.escape(meta["tree"])}
     &middot; {html.escape(meta["host"])}</p>
  <div class="banner">{banner}</div>
  <p class="tally">{tally} &middot; {total:0.1f}s total</p>
  {''.join(rows)}
</body></html>"""
    destination.write_text(document, encoding="utf-8")


def write_json(results: list[Stage], meta: dict[str, str], destination: Path) -> None:
    payload = {
        "meta": meta,
        "stages": [
            {
                "name": stage.name,
                "status": stage.status,
                "seconds": round(stage.seconds, 2),
                "summary": stage.summary,
                "notes": stage.notes,
            }
            for stage in results
        ],
    }
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _meta() -> dict[str, str]:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip()

    dirty = git("status", "--porcelain")
    machine = os.uname()
    return {
        "when": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "commit": git("rev-parse", "--short", "HEAD") or "unknown",
        "branch": git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "tree": "clean" if not dirty else f"dirty, {len(dirty.splitlines())} file(s)",
        "host": f"{machine.nodename} / {machine.machine}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", default=None, help="where to write the report page")
    parser.add_argument("--json", default=None, help="where to write the machine-readable result")
    parser.add_argument("--tor-sock", default=str(DEFAULT_TOR))
    parser.add_argument(
        "--live",
        action="store_true",
        help="rewrite the report between stages and make the page refresh itself",
    )
    args = parser.parse_args()

    tor_sock = Path(args.tor_sock)
    results = stages(tor_sock)
    meta = _meta()

    destination = Path(args.html) if args.html else ROOT / "out" / "full-check.html"
    destination.parent.mkdir(parents=True, exist_ok=True)
    json_path = Path(args.json) if args.json else destination.with_suffix(".json")
    refresh = 2 if args.live else None

    for stage in results:
        print(f"--- {stage.name}: running", flush=True)
        stage.status = "running"
        if args.live:
            render(results, meta, destination, refresh=refresh)
        run(stage, tor_sock)
        print(f"    {stage.status} ({stage.seconds:0.1f}s) {stage.summary}", flush=True)
        for note in stage.notes:
            print(f"      note: {note}", flush=True)
        if args.live:
            render(results, meta, destination, refresh=refresh)

    render(results, meta, destination)
    write_json(results, meta, json_path)

    failed = [stage for stage in results if stage.status in ("FAIL", "UNPROVEN")]
    print(f"\nreport: {destination}")
    print(f"result: {json_path}")
    print(
        "verdict: "
        + (
            "all checks passed"
            if not failed
            else " | ".join(
                f"{stage.name}: {stage.status} ({stage.summary})" for stage in failed
            )
        )
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
