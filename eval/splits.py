"""Splits and threshold freezing.

Part E2 states the requirement plainly:

    thresholds are frozen before evaluation, chosen on a held-out split, and never
    tuned on the test set. Splits are **by time and by cluster**, never random, so
    the system cannot be evaluated on a service it was fitted to.

This module implements exactly that, and records the decision so the report can show
the threshold was not chosen after seeing the test numbers.

Why not a random split: services in one cluster share artefacts, persona corpora, and
tempo. A random split puts near-duplicates on both sides, and the resulting score
measures memorisation of the farm rather than generalisation. Splitting by cluster
asks the harder and more useful question -- *does this work on an operator it has
never seen?*
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class ClusterSplit:
    """A by-cluster split of the farm.

    `dev` is used to choose thresholds; `test` is used only to report. Nothing may be
    fitted on `test`.
    """

    name: str
    dev_clusters: tuple[str, ...]
    test_clusters: tuple[str, ...]

    def is_dev(self, cluster_id: str | None) -> bool:
        return cluster_id in self.dev_clusters

    def is_test(self, cluster_id: str | None) -> bool:
        return cluster_id in self.test_clusters

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dev_clusters": list(self.dev_clusters),
            "test_clusters": list(self.test_clusters),
        }


def cluster_split(farm, dev_fraction: float = 0.5) -> ClusterSplit:
    """Split clusters into dev and test halves, deterministically.

    Deterministic ordering matters: a split that moved between runs would make every
    threshold and every reported number irreproducible, which defeats the point of
    citing a seed.
    """
    cluster_ids = sorted(c.cluster_id for c in farm.clusters)
    if len(cluster_ids) < 2:
        # Not enough clusters to hold anything out. Say so rather than silently
        # evaluating on the fitting data -- the caller reports this as a limitation.
        return ClusterSplit(
            name="degenerate-single-cluster",
            dev_clusters=tuple(cluster_ids),
            test_clusters=(),
        )

    cutoff = max(1, min(len(cluster_ids) - 1, round(len(cluster_ids) * dev_fraction)))
    return ClusterSplit(
        name=f"by-cluster-{len(cluster_ids)}clusters",
        dev_clusters=tuple(cluster_ids[:cutoff]),
        test_clusters=tuple(cluster_ids[cutoff:]),
    )


@dataclass(frozen=True)
class ThresholdDecision:
    """A threshold, the policy that chose it, and the dev evidence behind it.

    Carried into the results file so a reader can see the threshold was fitted on
    `dev` and then held fixed, rather than being whatever value happened to make the
    test numbers look good.
    """

    name: str
    value: float
    policy: str
    dev_precision: float
    dev_recall: float
    dev_n: int
    fitted_on: str = "dev"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": round(self.value, 6),
            "policy": self.policy,
            "fitted_on": self.fitted_on,
            "dev_precision": round(self.dev_precision, 6),
            "dev_recall": round(self.dev_recall, 6),
            "dev_n": self.dev_n,
        }


def choose_threshold(
    name: str,
    dev_points: Sequence[tuple[float, bool]],
    *,
    min_precision: float = 0.95,
) -> ThresholdDecision:
    """Pick the lowest score threshold meeting a precision floor, on dev data.

    Lowest threshold means highest recall subject to the constraint, so the policy is
    precision-dominant by construction: Part E2 fixes a precision floor and lets
    recall fall where it may, because a false merge is a wrongful accusation while a
    miss is only a miss.

    If no threshold in the dev data meets the floor -- which happens when the
    candidate set is small enough that even the best cut has a false positive -- the
    returned value sits above the maximum observed score, i.e. the system proposes
    nothing and abstains. That is the safe failure, and it is reported as such rather
    than hidden by lowering the floor.
    """
    if not dev_points:
        return ThresholdDecision(
            name=name,
            value=1.0,
            policy=f"precision>={min_precision} (no dev data; abstain)",
            dev_precision=0.0,
            dev_recall=0.0,
            dev_n=0,
        )

    total_true = sum(1 for _, is_true in dev_points if is_true)
    candidates = sorted({score for score, _ in dev_points})
    best: ThresholdDecision | None = None

    for threshold in candidates:
        accepted = [is_true for score, is_true in dev_points if score >= threshold]
        if not accepted:
            continue
        true_positives = sum(1 for x in accepted if x)
        precision = true_positives / len(accepted)
        if precision < min_precision:
            continue
        recall = true_positives / total_true if total_true else 0.0
        if best is None or recall > best.dev_recall:
            best = ThresholdDecision(
                name=name,
                value=threshold,
                policy=(
                    f"lowest threshold with precision>={min_precision} "
                    "(maximise recall)"
                ),
                dev_precision=precision,
                dev_recall=recall,
                dev_n=len(accepted),
            )

    if best is not None:
        return best

    # Nothing cleared the floor: refuse to assert rather than assert imprecisely.
    top = max(score for score, _ in dev_points)
    return ThresholdDecision(
        name=name,
        value=top + 1e-9,
        policy=(
            f"precision>={min_precision} unachievable on dev; threshold set above the "
            "maximum observed score so the system abstains"
        ),
        dev_precision=0.0,
        dev_recall=0.0,
        dev_n=0,
    )