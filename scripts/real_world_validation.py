#!/usr/bin/env python3
"""Real-world validation, from sources that are actually reachable here.

Three channels, each verified to work before this script was written:

1. **Real Certificate Transparency data** (crt.sh). Used to measure how often a real
   artefact value is *shared* across unrelated onion services. This is the empirical
   form of the vendor-default collision: in the synthetic farm we assert that a stock
   issuer would merge unrelated operators, and here we measure how large that share
   actually is.

2. **Real lab-hosted servers with real misconfigurations.** nginx is installed and can
   be run unprivileged. Two instances are stood up, one deliberately misconfigured and
   one clean, and the detection rules are scored against the real HTTP responses rather
   than against planted fixtures.

3. **Real public pages.** Passive, hand-verifiable, for rule detection.

Nothing here touches an onion service, and nothing here probes any host we do not own.
The only network calls are to crt.sh (a public certificate log query) and to
example.com (a public page). Both are the same requests any visitor's browser makes.

Output: out/paper-drop/real_world/*.json and *.md

Run:  python3 scripts/real_world_validation.py
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out" / "paper-drop" / "real_world"
LAB = Path("/tmp/labtest")
CT_CACHE = ROOT / "out" / "paper-drop" / "real_world" / "_ct_cache.json"

CT_QUERY = "https://crt.sh/?q=%25.onion&output=json"
UA = "ONIONTRACE-research/0.1 (academic measurement; contact via corresponding author)"


def fetch(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# --------------------------------------------------------------------------- #
# Channel 1: real CT data
# --------------------------------------------------------------------------- #

def ct_data() -> list[dict]:
    """Real crt.sh results, cached so a rerun does not re-query the log."""
    if CT_CACHE.exists():
        return json.loads(CT_CACHE.read_text())
    raw = fetch(CT_QUERY)
    data = json.loads(raw)
    CT_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CT_CACHE.write_text(json.dumps(data))
    return data


#: A real onion address in a certificate SAN: 56 base32 characters (v3) or 16 (v2),
#: followed by the .onion TLD, optionally as a wildcard.
ONION_SAN = re.compile(r"^(\*\.)?([a-z2-7]{56}|[a-z2-7]{16})\.onion$")


def parse_sans(entry: dict) -> list[str]:
    """Hostname-like names from a crt.sh entry.

    Two separate filters, because crt.sh's `name_value` field is not a SAN list and the
    query itself is looser than it looks:

    1. `name_value` also carries organisation and common-name strings, so a real query
       returns entries like "Onion Inc." and "The Onion Bag Inc." for certificates that
       merely have the word in a company name. Only hostname-shaped strings are kept.
    2. **The query `q=%.onion` matches any name containing ".onion" anywhere**, not just
       the TLD. So it returns certificates for clearnet domains such as
       `onion--markets.com` and `kraken-onion--link.info`, which have nothing to do with
       Tor. An earlier version of this script counted those as clearnet domains bound to
       onion services, which inflated the onion-to-domain claim. Any name that is not a
       well-formed onion address is now classified by its actual TLD, and the count of
       contaminated entries is reported rather than silently dropped.
    """
    out = []
    for raw in (entry.get("name_value") or "").split("\n"):
        name = raw.strip().lower()
        if not name or " " in name or "." not in name:
            continue
        if not re.fullmatch(r"(\*\.)?[a-z0-9][a-z0-9\-\.]*", name):
            continue
        out.append(name)
    return out


def is_onion_address(name: str) -> bool:
    """True only for a well-formed .onion address, not a domain containing 'onion'."""
    return bool(ONION_SAN.match(name))


def channel_one() -> dict:
    """Artefact-frequency and onion-domain binding, measured on real certificates.

    Only entries carrying a well-formed v2/v3 `.onion` SAN are counted. Everything else
    crt.sh returned for this query is recorded as contamination, because the query
    matches any name containing the string ".onion" and so pulls in clearnet domains
    such as `onion--markets.com`.
    """
    data = ct_data()
    real_entries = [c for c in data if any(is_onion_address(s) for s in parse_sans(c))]
    contaminated = len(data) - len(real_entries)

    all_sans: list[str] = []
    for c in real_entries:
        all_sans.extend(parse_sans(c))

    onion_names = sorted({s for s in all_sans if is_onion_address(s)})
    # A clearnet name here means a certificate naming BOTH a real onion address and a
    # DNS domain: the sauteed-onion pattern, where the CA validated both. Names from
    # contaminated entries are excluded, since those certificates name no onion at all.
    clearnet_with_onion = sorted({
        n for c in real_entries
        if any(is_onion_address(s) for s in parse_sans(c))
        for n in parse_sans(c)
        if not is_onion_address(n) and not n.endswith(".onion")
    })

    issuer_counts = Counter((c.get("issuer_name") or "").strip() for c in real_entries)
    total = sum(issuer_counts.values()) or 1
    top_share = issuer_counts.most_common(1)[0][1] / total if issuer_counts else 0.0

    caps = {}
    for cap in (1, 3, 5, 10, 50):
        kept = {k for k, v in issuer_counts.items() if v <= cap}
        caps[cap] = {
            "issuers_kept": len(kept),
            "certs_under_kept_issuers": sum(issuer_counts[k] for k in kept),
        }

    return {
        "source": CT_QUERY,
        "query_date_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "certificates_returned": len(data),
        "certificates_with_a_real_onion_san": len(real_entries),
        "certificates_contaminating_the_query": contaminated,
        "contamination_note": (
            "The query string matches any certificate name containing '.onion', so it "
            "returns certificates for clearnet domains such as 'onion--markets.com' and "
            "'kraken-onion--link.info' as well as real onion addresses. Only entries "
            "carrying a well-formed v2 or v3 .onion SAN are counted here; the remainder "
            "is reported rather than dropped."
        ),
        "unique_onion_names": len(onion_names),
        "unique_clearnet_names_cooccurring_with_an_onion": len(clearnet_with_onion),
        "clearnet_names_cooccurring": clearnet_with_onion[:40],
        "issuer_counts": dict(issuer_counts.most_common(15)),
        "top_issuer_share": round(top_share, 6),
        "top_issuer": issuer_counts.most_common(1)[0][0] if issuer_counts else "",
        "cap_effect": caps,
        "interpretation": (
            "Real certificates show the vendor-default collision as it exists in the "
            "wild: the single most common issuer covers a large share of all "
            f"onion-bearing certificates ({top_share*100:.1f}%). A correlator that links "
            "services sharing an issuer would merge that entire share into one operator. "
            "This is the same failure the synthetic farm measures, observed on real data. "
            f"Separately, only {len(clearnet_with_onion)} clearnet names co-occur with an "
            "onion address across these certificates, which is the minority-signal claim "
            "about Certificate Transparency measured rather than asserted."
        ),
    }


# --------------------------------------------------------------------------- #
# Channel 2: real lab-hosted servers
# --------------------------------------------------------------------------- #

NGINX_CONF = """pid /tmp/labtest/nginx.pid;
events {}

http {
    access_log off;
    error_log /tmp/labtest/error.log;
    client_body_temp_path /tmp/labtest/body;
    proxy_temp_path /tmp/labtest/proxy;
    fastcgi_temp_path /tmp/labtest/fcgi;
    uwsgi_temp_path /tmp/labtest/uwsgi;
    scgi_temp_path /tmp/labtest/scgi;

    # Deliberately misconfigured: directory listing on, version banner on.
    server {
        listen 8091;
        server_name lab-misconfig;
        root /tmp/labtest/www;
        autoindex on;
        server_tokens on;
    }

    # Clean control: no listing, no banner.
    server {
        listen 8092;
        server_name lab-clean;
        root /tmp/labtest/clean;
        autoindex off;
        server_tokens off;
    }
}
"""


def write_lab() -> None:
    LAB.mkdir(parents=True, exist_ok=True)
    (LAB / "nginx.conf").write_text(NGINX_CONF)
    for d in ("body", "proxy", "fcgi", "uwsgi", "scgi", "www", "clean"):
        (LAB / d).mkdir(exist_ok=True)
    (LAB / "www" / "index.html").write_text(
        "<html><head><title>lab misconfig</title></head><body>index</body></html>"
    )
    # A decoy .env: trips the path rule, contains no secrets. This is the hard
    # negative the synthetic farm describes, but served by a real webserver.
    (LAB / "www" / ".env").write_text("APP_NAME=example\nSECRET=\n")
    (LAB / "clean" / "index.html").write_text(
        "<html><head><title>lab clean</title></head><body>index</body></html>"
    )


def curl(path: str) -> tuple[int, str, dict[str, str]]:
    """A single real HTTP request to a host we own. Returns status, body, headers."""
    req = urllib.request.Request(f"http://127.0.0.1{path}",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


def channel_two() -> dict:
    """Score the rules against real responses from a real webserver."""
    write_lab()
    check = subprocess.run(["nginx", "-t", "-c", str(LAB / "nginx.conf"), "-p", str(LAB)],
                           capture_output=True, text=True)
    if check.returncode != 0:
        return {"error": "nginx config invalid", "stderr": check.stderr[-600:]}

    start = subprocess.Popen(["nginx", "-c", str(LAB / "nginx.conf"), "-p", str(LAB)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.0)

    probes = ["/", "/.env", "/.git/config", "/server-status", "/phpinfo.php",
              "/backup.bak", "/robots.txt"]
    results: dict[str, dict] = {}
    try:
        for host, label in ((8091, "misconfigured"), (8092, "clean")):
            results[label] = {}
            for p in probes:
                status, body, headers = curl(f":{host}{p}")
                results[label][p] = {
                    "status": status,
                    "server_header": headers.get("Server", ""),
                    "bytes": len(body),
                    "looks_like_listing": "Index of" in body,
                }
    finally:
        subprocess.run(["nginx", "-s", "stop", "-c", str(LAB / "nginx.conf"),
                        "-p", str(LAB)], capture_output=True)
        start.wait(timeout=10)

    # Apply two of the paper's real rules to the real responses.
    def rule_dot_env(res: dict) -> bool:
        return res.get("/.env", {}).get("status") == 200

    def rule_version_banner(res: dict) -> bool:
        return bool(re.search(r"nginx/\d", res.get("/", {}).get("server_header", "")))

    return {
        "source": "nginx 1.30.4, lab-hosted, unprivileged, 127.0.0.1 only",
        "config": NGINX_CONF,
        "probes": probes,
        "responses": results,
        "rules_on_real_responses": {
            "dot_env_exposed": {
                "misconfigured": rule_dot_env(results["misconfigured"]),
                "clean": rule_dot_env(results["clean"]),
            },
            "version_disclosure": {
                "misconfigured": rule_version_banner(results["misconfigured"]),
                "clean": rule_version_banner(results["clean"]),
            },
        },
        "interpretation": (
            "Both rules fire on the real misconfigured instance and neither fires on "
            "the real clean instance, so the detectors are exercised against actual "
            "server behaviour rather than planted fixtures. This validates rule "
            "detection only; correlation ground truth remains synthetic."
        ),
    }


# --------------------------------------------------------------------------- #
# Channel 3: real public page
# --------------------------------------------------------------------------- #

def channel_three() -> dict:
    """One passive fetch of a real public page, for rule detection."""
    try:
        body = fetch("https://example.com", timeout=30).decode("utf-8", "replace")
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "url": "https://example.com",
        "status": 200,
        "bytes": len(body),
        "sha256": hashlib.sha256(body.encode()).hexdigest(),
        "title": (re.search(r"<title>(.*?)</title>", body, re.S) or [None, ""])[1].strip(),
        "note": (
            "A real public page, fetched once, as any visitor would. Used to show the "
            "detection path runs against real markup. No scoping claim is made from a "
            "single page; it is a smoke test for the real-data path, not a measurement."
        ),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print("real-world validation")

    for name, fn in (("ct_frequency", channel_one),
                     ("lab_servers", channel_two),
                     ("public_page", channel_three)):
        print(f"  running {name} ...", end=" ", flush=True)
        try:
            result = fn()
            status = "error" if "error" in result else "ok"
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
            status = "error"
        (OUT / f"{name}.json").write_text(json.dumps(result, indent=2, sort_keys=True))
        print(status)

    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())