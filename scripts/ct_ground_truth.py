"""Score the correlator against real operator groupings from Certificate Transparency.

    python3 scripts/ct_ground_truth.py

Reads the cached CT records, builds the groupings a certificate asserts (one certificate naming
several onions means one holder owns them all), then asks how much of that is recoverable from
the public fields alone: issuer, and issuer plus issue date.

Writes data/evidence/real_world/ct_ground_truth.json and prints a VERDICT line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.ct_ground_truth import run  # noqa: E402

CACHE = ROOT / "data" / "evidence" / "real_world" / "_ct_cache.json"
DESTINATION = ROOT / "data" / "evidence" / "real_world" / "ct_ground_truth.json"


def main() -> int:
    if not CACHE.exists():
        print(f"no cached CT records at {CACHE}; run scripts/real_world_validation.py first")
        return 1

    result = run(json.loads(CACHE.read_text(encoding="utf-8")))
    result["source"] = str(CACHE.relative_to(ROOT))
    result["reproduce"] = "python3 scripts/ct_ground_truth.py"
    result["ground_truth_note"] = (
        "A certificate naming several onion services asserts that one holder controls them. "
        "That is the target, never a feature: a linker using co-certificate would be reading "
        "the answer key. What is measured is whether the public fields recover it."
    )
    DESTINATION.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print(
        f"{result['services']} services, {result['clusters']} certificate-implied clusters, "
        f"{result['true_pairs']} true pairs, largest cluster {result['largest_cluster']}\n"
    )
    header = f"{'linker':<22}{'pairs':>8}{'P':>8}{'R':>8}{'F1':>8}"
    print(header)
    print("-" * len(header))
    for name, key in (("issuer alone", "issuer_only"), ("issuer + issue date", "issuer_and_date")):
        row = result[key]
        print(
            f"{name:<22}{row['proposed']:>8}{row['precision']:>8.3f}"
            f"{row['recall']:>8.3f}{row['f1']:>8.3f}"
        )

    print("\nfrequency cap on batches")
    print(f"{'cap':>5}{'pairs':>8}{'P':>8}{'R':>8}{'F1':>8}")
    for row in result["sweep"]:
        label = "none" if row["cap"] is None else str(row["cap"])
        print(
            f"{label:>5}{row['proposed']:>8}{row['precision']:>8.3f}"
            f"{row['recall']:>8.3f}{row['f1']:>8.3f}"
        )

    held = result["held_out"]
    print(
        f"\nheld out: cap {held['chosen_cap']} chosen on {held['selection_services']} services "
        f"(F1 {held['selection_f1']:.3f}), scored on {held['report_services']} unseen ones: "
        f"P {held['report']['precision']:.3f} R {held['report']['recall']:.3f} "
        f"F1 {held['report']['f1']:.3f}"
    )
    print(f"\nwrote {DESTINATION.relative_to(ROOT)}")

    issuer = result["issuer_only"]
    batch = result["issuer_and_date"]
    held_f1 = held["report"]["f1"]
    if batch["precision"] <= issuer["precision"]:
        print(
            "VERDICT: the issue date does not separate operators better than the issuer alone "
            f"({batch['precision']:.3f} against {issuer['precision']:.3f})"
        )
        return 1
    print(
        f"VERDICT: on {result['services']} real services the issuer alone links at precision "
        f"{issuer['precision']:.3f}; adding the issue date lifts it to {batch['precision']:.3f} "
        f"at recall {batch['recall']:.3f}, and a cap chosen on held-out clusters keeps "
        f"F1 {held_f1:.3f} on unseen services"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
