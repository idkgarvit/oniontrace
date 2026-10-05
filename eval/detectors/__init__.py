"""Reference detectors for the modules under test.

Each module is a *plausible naive* implementation, not a strawman. They exist so that:

  1. every Part E metric is demonstrable today, before the production modules land; and
  2. when the production modules do land, they are scored in exactly the same harness
     and compared against a known floor rather than against nothing.

Where a detector has a hard-negative-resistant variant and a naive one, both are
implemented. The gap between them is the measurement of whether the farm's hard
negatives are doing their job.
"""

from eval.detectors.correlation import (
    artifact_frequencies,
    common_value_threshold,
    rank_services,
)
from eval.detectors.misconfig import (
    MISCONFIG_RULE_CLASSES,
    RULES,
    MisconfigScoring,
    RuleHit,
    scan_service,
)

__all__ = [
    "MISCONFIG_RULE_CLASSES",
    "RULES",
    "MisconfigScoring",
    "RuleHit",
    "artifact_frequencies",
    "common_value_threshold",
    "rank_services",
    "scan_service",
]
