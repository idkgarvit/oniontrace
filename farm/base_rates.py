"""Prevalence the farm uses, and where each number was measured.

A synthetic farm is a set of assumptions wearing a seed. Most of them are structural -- three
services per cluster, four personas, twenty flaws -- and those are honest because they are
visible and every report says the farm is synthetic. Prevalence is a different kind of
assumption: "how many services carry a stock default" is a claim about the world, and a number
picked for convenience makes every metric downstream of it an opinion with a decimal point.

So nothing here is chosen. Each constant is measured from the cached certificate-transparency
records under ``data/evidence/real_world/`` by running::

    python3 scripts/measure_base_rates.py

which rewrites ``base_rates.json`` and prints the same figures. The artefact class is the
certificate issuer, because a certificate's issuer is a byte string in the DER rather than an
inference: 3,018 onion-bearing certificates give one free, measured on 2026-10-23.

The farm still plants at ``DESIGN_SHARE`` by default. That is deliberate and it is not a
dodge: 100 percent is the condition most favourable to the naive correlator, so a defence that
holds there holds at any lower rate, and reporting the hardest case as the headline is the fair
way round. What changed is that the real figure is now known and the experiment can be run at
it, which is what ``scripts/base_rate_sensitivity.py`` does.
"""

from __future__ import annotations

#: Share of the measured population covered by its single most common artefact value.
#: A correlator that links on "the same issuer" collapses this share into one operator.
MEASURED_TOP_ARTEFACT_SHARE = 0.592445  # 1,788 of 3,018 certificates, DigiCert EV CA

#: Share of the measured population whose value is shared with at least one other member.
#: This is the rate at which a naive merge has *something* to wrongly attach itself to.
MEASURED_SHARED_ARTEFACT_SHARE = 0.999006  # 3,015 of 3,018 certificates

MEASURED_POPULATION = 3018
MEASURED_DISTINCT_VALUES = 19
MEASURED_ON = "2026-10-23"
MEASURED_SOURCE = "data/evidence/real_world/base_rates.json"
MEASURE_COMMAND = "python3 scripts/measure_base_rates.py"

#: What the farm plants by default: every eligible service, the adversarial upper bound.
DESIGN_SHARE = 1.0

#: Rates the sensitivity check sweeps, cheapest first. The middle one is the measurement.
SENSITIVITY_SHARES: tuple[float, ...] = (DESIGN_SHARE, MEASURED_TOP_ARTEFACT_SHARE, 0.25)

#: One sentence for a results file, so a reader of the JSON knows why the sweep exists.
MEASURED_SHARES_NOTE = (
    "The farm's headline plants a stock default on every eligible service (share 1.0), the "
    "adversarial upper bound. The measured share is the fraction of 3,018 real onion-bearing "
    "certificates covered by their single most common issuer. The sweep reports both, plus a "
    "quarter-rate condition, so a reader can see whether the frequency cap's advantage depends "
    "on defaults being universal."
)


def describe() -> str:
    """One line for a report or a console header."""

    return (
        f"widespread-default prevalence: design {DESIGN_SHARE:.1%}, "
        f"measured {MEASURED_TOP_ARTEFACT_SHARE:.1%} of {MEASURED_POPULATION} real "
        f"certificates ({MEASURED_DISTINCT_VALUES} issuers)"
    )
