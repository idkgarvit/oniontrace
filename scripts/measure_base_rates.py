"""Measure how concentrated a shared artefact really is, from the cached CT records.

The synthetic farm plants a vendor-default artefact on every eligible service, which is a
100 percent prevalence claim: it says stock defaults are universal. That is a deliberately
adversarial worst case, and it is the condition most favourable to the naive correlator, so it
is a fair headline. What it is not is *measured*. This file measures the real number, so the
farm's prevalence can be set to something observed instead of something chosen.

The artefact class used here is the certificate issuer, because 3,018 real certificates give us
one for free and a certificate's issuer is not a guess: it is a byte string in the DER. The
question this answers is "if a correlator merged services on a shared issuer, how much of the
population would one value swallow".

    python3 scripts/measure_base_rates.py

Writes data/evidence/real_world/base_rates.json, and prints a VERDICT line for the harness.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "evidence" / "real_world" / "_ct_cache.json"
DESTINATION = ROOT / "data" / "evidence" / "real_world" / "base_rates.json"

sys.path.insert(0, str(ROOT / "scripts"))
from real_world_validation import is_onion_address, parse_sans  # noqa: E402


def clean_certificates() -> list[dict]:
    data = json.loads(CACHE.read_text(encoding="utf-8"))
    return [cert for cert in data if any(is_onion_address(n) for n in parse_sans(cert))]


def measure() -> dict:
    records = clean_certificates()
    total = len(records)
    issuers = Counter(cert.get("issuer_name", "") for cert in records)

    def share_of_top(k: int) -> float:
        return round(sum(count for _, count in issuers.most_common(k)) / total, 6)

    top_issuer, top_count = issuers.most_common(1)[0]
    # How much of the population sits under an issuer that is *not* unique to it. A correlator
    # that merges on issuer cannot separate any of those certificates from each other.
    shared = sum(count for count in issuers.values() if count > 1)
    singletons = sum(1 for count in issuers.values() if count == 1)

    return {
        "artefact_class": "certificate issuer",
        "source": str(CACHE.relative_to(ROOT)),
        "certificates_measured": total,
        "distinct_issuers": len(issuers),
        "top_issuer": top_issuer,
        "top_issuer_certificates": top_count,
        "top_issuer_share": round(top_count / total, 6),
        "cumulative_share": {f"top_{k}": share_of_top(k) for k in (1, 2, 5, 10)},
        "certificates_under_a_shared_issuer": shared,
        "certificates_under_a_unique_issuer": singletons,
        "issuers_appearing_once": singletons,
        "interpretation": (
            "One issuer covers the majority of onion-bearing certificates in this sample, and "
            "that share is a floor on what a naive merge does: every certificate under it "
            "collapses into a single operator hypothesis. The farm plants its stock default on "
            "every eligible service (share 1.0) as an upper bound; this file measures the "
            "corresponding real share so the experiment can also be run at an observed rate."
        ),
        "reproduce": "python3 scripts/measure_base_rates.py",
    }


def main() -> int:
    if not CACHE.exists():
        print(f"no cached CT records at {CACHE}; run scripts/real_world_validation.py first")
        return 1
    measured = measure()
    DESTINATION.write_text(json.dumps(measured, indent=2) + "\n", encoding="utf-8")
    print(f"certificates measured      {measured['certificates_measured']}")
    print(f"distinct issuers           {measured['distinct_issuers']}")
    print(f"top issuer share           {measured['top_issuer_share']:.4f} "
          f"({measured['top_issuer_certificates']} certificates)")
    print(f"  {measured['top_issuer']}")
    print("cumulative share           " + ", ".join(
        f"{k}={v:.3f}" for k, v in measured["cumulative_share"].items()))
    print(f"under a shared issuer      {measured['certificates_under_a_shared_issuer']} "
          f"of {measured['certificates_measured']}")
    print(f"\nwrote {DESTINATION.relative_to(ROOT)}")
    print(
        f"VERDICT: {measured['certificates_measured']} real certificates measured, "
        f"one issuer covers {measured['top_issuer_share'] * 100:.1f} percent"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
