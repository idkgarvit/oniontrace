#!/usr/bin/env python3
"""Score the real detection rules against a real onion service we operate ourselves.

Why this exists
---------------
The paper's rule-detection claim was previously scored only against the synthetic farm.
This closes that gap without touching anyone else's infrastructure.

The service is **ours**. tor in the jail publishes a v3 onion service fronting the lab
nginx, and this script fetches it over the real Tor network: real SOCKS, real hidden
service protocol, real nginx responses, real Tor circuit. Nothing is simulated, and no
third party is involved at either end.

The responses are then converted into the farm's own ``Service``/``Endpoint`` shapes and
run through the farm's own rule table, unmodified. The rules are not re-implemented here,
because a re-implementation would prove only that the copy works.

The honest limit, stated up front: this validates that the rules behave correctly on real
onion-served content. It is not a prevalence measurement. One service that we built cannot
say how often anything appears in the wild.

Run:
    ./scripts/tor_isolated.sh start                    # client on 127.0.0.1:9150
    python3 scripts/real_onion_validation.py
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "out" / "paper-drop" / "real_world"

#: The paths probed on each service. Two are the intentional flaws on the misconfigured
#: instance, the rest are controls: a real server always answers something for an unknown
#: path, and those answers are what a false-positive rate has to be measured against.
PROBES = ("/", "/.env", "/.git/config", "/nope-does-not-exist")

#: Our hidden services, as published by the jail. Read at run time, never hardcoded, so a
#: rerun with fresh services uses the new addresses rather than stale ones.
SERVICE_DIRS = {
    "misconfigured": Path("/tmp/onsvc/data/hs_misconfig/hostname"),
    "clean_control": Path("/tmp/onsvc/data/hs_clean/hostname"),
}
SOCKS_PORT = 9151


def onion_addresses() -> dict[str, str]:
    out = {}
    for label, path in SERVICE_DIRS.items():
        if path.exists():
            addr = path.read_text().strip()
            if addr:
                out[label] = addr
    return out


def fetch(addr: str, path: str, timeout: int = 90) -> dict:
    """One GET of our own service over the real Tor network, via curl over SOCKS.

    curl rather than the collector's requests path, so the transport under test is Tor's
    SOCKS5h and not a Python shim. ``--socks5-hostname`` keeps the onion name out of
    local DNS, the same property the collector's own transport enforces.
    """
    url = f"http://{addr}{path}"
    proc = subprocess.run(
        ["curl", "-s", "-i", "--max-time", str(timeout),
         "--socks5-hostname", f"127.0.0.1:{SOCKS_PORT}", url],
        capture_output=True,
    )
    raw = proc.stdout.decode("utf-8", "replace")
    head, _, body = raw.partition("\r\n\r\n")
    lines = head.split("\r\n") if head else []
    status = 0
    if lines and lines[0].startswith("HTTP/"):
        try:
            status = int(lines[0].split()[1])
        except (IndexError, ValueError):
            status = 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip()] = v.strip()
    return {
        "url": url,
        "path": path,
        "status": status,
        "headers": headers,
        "body": body,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stderr": proc.stderr.decode("utf-8", "replace").strip()[:200],
    }


def to_farm(service_id: str, addr: str, responses: list[dict]):
    """Convert real responses into the farm's own model, so the real rules can run.

    ``scan_service`` takes a ``FarmTruth`` and resolves the service by id, so a minimal
    manifest holding exactly one service is built here. Imported lazily so this script can
    be read without the package on the path.
    """
    from farm.model import Endpoint, FarmConfig, FarmTruth, Service

    endpoints = []
    for r in responses:
        if r["status"] == 0:
            continue  # unreachable: not an observation, so not an endpoint
        endpoints.append(
            Endpoint(
                path=r["path"],
                body=r["body"],
                status=r["status"],
                headers=r["headers"],
                # Whether an endpoint is a clean control is a judgement about the
                # service's intent, and this service is deliberately mixed. Left False
                # here and stated in the writeup, rather than asserted.
                is_clean_control=False,
            )
        )
    service = Service(
        service_id=service_id,
        cluster_id=None,
        address=addr,
        title="OnionTrace lab (operated by us)",
        topic="self-operated validation service",
        endpoints=endpoints,
        shared_artifacts={},
        flaws=[],
    )
    return FarmTruth(config=FarmConfig(), services=[service])


#: Paths that genuinely do not exist on either instance. A rule that claims content exists
#: at one of these, when the server returns 404, is reporting something that is not there.
ABSENT_PATHS = frozenset({"/.git/config", "/nope-does-not-exist"})

#: Rules that make a claim about the service as a whole rather than about a path: a version
#: banner or a Date header is a property of every response the service sends. Firing on
#: several paths is one finding, not several, and it is not a false positive when the
#: property really is disclosed. Counting these per path would inflate both the hit count
#: and the false-positive rate, so they are collapsed to one finding per service.
SERVICE_LEVEL_KINDS = frozenset({
    "nginx_version_banner", "apache_version_banner",
    "timezone_leak", "etag_pattern", "cookie_name_unique", "http_header_order",
})

#: Rules that assert a piece of content is reachable at a path. These are the only ones
#: that can be a false positive in the strict sense: the rule says "this exists", and a 404
#: says it does not.
CONTENT_CLAIM_KINDS = frozenset({
    "dot_env", "dot_git", "backup_file", "phpinfo", "server_status",
    "directory_listing", "git_config", "exposed_key", "htaccess",
})


def score(service_id: str, responses: list[dict], rules) -> dict:
    """Run one rule table over one service's real responses.

    Two questions matter and they are not the same:

    - **Did the rule fire?** Collapsed per kind for service-level rules, so a banner seen
      on four paths is one finding.
    - **Is it wrong?** Only a rule that claims content exists at a path returning 404 is a
      false positive. A disclosed version banner on a 404 page is a true positive: the
      banner really is there.
    """
    from eval.detectors.misconfig import scan_service

    farm = to_farm(service_id, service_id, responses)
    hits = scan_service(farm, service_id, rules=rules)
    shaped = [
        {"kind": h.kind, "path": h.path, "evidence": (h.evidence or "")[:160]}
        for h in hits
    ]

    status_by_path = {r["path"]: r["status"] for r in responses}

    # A 404 on a path the rules call a content claim is the strict false positive.
    false_positives = [
        h for h in shaped
        if h["kind"] in CONTENT_CLAIM_KINDS
        and h["path"] in ABSENT_PATHS
        and status_by_path.get(h["path"]) == 404
    ]

    # Collapse service-level kinds to one finding each, for the headline count.
    collapsed: list[dict] = []
    seen_service_level: set[str] = set()
    for h in shaped:
        if h["kind"] in SERVICE_LEVEL_KINDS:
            if h["kind"] in seen_service_level:
                continue
            seen_service_level.add(h["kind"])
        collapsed.append(h)

    return {
        "hits": shaped,
        "hit_count": len(shaped),
        "collapsed_count": len(collapsed),
        "collapsed": collapsed,
        "fired_kinds": sorted({h["kind"] for h in shaped}),
        "false_positives": false_positives,
    }


def main() -> int:
    addresses = onion_addresses()
    if not addresses:
        print("no onion addresses published; start the services first")
        return 1

    from eval.detectors.misconfig import NAIVE_RULES, RULES

    per_service: dict[str, dict] = {}
    for label, addr in addresses.items():
        print(f"{label}: fetching our own onion service over the real Tor network")
        print(f"  {addr}")
        responses = [fetch(addr, p) for p in PROBES]
        for r in responses:
            print(f"    {r['path']:22} HTTP {r['status']:3}  {len(r['body']):5}B  "
                  f"Server={r['headers'].get('Server', '-')!r}")
        if not any(r["status"] for r in responses):
            print("    unreachable over Tor; skipping")
            continue
        per_service[label] = {
            "address": addr,
            "responses": responses,
            "careful": score(f"real-{label}", responses, RULES),
            "naive": score(f"real-{label}", responses, NAIVE_RULES),
        }

    if not per_service:
        print("nothing reachable")
        return 1

    # Real false positives come straight from score(), which already applies the strict
    # definition: a rule claiming content at a path that returned 404.
    summary: dict[str, dict] = {}
    for label, entry in per_service.items():
        summary[label] = {
            "address": entry["address"],
            "careful_collapsed_count": entry["careful"]["collapsed_count"],
            "naive_collapsed_count": entry["naive"]["collapsed_count"],
            "careful_fired_kinds": entry["careful"]["fired_kinds"],
            "naive_fired_kinds": entry["naive"]["fired_kinds"],
            "careful_false_positives": entry["careful"]["false_positives"],
            "naive_false_positives": entry["naive"]["false_positives"],
        }

    result = {
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "transport": f"socks5h via 127.0.0.1:{SOCKS_PORT}, real Tor network",
        "services": per_service,
        "summary": summary,
        "note": (
            "Two v3 onion services published by the operator, fronting lab nginx "
            "instances on loopback, fetched over the real Tor network. Both ends are "
            "ours; no third party is involved. The clean instance is a real hard "
            "negative: a correct rule must stay silent on it. This validates that the "
            "rule tables behave correctly on real onion-served content. It is not a "
            "prevalence measurement, and no claim is made about how common any of these "
            "misconfigurations is in the wild."
        ),
    }

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "real_onion_service.json").write_text(json.dumps(result, indent=2, sort_keys=True))

    for label, s in summary.items():
        print()
        print(f"  === {label} ({s['address']}) ===")
        print(f"    careful: {s['careful_collapsed_count']} findings, "
              f"kinds={s['careful_fired_kinds']}, "
              f"false positives={len(s['careful_false_positives'])}")
        print(f"    naive  : {s['naive_collapsed_count']} findings, "
              f"kinds={s['naive_fired_kinds']}, "
              f"false positives={len(s['naive_false_positives'])}")
        for fp in s["naive_false_positives"]:
            print(f"      naive FP  : {fp['kind']} on {fp['path']} -> {fp['evidence'][:45]}")
        for fp in s["careful_false_positives"]:
            print(f"      careful FP: {fp['kind']} on {fp['path']} -> {fp['evidence'][:45]}")
    print()
    print(f"wrote {OUT / 'real_onion_service.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())