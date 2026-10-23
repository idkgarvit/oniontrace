"""Command-line interface: ``python -m oniontrace``.

    python -m oniontrace run --seed 20260925 --case CASE-1
    python -m oniontrace run --seed 20260925 --case CASE-1 --out ./out/runs/CASE-1
    python -m oniontrace show --run-dir ./out/CASE-1

Defaults put each case in its own directory (`out/<case>-<seed>/`) because a shared run
directory means the second case silently overwrites the first case's manifest.

The exit code is 1 when any stage failed, so this is usable in a script or a CI job
without parsing the manifest.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from gates.environment import DEFAULT_TOR_ENDPOINT
from oniontrace.pipeline import run_pipeline


def cmd_run(args: argparse.Namespace) -> int:
    out = Path(args.out) if args.out else Path("out") / f"{args.case}-{args.seed}"
    case_root = Path(args.case_root) if args.case_root else Path("casework")
    result = run_pipeline(
        seed=args.seed,
        case_id=args.case,
        output_dir=out,
        case_root=case_root,
        owner=args.owner,
        authorisation=args.authorisation,
        retention_days=args.retention,
    )

    print(json.dumps(result.to_dict(), indent=2))
    print(f"\nreport: {result.output_dir / 'REPORT.md'}", file=sys.stderr)
    return 0 if result.ok else 1


def cmd_show(args: argparse.Namespace) -> int:
    manifest = Path(args.run_dir) / "manifest.json"
    if not manifest.exists():
        print(f"error: no manifest at {manifest}", file=sys.stderr)
        return 2
    data = json.loads(manifest.read_text())
    print(f"run {data['run_id']}  seed {data['seed']}  case {data['case_id']}")
    print(f"outcome: {'ok' if data['ok'] else 'FAILED'}\n")
    for stage in data["stages"]:
        if stage["skipped_because"]:
            state = f"skipped ({stage['skipped_because']})"
        elif stage["ok"]:
            state = "ok"
        else:
            state = f"FAILED: {stage['error']}"
        print(f"  {stage['stage']:12} {stage['duration_ms']:>6} ms  {state}")
    return 0 if data["ok"] else 1


def cmd_env(args: argparse.Namespace) -> int:
    """Preflight for live collection: prove the environment before trusting it."""

    from collect.preflight import check_environment

    report = check_environment(
        tor_endpoint=args.tor_socks,
        case_root=Path(args.case_root) if args.case_root else None,
        deep=args.deep,
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.summary())
        proved = sum(1 for check in report.checks if check.ok)
        unproved = [check.name for check in report.checks if not check.ok]
        print(
            f"VERDICT: {proved} of {len(report.checks)} preflight checks proved"
            + (f", not proved: {', '.join(unproved)}" if unproved else
               ", no route out and a circuit in")
        )
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m oniontrace", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run the full pipeline for one seed and case")
    r.add_argument("--seed", type=int, default=20260925)
    r.add_argument("--case", default="CASE-1")
    r.add_argument("--out", default=None,
                   help="run directory; defaults to out/<case>-<seed>")
    r.add_argument("--case-root", default=None,
                   help="gate case root and console case store; defaults to ./casework")
    r.add_argument("--owner", default="g.kanojia")
    r.add_argument("--authorisation", default="AUTH-PIPELINE")
    r.add_argument("--retention", type=int, default=30, dest="retention")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("show", help="print a previous run's stage table")
    s.add_argument("--run-dir", required=True)
    s.set_defaults(func=cmd_show)

    e = sub.add_parser("env", help="preflight the execution environment before live collection")
    # The launcher exports the socket path it actually bound, so honour it: an operator who
    # started Tor through scripts/tor_disposable.sh should not have to retype the path, and
    # a default that disagrees with the launcher reports a missing listener that is running.
    _env_sock = os.environ.get("ONIONTRACE_TOR_SOCK", "").strip()
    if not _env_sock:
        _default_sock = DEFAULT_TOR_ENDPOINT
    elif _env_sock.startswith("unix:"):
        _default_sock = _env_sock
    else:
        _default_sock = f"unix:{_env_sock}"
    e.add_argument("--tor-socks", default=_default_sock,
                   help=f"Tor SOCKS endpoint; a unix socket by default ({_default_sock})")
    e.add_argument("--case-root", default=None,
                   help="directory the run must be able to write to")
    e.add_argument("--deep", action="store_true",
                   help="also ask Tor to build a circuit (one CONNECT to the Tor Project); "
                        "off by default because it needs the network and reaches a third party")
    e.add_argument("--json", action="store_true", help="machine-readable report")
    e.set_defaults(func=cmd_env)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
