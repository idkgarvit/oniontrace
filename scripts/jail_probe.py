"""One GET per address through the jail's SOCKS bridge, for diagnosis.

    scripts/run_isolated.sh --tor-sock <sock> -- python3 scripts/jail_probe.py <address>...

This is not part of collection: it stores nothing and opens one request per address. It
exists because "the collector cannot reach the lab" and "the jail cannot reach Tor" look
identical in a failed run, and this splits them apart.
"""

from __future__ import annotations

import os
import sys
import time

import requests

proxy = os.environ.get("ONIONTRACE_SOCKS")
if not proxy:
    raise SystemExit("ONIONTRACE_SOCKS is not set; run this inside scripts/run_isolated.sh")

failed = 0
for address in sys.argv[1:]:
    started = time.time()
    try:
        response = requests.get(
            f"http://{address}/",
            proxies={"http": f"socks5h://{proxy}"},
            timeout=60,
        )
        print(f"{address}\t{response.status_code}\t{len(response.content)}\t"
              f"{time.time() - started:.1f}s", flush=True)
    except Exception as exc:  # noqa: BLE001 - the reason matters, not the type
        failed += 1
        print(
            f"{address}\tERROR\t{type(exc).__name__}: {str(exc)[:160]}\t"
            f"{time.time() - started:.1f}s",
            flush=True,
        )
raise SystemExit(1 if failed else 0)
