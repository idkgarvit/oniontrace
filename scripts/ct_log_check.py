"""Ask real Certificate Transparency logs to prove themselves.

Run inside the collector's isolation jail, through the SOCKS bridge:

    scripts/run_isolated.sh --tor-sock ~/.cache/oniontrace-tor/tor.sock -- \\
        python3 scripts/ct_log_check.py

What it demonstrates, in order:
  1. the operator list is fetched and its usable logs parsed
  2. each log's tree head is fetched and its ECDSA signature verified against the key the
     operator list publishes
  3. the key is checked to hash to the log's published id, which is what stops a
     substituted key from verifying a substituted head
  4. a handful of recent entries are read and their names extracted

A log that fails step 2 is reported as failing, not skipped: the point of the check is to
find out, not to look successful.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collect.ctlog_direct import (  # noqa: E402
    CtLogClient,
    CtLogError,
    LOG_LIST_URL,
    names_in_certificate,
    parse_log_list,
    requests_fetch,
    SignedTreeHead,
)


def main() -> int:
    socks = os.environ.get("ONIONTRACE_SOCKS") or os.environ.get("ONIONTRACE_PROXY")
    if not socks:
        print("no proxy configured; set ONIONTRACE_SOCKS or run through run_isolated.sh")
        return 2
    proxy = socks if "://" in socks else f"socks5h://{socks}"
    fetch = requests_fetch(proxy=proxy, timeout=60.0)

    logs = parse_log_list(fetch(LOG_LIST_URL))
    print(f"operator list: {len(logs)} usable logs")

    verified: list[tuple[CtLogClient, SignedTreeHead]] = []
    for log in logs[:4]:
        client = CtLogClient(log, fetch)
        try:
            sth = client.sth(verify=True)
        except CtLogError as failure:
            print(f"  FAIL {log.description}: {failure}")
            continue
        verified.append((client, sth))
        print(f"  ok   {log.description}: tree_size={sth.tree_size:,} signature verified")

    print(f"\n{len(verified)} of 4 tree heads verified against their published keys")
    if not verified:
        return 1

    # Reading entries is a second request to the same operator, and a shared exit address
    # gets throttled: a 429 here is about the network path, not about the log or this code.
    # So try each verified log in turn rather than reporting a throttle as a failure.
    entries = None
    for client, sth in verified:
        first = max(0, sth.tree_size - 3)
        try:
            entries = (client, first, client.entries(first, sth.tree_size - 1))
            break
        except CtLogError as failure:
            print(f"  entry read unavailable from {client.log.description}: {failure}")
            time.sleep(2.0)
    if entries is None:
        print("\nno log served entries on this path; the signature check above still stands")
        return 0

    client, first, leaves = entries
    print(f"\nreading entries {first}..{first + len(leaves) - 1} of {client.log.description}")
    for offset, leaf in enumerate(leaves):
        names = names_in_certificate(leaf.payload)
        kind = "precert" if leaf.is_precert else "x509"
        shown = ", ".join(names[:3]) if names else "(no names readable in the leaf)"
        print(f"  {first + offset}: {kind}, {len(names)} name(s): {shown}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
