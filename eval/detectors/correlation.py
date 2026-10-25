"""Cross-service fingerprint correlation (M2).

Reference implementations of two correlator variants, so the effect of the farm's
hard negatives is visible as a measured difference rather than an assertion:

  * **naive** -- links two services if they share any artefact value. This is the
    obvious first implementation, and the stock-default hard negatives defeat it:
    unrelated clusters receive byte-identical vendor-default artefacts, so a naive
    rule merges them and every one of those merges is false.

  * **frequency-filtered** -- additionally discards artefact values that appear on
    more than `max_artifact_frequency` services. A value shared across a large
    fraction of the population carries no operator signal, which is the real-world
    reason a stock CMS favicon is worthless as a linking identifier.

Both variants are scored against the same ground truth, so the harness reports the
precision difference that the hard negatives produce. When the production M2
correlator lands, it is scored here in place of the reference implementation.
"""

from __future__ import annotations

from collections import Counter
from typing import Sequence


def artifact_frequencies(farm) -> Counter:
    """Count services per (artefact key, value) pair."""
    counts: Counter = Counter()
    for svc in farm.services:
        for value in svc.shared_artifacts.values():
            counts[value] += 1
    return counts


def _comparable_keys(farm, query_id: str, candidate_id: str) -> list[str]:
    """Artefact keys both services carry, so a match is meaningful."""
    query = farm.service(query_id).shared_artifacts
    candidate = farm.service(candidate_id).shared_artifacts
    return list(query.keys() & candidate.keys())


def rank_services(
    farm,
    query_id: str,
    pool: Sequence[str],
    *,
    max_artifact_frequency: int | None = None,
) -> list[tuple[str, float]]:
    """Rank candidate services by fingerprint similarity to the query service.

    `max_artifact_frequency=None` gives the naive correlator. Setting it to an
    integer gives the frequency-filtered variant.

    Score is the fraction of comparable artefact keys on which the two services
    agree, so it is in [0, 1] and is comparable across services that carry different
    numbers of artefacts. Candidates with no agreement are omitted rather than
    returned with a zero score, so "no candidate" stays distinguishable from
    "a weak candidate" -- which is what lets the abstention cases mean anything.
    """
    frequencies = artifact_frequencies(farm) if max_artifact_frequency else None
    query_artifacts = farm.service(query_id).shared_artifacts

    ranked: list[tuple[str, float]] = []
    for candidate_id in pool:
        if candidate_id == query_id:
            continue
        candidate_artifacts = farm.service(candidate_id).shared_artifacts

        keys = _comparable_keys(farm, query_id, candidate_id)
        if not keys:
            continue

        agreements = 0
        for key in keys:
            if query_artifacts[key] != candidate_artifacts[key]:
                continue
            if (
                frequencies is not None
                and max_artifact_frequency is not None
                # A value shared across much of the population is not evidence.
                and frequencies[query_artifacts[key]] > max_artifact_frequency
            ):
                continue
            agreements += 1

        if agreements:
            ranked.append((candidate_id, agreements / len(keys)))

    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked


def common_value_threshold(farm, services_per_cluster: int) -> int:
    """A defensible default for `max_artifact_frequency`.

    A genuine cluster artefact appears on exactly the services in one cluster, so a
    value shared by *more* services than that cannot be cluster-specific. This is a
    derivation from the farm's structure, not a number tuned on the results -- which
    matters, because a threshold fitted on the test set would invalidate the score.
    """
    return max(2, services_per_cluster)
