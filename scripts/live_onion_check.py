"""Prove the acquisition path against a live onion service that we publish ourselves.

    python3 scripts/live_onion_check.py --publish

Why it is built this way. Testing acquisition against a stranger's onion service means
measuring a third party who did not ask to be measured, which the project's authorisation does
not cover. Testing it against a local server called "onion.example" tests the parser and
nothing else, because the only genuinely hard part of onion acquisition is Tor. So this check
publishes an onion of its own with the disposable Tor, points it at a static page served from
inside the same jail, and then drives the real collector at that address through the real Tor
network and the real jail bridge.

The page carries a nonce generated for this run. A cache, a stale snapshot, a fixture that
happened to contain the same words, or a collector that stored something it did not fetch all
fail to reproduce it.

Publishing an onion is a visible act on a public network, so it is opt-in: without --publish
this script explains what it would do and exits 0 without claiming anything.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: The port the jail republishes the Tor socket on, inside its own network namespace. The
#: host-side bridge opened for the reachability probe is on a different port and is not
#: visible from inside the jail, which is the whole point of the jail: the first version of
#: this script passed the host port to the collector and the collector correctly refused.
JAIL_BRIDGE_PORT = 19050
TOR_DIR = Path(os.environ.get("ONIONTRACE_TOR_DIR", Path.home() / ".cache" / "oniontrace-tor"))
TOR_SOCK = TOR_DIR / "tor.sock"
ACTOR = "g.kanojia"
ROLE = "case_owner"
ONION_RE = re.compile(r"^[a-z2-7]{56}\.onion$")


def run(command: list[str], *, timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and result.returncode != 0:
        sys.stderr.write(result.stdout[-2000:] + result.stderr[-2000:])
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(command[:4])} ...")
    return result


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def publish(webroot: Path) -> str:
    """Start the publishing Tor and return the onion address it owns."""

    result = run(
        ["bash", str(ROOT / "scripts" / "tor_disposable.sh"), "serve", str(webroot), "8080"],
        timeout=200,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if ONION_RE.match(line.strip())]
    if not lines:
        raise SystemExit(f"no onion address in output:\n{result.stdout[-1500:]}")
    return lines[-1]


#: How long a freshly published hidden service gets to become findable, measured rather than
#: guessed. Three runs of this check bracket it: the first succeeded in 5.8 seconds, but that
#: tor had already been up for three minutes; two later runs failed at 78 and 240 seconds; and a
#: timed probe finally answered at 319.5 seconds. Publishing to the hash ring is not a
#: local operation and it is not quick on a cold instance, so the window has to be generous and
#: the wait has to say what it is waiting for.
PUBLISH_WINDOW_SECONDS = 600


def wait_for_service(address: str, bridge_port: int, *, timeout: int = PUBLISH_WINDOW_SECONDS) -> float:
    """Probe the onion through Tor until it answers, and return how long that took.

    A freshly published hidden service is not findable immediately: its descriptor has to reach
    the hash ring before a rendezvous can complete. That is a fact about hidden services, not a
    fault in the collector, and it cost this check three attempts to learn: a 240 second window
    was not enough and the answer arrived at 319 seconds. The wait is explicit, generous and
    bounded rather than a silent retry loop.
    """

    import requests

    started = time.time()
    last: str = "no attempt made"
    while time.time() - started < timeout:
        try:
            response = requests.get(
                f"http://{address}/",
                proxies={"http": f"socks5h://127.0.0.1:{bridge_port}"},
                timeout=30,
            )
            if response.status_code == 200:
                return time.time() - started
            last = f"status {response.status_code}"
        except Exception as exc:  # noqa: BLE001 - the reason matters, not the type
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
        time.sleep(10)
    raise SystemExit(f"the published onion never answered within {timeout}s: {last}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="actually publish an onion")
    parser.add_argument("--case", default=None, help="case id (default: generated)")
    parser.add_argument("--root", default=str(ROOT / "casework"))
    parser.add_argument("--keep", action="store_true", help="leave the hidden service running")
    args = parser.parse_args()

    if not args.publish:
        print("This check publishes a short-lived onion service and then collects from it.")
        print("It is opt-in because publishing is a visible act on a public network.")
        print(f"Re-run with --publish to perform it. Tor directory: {TOR_DIR}")
        return 0

    nonce = hashlib.sha256(f"{time.time_ns()}{os.getpid()}".encode()).hexdigest()[:16]
    webroot = Path(tempfile.mkdtemp(prefix="onion-publish-"))
    (webroot / "index.html").write_text(
        "<!doctype html><html><head><title>ONIONTRACE live acquisition check</title></head>"
        f"<body><h1>Live acquisition check</h1><p>nonce {nonce}</p></body></html>",
        encoding="utf-8",
    )
    (webroot / "robots.txt").write_text("User-agent: *\nAllow: /\n", encoding="utf-8")

    print(f"1. publishing a page carrying nonce {nonce}")
    address = publish(webroot)
    print(f"   address: {address}")

    bridge_port = free_port()
    bridge = subprocess.Popen(
        ["socat", f"TCP-LISTEN:{bridge_port},fork,reuseaddr,bind=127.0.0.1",
         f"UNIX-CONNECT:{TOR_SOCK}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        print("2. waiting for the descriptor to be findable")
        elapsed = wait_for_service(address, bridge_port)
        print(f"   answered after {elapsed:.1f}s")

        case = args.case or f"CASE-LIVE-PUBLISH-{time.strftime('%H%M%S')}"
        print(f"3. opening {case} with the address as its only seed")
        run(["python3", "-m", "gates", "--root", args.root, "ack", "--accept",
             "--actor", ACTOR, "--role", ROLE])
        run(["python3", "-m", "gates", "--root", args.root, "case", "open",
             "--case", case, "--title", "Live acquisition against our own onion",
             "--authorisation", "AUTH-OWN-ONION-2026-10-23", "--retention-days", "30",
             "--actor", ACTOR, "--role", ROLE])
        run(["python3", "-m", "gates", "--root", args.root, "case", "add-seed",
             "--case", case, "--seed", address, "--actor", ACTOR, "--role", ROLE])

        print("4. collecting, inside the jail, through the SOCKS bridge")
        environment = dict(os.environ, ONIONTRACE_BRIDGE_PORT=str(JAIL_BRIDGE_PORT))
        collected = subprocess.run(
            ["bash", str(ROOT / "scripts" / "run_isolated.sh"),
             "--tor-sock", str(TOR_SOCK), "--case-dir", args.root, "--",
             "python3", "-m", "collect.cli", "--root", args.root, "run",
             "--case", case, "--actor", ACTOR, "--role", ROLE, "--live",
             "--origin", "onion_service", "--authorised-mode",
             "--socks-host", "127.0.0.1", "--socks-port", str(JAIL_BRIDGE_PORT)],
            cwd=ROOT, capture_output=True, text=True, timeout=300, env=environment,
        )
        if collected.returncode != 0:
            sys.stderr.write(collected.stdout[-2000:] + collected.stderr[-1000:])
            raise SystemExit("the collector refused or failed; see the output above")
        summary = [line for line in collected.stdout.splitlines() if line.startswith("onion_service")]
        for line in summary:
            print(f"   {line.strip()}")

        print("5. checking that what was stored is what this run served")
        snapshots = sorted((Path(args.root) / "cases" / case / "snapshots").glob("*.json"))
        if not snapshots:
            print("VERDICT: the collector stored no snapshot, so nothing was acquired")
            return 1
        blob = "\n".join(path.read_text(encoding="utf-8") for path in snapshots)
        found = nonce in blob
        print(f"   nonce {nonce} present in the stored snapshot: {found}")
        if not found:
            print("VERDICT: a snapshot was stored, but it is not the page this run served")
            return 1

        print(
            f"VERDICT: the collector fetched a live onion service over real Tor inside the "
            f"jail and stored the page it served, proven by a nonce generated for this run "
            f"({case}, {address[:16]}...)"
        )
        return 0
    finally:
        bridge.terminate()
        if not args.keep:
            run(["bash", str(ROOT / "scripts" / "tor_disposable.sh"), "stop"], check=False)
        shutil.rmtree(webroot, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
