"""Run the controlled live onion lab end to end.

    python3 scripts/run_live_lab.py --publish

What this does, in order:

1. builds the lab's eight services, with their planted flaws and clean controls;
2. publishes all eight as onion services from a disposable publisher Tor;
3. starts a second, keyless Tor for the collector, so the party that publishes and the
   party that fetches are not the same process;
4. waits until every descriptor is actually findable through the collector's Tor;
5. opens a case whose only seeds are those eight addresses;
6. runs the real collector inside the no-network jail, with the fixed probe list, retrying
   a pass when a cold rendezvous failed rather than recording a live service as down;
7. reads the WARC the collector wrote, scores it with the project's own detectors and
   correlator, and writes ``out/live-lab/live-lab.{json,md}``;
8. stops both Tors and deletes the publisher's keys, unless ``--keep`` says otherwise.

Why the truth is still constructed. The flaws are planted, because there is no labelled
corpus of real onion misconfigurations and inventing one would mean measuring strangers.
What is real here is the path: Tor rendezvous, HTTP responses and headers, robots checks,
budgets, the gate, WARC records, parsing and scoring. The report says that in the same
words; a live path is not the same claim as real-world accuracy.

Publishing is a visible act on a public network, so it is opt-in. Without ``--publish``
this script explains the experiment and exits without doing it.

``--only alpha-1,gamma-2`` publishes a subset. That is for a pilot: it proves the path
with fewer descriptors before spending the full publication window on eight.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.live_lab import (  # noqa: E402
    Lab,
    build_lab,
    observations_from_warc,
    score,
    write_report,
)

TOR_DIR = Path(os.environ.get("ONIONTRACE_TOR_DIR", Path.home() / ".cache/oniontrace-tor"))
TOR_SOCK = TOR_DIR / "tor.sock"
#: The collector's own Tor, separate from the publisher. Two reasons, and the second one
#: is the one that mattered: a service operator and an analyst are different parties, and
#: a client that shares a process with the publisher shares its descriptor cache. When the
#: publisher rebuilt its introduction circuits, the client kept using the descriptors it
#: had already cached and failed every service with SOCKS 0x01 until those entries aged
#: out. A fresh client fetches the descriptors as they are now.
CLIENT_DIR = Path(
    os.environ.get("ONIONTRACE_CLIENT_TOR_DIR", Path.home() / ".cache/oniontrace-tor-client")
)
CLIENT_SOCK = CLIENT_DIR / "tor.sock"
CLIENT_SESSION = "onionclient"
ACTOR = "lab.operator"
ROLE = "case_owner"
AUTHORISATION = "AUTH-OWN-ONION-LAB-2026-11-17"
JAIL_BRIDGE_PORT = 19050
ONION_RE = re.compile(r"^[a-z2-7]{56}\.onion$")


def run(cmd: list[str], *, timeout: int, check: bool = True, env=None):
    """Run one step, and on failure show the part of the output that explains it."""

    result = subprocess.run(
        cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env
    )
    if check and result.returncode != 0:
        sys.stderr.write(result.stdout[-3000:] + result.stderr[-2000:])
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for_service(
    address: str, bridge_port: int, *, timeout: int, successes: int = 1, gap: float = 0.0
) -> tuple[float, int]:
    """Probe the onion through Tor until it answers, and report how long that took.

    A freshly published service is not findable immediately: its descriptor has to reach
    the hash ring. The window is explicit and bounded rather than a silent retry loop,
    because "the collector is broken" and "the descriptor is not published yet" look
    identical from inside a failed fetch.

    ``successes`` above one asks for that many answers in a row. One answer proves the
    descriptor was findable at that moment; it does not prove the introduction points are
    still usable when the collector arrives a minute later, which is the failure the first
    full runs kept hitting.
    """

    import requests

    started = time.time()
    last = "no attempt made"
    streak = 0
    while time.time() - started < timeout:
        try:
            response = requests.get(
                f"http://{address}/",
                proxies={"http": f"socks5h://127.0.0.1:{bridge_port}"},
                timeout=30,
            )
            if response.status_code == 200:
                streak += 1
                if streak >= successes:
                    return time.time() - started, response.status_code
                time.sleep(gap)
                continue
            last = f"status {response.status_code}"
            streak = 0
        except Exception as exc:  # noqa: BLE001 - the reason matters, not the type
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
            streak = 0
        time.sleep(10)
    raise SystemExit(f"{address} never answered within {timeout}s: {last}")


def write_lab(lab: Lab, lab_root: Path) -> None:
    if lab_root.exists():
        shutil.rmtree(lab_root)
    for service in lab.services:
        directory = lab_root / "services" / service.service_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "routes.json").write_text(
            json.dumps({"routes": service.to_dict()["routes"]}, indent=2) + "\n",
            encoding="utf-8",
        )
    (lab_root / "lab.json").write_text(
        json.dumps(lab.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def read_addresses(path: Path, expected: set[str]) -> dict[str, str]:
    addresses: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        service_id, _, address = line.partition("\t")
        if address and ONION_RE.match(address.strip()):
            addresses[service_id.strip()] = address.strip()
    missing = expected - set(addresses)
    if missing:
        raise SystemExit(f"no onion address was published for: {', '.join(sorted(missing))}")
    return addresses


def start_client_tor() -> None:
    """Boot the collector's Tor, with no hidden services and no keys of its own."""

    if CLIENT_DIR.exists():
        # serve-lab has already stopped every tor by this point, so a directory or socket
        # here is a leftover from a run that did not reach its teardown. Removing it is
        # what makes "start a second instance" safe; a stale socket with a live publisher
        # would otherwise be mistaken for a running client.
        shutil.rmtree(CLIENT_DIR, ignore_errors=True)

    run(
        ["bash", str(ROOT / "scripts" / "tor_disposable.sh"), "start"],
        timeout=420,
        env=dict(
            os.environ,
            ONIONTRACE_TOR_DIR=str(CLIENT_DIR),
            ONIONTRACE_TOR_SESSION=CLIENT_SESSION,
        ),
    )


def collect_case(case: str, root: str, sock: Path, attempt: int, out_dir: Path, *, timeout: int):
    """One pass of the real collector, inside the jail, through ``sock``."""

    with_output = out_dir / (f"collector.out" if attempt == 1 else f"collector-{attempt}.out")
    environment = dict(os.environ, ONIONTRACE_BRIDGE_PORT=str(JAIL_BRIDGE_PORT))
    collected = run(
        [
            "bash",
            str(ROOT / "scripts" / "run_isolated.sh"),
            "--tor-sock",
            str(sock),
            "--case-dir",
            str(Path(root).resolve()),
            "--",
            "python3",
            "-m",
            "collect.cli",
            "--root",
            root,
            "run",
            "--case",
            case,
            "--actor",
            ACTOR,
            "--role",
            ROLE,
            "--live",
            "--origin",
            "onion_service",
            "--authorised-mode",
            "--socks-host",
            "127.0.0.1",
            "--socks-port",
            str(JAIL_BRIDGE_PORT),
            "--probe-known-paths",
            # 8 front pages + 8 x 13 fixed probes = 112 requests. The default
            # ceiling is 50 pages per run, which would stop halfway through and
            # leave the probes for the last services unrequested.
            "--max-pages",
            "150",
            # A cold rendezvous through Tor takes longer than the clearnet default, but
            # not minutes: a stuck stream has to fail before the 15-minute permit that
            # gates ingest expires, or the pages already read are refused with it.
            "--timeout",
            str(timeout),
        ],
        timeout=1200,
        env=environment,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    with_output.write_text(collected.stdout, encoding="utf-8")
    return collected


def observed_addresses(warc_dir: Path, addresses: dict[str, str]) -> tuple[set[str], list[Path]]:
    """Which of our addresses the WARC actually holds a response for."""

    warc_paths = sorted(warc_dir.glob("*.warc"))
    if not warc_paths:
        return set(), []
    observed = observations_from_warc(warc_paths)
    return {a for a in addresses.values() if a in observed}, warc_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="actually publish the lab (without this the script only describes it)",
    )
    parser.add_argument("--root", default=str(ROOT / "casework"), help="gate working directory")
    parser.add_argument("--out", default=str(ROOT / "out" / "live-lab"), help="result directory")
    parser.add_argument("--case", default=None, help="case id (default: generated)")
    parser.add_argument("--seed", type=int, default=20261117, help="lab seed")
    parser.add_argument(
        "--only",
        default=None,
        help="comma-separated service ids to publish (pilot mode; default: all)",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=600,
        help="seconds to wait for each descriptor to become findable",
    )
    parser.add_argument(
        "--settle",
        type=int,
        default=10,
        help=(
            "seconds to let introduction points settle after the first successful "
            "rendezvous, before the collector starts (0 disables)"
        ),
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        help=(
            "collection passes, retrying only seeds that recorded nothing (a cold onion "
            "rendezvous fails transiently; the collector's cursor skips finished seeds)"
        ),
    )
    parser.add_argument(
        "--collect-timeout",
        type=int,
        default=30,
        help="per-request timeout inside the collector (seconds)",
    )
    parser.add_argument(
        "--retry-wait",
        type=int,
        default=60,
        help="seconds between collection passes when a seed is still missing",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave Tor running and its keys in place (default: stop and delete)",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out).resolve()
    lab = build_lab(args.seed)

    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        unknown = wanted - set(lab.service_ids)
        if unknown:
            raise SystemExit(f"unknown service id(s): {', '.join(sorted(unknown))}")
        lab = Lab(seed=lab.seed, services=[s for s in lab.services if s.service_id in wanted])

    if not args.publish:
        print("This experiment publishes onion services we own, collects from them through")
        print("real Tor, and scores the result against flaws planted in advance.")
        print(f"  services: {', '.join(lab.service_ids)}")
        print(f"  lab fingerprint: {lab.fingerprint}")
        print(f"  case root: {Path(args.root).resolve()}")
        print(f"  results: {out_dir}")
        print(f"  tor: {TOR_DIR}")
        print()
        print("It is opt-in because publishing is a visible act on a public network.")
        print("Re-run with --publish to perform it. The run stops Tor and deletes its keys")
        print("at the end unless --keep is given.")
        return 0

    if shutil.which("socat") is None:
        raise SystemExit("socat is required for the readiness bridge; install it first")

    case = args.case or f"CASE-LAB-{time.strftime('%Y%m%dT%H%M%S')}"
    lab_root = out_dir / "lab"
    write_lab(lab, lab_root)
    print(f"1. lab built: {len(lab.services)} service(s), fingerprint {lab.fingerprint}")
    print(f"   routes and truth: {lab_root}")

    bridge = None
    try:
        addresses_path = out_dir / "addresses.tsv"
        print(f"2. publishing {len(lab.services)} onion service(s) from one disposable Tor")
        run(
            [
                "bash",
                str(ROOT / "scripts" / "tor_disposable.sh"),
                "serve-lab",
                str(lab_root),
                str(addresses_path),
            ],
            timeout=480,
        )
        addresses = read_addresses(addresses_path, set(lab.service_ids))
        for service_id, address in addresses.items():
            print(f"   {service_id}: {address}")

        print(f"3. starting the collector's own Tor (no hidden services, no keys)")
        start_client_tor()

        bridge_port = free_port()
        bridge = subprocess.Popen(
            [
                "socat",
                f"TCP-LISTEN:{bridge_port},fork,reuseaddr,bind=127.0.0.1",
                f"UNIX-CONNECT:{CLIENT_SOCK}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        print(f"4. warming every service through the collector's Tor (up to {args.wait}s each)")
        started = time.time()
        for service_id, address in sorted(addresses.items()):
            elapsed, status = wait_for_service(
                address, bridge_port, timeout=args.wait, successes=2, gap=5
            )
            print(f"   {service_id}: answered {status} twice after {elapsed:.1f}s")
        print(f"   every service answered twice in {time.time() - started:.1f}s")

        if args.settle:
            # A first successful rendezvous proves the descriptor is findable. It does not
            # prove every introduction point is usable yet: descriptors are re-uploaded as
            # introduction circuits finish, and a service can answer once and then fail
            # the next request while the new descriptor propagates. The first full run
            # lost six of eight services this way. Waiting is the lab equivalent of not
            # calling a service down because its first answer was the lucky one.
            print(f"   settling for {args.settle}s before collection")
            time.sleep(args.settle)

        print(f"5. opening {case} with the eight addresses as its only seeds")
        run(
            [sys.executable, "-m", "gates", "--root", args.root, "ack", "--accept",
             "--actor", ACTOR, "--role", ROLE],
            timeout=60,
        )
        run(
            [sys.executable, "-m", "gates", "--root", args.root, "case", "open",
             "--case", case, "--title", "Controlled live onion lab",
             "--authorisation", AUTHORISATION, "--retention-days", "30",
             "--actor", ACTOR, "--role", ROLE],
            timeout=60,
        )
        for service_id, address in addresses.items():
            run(
                [sys.executable, "-m", "gates", "--root", args.root, "case", "add-seed",
                 "--case", case, "--seed", address, "--actor", ACTOR, "--role", ROLE],
                timeout=60,
            )

        print("6. collecting inside the no-network jail, through the SOCKS bridge")
        warc_dir = Path(args.root) / "cases" / case / "derived" / "warc"
        expected = set(addresses.values())
        attempts = max(1, args.attempts)
        warc_paths: list[Path] = []
        for attempt in range(1, attempts + 1):
            collected = collect_case(
                case, args.root, CLIENT_SOCK, attempt, out_dir, timeout=args.collect_timeout
            )
            summary_lines = [
                line for line in collected.stdout.splitlines()
                if line.startswith("onion_service")
            ]
            for line in summary_lines:
                print(f"   {line.strip()}")
            for line in collected.stdout.splitlines():
                if any(word in line.lower() for word in ("unreachable", "refused", "budget", "error")):
                    print(f"   note: {line.strip()}")

            seen, warc_paths = observed_addresses(warc_dir, addresses)
            missing = expected - seen
            if not missing:
                break
            missing_ids = sorted(sid for sid, a in addresses.items() if a in missing)
            if attempt >= attempts:
                raise SystemExit(
                    f"{len(missing)} service(s) never answered: {', '.join(missing_ids)}; "
                    f"the collector output is in {out_dir}"
                )
            # A cold onion rendezvous fails transiently: Tor may have cached a descriptor
            # whose introduction points have since been replaced. The retry is not a
            # second page fetch for a service that answered, because the collector's
            # cursor skips seeds that are already finished.
            print(
                f"   {len(missing)} service(s) did not answer "
                f"({', '.join(missing_ids)}); retrying in {args.retry_wait}s"
            )
            time.sleep(args.retry_wait)

        print("7. reading the WARC and scoring against the planted truth")
        observed = observations_from_warc(warc_paths)
        result = score(lab, addresses, observed)
        result["case_id"] = case
        result["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        result["warc_files"] = [path.name for path in warc_paths]
        json_path, md_path = write_report(result, out_dir, case_id=case)

        live = result["live"]
        detection = result["detection"]
        careful = detection["careful"]
        naive = detection["naive"]
        links = result["correlation"]["filtered"]["link_scoring"]
        print()
        print(f"   responses observed: {live['routes_observed']}")
        print(
            f"   careful detection:  TP {careful['tp']}  FP {careful['fp']}  "
            f"FN {careful['fn']}"
        )
        print(
            f"   naive detection:    TP {naive['tp']}  FP {naive['fp']}  "
            f"FN {naive['fn']}"
        )
        print(
            f"   filtered correlation: {links['proposed']} link(s), TP {links['tp']}, "
            f"FP {links['fp']}"
        )
        print(f"   report: {md_path}")
        print(f"   record: {json_path}")
        return 0
    finally:
        if bridge is not None:
            bridge.terminate()
            try:
                bridge.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bridge.kill()
        if args.keep:
            print(f"kept the publishing Tor and its keys: {TOR_DIR}", file=sys.stderr)
            print(f"kept the collector's Tor: {CLIENT_DIR}", file=sys.stderr)
        else:
            # Teardown removes the keys, which is the point. The log is not a key, and a
            # failed run whose log disappeared is a run nobody can diagnose; keep a copy
            # beside the results before clean deletes the directory.
            log = TOR_DIR / "tor.log"
            if log.exists():
                out_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(log, out_dir / "tor.log")
            client_log = CLIENT_DIR / "tor.log"
            if client_log.exists():
                out_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(client_log, out_dir / "client-tor.log")
            logs = TOR_DIR / "logs"
            if logs.is_dir():
                shutil.copytree(logs, out_dir / "service-logs", dirs_exist_ok=True)
            run(
                ["bash", str(ROOT / "scripts" / "tor_disposable.sh"), "clean"],
                timeout=120,
                check=False,
            )
            # The client's clean runs last: its cmd_stop uses pkill -x tor, which is the
            # only handle that survives a nohup start, and by now both instances are
            # supposed to be gone.
            run(
                ["bash", str(ROOT / "scripts" / "tor_disposable.sh"), "clean"],
                timeout=120,
                check=False,
                env=dict(
                    os.environ,
                    ONIONTRACE_TOR_DIR=str(CLIENT_DIR),
                    ONIONTRACE_TOR_SESSION=CLIENT_SESSION,
                ),
            )
            for sock in (TOR_SOCK, CLIENT_SOCK):
                if sock.exists():
                    print(f"warning: a Tor socket survived clean: {sock}", file=sys.stderr)
            print(
                "teardown: publishing and collector Tors stopped, keys and sockets removed",
                file=sys.stderr,
            )


if __name__ == "__main__":
    raise SystemExit(main())
