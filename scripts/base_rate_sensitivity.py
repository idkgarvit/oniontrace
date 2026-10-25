"""Re-run the correlation experiment at measured prevalence instead of the designed one.

The farm plants a stock default on every eligible service. That is a 100 percent prevalence
claim, chosen because it is the hardest case for a naive correlator, and a defence that holds
there holds everywhere below it. It is also, until now, the one number in the experiment that
came from nowhere.

`farm/base_rates.py` holds the measured figure -- one issuer covers 59.2 percent of 3,018 real
onion-bearing certificates -- so the same experiment can be run at the rate the world actually
shows. If the frequency cap is doing real work rather than winning against a strawman, the
conclusion survives the drop; if it only works because defaults are universal, this is where
that would show.

    python3 scripts/base_rate_sensitivity.py

Prints a table and a VERDICT line, and writes out/base_rates/sensitivity.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval.harness import correlation_experiment  # noqa: E402
from farm.base_rates import (  # noqa: E402
    DESIGN_SHARE,
    MEASURE_COMMAND,
    MEASURED_ON,
    MEASURED_POPULATION,
    MEASURED_SHARES_NOTE,
    MEASURED_SOURCE,
    MEASURED_TOP_ARTEFACT_SHARE,
    SENSITIVITY_SHARES,
    describe,
)
from farm.generator import generate_farm  # noqa: E402
from farm.model import FarmConfig  # noqa: E402

SEED = 20260925
CAP = 3
DESTINATION = ROOT / "out" / "base_rates" / "sensitivity.json"


def run(share: float) -> dict:
    farm = generate_farm(FarmConfig(seed=SEED, widespread_default_share=share))
    planted = sum(
        1
        for negative in farm.hard_negatives
        if "[widespread" in negative.why
    )
    naive = correlation_experiment(farm, max_artifact_frequency=None)
    capped = correlation_experiment(farm, max_artifact_frequency=CAP)
    return {
        "widespread_default_share": share,
        "widespread_defaults_planted": planted,
        "naive": {
            "proposed": naive["link_scoring"]["proposed"],
            "precision": naive["link_scoring"]["precision"],
            "false_merge_rate": naive["link_scoring"]["false_merge_rate"],
            "precision@1": naive["ranking"]["precision@1"]["value"],
        },
        "capped": {
            "proposed": capped["link_scoring"]["proposed"],
            "precision": capped["link_scoring"]["precision"],
            "false_merge_rate": capped["link_scoring"]["false_merge_rate"],
            "precision@1": capped["ranking"]["precision@1"]["value"],
        },
    }


def main() -> int:
    rows = [run(share) for share in SENSITIVITY_SHARES]

    print(f"seed {SEED}, cap {CAP}, {describe()}\n")
    header = (
        f"{'prevalence':>10}  {'defaults':>8}  {'naive P':>8}  {'naive FM':>8}  "
        f"{'capped P':>8}  {'capped FM':>9}  {'naive p@1':>9}  {'capped p@1':>10}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        naive, capped = row["naive"], row["capped"]
        print(
            f"{row['widespread_default_share']:>10.3f}  "
            f"{row['widespread_defaults_planted']:>8}  "
            f"{naive['precision']:>8.3f}  {naive['false_merge_rate']:>8.3f}  "
            f"{capped['precision']:>8.3f}  {capped['false_merge_rate']:>9.3f}  "
            f"{naive['precision@1']:>9.3f}  {capped['precision@1']:>10.3f}"
        )

    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_text(
        json.dumps(
            {
                "seed": SEED,
                "cap": CAP,
                "measured_population": MEASURED_POPULATION,
                "measured_on": MEASURED_ON,
                "measured_source": MEASURED_SOURCE,
                "measure_command": MEASURE_COMMAND,
                "measured_share": MEASURED_TOP_ARTEFACT_SHARE,
                "design_share": DESIGN_SHARE,
                "note": MEASURED_SHARES_NOTE,
                "rows": rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    at_measurement = next(
        row for row in rows if row["widespread_default_share"] == MEASURED_TOP_ARTEFACT_SHARE
    )
    naive_precision = at_measurement["naive"]["precision"]
    capped_precision = at_measurement["capped"]["precision"]
    held = capped_precision == 1.0 and naive_precision < 1.0

    print(f"\nwrote {DESTINATION.relative_to(ROOT)}")
    if not held:
        print(
            "VERDICT: at the measured prevalence the cap does not separate from naive "
            f"(naive {naive_precision:.3f}, capped {capped_precision:.3f}) -- the headline "
            "result depends on the designed rate"
        )
        return 1
    print(
        f"VERDICT: at the measured {MEASURED_TOP_ARTEFACT_SHARE:.1%} prevalence the cap still "
        f"separates from naive (precision {capped_precision:.3f} against "
        f"{naive_precision:.3f}), so the result does not depend on the designed rate"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
